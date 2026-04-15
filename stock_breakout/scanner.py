"""Screening logic for pump events and breakout watchlist candidates."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

import numpy as np
import pandas as pd

from stock_breakout.config import (
    BENCHMARK_SYMBOL,
    GAIN_THRESHOLD,
    LOOKBACK_DAYS,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    MAX_SYMBOLS_PER_DOWNLOAD,
    MIN_AVERAGE_DAILY_DOLLAR_VOLUME,
    MIN_PRICE,
    RSI_PERIOD,
    RVOL_PERIOD,
)
from stock_breakout.data_sources import (
    SymbolMetadata,
    download_benchmark_history,
    fetch_symbol_metadata,
    get_us_stock_universe,
    iter_daily_history_chunks,
)
from stock_breakout.db import replace_watchlist, upsert_pump_events


def run_scan(
    conn,
    lookback_days: int = LOOKBACK_DAYS,
    gain_threshold: float = GAIN_THRESHOLD,
    max_symbols: int | None = None,
    scan_start_override: date | None = None,
    status_callback=None,
    progress_callback=None,
) -> dict:
    """Run end-to-end data collection, event detection, and persistence."""
    end_date = date.today() + timedelta(days=1)
    scan_start_date = date.today() - timedelta(days=lookback_days)
    if scan_start_override:
        scan_start_date = max(scan_start_date, scan_start_override)
    # Add warmup so RSI/MACD/MA calculations are stable around lookback start.
    data_start_date = scan_start_date - timedelta(days=260)

    if status_callback:
        status_callback("Loading stock universe...")
    symbols = get_us_stock_universe()
    if max_symbols and max_symbols > 0:
        symbols = symbols[:max_symbols]

    if status_callback:
        status_callback(f"Downloading daily history for {len(symbols):,} symbols...")

    history_by_symbol: dict[str, pd.DataFrame] = {}
    chunk_iter = iter_daily_history_chunks(
        symbols=symbols,
        start=data_start_date,
        end=end_date,
        chunk_size=MAX_SYMBOLS_PER_DOWNLOAD,
    )
    for chunk_idx, chunk_total, chunk_data in chunk_iter:
        history_by_symbol.update(chunk_data)
        if progress_callback:
            progress_callback(chunk_idx, chunk_total)

    if status_callback:
        status_callback("Downloading benchmark history...")
    benchmark_history = download_benchmark_history(
        start=data_start_date, end=end_date, benchmark_symbol=BENCHMARK_SYMBOL
    )

    if status_callback:
        status_callback("Detecting 50%+ one-day gain events...")
    raw_events: list[dict] = []
    watchlist_candidates: list[dict] = []
    for symbol, history in history_by_symbol.items():
        if history.empty:
            continue
        if _illiquid_or_invalid(history):
            continue
        with_indicators = add_indicators(history)
        raw_events.extend(
            detect_pump_events(
                symbol=symbol,
                history=with_indicators,
                scan_start_date=scan_start_date,
                gain_threshold=gain_threshold,
            )
        )
        watchlist_row = score_breakout_watchlist_candidate(
            symbol=symbol,
            history=with_indicators,
            benchmark_history=benchmark_history,
        )
        if watchlist_row:
            watchlist_candidates.append(watchlist_row)

    # Keep top-ranked watchlist names.
    watchlist_candidates.sort(key=lambda row: row["breakout_score"], reverse=True)
    watchlist_candidates = watchlist_candidates[:150]

    symbols_needing_metadata = sorted(
        {event["ticker"] for event in raw_events}
        | {row["ticker"] for row in watchlist_candidates}
    )
    metadata_by_symbol = _fetch_metadata_for_symbols(symbols_needing_metadata, status_callback)

    final_events = [
        enrich_event_with_metadata(event, metadata_by_symbol.get(event["ticker"]))
        for event in raw_events
    ]
    upsert_counts = upsert_pump_events(conn, final_events)

    final_watchlist = [
        enrich_watchlist_row(row, metadata_by_symbol.get(row["ticker"]))
        for row in watchlist_candidates
    ]
    replace_watchlist(conn, final_watchlist)

    return {
        "symbols_in_universe": len(symbols),
        "symbols_with_history": len(history_by_symbol),
        "raw_events_detected": len(raw_events),
        "events_inserted": upsert_counts["inserted"],
        "events_updated": upsert_counts["updated"],
        "watchlist_count": len(final_watchlist),
        "scan_start_date": scan_start_date.isoformat(),
    }


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = out["Close"]
    volume = out["Volume"].replace(0, np.nan)

    # RSI (Wilder-like smoothing approximation).
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out["rsi"] = 100 - (100 / (1 + rs))

    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    out["macd"] = macd_line
    out["macd_signal"] = macd_line.ewm(span=MACD_SIGNAL, adjust=False).mean()

    out["rvol"] = volume / volume.rolling(RVOL_PERIOD, min_periods=RVOL_PERIOD).mean()
    out["sma20"] = close.rolling(20, min_periods=20).mean()
    out["sma50"] = close.rolling(50, min_periods=50).mean()
    out["sma200"] = close.rolling(200, min_periods=120).mean()
    out["atr_pct20"] = ((out["High"] - out["Low"]) / close.replace(0, np.nan)).rolling(
        20, min_periods=20
    ).mean()
    out["close_ret_10d"] = close.pct_change(10)
    out["close_ret_20d"] = close.pct_change(20)
    out["vol_sma10"] = volume.rolling(10, min_periods=10).mean()
    out["vol_sma30"] = volume.rolling(30, min_periods=30).mean()
    out["high_60d"] = out["High"].rolling(60, min_periods=40).max()
    return out


def detect_pump_events(
    symbol: str,
    history: pd.DataFrame,
    scan_start_date: date,
    gain_threshold: float,
) -> list[dict]:
    events: list[dict] = []
    if history.shape[0] < 3:
        return events

    close = history["Close"]
    open_ = history["Open"]
    high = history["High"]
    event_dates = history.index

    for i in range(1, len(history)):
        event_ts = event_dates[i]
        event_date = event_ts.date()
        if event_date < scan_start_date:
            continue

        prev_close = close.iloc[i - 1]
        current_open = open_.iloc[i]
        current_high = high.iloc[i]
        current_close = close.iloc[i]

        if not _is_positive_price(prev_close, current_open, current_high, current_close):
            continue

        overnight_change = (current_open / prev_close) - 1
        intraday_change = (current_high / current_open) - 1
        close_to_close_change = (current_close / prev_close) - 1

        session_type = None
        move_pct = None
        reference_price = None

        if overnight_change >= gain_threshold:
            session_type = "pre market / overnight"
            move_pct = overnight_change
            reference_price = current_open
        elif intraday_change >= gain_threshold:
            session_type = "normal trading hours"
            move_pct = intraday_change
            reference_price = current_high
        elif close_to_close_change >= gain_threshold:
            session_type = "after hours"
            move_pct = close_to_close_change
            reference_price = current_close

        if session_type is None or move_pct is None:
            continue

        event_id = f"{symbol}:{event_date.isoformat()}:{session_type}"
        events.append(
            {
                "event_id": event_id,
                "event_date": event_date.isoformat(),
                "ticker": symbol,
                "company_name": None,
                "industry": None,
                "market_cap": None,
                "rsi": _safe_float(history["rsi"].iloc[i]),
                "macd": _safe_float(history["macd"].iloc[i]),
                "rvol": _safe_float(history["rvol"].iloc[i]),
                "beta": None,
                "shares_outstanding": None,
                "float_shares": None,
                "days_since_last_earnings": None,
                "days_before_next_earnings": None,
                "country": None,
                "session_type": session_type,
                "one_day_change_pct": move_pct * 100.0,
                "reference_price": reference_price,
                "source_note": (
                    "Session type inferred from daily bars; market cap uses available shares and event-day reference price."
                ),
            }
        )

    return events


def score_breakout_watchlist_candidate(
    symbol: str,
    history: pd.DataFrame,
    benchmark_history: pd.DataFrame,
) -> dict | None:
    if history.shape[0] < 80:
        return None

    latest = history.iloc[-1]
    close = _safe_float(latest["Close"])
    if close is None or close < MIN_PRICE:
        return None

    atr_pct20 = _safe_float(latest["atr_pct20"])
    vol_sma10 = _safe_float(latest["vol_sma10"])
    vol_sma30 = _safe_float(latest["vol_sma30"])
    ret10 = _safe_float(latest["close_ret_10d"])
    ret20 = _safe_float(latest["close_ret_20d"])
    sma20 = _safe_float(latest["sma20"])
    sma50 = _safe_float(latest["sma50"])
    high_60d = _safe_float(latest["high_60d"])

    if any(v is None for v in [atr_pct20, vol_sma10, vol_sma30, ret10, ret20, sma20, sma50, high_60d]):
        return None

    benchmark_ret20 = _benchmark_return_20d(benchmark_history)
    if benchmark_ret20 is None:
        benchmark_ret20 = 0.0
    relative_strength = ret20 - benchmark_ret20

    tight_range_score = _clip((0.08 - atr_pct20) / 0.08)
    rising_volume_score = _clip((vol_sma10 / max(vol_sma30, 1.0) - 1.0) / 0.80) * _clip(
        (0.15 - abs(ret10)) / 0.15
    )
    above_ma_score = (
        1.0 if (close > sma20 > sma50 and close > 1.02 * sma50) else 0.3 if close > sma20 else 0.0
    )
    rs_score = _clip((relative_strength + 0.05) / 0.20)
    dist_to_breakout = (high_60d - close) / max(high_60d, 1e-6)
    near_breakout_score = _clip((0.06 - dist_to_breakout) / 0.06)

    breakout_score = 100.0 * (
        0.22 * tight_range_score
        + 0.20 * rising_volume_score
        + 0.20 * above_ma_score
        + 0.20 * rs_score
        + 0.18 * near_breakout_score
    )

    if breakout_score < 55:
        return None

    details = (
        f"TightRange={tight_range_score:.2f}; "
        f"QuietVolRise={rising_volume_score:.2f}; "
        f"AboveMAs={above_ma_score:.2f}; "
        f"RelStrength={rs_score:.2f}; "
        f"NearBreakout={near_breakout_score:.2f}"
    )

    return {
        "ticker": symbol,
        "company_name": None,
        "industry": None,
        "breakout_score": round(breakout_score, 2),
        "details": details,
        "last_price": close,
    }


def _fetch_metadata_for_symbols(symbols: Iterable[str], status_callback=None) -> dict[str, SymbolMetadata]:
    symbols = list(symbols)
    if status_callback:
        status_callback(f"Fetching fundamentals for {len(symbols):,} symbols...")
    out: dict[str, SymbolMetadata] = {}
    for idx, symbol in enumerate(symbols, start=1):
        out[symbol] = fetch_symbol_metadata(symbol)
        if status_callback and (idx % 25 == 0 or idx == len(symbols)):
            status_callback(f"Fetched fundamentals: {idx:,}/{len(symbols):,}")
    return out


def enrich_event_with_metadata(event: dict, metadata: SymbolMetadata | None) -> dict:
    row = event.copy()
    row.pop("reference_price", None)
    if metadata is None:
        return row

    row["company_name"] = metadata.company_name
    row["industry"] = metadata.industry
    row["country"] = metadata.country
    row["beta"] = metadata.beta_current
    row["shares_outstanding"] = metadata.shares_outstanding
    row["float_shares"] = metadata.float_shares

    reference_price = event.get("reference_price")
    if reference_price and metadata.shares_outstanding:
        row["market_cap"] = float(reference_price) * float(metadata.shares_outstanding)
    else:
        row["market_cap"] = metadata.market_cap_current

    event_dt = date.fromisoformat(event["event_date"])
    since, before = _earnings_offsets(event_dt, metadata.earnings_dates)
    row["days_since_last_earnings"] = since
    row["days_before_next_earnings"] = before
    return row


def enrich_watchlist_row(row: dict, metadata: SymbolMetadata | None) -> dict:
    out = row.copy()
    if metadata:
        out["company_name"] = metadata.company_name
        out["industry"] = metadata.industry
    return out


def _earnings_offsets(event_date: date, earnings_dates: list[date]) -> tuple[int | None, int | None]:
    if not earnings_dates:
        return None, None
    previous = [d for d in earnings_dates if d <= event_date]
    upcoming = [d for d in earnings_dates if d > event_date]
    days_since = (event_date - max(previous)).days if previous else None
    days_before = (min(upcoming) - event_date).days if upcoming else None
    return days_since, days_before


def _benchmark_return_20d(benchmark: pd.DataFrame) -> float | None:
    if benchmark.shape[0] < 21:
        return None
    closes = benchmark["Close"]
    return _safe_float(closes.iloc[-1] / closes.iloc[-21] - 1)


def _safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if np.isnan(num):
        return None
    return num


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return float(max(low, min(high, value)))


def _is_positive_price(*values: float) -> bool:
    return all(v is not None and np.isfinite(v) and v > 0 for v in values)


def _illiquid_or_invalid(history: pd.DataFrame) -> bool:
    if history.empty:
        return True
    closes = history["Close"].tail(30)
    volumes = history["Volume"].tail(30)
    if closes.empty or volumes.empty:
        return True
    last_close = closes.iloc[-1]
    avg_dollar_vol = (closes * volumes).mean()
    if pd.isna(last_close) or last_close < MIN_PRICE:
        return True
    if pd.isna(avg_dollar_vol) or avg_dollar_vol < MIN_AVERAGE_DAILY_DOLLAR_VOLUME:
        return True
    return False
