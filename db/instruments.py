"""商品主檔：instruments 表唯一的讀寫入口（seed、list、get、upsert）。

執行期一律以 DB 表為準；`config/instruments.json` 只是初始種子，只由 init-db 經
instruments_seed 灌一次（與 watchlist 同一套規則，CLAUDE.md 硬約束 #10）。
每筆以契約 `instruments.schema.json` 驗過才寫入。
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from db.connection import get_connection
from utils.contract_schema import require_valid

logger = logging.getLogger(__name__)

_SEED_PATH = Path(__file__).resolve().parent.parent / "config" / "instruments.json"


def instrument_upsert(conn: sqlite3.Connection, record: dict) -> None:
    """寫入一筆商品主檔（INSERT OR REPLACE）；不符契約 raise ContractError。"""
    require_valid("instruments", record)
    conn.execute(
        "INSERT OR REPLACE INTO instruments "
        "(instrument_id, payload, asset_class, tradable, active, as_of, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (record["instrument_id"], json.dumps(record, ensure_ascii=False),
         record["asset_class"], int(record["tradable"]), int(record.get("active", True)),
         record["as_of"], datetime.now().isoformat(timespec="seconds")),
    )


def instruments_seed(conn: sqlite3.Connection, json_path: str | None = None) -> int:
    """從種子檔匯入商品主檔（init-db 用）。回傳匯入筆數。"""
    with open(json_path or _SEED_PATH, encoding="utf-8") as f:
        rows = json.load(f)
    for r in rows:
        instrument_upsert(conn, r)
    conn.commit()
    logger.info("Imported %d instruments", len(rows))
    return len(rows)


def instruments_list(db_path: str | None = None, conn: sqlite3.Connection | None = None,
                     tradable_only: bool = False) -> list[dict]:
    """列出商品主檔（契約 dict 形狀），依 instrument_id 排序。"""
    sql = "SELECT payload FROM instruments"
    if tradable_only:
        sql += " WHERE tradable = 1"
    sql += " ORDER BY instrument_id"
    if conn is not None:
        rows = conn.execute(sql).fetchall()
    else:
        with get_connection(db_path) as c:
            rows = c.execute(sql).fetchall()
    return [json.loads(r[0]) for r in rows]


def instrument_get(instrument_id: str, db_path: str | None = None,
                   conn: sqlite3.Connection | None = None) -> dict | None:
    """取單一商品（契約 dict）；不存在回 None。"""
    sql = "SELECT payload FROM instruments WHERE instrument_id = ?"
    if conn is not None:
        r = conn.execute(sql, (instrument_id,)).fetchone()
    else:
        with get_connection(db_path) as c:
            r = c.execute(sql, (instrument_id,)).fetchone()
    return json.loads(r[0]) if r else None
