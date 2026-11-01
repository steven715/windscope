"""統一 K 線列：bars 表與 bars_v 視圖唯一的讀寫入口（query、upsert）。

讀取走 `bars_v`（新 bars 表 ∪ 三張舊 raw 表的映射），輸出一律是契約 `bars` 的 dict 形狀，
時刻正規化為 `YYYY-MM-DDTHH:MM:SS+08:00`（舊表的 collected_at 沒有時區、可能帶微秒）。
寫入只進 `bars` 表（新來源：串流落地、tick／1m），每筆以契約驗過。
"""

import json
import logging
import sqlite3

from utils.contract_schema import require_valid

logger = logging.getLogger(__name__)

_COLS = ("instrument_id", "resolution", "session", "session_date", "ts", "open", "high", "low",
         "close", "volume", "turnover", "open_interest", "source", "published_at",
         "ingested_at", "revision")


def normalize_ts(value: str | None, fallback: str | None = None) -> str | None:
    """把各種時間字串正規化成契約格式 `YYYY-MM-DDTHH:MM:SS+08:00`；空值用 fallback。"""
    v = value or fallback
    if not v:
        return None
    v = v.strip().replace(" ", "T")
    if "." in v:                       # 去微秒：...:41.123456(+08:00)
        head, _, tail = v.partition(".")
        tz = tail[tail.find("+"):] if "+" in tail else ""
        v = head + tz
    if len(v) == 10:                   # 只有日期
        v += "T00:00:00"
    if "+" not in v and not v.endswith("Z"):
        v += "+08:00"
    return v


def _row_to_record(row: tuple) -> dict:
    rec = dict(zip(_COLS, row))
    rec["published_at"] = normalize_ts(rec["published_at"])
    rec["ingested_at"] = normalize_ts(rec["ingested_at"], fallback=rec["published_at"])
    rec["ts"] = normalize_ts(rec["ts"])
    for k in ("volume", "open_interest"):
        if rec[k] is not None:
            rec[k] = int(rec[k])
    rec["revision"] = int(rec["revision"] or 0)
    return rec


def bars_query(conn: sqlite3.Connection, instrument_id: str, resolution: str = "1d",
               date_from: str | None = None, date_to: str | None = None,
               session: str | None = None, limit: int = 500) -> list[dict]:
    """查 bars_v（舊→新），回契約 bars dict 列表。同一鍵多 revision 時取最大。"""
    conds = ["instrument_id = ?", "resolution = ?"]
    params: list = [instrument_id, resolution]
    if session:
        conds.append("session = ?")
        params.append(session)
    if date_from:
        conds.append("session_date >= ?")
        params.append(date_from)
    if date_to:
        conds.append("session_date <= ?")
        params.append(date_to)
    sql = (f"SELECT {', '.join(_COLS)} FROM bars_v WHERE {' AND '.join(conds)} "
           "ORDER BY ts ASC, revision DESC LIMIT ?")
    rows = conn.execute(sql, [*params, max(1, limit)]).fetchall()
    out: dict[tuple, dict] = {}
    for r in rows:
        rec = _row_to_record(r)
        key = (rec["instrument_id"], rec["resolution"], rec["session"], rec["ts"])
        if key not in out:            # 已依 revision DESC 排，首筆即最大 revision
            out[key] = rec
    return list(out.values())


def bars_upsert(conn: sqlite3.Connection, records: list[dict]) -> int:
    """寫入 bars 表（新來源）。每筆先驗契約；回傳寫入筆數。"""
    n = 0
    for rec in records:
        rec = {**rec, "revision": rec.get("revision", 0)}
        require_valid("bars", rec)
        conn.execute(
            f"INSERT OR REPLACE INTO bars ({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})",
            tuple(rec.get(c) for c in _COLS),
        )
        n += 1
    return n


def bars_latest_ts(conn: sqlite3.Connection, instrument_id: str, resolution: str) -> str | None:
    """該商品該粒度在 bars 表（不含舊表）最新的 ts；無資料回 None。串流續接用。"""
    r = conn.execute("SELECT MAX(ts) FROM bars WHERE instrument_id = ? AND resolution = ?",
                     (instrument_id, resolution)).fetchone()
    return r[0] if r and r[0] else None


def bars_from_jsonl(path: str) -> list[dict]:
    """讀一個 JSONL 檔成 bars 記錄列表（離線交換用）。"""
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]

