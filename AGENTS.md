# AGENTS.md

## Cursor Cloud specific instructions

### Overview

Pumpamentals is a single-service Streamlit app that scans US-listed stocks for 50%+ one-day gains and builds a breakout watchlist. All application code lives on the `cursor/stock-pump-breakout-app-a42c` feature branch (main has only the initial README commit).

### Running the app

```bash
streamlit run app.py --server.headless true --server.port 8501
```

The app auto-creates its SQLite database at `data/stocks.db` on first run. No external database server is required.

### Dependencies

- Python 3.11+ (system Python 3.12 works)
- `pip install -r requirements.txt` (streamlit, pandas, numpy, yfinance, requests)

### Linting

No project-level linter config exists. Use `ruff check .` and `pyright .` for ad-hoc checks. Expect pre-existing pyright type errors from dynamic pandas/yfinance usage and 2 unused-import warnings from ruff.

### Testing

No automated test suite exists in this codebase. Manual testing is done through the Streamlit UI by running a scan with a small symbol count (e.g., 50) to verify data pipeline functionality.

### Key caveats

- The scan requires internet access to download stock data from Yahoo Finance and Nasdaq Trader symbol directories. Scans will fail without network connectivity.
- Scanning the full US stock universe (~7000+ symbols) takes a long time. Use "Max symbols to scan" = 50 for quick smoke tests.
- The `data/` directory is gitignored; the SQLite DB is ephemeral per environment.
- yfinance may return MultiIndex columns when downloading batches of tickers. The code handles this in `iter_daily_history_chunks` but single-ticker edge cases in `download_benchmark_history` may occasionally produce `KeyError` warnings — these are non-fatal for the overall scan.
