"""Market data and symbol metadata retrieval helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from io import StringIO
from typing import Callable

import pandas as pd
import requests
import yfinance as yf


NQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"


@dataclass(slots=True)
class SymbolMetadata:
    ticker: str
    company_name: str | None
    industry: str | None
    shares_outstanding: float | None
    float_shares: float | None
    market_cap_current: float | None
    beta_current: float | None
    earnings_dates: list[date]


def _download_symbol_table(url: str) -> pd.DataFrame:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    lines = response.text.splitlines()
    # Remove footer line: "File Creation Time: ..."
    content = [line for line in lines if "File Creation Time" not in line]
    text = "\n".join(content)
    return pd.read_csv(StringIO(text), sep="|")


def _normalize_symbol(symbol: str) -> str:
    cleaned = symbol.strip().upper()
    # Yahoo Finance uses '-' instead of '.' for class shares
    return cleaned.replace(".", "-")


def get_us_stock_universe() -> list[str]:
    """Return a de-duplicated list of US listed common stock tickers."""
    nasdaq = _download_symbol_table(NQ_LISTED_URL)
    other = _download_symbol_table(OTHER_LISTED_URL)

    nasdaq_symbols = nasdaq.loc[
        (nasdaq["Test Issue"] == "N")
        & (nasdaq["ETF"] == "N")
        & (nasdaq["NextShares"] == "N"),
        "Symbol",
    ].dropna()

    other_symbols = other.loc[
        (other["Test Issue"] == "N")
        & (other["ETF"] == "N")
        & (other["Exchange"].isin(["N", "A", "P", "V", "Z"])),
        "ACT Symbol",
    ].dropna()

    raw_symbols = pd.concat([nasdaq_symbols, other_symbols], ignore_index=True)
    filtered = []
    for symbol in raw_symbols:
        norm = _normalize_symbol(str(symbol))
        if not norm:
            continue
        # Exclude warrants/units/rights syntaxes often unavailable as standard equities.
        if any(ch in norm for ch in ["$", "/", "^", "="]):
            continue
        filtered.append(norm)

    return sorted(set(filtered))


def chunked_symbols(symbols: list[str], chunk_size: int) -> list[list[str]]:
    return [symbols[i : i + chunk_size] for i in range(0, len(symbols), chunk_size)]


def download_daily_history(
    symbols: list[str],
    start: date,
    end: date,
    chunk_size: int,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, pd.DataFrame]:
    """
    Download daily OHLCV data from Yahoo in chunks.

    Returns a mapping of ticker -> dataframe indexed by date with OHLCV columns.
    """
    all_data: dict[str, pd.DataFrame] = {}
    for idx, total, chunk_data in iter_daily_history_chunks(symbols, start, end, chunk_size):
        all_data.update(chunk_data)
        if progress_callback:
            progress_callback(idx, total)
    return all_data


def iter_daily_history_chunks(
    symbols: list[str],
    start: date,
    end: date,
    chunk_size: int,
):
    """Yield chunk download results as `(chunk_number, total_chunks, data_by_symbol)`."""
    chunks = chunked_symbols(symbols, chunk_size)
    total = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        try:
            raw = yf.download(
                tickers=" ".join(chunk),
                start=start.isoformat(),
                end=end.isoformat(),
                interval="1d",
                progress=False,
                auto_adjust=False,
                group_by="ticker",
                threads=True,
                ignore_tz=True,
            )
        except Exception:
            yield idx, total, {}
            continue

        by_symbol: dict[str, pd.DataFrame] = {}
        if raw.empty:
            yield idx, total, by_symbol
            continue

        if isinstance(raw.columns, pd.MultiIndex):
            available = set(raw.columns.get_level_values(0))
            for symbol in chunk:
                if symbol not in available:
                    continue
                df = raw[symbol].copy()
                df = _standardize_history(df)
                if not df.empty:
                    by_symbol[symbol] = df
        else:
            symbol = chunk[0]
            df = _standardize_history(raw.copy())
            if not df.empty:
                by_symbol[symbol] = df

        yield idx, total, by_symbol


def _standardize_history(df: pd.DataFrame) -> pd.DataFrame:
    expected = ["Open", "High", "Low", "Close", "Volume"]
    missing = [col for col in expected if col not in df.columns]
    if missing:
        return pd.DataFrame()
    out = df.loc[:, expected].copy()
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    out.index = pd.to_datetime(out.index).tz_localize(None).normalize()
    return out.sort_index()


def download_benchmark_history(start: date, end: date, benchmark_symbol: str) -> pd.DataFrame:
    raw = yf.download(
        tickers=benchmark_symbol,
        start=start.isoformat(),
        end=end.isoformat(),
        interval="1d",
        progress=False,
        auto_adjust=False,
        ignore_tz=True,
    )
    return _standardize_history(raw)


def fetch_symbol_metadata(symbol: str) -> SymbolMetadata:
    ticker = yf.Ticker(symbol)
    info: dict = {}
    try:
        info = ticker.get_info() or {}
    except Exception:
        try:
            info = ticker.info or {}
        except Exception:
            info = {}

    earnings_dates: list[date] = []
    try:
        earnings_df = ticker.get_earnings_dates(limit=16)
        if isinstance(earnings_df, pd.DataFrame) and not earnings_df.empty:
            earnings_dates = sorted(
                {pd.Timestamp(idx).date() for idx in earnings_df.index if pd.notna(idx)}
            )
    except Exception:
        earnings_dates = []

    company_name = info.get("longName") or info.get("shortName")
    industry = info.get("industryDisp") or info.get("industry")
    shares_outstanding = _to_float(info.get("sharesOutstanding"))
    float_shares = _to_float(info.get("floatShares"))
    market_cap_current = _to_float(info.get("marketCap"))
    beta_current = _to_float(info.get("beta"))

    return SymbolMetadata(
        ticker=symbol,
        company_name=company_name,
        industry=industry,
        shares_outstanding=shares_outstanding,
        float_shares=float_shares,
        market_cap_current=market_cap_current,
        beta_current=beta_current,
        earnings_dates=earnings_dates,
    )


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(num):
        return None
    return num
