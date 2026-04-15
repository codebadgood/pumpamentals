"""Application constants and configuration defaults."""

from __future__ import annotations

from pathlib import Path


APP_TITLE = "Pumpamentals Scanner"
DATABASE_PATH = Path("data/stocks.db")
LOOKBACK_DAYS = 183  # ~6 months
GAIN_THRESHOLD = 0.50
MAX_SYMBOLS_PER_DOWNLOAD = 175
MIN_AVERAGE_DAILY_DOLLAR_VOLUME = 200_000
MIN_PRICE = 0.3
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
RVOL_PERIOD = 20
BENCHMARK_SYMBOL = "^GSPC"
