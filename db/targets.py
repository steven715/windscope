"""目標部位（契約 targets＝Insight＋Trade Plan）：targets 表唯一的讀寫入口。

put／get／list／lock。payload 存完整契約 JSON，索引欄位只供查詢。
每筆寫入前以契約 `targets.schema.json` 驗過；鎖定是唯一的事後修改（只改 locked_at）。
"""

import json
import logging
import sqlite3

from config import settings
from utils.contract_schema import require_valid
from utils.tw_time import now_tw_iso

logger = logging.getLogger(__name__)

_MUTABLE = {"locked_at", "produced_at"}   # 重算時允許不同的欄位；其餘不同＝內容變了


class TargetLockedError(ValueError):
    """已鎖定的 target 不可再改內容（盤前鎖定、盤中只執行）。"""


def _now() -> str:
    return now_tw_iso()


def target_put(conn: sqlite3.Connection, record: dict, force: bool = False) -> str:
    """寫入／取代一筆 target。已鎖定者：內容相同只保留鎖定；內容不同 raise TargetLockedError（force 才覆蓋）。"""
    require_valid("targets", record)
    existing = target_get(conn, record["target_id"])
    if existing and existing.get("locked_at"):
        changed = {k for k in set(existing) | set(record)
                   if k not in _MUTABLE and existing.get(k) != record.get(k)}
        if changed and not force:
            raise TargetLockedError(
                f"target {record['target_id']} 已於 {existing['locked_at']} 鎖定，不可修改 {sorted(changed)}")
        if changed:
            logger.warning("target %s 已鎖定仍被 force 覆蓋：%s", record["target_id"], sorted(changed))
        record = {**record, "locked_at": existing["locked_at"]}   # 重算不會解除鎖定
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
                 locked_only: bool = False, limit: int = 200) -> list[dict]:
    """列 targets（valid_from 升冪）。session_date＝某日；since＝**有變動**（產生或鎖定）≥ 此時刻。"""
    conds, params = [], []
    if session_date:
        conds.append("session_date = ?")
        params.append(session_date)
    if since:
        conds.append("updated_at >= ?")       # put 與 lock 都更新 updated_at → 鎖定後再 poll 拿得到
        params.append(since)
    if strategy_id:
        conds.append("strategy_id = ?")
        params.append(strategy_id)
    if locked_only:
        conds.append("locked_at IS NOT NULL")
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
    deadline = f"{rec['valid_from'][:10]}T{settings.TARGET_LOCK_DEADLINE}:00+08:00"
    if rec["locked_at"] > deadline:
        logger.warning("target %s 在期限 %s 之後才鎖定（%s）；tradewind 可能已視為中性",
                       target_id, settings.TARGET_LOCK_DEADLINE, rec["locked_at"])
    conn.execute("UPDATE targets SET locked_at = ?, payload = ?, updated_at = ? WHERE target_id = ?",
                 (rec["locked_at"], json.dumps(rec, ensure_ascii=False), _now(), target_id))
    logger.info("target %s locked at %s", target_id, rec["locked_at"])
    return rec

