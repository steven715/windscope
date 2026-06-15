"""JSON 查詢 API：原始資料、衍生指標、訊號、驗證、統計。"""

import json
import logging
import sqlite3

from fastapi import APIRouter, HTTPException, Request

from db.connection import get_connection
from integration.verification import get_verification_stats

logger = logging.getLogger(__name__)

router = APIRouter()

# 可查詢的表白名單：避免任意 table name 注入
_QUERYABLE_TABLES = {
    "raw_fx", "raw_futures", "raw_chip", "raw_institutional", "raw_index",
    "daily_metrics", "daily_stock_metrics",
}

_MAX_ROWS = 500


def _query_by_date_range(conn: sqlite3.Connection, table: str,
                         date_from: str | None, date_to: str | None) -> list[dict]:
    """以日期區間查表，回傳 dict 列表（新到舊，上限 _MAX_ROWS）。"""
    sql = f"SELECT * FROM {table}"  # table 已通過白名單檢查
    conditions = []
    params: list[str] = []
    if date_from:
        conditions.append("date >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("date <= ?")
        params.append(date_to)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += f" ORDER BY date DESC LIMIT {_MAX_ROWS}"

    cursor = conn.execute(sql, params)
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


@router.get("/raw/{table}")
def query_raw(table: str, request: Request,
              date_from: str | None = None, date_to: str | None = None) -> dict:
    """查詢原始資料表（白名單內），可選日期區間。"""
    if table not in _QUERYABLE_TABLES:
        raise HTTPException(status_code=404, detail=f"unknown table: {table}")
    with get_connection(request.app.state.db_path) as conn:
        rows = _query_by_date_range(conn, table, date_from, date_to)
    return {"table": table, "count": len(rows), "rows": rows}


# signals/stock_signals/verifications 屬於判斷紀錄，各自有專屬 endpoint，
# 不放進 /raw 的白名單。


@router.get("/signals")
def query_signals(request: Request,
                  date_from: str | None = None, date_to: str | None = None) -> dict:
    """查詢市場訊號（reasons 已解開為 list）。"""
    with get_connection(request.app.state.db_path) as conn:
        rows = _query_by_date_range(conn, "signals", date_from, date_to)
    for row in rows:
        if row.get("reasons"):
            row["reasons"] = json.loads(row["reasons"])
    return {"count": len(rows), "rows": rows}


@router.get("/stock-signals")
def query_stock_signals(request: Request,
                        date_from: str | None = None,
                        date_to: str | None = None) -> dict:
    """查詢個股觀察訊號。"""
    with get_connection(request.app.state.db_path) as conn:
        rows = _query_by_date_range(conn, "stock_signals", date_from, date_to)
    return {"count": len(rows), "rows": rows}


@router.get("/verifications")
def query_verifications(request: Request,
                        date_from: str | None = None,
                        date_to: str | None = None) -> dict:
    """查詢驗證紀錄。"""
    with get_connection(request.app.state.db_path) as conn:
        rows = _query_by_date_range(conn, "verifications", date_from, date_to)
    return {"count": len(rows), "rows": rows}


@router.get("/stats")
def query_stats(request: Request, last_n: int = 20) -> dict:
    """近 N 日命中率統計。"""
    with get_connection(request.app.state.db_path) as conn:
        return get_verification_stats(conn, last_n=last_n)


@router.get("/live")
def query_live(request: Request) -> dict:
    """盤中即時驗證：當日訊號 + 即時加權指數雙基準比對（供 /live 頁輪詢）。"""
    from datetime import datetime

    from integration.live_verification import get_live_verification

    date = datetime.now().strftime("%Y-%m-%d")
    with get_connection(request.app.state.db_path) as conn:
        data = get_live_verification(date, conn)
    data["as_of"] = datetime.now().strftime("%H:%M:%S")
    return data
