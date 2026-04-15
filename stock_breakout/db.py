"""SQLite persistence for detections and watchlist rows."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd


def get_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pump_events (
            event_id TEXT PRIMARY KEY,
            event_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            company_name TEXT,
            industry TEXT,
            market_cap REAL,
            rsi REAL,
            macd REAL,
            rvol REAL,
            beta REAL,
            shares_outstanding REAL,
            float_shares REAL,
            days_since_last_earnings INTEGER,
            days_before_next_earnings INTEGER,
            session_type TEXT NOT NULL,
            one_day_change_pct REAL NOT NULL,
            source_note TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pump_date ON pump_events(event_date DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pump_ticker ON pump_events(ticker)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS breakout_watchlist (
            ticker TEXT PRIMARY KEY,
            company_name TEXT,
            industry TEXT,
            breakout_score REAL,
            details TEXT,
            last_price REAL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


def replace_watchlist(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.execute("DELETE FROM breakout_watchlist")
    conn.executemany(
        """
        INSERT INTO breakout_watchlist (
            ticker,
            company_name,
            industry,
            breakout_score,
            details,
            last_price,
            updated_at
        )
        VALUES (:ticker, :company_name, :industry, :breakout_score, :details, :last_price, CURRENT_TIMESTAMP)
        """,
        rows,
    )
    conn.commit()


def upsert_pump_events(conn: sqlite3.Connection, rows: list[dict]) -> dict[str, int]:
    inserted = 0
    updated = 0
    for row in rows:
        existing = conn.execute(
            "SELECT 1 FROM pump_events WHERE event_id = ? LIMIT 1",
            (row["event_id"],),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO pump_events (
                event_id,
                event_date,
                ticker,
                company_name,
                industry,
                market_cap,
                rsi,
                macd,
                rvol,
                beta,
                shares_outstanding,
                float_shares,
                days_since_last_earnings,
                days_before_next_earnings,
                session_type,
                one_day_change_pct,
                source_note
            )
            VALUES (
                :event_id,
                :event_date,
                :ticker,
                :company_name,
                :industry,
                :market_cap,
                :rsi,
                :macd,
                :rvol,
                :beta,
                :shares_outstanding,
                :float_shares,
                :days_since_last_earnings,
                :days_before_next_earnings,
                :session_type,
                :one_day_change_pct,
                :source_note
            )
            ON CONFLICT(event_id) DO UPDATE SET
                event_date=excluded.event_date,
                ticker=excluded.ticker,
                company_name=excluded.company_name,
                industry=excluded.industry,
                market_cap=excluded.market_cap,
                rsi=excluded.rsi,
                macd=excluded.macd,
                rvol=excluded.rvol,
                beta=excluded.beta,
                shares_outstanding=excluded.shares_outstanding,
                float_shares=excluded.float_shares,
                days_since_last_earnings=excluded.days_since_last_earnings,
                days_before_next_earnings=excluded.days_before_next_earnings,
                session_type=excluded.session_type,
                one_day_change_pct=excluded.one_day_change_pct,
                source_note=excluded.source_note,
                created_at=CURRENT_TIMESTAMP
            """,
            row,
        )
        if existing:
            updated += 1
        else:
            inserted += 1
    conn.commit()
    return {"inserted": inserted, "updated": updated}


def load_pump_events(conn: sqlite3.Connection) -> pd.DataFrame:
    query = """
        SELECT
            event_date,
            ticker,
            company_name,
            industry,
            market_cap,
            rsi,
            macd,
            rvol,
            beta,
            shares_outstanding,
            float_shares,
            days_since_last_earnings,
            days_before_next_earnings,
            session_type,
            one_day_change_pct,
            source_note
        FROM pump_events
        ORDER BY event_date DESC, one_day_change_pct DESC
    """
    return pd.read_sql_query(query, conn)


def load_watchlist(conn: sqlite3.Connection) -> pd.DataFrame:
    query = """
        SELECT
            ticker,
            company_name,
            industry,
            breakout_score,
            details,
            last_price,
            updated_at
        FROM breakout_watchlist
        ORDER BY breakout_score DESC, ticker ASC
    """
    return pd.read_sql_query(query, conn)


def get_latest_pump_event_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(event_date) AS latest FROM pump_events").fetchone()
    if row is None:
        return None
    return row["latest"]

