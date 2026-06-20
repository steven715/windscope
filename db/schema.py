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
    quote_pm REAL,
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

CREATE TABLE IF NOT EXISTS stock_info (
    stock_id TEXT PRIMARY KEY,
    stock_name TEXT,
    updated_at TEXT
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

CREATE TABLE IF NOT EXISTS raw_index (
    date TEXT PRIMARY KEY,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    collected_at TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    date TEXT PRIMARY KEY,
    direction TEXT,
    confidence INTEGER,
    fx_vote TEXT,
    futures_vote TEXT,
    reasons TEXT,
    rule_version TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS stock_signals (
    date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    broker_name TEXT NOT NULL,
    category TEXT,
    reasons TEXT,
    rule_version TEXT,
    created_at TEXT,
    PRIMARY KEY (date, stock_id, broker_name)
);

CREATE TABLE IF NOT EXISTS verifications (
    date TEXT PRIMARY KEY,
    predicted_direction TEXT,
    confidence INTEGER,
    prev_close REAL,
    open REAL,
    close REAL,
    open_gap_pct REAL,
    day_change_pct REAL,
    open_gap_class TEXT,
    day_change_class TEXT,
    hit_day INTEGER,
    hit_open INTEGER,
    verified_at TEXT
);

CREATE TABLE IF NOT EXISTS schedule_config (
    job_id TEXT PRIMARY KEY,
    time_hhmm TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS intraday_fx (
    date TEXT NOT NULL,
    currency_pair TEXT NOT NULL,
    ts INTEGER NOT NULL,
    close REAL,
    collected_at TEXT,
    PRIMARY KEY (date, currency_pair, ts)
);

CREATE TABLE IF NOT EXISTS market_holidays (
    date TEXT PRIMARY KEY,
    name TEXT,
    source TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS job_config (
    job_id TEXT PRIMARY KEY,
    display_name TEXT,
    display_desc TEXT,
    notify_enabled INTEGER,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    job_name TEXT,
    trigger_type TEXT NOT NULL,
    run_date TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    status TEXT NOT NULL,
    summary TEXT,
    error TEXT,
    result_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_runs_started ON job_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs(job_id, started_at DESC);
"""


# 既有表加欄位用的輕量 migration：(table, column, coltype)。
# CREATE TABLE IF NOT EXISTS 不會替既有表補欄位，故用 PRAGMA 檢查後 ALTER。
_COLUMN_MIGRATIONS = [
    ("raw_fx", "quote_pm", "REAL"),
    ("job_config", "display_desc", "TEXT"),
]


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """為既有表補上新欄位（SQLite 無 ADD COLUMN IF NOT EXISTS，故先查 PRAGMA）。"""
    for table, column, coltype in _COLUMN_MIGRATIONS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            logger.info("migrated %s: added column %s", table, column)


def create_all_tables(conn: sqlite3.Connection) -> None:
    """建立所有表並套用欄位 migration。可重複執行（冪等）。"""
    conn.executescript(_SCHEMA_SQL)
    _migrate_columns(conn)
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
        upsert_stock_info(conn, stock["stock_id"], stock["stock_name"])
    conn.commit()
    logger.info("Imported %d watchlist entries", len(stocks))
    return len(stocks)


def upsert_stock_info(conn: sqlite3.Connection, stock_id: str,
                      stock_name: str | None) -> None:
    """更新股票資訊表。stock_name 為空時不覆蓋既有名稱。"""
    if not stock_name:
        return
    from datetime import datetime

    conn.execute(
        """INSERT INTO stock_info (stock_id, stock_name, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(stock_id) DO UPDATE SET
               stock_name = excluded.stock_name,
               updated_at = excluded.updated_at""",
        (stock_id, stock_name, datetime.now().isoformat()),
    )
