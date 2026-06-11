"""Server-rendered 頁面：dashboard、訊號紀錄、資料查詢、觀察名單、排程狀態。"""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
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

    columns = list(rows[0].keys()) if rows else []
    return templates.TemplateResponse(request, "data.html", {
        "active": "data", "table": table,
        "tables": sorted(_QUERYABLE_TABLES),
        "columns": columns, "rows": rows,
        "date_from": date_from or "", "date_to": date_to or "",
    })


@router.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request):
    """觀察名單 + 各股最新的個股觀察訊號。"""
    db_path = request.app.state.db_path
    with get_connection(db_path) as conn:
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

    return templates.TemplateResponse(request, "watchlist.html", {
        "active": "watchlist", "stocks": stocks,
        "signals_by_stock": signals_by_stock,
    })


@router.get("/scheduler", response_class=HTMLResponse)
def scheduler_page(request: Request):
    """排程狀態：各 job 的下次執行時間。"""
    from server.scheduler import get_jobs_info

    jobs = get_jobs_info(request.app.state.scheduler)
    return templates.TemplateResponse(request, "scheduler.html", {
        "active": "scheduler", "jobs": jobs,
        "scheduler_enabled": request.app.state.scheduler is not None,
    })
