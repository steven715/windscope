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

-- 個股日K OHLCV（趨勢訊號層的原始資料來源）。來源：證交所 STOCK_DAY，
-- 注意為「未還原股價」——除權息當日會有跳空缺口（TODO: 接還原股價來源）。
CREATE TABLE IF NOT EXISTS raw_stock_daily (
    date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER,
    collected_at TEXT,
    PRIMARY KEY (date, stock_id)
);
CREATE INDEX IF NOT EXISTS idx_stock_daily_id ON raw_stock_daily(stock_id, date);

-- 個股還原因子：免費 FinMind TaiwanStockDividendResult 的除權息 after/before 比例。
-- 供還原股價「配息細調」用（分割/大除權缺口另由價格自動偵測，不需此表）。
CREATE TABLE IF NOT EXISTS stock_dividends (
    stock_id TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    factor REAL,
    collected_at TEXT,
    PRIMARY KEY (stock_id, ex_date)
);

-- 個股趨勢訊號（spec v2 每日輸出 schema）。純技術面，由 raw_stock_daily 還原後導出。
CREATE TABLE IF NOT EXISTS stock_trend_signals (
    date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    big_trend TEXT,
    big_trend_confidence REAL,
    value_center REAL,
    bias_pct REAL,
    small_state TEXT,
    small_turn_up INTEGER,
    fundamental_gate TEXT,
    action_hint TEXT,
    position_hint TEXT,
    flat_redirect TEXT,
    reasons TEXT,
    adjust_note TEXT,
    rule_version TEXT,
    created_at TEXT,
    PRIMARY KEY (date, stock_id)
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
    verified_at TEXT,
    rule_version TEXT           -- 驗證當下那筆判斷的規則版本（signals 之後被 recompute 改掉也不變；命中率依它分版）
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
    enabled INTEGER,
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

-- 回測層（Layer 5）：一次 run 一列摘要 + 逐筆交易 + 權益曲線。純離線重算結果，
-- 唯讀不影響訊號/排程；params_json 記下當次的門檻/成本/rule_version 快照供逐版比較。
CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    stock_id TEXT NOT NULL,
    entry_mode TEXT,
    max_units INTEGER,
    date_from TEXT,
    date_to TEXT,
    n_bars INTEGER,
    n_trades INTEGER,
    total_return REAL,
    cagr REAL,
    max_drawdown REAL,
    sharpe REAL,
    win_rate REAL,
    profit_factor REAL,
    expectancy REAL,
    benchmark_return REAL,
    exposure_pct REAL,
    final_equity REAL,
    trend_rule_version TEXT,
    backtest_rule_version TEXT,
    params_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_stock ON backtest_runs(stock_id, created_at DESC);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    stock_id TEXT,
    entry_date TEXT,
    entry_price REAL,
    exit_date TEXT,
    exit_price REAL,
    shares REAL,
    unit_index INTEGER,
    gross_pnl REAL,
    fees REAL,
    tax REAL,
    net_pnl REAL,
    return_pct REAL,
    hold_days INTEGER,
    exit_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_backtest_trades_run ON backtest_trades(run_id);

CREATE TABLE IF NOT EXISTS backtest_equity (
    run_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    equity REAL,
    drawdown REAL,
    units INTEGER,
    price REAL,
    PRIMARY KEY (run_id, date)
);

-- ── 契約表（docs/contracts，ADR-0005／0006）────────────────────────────
-- 商品主檔：只由 init-db 經 db/instruments.instruments_seed 灌種子，執行期以表為準。
CREATE TABLE IF NOT EXISTS instruments (
    instrument_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,          -- 契約 instruments 一筆的 JSON（schema 驗過）
    asset_class TEXT,
    tradable INTEGER,
    active INTEGER,
    as_of TEXT,
    updated_at TEXT
);

-- 統一 K 線列（契約 bars）：新來源（即時串流落地、1m/tick）寫這裡；舊三張 raw 表經 bars_v 映射。
CREATE TABLE IF NOT EXISTS bars (
    instrument_id TEXT NOT NULL,
    resolution TEXT NOT NULL,
    session TEXT NOT NULL,
    session_date TEXT NOT NULL,
    ts TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL NOT NULL,
    volume INTEGER, turnover REAL, open_interest INTEGER,
    source TEXT NOT NULL,
    published_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (instrument_id, resolution, session, ts, revision)
);
CREATE INDEX IF NOT EXISTS idx_bars_lookup ON bars(instrument_id, resolution, session_date);

-- 目標部位（契約 targets＝Insight＋Trade Plan）：只由 db/targets.py 讀寫。
CREATE TABLE IF NOT EXISTS targets (
    target_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    session_date TEXT NOT NULL,     -- valid_from 的日期（查詢用）
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    locked_at TEXT,
    produced_at TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    payload TEXT NOT NULL,          -- 完整契約 JSON（含 trade_plan、invalidation）
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_targets_date ON targets(session_date, strategy_id);

-- 事件回流（契約 events）：tradewind 寫、windscope 讀；只增不改，event_id 去重。
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    run_mode TEXT NOT NULL,
    session_date TEXT NOT NULL,     -- ts 的日期
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    action TEXT NOT NULL,
    strategy_id TEXT,
    target_id TEXT,
    instrument_id TEXT,
    payload TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(session_date, run_mode);

-- 帳戶快照（契約 account_snapshots）：tradewind 寫；錢包／評分讀。
CREATE TABLE IF NOT EXISTS account_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    run_mode TEXT NOT NULL,
    account_id TEXT NOT NULL,
    session_date TEXT NOT NULL,     -- as_of 的日期
    as_of TEXT NOT NULL,
    equity REAL,
    payload TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON account_snapshots(session_date, run_mode);
"""

# bars_v：契約 bars 的統一讀取視圖＝新 bars 表 ∪ 三張舊 raw 表（不搬資料，過渡用）。
# 舊表沒有可知時刻，依契約規則推：日 K＝收盤時刻、夜盤＝05:00；ingested_at 取 collected_at。
# 夜盤 ts 以「歸屬日前一日 15:00」近似（週一的夜盤實為上週五；精確值由 bars 表承載），所以
# 同一商品同日若 bars 表已有資料，raw 列一律排除（NOT EXISTS），避免同一根 K 出現兩筆。
# origin：1＝bars 表、0＝raw 映射，讀取端去重時 bars 優先。raw_futures 的 oi_net_foreign 是外資
# 淨部位（帶號），不是契約的 open_interest（總未平倉），故不映射。
# 日期格式／時區正規化（+08:00）在 db/bars.py 讀取端做，view 只負責對映欄位。
# DROP/CREATE 包在一個交易裡，server 執行中跑 init-db 不會出現「no such table: bars_v」的空窗。
_BARS_VIEW_SQL = """
BEGIN;
DROP VIEW IF EXISTS bars_v;
CREATE VIEW bars_v AS
SELECT instrument_id, resolution, session, session_date, ts, open, high, low, close,
       volume, turnover, open_interest, source, published_at, ingested_at, revision, 1 AS origin
  FROM bars
UNION ALL
SELECT 'TWSE:TAIEX', '1d', 'regular', r.date, r.date || 'T09:00:00+08:00',
       r.open, r.high, r.low, r.close, NULL, NULL, NULL, 'twse.raw_index',
       r.date || 'T13:30:00+08:00', r.collected_at, 0, 0
  FROM raw_index r WHERE r.close IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM bars b WHERE b.instrument_id = 'TWSE:TAIEX'
                   AND b.resolution = '1d' AND b.session = 'regular' AND b.session_date = r.date)
UNION ALL
SELECT 'TWSE:' || r.stock_id, '1d', 'regular', r.date, r.date || 'T09:00:00+08:00',
       r.open, r.high, r.low, r.close, r.volume, NULL, NULL, 'twse.raw_stock_daily',
       r.date || 'T13:30:00+08:00', r.collected_at, 0, 0
  FROM raw_stock_daily r WHERE r.close IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM bars b WHERE b.instrument_id = 'TWSE:' || r.stock_id
                   AND b.resolution = '1d' AND b.session = 'regular' AND b.session_date = r.date)
UNION ALL
SELECT 'TAIFEX:TX', '1d', 'night', r.date, date(r.date, '-1 day') || 'T15:00:00+08:00',
       NULL, NULL, NULL, r.night_close, r.night_volume, NULL, NULL, 'taifex.raw_futures',
       r.date || 'T05:00:00+08:00', r.collected_at, 0, 0
  FROM raw_futures r WHERE r.night_close IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM bars b WHERE b.instrument_id = 'TAIFEX:TX'
                   AND b.resolution = '1d' AND b.session = 'night' AND b.session_date = r.date);
COMMIT;
"""


# 既有表加欄位用的輕量 migration：(table, column, coltype)。
# CREATE TABLE IF NOT EXISTS 不會替既有表補欄位，故用 PRAGMA 檢查後 ALTER。
_COLUMN_MIGRATIONS = [
    ("job_config", "display_desc", "TEXT"),
    ("job_config", "enabled", "INTEGER"),
    ("stock_trend_signals", "adjust_note", "TEXT"),
    ("verifications", "rule_version", "TEXT"),
]

# 既有表移除欄位用的 migration：(table, column)。SQLite >=3.35 支援 DROP COLUMN，
# 先查 PRAGMA 確認欄位還在才 drop（冪等）。quote_pm（午盤匯率觀察）已停用、移除。
_DROP_COLUMNS = [
    ("raw_fx", "quote_pm"),
]


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """為既有表補上新欄位（SQLite 無 ADD COLUMN IF NOT EXISTS，故先查 PRAGMA）。"""
    for table, column, coltype in _COLUMN_MIGRATIONS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            logger.info("migrated %s: added column %s", table, column)


def _migrate_drop_columns(conn: sqlite3.Connection) -> None:
    """移除既有表的廢棄欄位（欄位還在才 drop，冪等）。"""
    for table, column in _DROP_COLUMNS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column in existing:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            logger.info("migrated %s: dropped column %s", table, column)


def _backfill_verification_rule_version(conn: sqlite3.Connection) -> int:
    """verifications 剛加上 rule_version 時，補上舊列的版本（只在補欄位那次跑）。

    只有「判斷在驗證之前就產生、之後沒被 recompute 覆寫」（signals.created_at <= verified_at）的列，
    signals 現在的版本才是驗證當下的版本；其他列不猜、留 NULL（不列入分版命中率）。回傳補上的筆數。
    """
    conn.execute(
        """UPDATE verifications SET rule_version = (
               SELECT s.rule_version FROM signals s
               WHERE s.date = verifications.date AND s.created_at <= verifications.verified_at)
           WHERE rule_version IS NULL""")
    filled = conn.execute("SELECT COUNT(*) FROM verifications WHERE rule_version IS NOT NULL").fetchone()[0]
    unknown = conn.execute("SELECT COUNT(*) FROM verifications WHERE rule_version IS NULL").fetchone()[0]
    logger.info("migrated verifications.rule_version: %d 筆補上版本，%d 筆判斷在驗證後被重算過、版本不明（留 NULL）",
                filled, unknown)
    return filled


def create_all_tables(conn: sqlite3.Connection) -> None:
    """建立所有表並套用欄位 migration。可重複執行（冪等）。"""
    conn.executescript(_SCHEMA_SQL)
    had_ver_version = "rule_version" in {r[1] for r in conn.execute("PRAGMA table_info(verifications)")}
    _migrate_columns(conn)
    if not had_ver_version:
        _backfill_verification_rule_version(conn)
    _migrate_drop_columns(conn)
    conn.executescript(_BARS_VIEW_SQL)   # view 每次重建，定義改了才會跟上
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
