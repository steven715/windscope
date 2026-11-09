"""JSON 查詢 API：原始資料、衍生指標、訊號、驗證、統計。"""

import json
import logging
import re
import sqlite3

from fastapi import APIRouter, HTTPException, Request

from config import settings
from db.connection import get_connection
from integration.verification import get_verification_stats

logger = logging.getLogger(__name__)

router = APIRouter()

# 可查詢的表白名單：避免任意 table name 注入
_QUERYABLE_TABLES = {
    "raw_fx", "raw_futures", "raw_chip", "raw_institutional", "raw_index",
    "raw_stock_daily", "daily_metrics", "daily_stock_metrics",
    "stock_trend_signals", "market_holidays",
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
def query_stats(request: Request, last_n: int = 20, rule_version: str | None = None) -> dict:
    """某規則版本（預設現行版本）近 N 筆的命中率統計；不同版本不混算。"""
    with get_connection(request.app.state.db_path) as conn:
        return get_verification_stats(conn, rule_version or settings.SIGNAL_RULE_VERSION, last_n=last_n)


# ── 契約 I/O（docs/contracts v0.2）：JSON 信封 {schema_version, count, records} ─────────────


def _envelope(name: str, records: list[dict]) -> dict:
    from utils.contract_schema import schema_version

    return {"schema_version": schema_version(name), "count": len(records), "records": records}


@router.get("/instruments")
def query_instruments(request: Request, tradable_only: bool = False) -> dict:
    """商品主檔（契約 instruments）。"""
    from db.instruments import instruments_list

    with get_connection(request.app.state.db_path) as conn:
        return _envelope("instruments", instruments_list(conn=conn, tradable_only=tradable_only))


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$")
_EVENTS_MAX_BATCH = 10_000


@router.get("/calendar")
def query_calendar(request: Request, date_from: str, date_to: str) -> dict:
    """台股休市日曆（給 tradewind 算交易日、換月日）：[date_from, date_to] 之間的休市日＋休市表有資料的年份。
    休市表沒有某年資料時，那年只能排除週末（years_covered 不含它，呼叫端要記 WARNING）。不是契約表；唯讀。"""
    from utils.trading_calendar import holiday_years, holidays_between

    for v in (date_from, date_to):
        if not _DATE_RE.match(v):
            raise HTTPException(status_code=422, detail="date_from/date_to 要是 YYYY-MM-DD")
    if date_to < date_from:
        raise HTTPException(status_code=422, detail="date_to 早於 date_from")
    if int(date_to[:4]) - int(date_from[:4]) + 1 > 5:
        raise HTTPException(status_code=422, detail="一次最多 5 個年份")
    with get_connection(request.app.state.db_path) as conn:
        return {"date_from": date_from, "date_to": date_to, "holidays": holidays_between(conn, date_from, date_to),
                "years_covered": holiday_years(conn)}


@router.get("/bars")
def query_bars(request: Request, instrument_id: str, resolution: str = "1d",
               date_from: str | None = None, date_to: str | None = None,
               session: str | None = None, limit: int = 500, offset: int = 0) -> dict:
    """統一 K 線列（契約 bars，舊→新）：新 bars 表 ∪ 舊 raw 表的映射。去重後再分頁。"""
    from db.bars import bars_query

    limit = max(1, min(limit, 5000))
    with get_connection(request.app.state.db_path) as conn:
        rows = bars_query(conn, instrument_id, resolution, date_from, date_to, session, limit,
                          max(0, offset))
    return {**_envelope("bars", rows), "instrument_id": instrument_id, "resolution": resolution,
            "limit": limit, "offset": max(0, offset)}


@router.get("/targets")
def query_targets(request: Request, date: str | None = None, since: str | None = None,
                  strategy_id: str | None = None, locked_only: bool = False,
                  limit: int = 200) -> dict:
    """目標部位（契約 targets＝Insight＋Trade Plan）。date＝某交易日；since＝有變動（產生或鎖定）≥ 時刻。"""
    from db.targets import targets_list

    if since:
        since = since.replace(" ", "+")      # query string 未編碼的 '+' 會變成空格
        if not _TS_RE.match(since):
            raise HTTPException(status_code=422, detail="since 須為 YYYY-MM-DDTHH:MM:SS+08:00（+ 請編碼為 %2B）")
    with get_connection(request.app.state.db_path) as conn:
        rows = targets_list(conn, session_date=date, since=since, strategy_id=strategy_id,
                            locked_only=locked_only, limit=max(1, min(limit, _MAX_ROWS)))
    return _envelope("targets", rows)


@router.post("/targets/{target_id}/lock")
def lock_target(target_id: str, request: Request) -> dict:
    """盤前鎖定 Trade Plan（寫 locked_at；已鎖定不變）。不存在回 404。"""
    from db.targets import target_lock
    from utils.contract_schema import ContractError

    try:
        with get_connection(request.app.state.db_path) as conn:
            rec = target_lock(conn, target_id)
    except ContractError as e:
        raise HTTPException(status_code=422, detail=f"target 不符契約，無法鎖定：{e.errors[0]}")
    if rec is None:
        raise HTTPException(status_code=404, detail=f"unknown target: {target_id}")
    return {"target_id": target_id, "locked_at": rec["locked_at"]}


@router.get("/events")
def query_events(request: Request, date: str | None = None, run_mode: str | None = None,
                 target_id: str | None = None, limit: int = 500) -> dict:
    """tradewind 回流的事件（契約 events）。"""
    from db.events import events_list

    with get_connection(request.app.state.db_path) as conn:
        rows = events_list(conn, date, run_mode, target_id, max(1, min(limit, 5000)))
    return _envelope("events", rows)


@router.post("/events")
def ingest_events(request: Request, body: dict) -> dict:
    """接收 events／account_snapshots（冪等 by id；不符契約的筆數回 invalid）。

    body：{"events": [...], "account_snapshots": [...]}；任一鍵可省略。
    """
    from db.events import events_ingest, snapshots_ingest

    ev = body.get("events") or []
    sn = body.get("account_snapshots") or []
    if not isinstance(ev, list) or not isinstance(sn, list):
        raise HTTPException(status_code=422, detail="events / account_snapshots 必須是陣列")
    if len(ev) + len(sn) > _EVENTS_MAX_BATCH:
        raise HTTPException(status_code=413, detail=f"一次最多 {_EVENTS_MAX_BATCH} 筆")
    with get_connection(request.app.state.db_path) as conn:
        r1 = events_ingest(conn, ev)
        r2 = snapshots_ingest(conn, sn)
    return {"events": r1, "account_snapshots": r2}


@router.get("/account-snapshots")
def query_snapshots(request: Request, date: str | None = None,
                    run_mode: str | None = None, limit: int = 200) -> dict:
    """帳戶快照（契約 account_snapshots）。"""
    from db.events import snapshots_list

    with get_connection(request.app.state.db_path) as conn:
        rows = snapshots_list(conn, date, run_mode, max(1, min(limit, _MAX_ROWS)))
    return _envelope("account_snapshots", rows)


_RUN_MODES = ("backtest", "replay", "paper", "shioaji_sim", "shioaji_live")


@router.get("/trade-stats")
def query_trade_stats(request: Request, run_mode: str | None = None, strategy_id: str | None = None,
                      account_id: str | None = None, date_from: str | None = None, date_to: str | None = None,
                      trades_limit: int = 500) -> dict:
    """Paper／券商模擬的交易成績（依成交打分，integration/trade_stats）：分組（run_mode×策略×規則版本×Playbook，永不混算）的 R 統計、
    出場原因、override、Insight 對錯、帳戶權益曲線、交易明細。純讀取。"""
    from integration.trade_stats import build_trade_stats

    for v in (date_from, date_to):
        if v and not _DATE_RE.match(v):
            raise HTTPException(status_code=422, detail="date_from/date_to 要是 YYYY-MM-DD")
    if run_mode and run_mode not in _RUN_MODES:
        raise HTTPException(status_code=422, detail=f"run_mode 要是 {_RUN_MODES} 之一")
    with get_connection(request.app.state.db_path) as conn:
        return build_trade_stats(conn, run_mode or None, strategy_id or None, account_id or None, date_from or None,
                                 date_to or None, max(0, min(trades_limit, 5000)))


@router.get("/call-stats")
def query_call_stats(request: Request, rule_version: str | None = None,
                     date_from: str | None = None, date_to: str | None = None,
                     cost_bps: float | None = None) -> dict:
    """盤前判斷有效性統計（基準線/信心校準/單因子/判斷回測＋結論），與 /stats 頁同一份結果。

    結構化戰績，供外部工具或 LLM 讀取；比例以 0–1 表示、報酬為小數。
    """
    from integration.call_stats import build_call_stats

    with get_connection(request.app.state.db_path) as conn:
        return build_call_stats(conn, rule_version or None, date_from or None,
                                date_to or None, cost_bps)
@router.get("/live")
def query_live(request: Request) -> dict:
    """盤中即時驗證：當日訊號 + 即時加權指數雙基準比對。讀背景快取，瞬間返回。"""
    from datetime import datetime

    from integration.live_verification import get_live_verification

    date = datetime.now().strftime("%Y-%m-%d")
    with get_connection(request.app.state.db_path) as conn:
        return get_live_verification(date, conn)


@router.get("/stock/{stock_id}/ohlc")
def query_stock_ohlc(stock_id: str, request: Request, limit: int = 300,
                     adjusted: bool = True) -> dict:
    """個股日K OHLCV（舊→新，最近 limit 根）。預設為還原價（與頁面K線圖、趨勢訊號同一序列）。

    adjusted=false 回原始成交價；volume 一律為原始股數（不隨還原換算）；排除 OHLC 缺值列。
    """
    from integration.price_adjust import adjust_note, load_adjusted_bars, load_bars

    limit = max(1, min(limit, _MAX_ROWS))
    with get_connection(request.app.state.db_path) as conn:
        if adjusted:
            bars, events = load_adjusted_bars(stock_id, conn)
        else:
            bars, events = load_bars(stock_id, conn), []
    bars = [{"time": b["date"], "open": b["open"], "high": b["high"], "low": b["low"],
             "close": b["close"], "volume": b["volume"]} for b in bars[-limit:]]
    return {"stock_id": stock_id, "count": len(bars), "bars": bars,
            "adjusted": adjusted, "adjust_note": adjust_note(events)}


@router.get("/stock/{stock_id}/trend")
def query_stock_trend(stock_id: str, request: Request) -> dict:
    """個股最新趨勢訊號（reasons 已解開為 list）。無訊號回 found=false。"""
    from dataclasses import asdict

    from integration.trend_signal import load_stock_trend

    with get_connection(request.app.state.db_path) as conn:
        trend = load_stock_trend(stock_id, conn)
    if trend is None:
        return {"stock_id": stock_id, "found": False}
    return {"stock_id": stock_id, "found": True, **asdict(trend)}
