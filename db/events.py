"""事件回流與帳戶快照：events／account_snapshots 表唯一的讀寫入口（ingest、list、掃 inbox）。

tradewind 每日把 `events/{run_mode}/{date}.jsonl`、`account_snapshots/{run_mode}/{date}.jsonl`
寫進 inbox（settings.TRADEWIND_INBOX_DIR）；after_close 或 CLI 呼叫 ingest_inbox 入庫。
每筆以契約驗過；以 event_id／snapshot_id 去重（只增不改，重送不重複）。
"""

import json
import logging
import sqlite3
from pathlib import Path

from utils.contract_schema import validate_record
from utils.tw_time import now_tw_iso

logger = logging.getLogger(__name__)


def _now() -> str:
    return now_tw_iso()


def _check(name: str, rec, res: dict, id_key: str, expect_run_mode: str | None) -> bool:
    """契約驗證＋run_mode 檢查；不合法計入 invalid 並回 False。"""
    if not isinstance(rec, dict):
        res["invalid"] += 1
        res["errors"].append(f"?: 不是物件（{type(rec).__name__}）")
        return False
    errs = validate_record(name, rec)
    if not errs and expect_run_mode and rec.get("run_mode") != expect_run_mode:
        errs = [f"run_mode {rec.get('run_mode')!r} 與來源目錄 {expect_run_mode!r} 不符"]
    if errs:
        res["invalid"] += 1
        res["errors"].append(f"{rec.get(id_key, '?')}: {errs[0]}")
        return False
    return True


def events_ingest(conn: sqlite3.Connection, records: list, session_date: str | None = None,
                  expect_run_mode: str | None = None) -> dict:
    """寫入 events（去重 by event_id）。回傳 {inserted, duplicates, invalid, errors}。

    session_date 給定時以它為歸屬交易日（inbox 檔名），否則取 ts 的日期；
    expect_run_mode 給定時 run_mode 不符的筆數計入 invalid（Paper 與實盤永不混算）。
    """
    res = {"inserted": 0, "duplicates": 0, "invalid": 0, "errors": []}
    now = _now()
    for rec in records:
        if not _check("events", rec, res, "event_id", expect_run_mode):
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO events (event_id, run_mode, session_date, ts, kind, action, "
            "strategy_id, target_id, instrument_id, payload, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rec["event_id"], rec["run_mode"], session_date or rec["ts"][:10], rec["ts"], rec["kind"],
             rec["action"], rec.get("strategy_id"), rec.get("target_id"),
             rec.get("instrument_id"), json.dumps(rec, ensure_ascii=False), now),
        )
        if cur.rowcount:
            res["inserted"] += 1
        else:
            res["duplicates"] += 1
    return res


def snapshots_ingest(conn: sqlite3.Connection, records: list, session_date: str | None = None,
                     expect_run_mode: str | None = None) -> dict:
    """寫入 account_snapshots（去重 by snapshot_id）。參數語意同 events_ingest。"""
    res = {"inserted": 0, "duplicates": 0, "invalid": 0, "errors": []}
    now = _now()
    for rec in records:
        if not _check("account_snapshots", rec, res, "snapshot_id", expect_run_mode):
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO account_snapshots (snapshot_id, run_mode, account_id, "
            "session_date, as_of, equity, payload, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rec["snapshot_id"], rec["run_mode"], rec["account_id"], session_date or rec["as_of"][:10],
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


def events_range(conn: sqlite3.Connection, run_mode: str | None = None, date_from: str | None = None,
                 date_to: str | None = None, kinds: tuple[str, ...] = (), limit: int = 200_000) -> list[dict]:
    """區間內的 events（session_date 介於兩者、含端點；kinds 給了只取這些 kind），ts、event_id 升冪。給打分（integration/trade_stats）用。
    每筆另加 `_session_date`（入庫時的交易日：夜盤的 ts 日期不等於它）；超過 limit 記 WARNING（被截掉的是最新的）。"""
    conds, params = [], []
    for cond, val in (("run_mode = ?", run_mode), ("session_date >= ?", date_from), ("session_date <= ?", date_to)):
        if val:
            conds.append(cond)
            params.append(val)
    if kinds:
        conds.append(f"kind IN ({','.join('?' * len(kinds))})")
        params.extend(kinds)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    rows = conn.execute(f"SELECT payload, session_date FROM events{where} ORDER BY ts ASC, event_id ASC LIMIT ?",
                        [*params, max(1, limit)]).fetchall()
    if len(rows) >= limit:
        logger.warning("events_range hit the limit (%d rows); the newest events are cut off", limit)
    return [{**json.loads(r[0]), "_session_date": r[1]} for r in rows]


def snapshots_range(conn: sqlite3.Connection, run_mode: str | None = None, date_from: str | None = None,
                    date_to: str | None = None, limit: int = 100_000) -> list[dict]:
    """區間內的帳戶快照（as_of 升冪）。"""
    conds, params = [], []
    for cond, val in (("run_mode = ?", run_mode), ("session_date >= ?", date_from), ("session_date <= ?", date_to)):
        if val:
            conds.append(cond)
            params.append(val)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    rows = conn.execute(f"SELECT payload FROM account_snapshots{where} ORDER BY as_of ASC, snapshot_id ASC LIMIT ?",
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
                # 歸屬交易日取檔名、run_mode 取目錄：檔案內容不符的筆數算 invalid
                res = fn(conn, _read_jsonl(path), session_date=path.stem,
                         expect_run_mode=path.parent.name)
            except Exception as e:   # 一個壞檔不該殺掉整個入庫步驟
                logger.error("ingest %s 失敗：%s", path, e)
                summary["errors"].append(f"{path}: {e}")
                continue
            summary["files"] += 1
            for k in ("inserted", "duplicates", "invalid"):
                summary[table][k] += res[k]
            summary["errors"] += [f"{path.name}: {m}" for m in res["errors"][:5]]
    logger.info("ingest_inbox %s: %s", root, summary)
    return summary

