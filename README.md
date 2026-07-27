# FTSE Strategy Tracker

An automated daily scanner that applies a backtested strategy to FTSE 100/250/SmallCap
stocks: it looks for a 20-25% price run-up, tracks the peak, buys (on paper) if the
price falls 30% below that peak, and sells once it recovers to within 10% of the peak
— with a 300-trading-day maximum hold and a quality filter based on how fast the drop
happened and how volatile the stock was beforehand. Full methodology notes are in the
docstring at the top of `scanner.py`.

**This is decision-support only. It does not place real trades**, and it does not
check news or financial health before flagging a buy — backtesting showed that
screening out companies with recent profit warnings or debt distress meaningfully
improves results, but that needs a human judgement call. Always sanity-check a new
buy signal yourself (a quick news search on the ticker) before acting on it.

## What's included

- `scanner.py` — the daily logic. Fetches ~6 months of price history for every
  tracked company, updates the watchlist/holdings, and writes results to `data/`.
- `.github/workflows/daily_scan.yml` — runs `scanner.py` automatically every
  weekday evening via GitHub Actions, and commits the updated data back to the repo.
- `index.html` — a static dashboard that reads the `data/` files and displays
  current holdings, the watchlist, today's signals, and trade history.
- `data/tickers.csv` — the starting universe (429 FTSE 100/250/SmallCap companies).
  Refresh this occasionally (see below) as constituents change.
- `data/state.json`, `data/trade_log.json` — the tracker's persistent memory.
  Pre-seeded with the actual backtested results through 24 July 2026, so the
  tracker picks up from there rather than starting from zero.

## One-time setup (10 minutes)

1. **Create a new GitHub repository** (public or private both work) and push
   everything in this folder to it.
2. **Enable GitHub Actions**: on GitHub, go to the repo's *Settings → Actions →
   General*, and under "Workflow permissions" select **"Read and write
   permissions"** — the daily job needs this to commit its results back.
3. **Enable GitHub Pages**: go to *Settings → Pages*, set "Source" to
   **Deploy from a branch**, branch `main`, folder `/ (root)`. Save. GitHub will
   give you a URL like `https://yourusername.github.io/your-repo-name/` —
   that's your dashboard.
4. **Trigger a first run manually**: go to the *Actions* tab, select "Daily FTSE
   Strategy Scan" in the sidebar, click "Run workflow". This proves everything
   works before waiting for the schedule.
5. Visit your GitHub Pages URL. You should see the dashboard populated with
   the seeded historical data, and after the first scan run, current results.

After that, it runs itself: every weekday at 18:00 UTC (adjust the cron line in
the workflow file if you want a different time), it fetches fresh prices,
updates the watchlist and holdings, and pushes the results — the dashboard
picks them up automatically next time you open it.

## Refreshing the ticker universe

`data/tickers.csv` is a snapshot. FTSE index constituents change quarterly.
To refresh it, you'd re-run the Wikipedia-based constituent scraper (the same
approach used to build the original backtest data) and overwrite this file.
This isn't automated in this package — a stale universe just means you might
miss very recently-added constituents and keep tracking recently-removed ones,
which is a minor issue, not a correctness bug.

## Tuning the strategy

The key parameters are all at the top of `scanner.py`:

- `GROWTH_MIN` / `GROWTH_MAX` — the growth-signal window (currently 20-25%)
- `DROP_PCT` — how far below peak triggers a buy (currently 30%)
- `SELL_PCT` — how close to peak triggers a sell (currently 10%, i.e. sell at 90% of peak)
- `MAX_HOLD_DAYS` — force-exit cap (currently 300 trading days, ~14 months)
- `CYCLE_DAYS_THRESHOLD` / `PRE_VOL_THRESHOLD` — the quality filter

These were calibrated against 2024-2026 FTSE data in backtesting. They're a
reasonable starting point, not a guarantee — markets change, and it's worth
periodically re-validating against fresh data rather than assuming these stay
optimal forever.

## Costs

£0. Yahoo Finance data via `yfinance` is free and needs no API key. GitHub
Actions is free for public repos (2,000 free minutes/month for private repos,
and this job takes well under a minute to run). GitHub Pages hosting is free.
