"""事件回流與帳戶快照：events／account_snapshots 表唯一的讀寫入口（ingest、list、掃 inbox）。

tradewind 每日把 `events/{run_mode}/{date}.jsonl`、`account_snapshots/{run_mode}/{date}.jsonl`
寫進 inbox（settings.TRADEWIND_INBOX_DIR）；after_close 或 CLI 呼叫 ingest_inbox 入庫。
每筆以契約驗過；以 event_id／snapshot_id 去重（只增不改，重送不重複）。
"""

import json
import logging
import sqlite3
from pathlib import Path

from utils.tw_time import now_tw_iso
from utils.contract_schema import ContractError, validate_record

logger = logging.getLogger(__name__)


def _now() -> str:
    return now_tw_iso()


def events_ingest(conn: sqlite3.Connection, records: list[dict]) -> dict:
    """寫入 events（去重 by event_id）。回傳 {inserted, duplicates, invalid, errors}。"""
    res = {"inserted": 0, "duplicates": 0, "invalid": 0, "errors": []}
    now = _now()
    for rec in records:
        errs = validate_record("events", rec)
        if errs:
            res["invalid"] += 1
            res["errors"].append(f"{rec.get('event_id', '?')}: {errs[0]}")
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO events (event_id, run_mode, session_date, ts, kind, action, "
            "strategy_id, target_id, instrument_id, payload, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rec["event_id"], rec["run_mode"], rec["ts"][:10], rec["ts"], rec["kind"],
             rec["action"], rec.get("strategy_id"), rec.get("target_id"),
             rec.get("instrument_id"), json.dumps(rec, ensure_ascii=False), now),
        )
        if cur.rowcount:
            res["inserted"] += 1
        else:
            res["duplicates"] += 1
    return res


def snapshots_ingest(conn: sqlite3.Connection, records: list[dict]) -> dict:
    """寫入 account_snapshots（去重 by snapshot_id）。回傳同 events_ingest 的統計。"""
    res = {"inserted": 0, "duplicates": 0, "invalid": 0, "errors": []}
    now = _now()
    for rec in records:
        errs = validate_record("account_snapshots", rec)
        if errs:
            res["invalid"] += 1
            res["errors"].append(f"{rec.get('snapshot_id', '?')}: {errs[0]}")
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO account_snapshots (snapshot_id, run_mode, account_id, "
            "session_date, as_of, equity, payload, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rec["snapshot_id"], rec["run_mode"], rec["account_id"], rec["as_of"][:10],
             rec["as_of"], rec.get("equity"), json.dumps(rec, ensure_ascii=False), now),
        )
        res["inserted" if cur.rowcount else "duplicates"] += 1
    return res


def events_list(conn: sqlite3.Connection, session_date: str | None = None,
                run_mode: str | None = None, target_id: str | None = None,
                limit: int = 500) -> list[dict]:
    """列 events（ts 升冪）。"""
    conds, params = [], []
    for col, val in (("session_date", session_date), ("run_mode", run_mode), ("target_id", target_id)):
        if val:
            conds.append(f"{col} = ?")
            params.append(val)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    rows = conn.execute(f"SELECT payload FROM events{where} ORDER BY ts ASC LIMIT ?",
                        [*params, max(1, limit)]).fetchall()
    return [json.loads(r[0]) for r in rows]


def snapshots_list(conn: sqlite3.Connection, session_date: str | None = None,
                   run_mode: str | None = None, limit: int = 200) -> list[dict]:
    """列帳戶快照（as_of 升冪）。"""
    conds, params = [], []
    for col, val in (("session_date", session_date), ("run_mode", run_mode)):
        if val:
            conds.append(f"{col} = ?")
            params.append(val)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    rows = conn.execute(f"SELECT payload FROM account_snapshots{where} ORDER BY as_of ASC LIMIT ?",
                        [*params, max(1, limit)]).fetchall()
    return [json.loads(r[0]) for r in rows]


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning("%s:%d 不是合法 JSON：%s", path, i, e)
    return out


def ingest_inbox(conn: sqlite3.Connection, inbox_dir: str | None = None,
                 session_date: str | None = None) -> dict:
    """掃 inbox 的 events／account_snapshots JSONL 入庫（冪等，可重跑）。

    目錄：{inbox}/events/{run_mode}/{date}.jsonl、{inbox}/account_snapshots/{run_mode}/{date}.jsonl。
    session_date 給定只掃該日檔案。回傳兩張表的統計與掃到的檔案數。
    """
    from config import settings

    root = Path(inbox_dir or settings.TRADEWIND_INBOX_DIR)
    summary = {"files": 0, "events": {"inserted": 0, "duplicates": 0, "invalid": 0},
               "account_snapshots": {"inserted": 0, "duplicates": 0, "invalid": 0}, "errors": []}
    if not root.exists():
        logger.info("inbox %s 不存在，略過", root)
        return summary
    for table, fn in (("events", events_ingest), ("account_snapshots", snapshots_ingest)):
        base = root / table
        if not base.exists():
            continue
        pattern = f"*/{session_date}.jsonl" if session_date else "*/*.jsonl"
        for path in sorted(base.glob(pattern)):
            try:
                res = fn(conn, _read_jsonl(path))
            except ContractError as e:
                summary["errors"].append(f"{path}: {e}")
                continue
            summary["files"] += 1
            for k in ("inserted", "duplicates", "invalid"):
                summary[table][k] += res[k]
            summary["errors"] += [f"{path.name}: {m}" for m in res["errors"][:5]]
    logger.info("ingest_inbox %s: %s", root, summary)
    return summary

