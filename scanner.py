"""
Daily FTSE strategy scanner
-----------------------------------------------------------------------
Implements the exact methodology validated in backtesting:

  1. Growth signal: a stock rises 20-25% within any 20-trading-day window.
  2. From that point, track a trailing peak (updates on any new high).
  3. Buy trigger: price falls to 30% below the trailing peak.
  4. Quality filter (evaluated only at the moment of a buy trigger):
       - "fast cycle": time from growth signal to buy trigger <= CYCLE_DAYS_THRESHOLD
       - "higher pre-event volatility": daily volatility in the period BEFORE
         the growth signal was above PRE_VOL_THRESHOLD
     Both were found in backtesting to correlate with better outcomes.
     A signal that fails this filter is logged but not bought.
  5. Sell trigger: price recovers to within 10% of the peak that triggered
     the buy.
  6. Maximum hold: if neither sold nor stopped after MAX_HOLD_DAYS trading
     days, the position is force-closed at whatever the price is that day
     (this materially reduced worst-case losses in backtesting).

This script is designed to run ONCE PER DAY, after market close, via a
scheduled job (see .github/workflows/daily_scan.yml). It is intentionally
NOT designed for intraday/real-time use — the methodology was validated
against daily closing prices, and reacting to intraday noise is a
different, unvalidated strategy.

State is persisted in data/state.json (current watchlist + holdings) and
data/trade_log.json (completed trades). Both are read at the start of each
run and rewritten at the end, so the script is safe to run daily via CI
with no other infrastructure required.

IMPORTANT LIMITATIONS - read before relying on this:
  - The "quality filter" thresholds (CYCLE_DAYS_THRESHOLD, PRE_VOL_THRESHOLD)
    were empirically calibrated on 2024-2026 FTSE data. They are a
    reasonable starting point, not a law of nature - markets change, and
    these should be periodically re-validated against fresh data.
  - This script does NOT do any news/financial red-flag screening (profit
    warnings, debt distress, etc.). Backtesting showed this can meaningfully
    improve results, but it requires human judgement - review each new buy
    signal yourself before acting on it, don't auto-trade.
  - This is a decision-support tool, not a trading bot. It does not place
    any real orders. You choose whether to act on what it reports.
"""

import json
import os
from datetime import datetime, timedelta

import pandas as pd
import numpy as np
import yfinance as yf

# ---------- CONFIG (calibrated from backtesting - see notes above) ----------
WINDOW = 20                    # trading days for the growth-signal window
GROWTH_MIN, GROWTH_MAX = 20, 25  # % growth that qualifies as a signal
DROP_PCT = 30                  # % below peak that triggers a buy
SELL_PCT = 10                  # % below peak that triggers a sell (i.e. recovers to 90% of peak)
MAX_HOLD_DAYS = 300            # trading days - force-exit if neither sold nor stopped
CYCLE_DAYS_THRESHOLD = 150     # "fast cycle" filter: signal-to-buy must be <= this many days
PRE_VOL_THRESHOLD = 2.3        # "higher pre-event volatility" filter, in % daily std dev
INVEST = 1000                  # notional £ per position, for P&L reporting only

TICKERS_CSV = "data/tickers.csv"
STATE_FILE = "data/state.json"
TRADE_LOG_FILE = "data/trade_log.json"
TODAY_SIGNALS_FILE = "data/today_signals.json"

DROP_TRIGGER = 1 - DROP_PCT / 100
SELL_TRIGGER = 1 - SELL_PCT / 100
# ------------------------------------------------------------------------


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def to_yahoo_ticker(uk_ticker):
    t = uk_ticker.replace(".", "-")
    return f"{t}.L"


def fetch_universe_prices(tickers):
    """Batch-download ~6 months of daily closes for every ticker in one go.
    6 months gives enough history for the 20-day growth window plus a
    pre-event volatility lookback before it."""
    yahoo_tickers = [to_yahoo_ticker(t) for t in tickers]
    print(f"Fetching {len(yahoo_tickers)} tickers from Yahoo Finance...")
    data = yf.download(
        yahoo_tickers, period="6mo", interval="1d",
        group_by="ticker", auto_adjust=True, threads=True, progress=False,
    )
    prices = {}
    for uk_ticker, yahoo_ticker in zip(tickers, yahoo_tickers):
        try:
            series = data[yahoo_ticker]["Close"].dropna()
            if len(series) > 5:
                prices[uk_ticker] = series
        except Exception:
            continue
    print(f"  Got usable data for {len(prices)} tickers")
    return prices


def find_growth_signal(closes):
    """Return (date_of_signal, growth_pct) for the FIRST qualifying 20-25%
    growth window found in this price history, or (None, None)."""
    n = len(closes)
    if n <= WINDOW:
        return None, None
    values = closes.values
    dates = closes.index
    for t in range(WINDOW, n):
        p0, p1 = values[t - WINDOW], values[t]
        if p0 <= 0:
            continue
        growth = (p1 - p0) / p0 * 100
        if GROWTH_MIN <= growth <= GROWTH_MAX:
            return dates[t], growth
    return None, None


def pre_event_vol(closes, signal_date):
    """Daily return std dev (%) using only data strictly before signal_date."""
    pre = closes[closes.index < signal_date]
    if len(pre) < 10:
        return None
    rets = pre.pct_change().dropna() * 100
    return float(rets.std())


def main():
    today = datetime.today().date()
    print(f"=== Daily scan: {today} ===")

    tickers_df = pd.read_csv(TICKERS_CSV)
    ticker_names = dict(zip(tickers_df["Ticker"], tickers_df["Company"]))
    all_tickers = tickers_df["Ticker"].tolist()

    state = load_json(STATE_FILE, {"watching": {}, "holdings": {}})
    trade_log = load_json(TRADE_LOG_FILE, [])

    prices = fetch_universe_prices(all_tickers)

    todays_signals = {
        "date": str(today),
        "new_growth_signals": [],
        "new_buys": [],
        "rejected_buys": [],
        "new_sells": [],
        "forced_exits": [],
        "watchlist_status": [],
    }

    tracked = set(state["watching"].keys()) | set(state["holdings"].keys())

    # --- 1. Look for brand-new growth signals among untracked tickers ---
    for ticker in all_tickers:
        if ticker in tracked or ticker not in prices:
            continue
        closes = prices[ticker]
        signal_date, growth_pct = find_growth_signal(closes)
        if signal_date is None:
            continue
        vol = pre_event_vol(closes, signal_date)
        if vol is None:
            continue
        peak_price = float(closes[closes.index >= signal_date].iloc[0])
        state["watching"][ticker] = {
            "company": ticker_names.get(ticker, ticker),
            "signal_date": str(signal_date.date()),
            "growth_pct": round(growth_pct, 1),
            "pre_event_vol": round(vol, 2),
            "peak_price": peak_price,
            "peak_date": str(signal_date.date()),
        }
        todays_signals["new_growth_signals"].append({
            "ticker": ticker, "company": ticker_names.get(ticker, ticker),
            "growth_pct": round(growth_pct, 1),
        })

    # --- 2. Update every watched ticker: track peak, check buy trigger ---
    for ticker in list(state["watching"].keys()):
        if ticker not in prices:
            continue
        w = state["watching"][ticker]
        closes = prices[ticker]
        last_price = float(closes.iloc[-1])
        last_date = closes.index[-1]

        if last_price > w["peak_price"]:
            w["peak_price"] = last_price
            w["peak_date"] = str(last_date.date())

        drop_pct_now = (w["peak_price"] - last_price) / w["peak_price"] * 100

        if last_price <= w["peak_price"] * DROP_TRIGGER:
            # Buy trigger fired - check the quality filter
            cycle_days = (last_date.date() - pd.Timestamp(w["signal_date"]).date()).days
            passes_filter = (cycle_days <= CYCLE_DAYS_THRESHOLD) and (w["pre_event_vol"] > PRE_VOL_THRESHOLD)

            if passes_filter:
                state["holdings"][ticker] = {
                    "company": w["company"],
                    "buy_date": str(last_date.date()),
                    "buy_price": last_price,
                    "peak_at_buy": w["peak_price"],
                    "cycle_days": cycle_days,
                }
                todays_signals["new_buys"].append({
                    "ticker": ticker, "company": w["company"],
                    "buy_price": round(last_price, 2), "peak_price": round(w["peak_price"], 2),
                    "cycle_days": cycle_days,
                })
            else:
                todays_signals["rejected_buys"].append({
                    "ticker": ticker, "company": w["company"],
                    "reason": f"cycle_days={cycle_days} (max {CYCLE_DAYS_THRESHOLD}), "
                              f"pre_event_vol={w['pre_event_vol']}% (min {PRE_VOL_THRESHOLD}%)",
                })
            del state["watching"][ticker]
        else:
            todays_signals["watchlist_status"].append({
                "ticker": ticker, "company": w["company"],
                "current_price": round(last_price, 2), "peak_price": round(w["peak_price"], 2),
                "pct_below_peak": round(drop_pct_now, 1),
                "still_needs_to_fall_pct": round(DROP_PCT - drop_pct_now, 1),
            })

    # --- 3. Update every holding: check sell trigger and max-hold cap ---
    for ticker in list(state["holdings"].keys()):
        if ticker not in prices:
            continue
        h = state["holdings"][ticker]
        closes = prices[ticker]
        last_price = float(closes.iloc[-1])
        last_date = closes.index[-1]
        buy_date = pd.Timestamp(h["buy_date"])
        days_held = int((closes.index >= buy_date).sum()) - 1

        sell_price = None
        status = None
        if last_price >= h["peak_at_buy"] * SELL_TRIGGER:
            sell_price = last_price
            status = "Sold (hit target)"
        elif days_held >= MAX_HOLD_DAYS:
            sell_price = last_price
            status = "Sold (max hold reached)"

        if sell_price is not None:
            shares = INVEST / h["buy_price"]
            proceeds = shares * sell_price
            record = {
                "ticker": ticker, "company": h["company"], "status": status,
                "buy_date": h["buy_date"], "sell_date": str(last_date.date()),
                "buy_price": round(h["buy_price"], 2), "sell_price": round(sell_price, 2),
                "days_held": days_held, "invested": INVEST,
                "proceeds": round(proceeds, 2), "profit": round(proceeds - INVEST, 2),
                "return_pct": round((proceeds - INVEST) / INVEST * 100, 2),
            }
            trade_log.append(record)
            key = "new_sells" if status == "Sold (hit target)" else "forced_exits"
            todays_signals[key].append(record)
            del state["holdings"][ticker]

    save_json(STATE_FILE, state)
    save_json(TRADE_LOG_FILE, trade_log)
    save_json(TODAY_SIGNALS_FILE, todays_signals)

    print(f"New growth signals: {len(todays_signals['new_growth_signals'])}")
    print(f"New buys: {len(todays_signals['new_buys'])}")
    print(f"Rejected buys (failed quality filter): {len(todays_signals['rejected_buys'])}")
    print(f"New sells: {len(todays_signals['new_sells'])}")
    print(f"Forced exits (max hold): {len(todays_signals['forced_exits'])}")
    print(f"Currently watching: {len(state['watching'])}")
    print(f"Currently holding: {len(state['holdings'])}")
    print("Done.")


if __name__ == "__main__":
    main()
