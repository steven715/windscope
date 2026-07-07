"""觀察名單管理：唯一的讀寫入口（list、add、remove、collector 載入）。"""

import json
import logging
import sqlite3
from datetime import date
from pathlib import Path

from db.connection import get_connection

logger = logging.getLogger(__name__)

# 初始種子檔：只在 DB 的 watchlist 表不存在/為空時當 fallback（見 load_watchlist_seeded）。
# 執行期真相一律以 DB 表為準——增刪只走本模組的 watchlist_add/remove。
_SEED_PATH = Path(__file__).resolve().parent.parent / "config" / "watchlist.json"


def load_watchlist_seeded(
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Collector 專用名單載入：DB 的 watchlist 表為準（網頁/CLI 增刪即時生效），
    表不存在或為空時 fallback 到 config/watchlist.json（初始種子）。
    回傳 [{"stock_id", "stock_name"}, ...]。"""
    try:
        rows = watchlist_list(db_path=db_path, conn=conn)
        if rows:
            return [{"stock_id": r["stock_id"], "stock_name": r["stock_name"]}
                    for r in rows]
    except Exception as e:
        logger.warning("load_watchlist_seeded: DB read failed (%s), using JSON seed", e)

    with open(_SEED_PATH, encoding="utf-8") as f:
        return json.load(f)


def watchlist_list(db_path: str | None = None,
                   conn: sqlite3.Connection | None = None) -> list[dict]:
    """列出所有觀察名單。回傳 list of dict。"""
    if conn is not None:
        rows = conn.execute(
            "SELECT stock_id, stock_name, added_date, reason "
            "FROM watchlist ORDER BY stock_id"
        ).fetchall()
    else:
        with get_connection(db_path) as c:
            rows = c.execute(
                "SELECT stock_id, stock_name, added_date, reason "
                "FROM watchlist ORDER BY stock_id"
            ).fetchall()

    return [
        {
            "stock_id": r[0],
            "stock_name": r[1],
            "added_date": r[2],
            "reason": r[3],
        }
        for r in rows
    ]


def watchlist_add(stock_id: str, stock_name: str, reason: str,
                  db_path: str | None = None,
                  conn: sqlite3.Connection | None = None) -> bool:
    """新增到 watchlist 表。INSERT OR REPLACE。"""
    today = date.today().isoformat()

    def _do(c: sqlite3.Connection) -> None:
        from db.schema import upsert_stock_info

        c.execute(
            "INSERT OR REPLACE INTO watchlist "
            "(stock_id, stock_name, added_date, reason) "
            "VALUES (?, ?, ?, ?)",
            (stock_id, stock_name, today, reason),
        )
        upsert_stock_info(c, stock_id, stock_name)

    if conn is not None:
        _do(conn)
    else:
        with get_connection(db_path) as c:
            _do(c)

    logger.info("Watchlist: added %s %s", stock_id, stock_name)
    return True


def watchlist_remove(stock_id: str,
                     db_path: str | None = None,
                     conn: sqlite3.Connection | None = None) -> bool:
    """從 watchlist 表移除。"""
    def _do(c: sqlite3.Connection) -> int:
        cursor = c.execute(
            "DELETE FROM watchlist WHERE stock_id = ?",
            (stock_id,),
        )
        return cursor.rowcount

    if conn is not None:
        removed = _do(conn)
    else:
        with get_connection(db_path) as c:
            removed = _do(c)

    if removed > 0:
        logger.info("Watchlist: removed %s", stock_id)
        return True
    else:
        logger.warning("Watchlist: %s not found", stock_id)
        return False
