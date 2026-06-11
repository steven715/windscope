"""觀察名單管理：list、add、remove。"""

import logging
import sqlite3
from datetime import date

from db.connection import get_connection

logger = logging.getLogger(__name__)


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
