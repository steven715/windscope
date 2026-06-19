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


def _date_conditions(date_from: str | None, date_to: str | None):
    """組出 date 區間的 WHERE 片段與參數。"""
    conditions = []
    params: list[str] = []
    if date_from:
        conditions.append("date >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("date <= ?")
        params.append(date_to)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, params


def _query_by_date_range(conn: sqlite3.Connection, table: str,
                         date_from: str | None, date_to: str | None,
                         limit: int = _MAX_ROWS, offset: int = 0) -> list[dict]:
    """以日期區間查表，回傳 dict 列表（新到舊）。支援 limit/offset 分頁。"""
    where, params = _date_conditions(date_from, date_to)
    sql = f"SELECT * FROM {table}{where} ORDER BY date DESC LIMIT ? OFFSET ?"
    cursor = conn.execute(sql, [*params, limit, offset])
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _count_by_date_range(conn: sqlite3.Connection, table: str,
                         date_from: str | None, date_to: str | None) -> int:
    """回傳符合日期區間的總筆數（供分頁計算頁數）。"""
    where, params = _date_conditions(date_from, date_to)
    return conn.execute(f"SELECT COUNT(*) FROM {table}{where}", params).fetchone()[0]


# job_runs 執行紀錄查詢：篩選 job_id / status / run_date 區間，新到舊分頁。
# status 正規化後的有效值（_record_run 寫入時保證）。
_JOB_RUN_STATUSES = {"completed", "partial", "failed", "skipped", "error", "done"}


def _job_runs_where(job_id: str | None, status: str | None,
                    date_from: str | None, date_to: str | None):
    """組出 job_runs 篩選的 WHERE 片段與參數（全為 bound params，免注入）。"""
    conditions = []
    params: list[str] = []
    if job_id:
        conditions.append("job_id = ?")
        params.append(job_id)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if date_from:
        conditions.append("run_date >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("run_date <= ?")
        params.append(date_to)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, params


def _query_job_runs(conn: sqlite3.Connection, job_id: str | None,
                    status: str | None, date_from: str | None, date_to: str | None,
                    limit: int = _MAX_ROWS, offset: int = 0) -> list[dict]:
    """查 job_runs（新到舊），支援篩選與 limit/offset 分頁。"""
    where, params = _job_runs_where(job_id, status, date_from, date_to)
    sql = (f"SELECT * FROM job_runs{where} "
           "ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?")
    cursor = conn.execute(sql, [*params, limit, offset])
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _count_job_runs(conn: sqlite3.Connection, job_id: str | None,
                    status: str | None, date_from: str | None,
                    date_to: str | None) -> int:
    """符合篩選的 job_runs 總筆數（供分頁）。"""
    where, params = _job_runs_where(job_id, status, date_from, date_to)
    return conn.execute(f"SELECT COUNT(*) FROM job_runs{where}", params).fetchone()[0]


def _delete_job_runs(conn: sqlite3.Connection, mode: str,
                     run_id: int | None = None, job_id: str | None = None,
                     status: str | None = None, date_from: str | None = None,
                     date_to: str | None = None) -> int:
    """刪除 job_runs，回傳刪除筆數。拒絕無界限刪除時回 -1。

    mode：
      'one'    依 id 刪單筆（缺 id 回 -1）
      'filter' 依 job_id/status/run_date 區間刪——至少一個條件，全空則拒絕回 -1
      'all'    清空全部（呼叫端須明確選此 mode，UI 另加 confirm）
    """
    if mode == "one":
        if not run_id:
            return -1
        return conn.execute("DELETE FROM job_runs WHERE id = ?", (run_id,)).rowcount
    if mode == "all":
        return conn.execute("DELETE FROM job_runs").rowcount
    if mode == "filter":
        where, params = _job_runs_where(job_id, status, date_from, date_to)
        if not where:  # 無任何條件 → 拒絕誤刪全表
            return -1
        return conn.execute(f"DELETE FROM job_runs{where}", params).rowcount
    return -1


@router.get("/job-runs")
def query_job_runs(request: Request, job_id: str | None = None,
                   status: str | None = None, date_from: str | None = None,
                   date_to: str | None = None,
                   limit: int = 100, offset: int = 0) -> dict:
    """查詢排程執行紀錄，可篩 job_id / status / run_date 區間。limit/offset 分頁。"""
    limit = max(1, min(limit, _MAX_ROWS))
    offset = max(0, offset)
    with get_connection(request.app.state.db_path) as conn:
        total = _count_job_runs(conn, job_id, status, date_from, date_to)
        rows = _query_job_runs(conn, job_id, status, date_from, date_to, limit, offset)
    return {"total": total, "count": len(rows),
            "limit": limit, "offset": offset, "rows": rows}


@router.get("/raw/{table}")
def query_raw(table: str, request: Request,
              date_from: str | None = None, date_to: str | None = None,
              limit: int = 100, offset: int = 0) -> dict:
    """查詢原始資料表（白名單內），可選日期區間。limit/offset 分頁，回傳 total。"""
    if table not in _QUERYABLE_TABLES:
        raise HTTPException(status_code=404, detail=f"unknown table: {table}")
    limit = max(1, min(limit, _MAX_ROWS))
    offset = max(0, offset)
    with get_connection(request.app.state.db_path) as conn:
        total = _count_by_date_range(conn, table, date_from, date_to)
        rows = _query_by_date_range(conn, table, date_from, date_to, limit, offset)
    return {"table": table, "total": total, "count": len(rows),
            "limit": limit, "offset": offset, "rows": rows}


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
    """盤中即時驗證：當日訊號 + 即時加權指數雙基準比對。讀背景快取，瞬間返回。"""
    from datetime import datetime

    from integration.live_verification import get_live_verification

    date = datetime.now().strftime("%Y-%m-%d")
    with get_connection(request.app.state.db_path) as conn:
        return get_live_verification(date, conn)
