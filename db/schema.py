import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS raw_fx (
    date TEXT NOT NULL,
    currency_pair TEXT NOT NULL,
    close_16 REAL,
    quote_0845 REAL,
    ny_close REAL,
    collected_at TEXT,
    PRIMARY KEY (date, currency_pair)
);

CREATE TABLE IF NOT EXISTS raw_futures (
    date TEXT PRIMARY KEY,
    night_close REAL,
    night_volume INTEGER,
    spot_close REAL,
    oi_net_foreign INTEGER,
    ex_dividend_points REAL,
    ftse_tw_close REAL,
    sp500_close REAL,
    collected_at TEXT
);

CREATE TABLE IF NOT EXISTS raw_chip (
    date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    stock_name TEXT,
    broker_name TEXT NOT NULL,
    buy_volume INTEGER,
    sell_volume INTEGER,
    net_volume INTEGER,
    close_price REAL,
    collected_at TEXT,
    PRIMARY KEY (date, stock_id, broker_name)
);

CREATE TABLE IF NOT EXISTS raw_institutional (
    date TEXT PRIMARY KEY,
    foreign_buy REAL,
    foreign_sell REAL,
    foreign_net REAL,
    trust_buy REAL,
    trust_sell REAL,
    trust_net REAL,
    dealer_buy REAL,
    dealer_sell REAL,
    dealer_net REAL,
    total_net REAL,
    collected_at TEXT
);

CREATE TABLE IF NOT EXISTS broker_tags (
    broker_name TEXT PRIMARY KEY,
    broker_type TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS watchlist (
    stock_id TEXT PRIMARY KEY,
    stock_name TEXT,
    added_date TEXT,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS daily_metrics (
    date TEXT PRIMARY KEY,
    fx_delta_twd REAL,
    fx_delta_cny REAL,
    fx_delta_krw REAL,
    fx_direction TEXT,
    fx_asia_sync INTEGER,
    fx_asia_detail TEXT,
    futures_spread REAL,
    futures_spread_adjusted REAL,
    futures_volume_ratio REAL,
    oi_net_foreign INTEGER,
    oi_delta INTEGER,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS daily_stock_metrics (
    date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    broker_name TEXT NOT NULL,
    net_amount REAL,
    consecutive_days INTEGER,
    price_vs_ma20 REAL,
    price_zone TEXT,
    both_sides_flag INTEGER,
    broker_type TEXT,
    PRIMARY KEY (date, stock_id, broker_name)
);
"""


def create_all_tables(conn: sqlite3.Connection) -> None:
    """建立所有 Phase 1 的表。可重複執行（CREATE TABLE IF NOT EXISTS）。"""
    conn.executescript(_SCHEMA_SQL)
    logger.info("All tables created (or already exist)")


def import_broker_tags(
    conn: sqlite3.Connection, json_path: str | None = None
) -> int:
    """從 broker_tags.json 匯入分點標籤，回傳匯入筆數。"""
    json_path = json_path or str(
        Path(__file__).resolve().parent.parent / "config" / "broker_tags.json"
    )
    with open(json_path, encoding="utf-8") as f:
        tags = json.load(f)

    for tag in tags:
        conn.execute(
            "INSERT OR REPLACE INTO broker_tags (broker_name, broker_type, notes) "
            "VALUES (?, ?, ?)",
            (tag["broker_name"], tag["broker_type"], tag["notes"]),
        )
    conn.commit()
    logger.info("Imported %d broker tags", len(tags))
    return len(tags)


def import_watchlist(
    conn: sqlite3.Connection, json_path: str | None = None
) -> int:
    """從 watchlist.json 匯入觀察名單，回傳匯入筆數。"""
    json_path = json_path or str(
        Path(__file__).resolve().parent.parent / "config" / "watchlist.json"
    )
    with open(json_path, encoding="utf-8") as f:
        stocks = json.load(f)

    for stock in stocks:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist (stock_id, stock_name, added_date, reason) "
            "VALUES (?, ?, ?, ?)",
            (
                stock["stock_id"],
                stock["stock_name"],
                stock["added_date"],
                stock["reason"],
            ),
        )
    conn.commit()
    logger.info("Imported %d watchlist entries", len(stocks))
    return len(stocks)
