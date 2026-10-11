"""觀察名單管理：watchlist 表唯一的讀寫入口（list、get、add、remove、seed）。"""

import json
import logging
import sqlite3
from datetime import date
from pathlib import Path

from db.connection import get_connection

logger = logging.getLogger(__name__)

# 初始種子檔：只由 watchlist_seed（init-db）匯入一次。執行期真相一律以 DB 表為準，
# 表空就是觀察名單為空——不退回種子檔。
_SEED_PATH = Path(__file__).resolve().parent.parent / "config" / "watchlist.json"


def watchlist_seed(conn: sqlite3.Connection, json_path: str | None = None) -> int:
    """從種子檔匯入觀察名單（INSERT OR REPLACE，並寫 stock_info）。回傳匯入筆數。"""
    from db.schema import upsert_stock_info

    with open(json_path or _SEED_PATH, encoding="utf-8") as f:
        stocks = json.load(f)
    for stock in stocks:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist (stock_id, stock_name, added_date, reason) "
            "VALUES (?, ?, ?, ?)",
            (stock["stock_id"], stock["stock_name"], stock["added_date"], stock["reason"]),
        )
        upsert_stock_info(conn, stock["stock_id"], stock["stock_name"])
    conn.commit()
    logger.info("Imported %d watchlist entries", len(stocks))
    return len(stocks)


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


def watchlist_get(stock_id: str, db_path: str | None = None,
                  conn: sqlite3.Connection | None = None) -> dict | None:
    """取單檔觀察名單（同 watchlist_list 的 dict 形狀）；不在名單回 None。"""
    sql = ("SELECT stock_id, stock_name, added_date, reason "
           "FROM watchlist WHERE stock_id = ?")
    if conn is not None:
        r = conn.execute(sql, (stock_id,)).fetchone()
    else:
        with get_connection(db_path) as c:
            r = c.execute(sql, (stock_id,)).fetchone()
    if r is None:
        return None
    return {"stock_id": r[0], "stock_name": r[1], "added_date": r[2], "reason": r[3]}


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
