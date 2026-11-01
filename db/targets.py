"""目標部位（契約 targets＝Insight＋Trade Plan）：targets 表唯一的讀寫入口。

put／get／list／lock。payload 存完整契約 JSON，索引欄位只供查詢。
每筆寫入前以契約 `targets.schema.json` 驗過；鎖定是唯一的事後修改（只改 locked_at）。
"""

import json
import logging
import sqlite3

from utils.tw_time import now_tw_iso
from utils.contract_schema import require_valid

logger = logging.getLogger(__name__)


def _now() -> str:
    return now_tw_iso()


def target_put(conn: sqlite3.Connection, record: dict) -> str:
    """寫入／取代一筆 target（同 target_id 覆蓋，locked_at 保留既有值）。回傳 target_id。"""
    require_valid("targets", record)
    existing = conn.execute("SELECT locked_at FROM targets WHERE target_id = ?",
                            (record["target_id"],)).fetchone()
    if existing and existing[0] and not record.get("locked_at"):
        record = {**record, "locked_at": existing[0]}   # 重算不會解除鎖定
    conn.execute(
        "INSERT OR REPLACE INTO targets (target_id, strategy_id, rule_version, instrument_id, "
        "session_date, valid_from, valid_until, locked_at, produced_at, schema_version, payload, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (record["target_id"], record["strategy_id"], record["rule_version"],
         record["instrument_id"], record["valid_from"][:10], record["valid_from"],
         record["valid_until"], record.get("locked_at"), record["produced_at"],
         record["schema_version"], json.dumps(record, ensure_ascii=False), _now()),
    )
    return record["target_id"]


def target_get(conn: sqlite3.Connection, target_id: str) -> dict | None:
    """取單筆 target（契約 dict）；不存在回 None。"""
    r = conn.execute("SELECT payload FROM targets WHERE target_id = ?", (target_id,)).fetchone()
    return json.loads(r[0]) if r else None


def targets_list(conn: sqlite3.Connection, session_date: str | None = None,
                 since: str | None = None, strategy_id: str | None = None,
                 limit: int = 200) -> list[dict]:
    """列 targets（valid_from 升冪）。session_date＝某日；since＝produced_at ≥ 此時刻。"""
    conds, params = [], []
    if session_date:
        conds.append("session_date = ?")
        params.append(session_date)
    if since:
        conds.append("produced_at >= ?")
        params.append(since)
    if strategy_id:
        conds.append("strategy_id = ?")
        params.append(strategy_id)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    rows = conn.execute(f"SELECT payload FROM targets{where} ORDER BY valid_from ASC, strategy_id "
                        "LIMIT ?", [*params, max(1, limit)]).fetchall()
    return [json.loads(r[0]) for r in rows]


def target_lock(conn: sqlite3.Connection, target_id: str, locked_at: str | None = None) -> dict | None:
    """鎖定 Trade Plan：寫 locked_at（預設現在）。已鎖定者不改；不存在回 None。"""
    rec = target_get(conn, target_id)
    if rec is None:
        return None
    if rec.get("locked_at"):
        return rec
    rec = {**rec, "locked_at": locked_at or _now()}
    require_valid("targets", rec)
    conn.execute("UPDATE targets SET locked_at = ?, payload = ?, updated_at = ? WHERE target_id = ?",
                 (rec["locked_at"], json.dumps(rec, ensure_ascii=False), _now(), target_id))
    logger.info("target %s locked at %s", target_id, rec["locked_at"])
    return rec

