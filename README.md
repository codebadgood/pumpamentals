# pumpamentals

Streamlit app that:

1. Scans US-listed stocks for **50%+ one-day gains** over a configurable lookback window (default: last ~6 months).
2. Classifies the move type using daily-bar proxies:
   - **Overnight / pre-market** (`open vs prior close`)
   - **Regular session** (`high vs open`)
   - **After-hours / close-to-close** (`close vs prior close`)
3. Stores detected events in SQLite and appends only new records on future runs.
4. Builds a **breakout watchlist** for future setups based on:
   - Tight range (low volatility)
   - Quietly rising volume
   - Holding above key moving averages
   - Strength vs market
   - Near breakout levels

## Data fields shown in pump table

- Ticker
- Company name
- Industry
- Market cap at pump time (approx: `reference price * shares outstanding` when available)
- RSI at time of event
- MACD at time of event
- RVOL at time of event
- Beta
- Shares outstanding
- Float shares
- Days since last earnings
- Days before next earnings

## Stack

- Python 3.11+
- Streamlit UI
- yfinance + Nasdaq Trader symbol directories
- SQLite for persistence

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

Then in the sidebar:
- Click **Run / Refresh scan**
- Optionally raise **Max symbols** to scan the full US universe (it can take a while)

## Notes and limitations

- Intraday/pre/post market classification is inferred from daily OHLC bars, so it is approximate.
- Some fundamentals (float, earnings dates, beta) may be unavailable for certain symbols from Yahoo.
- The app is designed to append newly discovered daily events across runs via dedupe key:
  `ticker + date + inferred session type`.
