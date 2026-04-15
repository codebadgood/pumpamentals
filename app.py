from __future__ import annotations

from datetime import date, timedelta
import sqlite3

import pandas as pd
import streamlit as st

from stock_breakout.config import APP_TITLE, DATABASE_PATH, GAIN_THRESHOLD, LOOKBACK_DAYS
from stock_breakout.db import (
    get_connection,
    get_latest_pump_event_date,
    initialize_db,
    load_pump_events,
    load_watchlist,
)
from stock_breakout.scanner import run_scan


def _format_large_number(value) -> str:
    if value is None or pd.isna(value):
        return "-"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "-"
    abs_num = abs(num)
    if abs_num >= 1_000_000_000:
        return f"{num / 1_000_000_000:.2f}B"
    if abs_num >= 1_000_000:
        return f"{num / 1_000_000:.2f}M"
    if abs_num >= 1_000:
        return f"{num / 1_000:.2f}K"
    return f"{num:.0f}"


def _style_pump_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    formatted = df.copy()
    for col in ["market_cap", "shares_outstanding", "float_shares"]:
        if col in formatted.columns:
            formatted[col] = formatted[col].map(_format_large_number)
    for col in ["rsi", "macd", "rvol", "beta"]:
        if col in formatted.columns:
            formatted[col] = formatted[col].map(
                lambda x: "-" if pd.isna(x) else f"{float(x):.2f}"
            )
    if "one_day_change_pct" in formatted.columns:
        formatted["one_day_change_pct"] = formatted["one_day_change_pct"].map(
            lambda x: "-" if pd.isna(x) else f"{float(x):.2f}%"
        )
    return formatted


def _style_watchlist_table(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    formatted = df.copy()
    if "breakout_score" in formatted.columns:
        formatted["breakout_score"] = formatted["breakout_score"].map(
            lambda x: "-" if pd.isna(x) else f"{float(x):.1f}"
        )
    if "last_price" in formatted.columns:
        formatted["last_price"] = formatted["last_price"].map(
            lambda x: "-" if pd.isna(x) else f"${float(x):.2f}"
        )
    return formatted


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)
    st.caption(
        "Scans US stocks for 50%+ one-day moves (intraday, overnight/pre-market, or close-to-close), "
        "stores results in SQLite, and builds a breakout watchlist."
    )

    conn = get_connection(DATABASE_PATH)
    initialize_db(conn)

    with st.sidebar:
        st.header("Scanner Controls")
        lookback_days = st.number_input(
            "Lookback days",
            min_value=30,
            max_value=730,
            value=LOOKBACK_DAYS,
            step=1,
        )
        gain_threshold_pct = st.number_input(
            "One-day gain threshold (%)",
            min_value=10.0,
            max_value=300.0,
            value=float(GAIN_THRESHOLD * 100.0),
            step=5.0,
        )
        max_symbols = st.number_input(
            "Max symbols to scan (0 = all)",
            min_value=0,
            max_value=20_000,
            value=3000,
            step=250,
        )
        run_button = st.button("Run / Refresh scan", type="primary")
        st.markdown("---")
        latest_event = get_latest_pump_event_date(conn)
        st.write(f"Latest stored event date: `{latest_event or 'none'}`")
        st.write(f"DB file: `{DATABASE_PATH}`")

    try:
        if run_button:
            status_slot = st.empty()
            progress_bar = st.progress(0)
            progress_text = st.empty()

            def status_callback(message: str) -> None:
                status_slot.info(message)

            def progress_callback(current: int, total: int) -> None:
                pct = int((current / max(total, 1)) * 100)
                progress_bar.progress(min(100, pct))
                progress_text.caption(f"History chunk {current}/{total}")

            result = run_scan(
                conn=conn,
                lookback_days=int(lookback_days),
                gain_threshold=float(gain_threshold_pct / 100.0),
                max_symbols=None if int(max_symbols) == 0 else int(max_symbols),
                status_callback=status_callback,
                progress_callback=progress_callback,
            )
            progress_bar.progress(100)
            st.success(
                "Scan complete: "
                f"{result['events_inserted']} new pump events added, "
                f"{result['events_updated']} refreshed from real historical data "
                f"({result['raw_events_detected']} detected this run), "
                f"{result['watchlist_count']} watchlist rows."
            )

        pump_df = load_pump_events(conn)
        watchlist_df = load_watchlist(conn)
    finally:
        conn.close()

    left, right = st.columns([2.2, 1.2])
    with left:
        st.subheader("50%+ One-Day Pump Events (Last 6 Months default)")
        st.caption(
            "Session type classification is inferred from daily bars. "
            "For post/pre market precision, replace Yahoo source with a full intraday feed."
        )
        if pump_df.empty:
            st.warning("No events in database yet. Click 'Run / Refresh scan'.")
        else:
            event_filter_start = st.date_input(
                "Show events from",
                value=max(date.today() - timedelta(days=int(lookback_days)), date(2000, 1, 1)),
            )
            filtered = pump_df[pd.to_datetime(pump_df["event_date"]).dt.date >= event_filter_start]
            if "session_type" in filtered.columns:
                filtered = filtered.rename(columns={"session_type": "move_occurred"})
            if "one_day_change_pct" in filtered.columns:
                filtered = filtered.rename(columns={"one_day_change_pct": "percent_move"})
            st.dataframe(
                _style_pump_table(filtered),
                use_container_width=True,
                hide_index=True,
            )

    with right:
        st.subheader("Breakout Watchlist")
        st.caption(
            "Scored on tight range, quiet volume rise, MA support, relative strength, and proximity to breakout."
        )
        if watchlist_df.empty:
            st.warning("Watchlist empty until scan is run.")
        else:
            st.dataframe(
                _style_watchlist_table(watchlist_df),
                use_container_width=True,
                hide_index=True,
            )

    with st.expander("Methodology + Notes"):
        st.markdown(
            """
            **Pump event conditions (any one of):**
            - Overnight / pre-market proxy: `(today open / prior close) - 1 >= threshold`
            - Normal trading hours proxy: `(today high / today open) - 1 >= threshold`
            - After-hours / close-to-close proxy: `(today close / prior close) - 1 >= threshold`

            **Fields at event time:**
            - RSI(14), MACD(12,26,9), RVOL(20), beta, shares outstanding, float shares, earnings offsets.
            - Market cap at pump is approximated as `reference_price * shares_outstanding` when available.

            **Automatic daily appending**
            - Re-running the scanner adds unseen `ticker+date+session` events and refreshes any existing rows with the latest real historical data.
            """
        )
    conn.close()


if __name__ == "__main__":
    main()
