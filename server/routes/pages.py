"""Server-rendered 頁面：dashboard、訊號紀錄、資料查詢、觀察名單、排程狀態。"""

import json
import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from db.connection import get_connection
from integration.verification import (
    get_recent_verifications,
    get_verification_stats,
)
from server.routes.api import _QUERYABLE_TABLES, _query_by_date_range

logger = logging.getLogger(__name__)

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_DIRECTION_LABELS = {"bullish": "↑ 偏多", "bearish": "↓ 偏空", "neutral": "— 中性"}
_CLASS_LABELS = {"up": "漲", "down": "跌", "flat": "平"}

_TABLE_LABELS = {
    "raw_fx": "匯率（原始）",
    "raw_futures": "期貨（原始）",
    "raw_chip": "分點進出（原始）",
    "raw_institutional": "三大法人（原始）",
    "raw_index": "加權指數日K（原始）",
    "daily_metrics": "每日衍生指標",
    "daily_stock_metrics": "個股籌碼指標",
}

# raw_chip 內部標記列的顯示名稱
_BROKER_MARKER_LABELS = {
    "__FOREIGN__": "外資合計（T86，單位：張）",
    "__PRICE_ONLY__": "（收盤價紀錄）",
}

_TABLE_NOTES = {
    "raw_chip": (
        "此表混合三種列：真實分點買賣明細（CSV 匯入）、"
        "「外資合計」= 外資對該股的每日買賣超合計（張，來自證交所 T86），"
        "「收盤價紀錄」= 只為計算 MA20 而存的收盤價占位列。"
    ),
}


def _fill_stock_names(conn, rows: list[dict]) -> None:
    """用 stock_info 補齊 rows 中缺漏的 stock_name（就地修改）。"""
    if not rows or "stock_id" not in rows[0]:
        return
    info = dict(conn.execute(
        "SELECT stock_id, stock_name FROM stock_info"
    ).fetchall())
    for row in rows:
        if not row.get("stock_name"):
            row["stock_name"] = info.get(row["stock_id"], "")


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    """今日情報：最新訊號 + 指標卡片 + 命中率 + 最近驗證。"""
    db_path = request.app.state.db_path
    with get_connection(db_path) as conn:
        latest = conn.execute(
            "SELECT date, direction, confidence, fx_vote, futures_vote, "
            "       reasons, rule_version FROM signals "
            "ORDER BY date DESC LIMIT 1"
        ).fetchone()

        signal = None
        metrics = None
        stock_signals = []
        if latest:
            signal = {
                "date": latest[0],
                "direction": latest[1],
                "direction_label": _DIRECTION_LABELS.get(latest[1], latest[1]),
                "confidence": latest[2],
                "fx_vote": latest[3],
                "futures_vote": latest[4],
                "reasons": json.loads(latest[5]) if latest[5] else [],
                "rule_version": latest[6],
            }
            m = conn.execute(
                "SELECT fx_delta_twd, fx_direction, fx_asia_sync, "
                "       futures_spread_adjusted, futures_volume_ratio, "
                "       oi_net_foreign "
                "FROM daily_metrics WHERE date = ?",
                (signal["date"],),
            ).fetchone()
            if m:
                metrics = {
                    "fx_delta_twd": m[0], "fx_direction": m[1],
                    "fx_asia_sync": m[2], "spread_adjusted": m[3],
                    "volume_ratio": m[4], "oi_net": m[5],
                }
            stock_signals = conn.execute(
                "SELECT stock_id, broker_name, category, reasons "
                "FROM stock_signals WHERE date = ? ORDER BY stock_id",
                (signal["date"],),
            ).fetchall()

        stats = get_verification_stats(conn)
        recent = get_recent_verifications(conn, last_n=10)

    for r in recent:
        r["direction_label"] = _DIRECTION_LABELS.get(
            r["predicted_direction"], r["predicted_direction"])
        r["day_label"] = _CLASS_LABELS.get(r["day_change_class"], "?")

    return templates.TemplateResponse(request, "dashboard.html", {
        "active": "dashboard", "signal": signal, "metrics": metrics,
        "stock_signals": stock_signals, "stats": stats, "recent": recent,
    })


@router.get("/signals", response_class=HTMLResponse)
def signals_page(request: Request, date_from: str | None = None,
                 date_to: str | None = None):
    """訊號與驗證紀錄：逐日列表（訊號 join 驗證）。"""
    db_path = request.app.state.db_path
    with get_connection(db_path) as conn:
        sql = (
            "SELECT s.date, s.direction, s.confidence, s.reasons, s.rule_version, "
            "       v.day_change_pct, v.day_change_class, v.hit_day, "
            "       v.open_gap_pct, v.hit_open "
            "FROM signals s LEFT JOIN verifications v ON s.date = v.date"
        )
        conditions, params = [], []
        if date_from:
            conditions.append("s.date >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("s.date <= ?")
            params.append(date_to)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY s.date DESC LIMIT 200"
        rows = conn.execute(sql, params).fetchall()
        stats = get_verification_stats(conn)

    records = [
        {
            "date": r[0],
            "direction_label": _DIRECTION_LABELS.get(r[1], r[1]),
            "confidence": r[2],
            "reasons": json.loads(r[3]) if r[3] else [],
            "rule_version": r[4],
            "day_change_pct": r[5],
            "day_label": _CLASS_LABELS.get(r[6], None),
            "hit_day": r[7],
            "open_gap_pct": r[8],
            "hit_open": r[9],
        }
        for r in rows
    ]

    return templates.TemplateResponse(request, "signals.html", {
        "active": "signals", "records": records, "stats": stats,
        "date_from": date_from or "", "date_to": date_to or "",
    })


@router.get("/data", response_class=HTMLResponse)
def data_page(request: Request, table: str = "daily_metrics",
              date_from: str | None = None, date_to: str | None = None):
    """資料瀏覽：白名單內的表 + 日期區間，通用表格渲染。"""
    db_path = request.app.state.db_path
    if table not in _QUERYABLE_TABLES:
        table = "daily_metrics"

    with get_connection(db_path) as conn:
        rows = _query_by_date_range(conn, table, date_from, date_to)
        _fill_stock_names(conn, rows)

    # raw_chip 的內部標記改成人話
    for row in rows:
        if row.get("broker_name") in _BROKER_MARKER_LABELS:
            row["broker_name"] = _BROKER_MARKER_LABELS[row["broker_name"]]

    columns = list(rows[0].keys()) if rows else []
    return templates.TemplateResponse(request, "data.html", {
        "active": "data", "table": table,
        "tables": [(t, _TABLE_LABELS.get(t, t)) for t in sorted(_QUERYABLE_TABLES)],
        "table_label": _TABLE_LABELS.get(table, table),
        "table_note": _TABLE_NOTES.get(table),
        "columns": columns, "rows": rows,
        "date_from": date_from or "", "date_to": date_to or "",
    })


@router.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request):
    """觀察名單 + 各股最新的個股觀察訊號。台股大盤指數固定置頂。"""
    db_path = request.app.state.db_path
    with get_connection(db_path) as conn:
        index_rows = conn.execute(
            "SELECT date, open, high, low, close FROM raw_index "
            "ORDER BY date DESC LIMIT 11"
        ).fetchall()
        stocks = conn.execute(
            "SELECT stock_id, stock_name, added_date, reason "
            "FROM watchlist ORDER BY stock_id"
        ).fetchall()
        signal_rows = conn.execute(
            "SELECT date, stock_id, broker_name, category, reasons "
            "FROM stock_signals ORDER BY date DESC LIMIT 100"
        ).fetchall()

    signals_by_stock: dict[str, list] = {}
    for date, stock_id, broker, category, reasons in signal_rows:
        signals_by_stock.setdefault(stock_id, []).append(
            {"date": date, "broker_name": broker,
             "category": category, "reasons": reasons}
        )

    # 大盤指數：最近 10 日 OHLC + 對前一日的漲跌幅（多查 1 列算第一天的漲跌）
    index_days = []
    for i, (date, open_, high, low, close) in enumerate(index_rows[:10]):
        change_pct = None
        if i + 1 < len(index_rows) and index_rows[i + 1][4] and close:
            prev_close = index_rows[i + 1][4]
            change_pct = round((close - prev_close) / prev_close * 100, 2)
        index_days.append({
            "date": date, "open": open_, "high": high,
            "low": low, "close": close, "change_pct": change_pct,
        })

    return templates.TemplateResponse(request, "watchlist.html", {
        "active": "watchlist", "stocks": stocks,
        "signals_by_stock": signals_by_stock,
        "index_days": index_days,
    })


@router.post("/watchlist/add")
def watchlist_add_route(request: Request,
                        stock_id: str = Form(...),
                        stock_name: str = Form(...),
                        reason: str = Form("")):
    """從網頁新增觀察股，完成後導回觀察名單頁。"""
    from db.watchlist import watchlist_add

    watchlist_add(stock_id.strip(), stock_name.strip(), reason.strip(),
                  db_path=request.app.state.db_path)
    return RedirectResponse(url="/watchlist", status_code=303)


@router.post("/watchlist/remove")
def watchlist_remove_route(request: Request, stock_id: str = Form(...)):
    """從網頁移除觀察股（歷史資料保留），完成後導回觀察名單頁。"""
    from db.watchlist import watchlist_remove

    watchlist_remove(stock_id.strip(), db_path=request.app.state.db_path)
    return RedirectResponse(url="/watchlist", status_code=303)


@router.get("/scheduler", response_class=HTMLResponse)
def scheduler_page(request: Request, msg: str | None = None):
    """排程狀態：各 job 的排程時間、下次執行、上次結果，可手動執行與調整時間。"""
    from server.scheduler import get_jobs_info

    jobs = get_jobs_info(request.app.state.scheduler,
                         db_path=request.app.state.db_path)
    return templates.TemplateResponse(request, "scheduler.html", {
        "active": "scheduler", "jobs": jobs, "msg": msg,
        "scheduler_enabled": request.app.state.scheduler is not None,
    })


@router.post("/scheduler/run")
def scheduler_run_route(request: Request, job_id: str = Form(...)):
    """手動觸發一次 job（背景執行，不影響原排程），完成後導回排程頁。"""
    from server.scheduler import JOB_DEFS, run_job_now

    ok = run_job_now(request.app.state.scheduler, job_id,
                     db_path=request.app.state.db_path)
    if ok:
        msg = f"已觸發「{JOB_DEFS[job_id]['name']}」，在背景執行中——稍後重新整理查看上次執行結果"
    else:
        msg = "觸發失敗：排程器未啟用或 job 不存在"
    return RedirectResponse(url=f"/scheduler?msg={quote(msg)}", status_code=303)


@router.post("/scheduler/time")
def scheduler_time_route(request: Request, job_id: str = Form(...),
                         time_hhmm: str = Form(...)):
    """調整 job 的排程時間（存入 DB，重啟後沿用），完成後導回排程頁。"""
    from server.scheduler import JOB_DEFS, set_schedule_time

    ok = set_schedule_time(job_id, time_hhmm.strip(),
                           scheduler=request.app.state.scheduler,
                           db_path=request.app.state.db_path)
    if ok:
        msg = f"「{JOB_DEFS[job_id]['name']}」排程時間已改為 {time_hhmm.strip()}"
    else:
        msg = "更新失敗：時間格式需為 HH:MM（24 小時制）"
    return RedirectResponse(url=f"/scheduler?msg={quote(msg)}", status_code=303)
