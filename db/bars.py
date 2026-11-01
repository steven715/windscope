"""統一 K 線列：bars 表與 bars_v 視圖唯一的讀寫入口（query、upsert）。

讀取走 `bars_v`（新 bars 表 ∪ 三張舊 raw 表的映射），輸出一律是契約 `bars` 的 dict 形狀，
時刻正規化為 `YYYY-MM-DDTHH:MM:SS+08:00`（舊表的 collected_at 沒有時區、可能帶微秒）。
寫入只進 `bars` 表（新來源：串流落地、tick／1m），每筆以契約驗過。
"""

import json
import logging
import sqlite3
from datetime import datetime

from utils.contract_schema import require_valid
from utils.tw_time import TW

logger = logging.getLogger(__name__)

_COLS = ("instrument_id", "resolution", "session", "session_date", "ts", "open", "high", "low",
         "close", "volume", "turnover", "open_interest", "source", "published_at",
         "ingested_at", "revision")
# bars_v 多一欄 origin（1＝bars 表、0＝raw 映射），只用於去重排序，不輸出。


def normalize_ts(value: str | None, fallback: str | None = None) -> str | None:
    """把各種 ISO 時間字串正規化成契約格式 `YYYY-MM-DDTHH:MM:SS+08:00`；空值用 fallback。

    naive（無時區）視為 Asia/Taipei；帶 Z／其他 offset 的換算成台北時間；微秒去掉。
    解析失敗回 None（不產生看似合法但錯誤的時刻）。
    """
    v = value or fallback
    if not v:
        return None
    v = v.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    dt = dt.replace(tzinfo=TW) if dt.tzinfo is None else dt.astimezone(TW)
    return dt.replace(microsecond=0).isoformat()


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
               session: str | None = None, limit: int = 500, offset: int = 0) -> list[dict]:
    """查 bars_v（舊→新），回契約 bars dict 列表。同一鍵取最大 revision（bars 表優先），再 LIMIT/OFFSET。"""
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
    cols = ", ".join(_COLS)
    sql = (f"SELECT {cols} FROM ("
           f"  SELECT {cols}, ROW_NUMBER() OVER (PARTITION BY instrument_id, resolution, session, ts "
           f"         ORDER BY revision DESC, origin DESC) AS rn"
           f"  FROM bars_v WHERE {' AND '.join(conds)}) "
           "WHERE rn = 1 ORDER BY ts ASC LIMIT ? OFFSET ?")
    rows = conn.execute(sql, [*params, max(1, limit), max(0, offset)]).fetchall()
    return [_row_to_record(r) for r in rows]


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

