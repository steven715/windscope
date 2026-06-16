"""Server-rendered 頁面：dashboard、訊號紀錄、資料查詢、觀察名單、排程狀態。"""

import json
import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from config import settings
from db.connection import get_connection
from integration.verification import (
    get_recent_verifications,
    get_verification_stats,
)
from server.routes.api import (
    _QUERYABLE_TABLES,
    _count_by_date_range,
    _query_by_date_range,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_DIRECTION_LABELS = {"bullish": "↑ 偏多", "bearish": "↓ 偏空", "neutral": "— 中性"}
_CLASS_LABELS = {"up": "漲", "down": "跌", "flat": "平"}
# 亞幣方向中文 + 卡片配色（升值對台股偏多→綠/up；貶值→紅/down）
_FX_DIR_ZH = {"bullish": ("升", "up"), "bearish": ("貶", "down"),
              "neutral": ("平", "flat")}
# 分點類型中文標籤
_BROKER_TYPE_LABELS = {
    "swing": "波段／主力", "day_trade": "隔日沖", "hedge": "避險（外資券商）",
}

_TABLE_LABELS = {
    "raw_fx": "匯率（原始）",
    "raw_futures": "期貨（原始）",
    "raw_chip": "分點進出（原始）",
    "raw_institutional": "三大法人（原始）",
    "raw_index": "加權指數日K（原始）",
    "daily_metrics": "每日衍生指標",
    "daily_stock_metrics": "個股籌碼指標",
}

# 資料瀏覽頁的欄位中文標籤（缺漏的欄位 fallback 原名）。
# 各表共用同一個 dict：同名欄位語意一致（date、stock_id...）。
_COLUMN_LABELS = {
    # 共用
    "date": "日期",
    "stock_id": "代號",
    "stock_name": "名稱",
    "broker_name": "分點券商",
    "collected_at": "收集時間",
    "updated_at": "更新時間",
    # raw_fx
    "currency_pair": "幣別",
    "close_16": "16:00 收盤",
    "quote_0845": "08:45 報價",
    "ny_close": "紐約盤收盤",
    # raw_futures
    "night_close": "夜盤收盤",
    "night_volume": "夜盤成交量",
    "spot_close": "加權指數收盤",
    "oi_net_foreign": "外資未平倉淨額（口）",
    "ex_dividend_points": "除息點數",
    "ftse_tw_close": "富台指收盤",
    "sp500_close": "S&P 500 收盤",
    # raw_chip
    "buy_volume": "買進（張）",
    "sell_volume": "賣出（張）",
    "net_volume": "買賣超（張）",
    "close_price": "收盤價",
    # raw_institutional（單位：元）
    "foreign_buy": "外資買進（元）",
    "foreign_sell": "外資賣出（元）",
    "foreign_net": "外資買賣超（元）",
    "trust_buy": "投信買進（元）",
    "trust_sell": "投信賣出（元）",
    "trust_net": "投信買賣超（元）",
    "dealer_buy": "自營商買進（元）",
    "dealer_sell": "自營商賣出（元）",
    "dealer_net": "自營商買賣超（元）",
    "total_net": "合計買賣超（元）",
    # raw_index
    "open": "開盤",
    "high": "最高",
    "low": "最低",
    "close": "收盤",
    # daily_metrics
    "fx_delta_twd": "台幣升貶（Δ）",
    "fx_delta_cny": "人民幣升貶（Δ）",
    "fx_delta_krw": "韓元升貶（Δ）",
    "fx_direction": "匯率方向",
    "fx_asia_sync": "亞幣同步",
    "fx_asia_detail": "亞幣明細",
    "futures_spread": "期現價差",
    "futures_spread_adjusted": "調整後價差",
    "futures_volume_ratio": "夜盤量比",
    "oi_delta": "未平倉增減（口）",
    # daily_stock_metrics
    "net_amount": "買賣超金額（元）",
    "consecutive_days": "連續天數",
    "price_vs_ma20": "價格 vs MA20（%）",
    "price_zone": "價位區間",
    "both_sides_flag": "兩面手法",
    "broker_type": "分點屬性",
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


_MANIFEST = json.dumps({
    "name": "開盤前情報",
    "short_name": "開盤前情報",
    "description": "台股開盤前情報系統",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0f1419",
    "theme_color": "#0f1419",
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192",
         "type": "image/png", "purpose": "any maskable"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512",
         "type": "image/png", "purpose": "any maskable"},
    ],
}, ensure_ascii=False)

# Service worker：network-first，/api/ 一律走網路（即時資料不快取），離線回退快取殼。
_SERVICE_WORKER = """\
const CACHE = "premarket-v1";
const SHELL = ["/", "/live"];
self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }).catch(function () {}));
  self.skipWaiting();
});
self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; })
                           .map(function (k) { return caches.delete(k); }));
  }));
  self.clients.claim();
});
self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  e.respondWith(
    fetch(req).then(function (res) {
      if (res.ok && new URL(req.url).pathname.indexOf("/api/") !== 0) {
        var copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put(req, copy); });
      }
      return res;
    }).catch(function () { return caches.match(req); })
  );
});
"""


@router.get("/manifest.webmanifest")
def manifest():
    """PWA manifest（手機可加到主畫面、全螢幕開啟）。"""
    return Response(content=_MANIFEST, media_type="application/manifest+json")


@router.get("/sw.js")
def service_worker():
    """Service worker（從根路徑提供，scope 涵蓋整個 app）。"""
    return Response(content=_SERVICE_WORKER, media_type="application/javascript")


@router.get("/live", response_class=HTMLResponse)
def live_page(request: Request):
    """盤中即時驗證：用即時加權指數對早上訊號做雙基準比對，JS 輪詢自動更新。"""
    from datetime import datetime

    from integration.live_verification import get_live_verification

    date = datetime.now().strftime("%Y-%m-%d")
    with get_connection(request.app.state.db_path) as conn:
        d = get_live_verification(date, conn)
    if d.get("has_signal"):
        d["direction_label"] = _DIRECTION_LABELS.get(
            d["predicted_direction"], d["predicted_direction"])
    return templates.TemplateResponse(request, "live.html", {
        "active": "live", "d": d, "class_labels": _CLASS_LABELS,
    })


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
                "       oi_net_foreign, fx_delta_cny, fx_delta_krw, "
                "       fx_asia_detail, oi_delta "
                "FROM daily_metrics WHERE date = ?",
                (signal["date"],),
            ).fetchone()
            if m:
                detail = json.loads(m[8]) if m[8] else {}
                fx_pairs = []
                for label, delta in (("TWD", m[0]), ("CNY", m[6]), ("KRW", m[7])):
                    zh, css = _FX_DIR_ZH.get(detail.get(label), ("—", "flat"))
                    fx_pairs.append({"label": label, "delta": delta,
                                     "zh": zh, "css": css})
                metrics = {
                    "fx_delta_twd": m[0], "fx_direction": m[1],
                    "fx_asia_sync": m[2], "spread_adjusted": m[3],
                    "volume_ratio": m[4], "oi_net": m[5],
                    "fx_pairs": fx_pairs, "oi_delta": m[9],
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
              date_from: str | None = None, date_to: str | None = None,
              page: int = 1):
    """資料瀏覽：白名單內的表 + 日期區間，server-side 分頁（每頁一次只拉一頁）。"""
    db_path = request.app.state.db_path
    if table not in _QUERYABLE_TABLES:
        table = "daily_metrics"

    page_size = settings.DATA_PAGE_SIZE
    with get_connection(db_path) as conn:
        total = _count_by_date_range(conn, table, date_from, date_to)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * page_size
        rows = _query_by_date_range(conn, table, date_from, date_to,
                                    limit=page_size, offset=offset)
        _fill_stock_names(conn, rows)

    # raw_chip 的內部標記改成人話
    for row in rows:
        if row.get("broker_name") in _BROKER_MARKER_LABELS:
            row["broker_name"] = _BROKER_MARKER_LABELS[row["broker_name"]]

    # (欄位原名, 中文標籤)；模板以原名取值、顯示中文標籤
    columns = (
        [(c, _COLUMN_LABELS.get(c, c)) for c in rows[0].keys()] if rows else []
    )
    return templates.TemplateResponse(request, "data.html", {
        "active": "data", "table": table,
        "tables": [(t, _TABLE_LABELS.get(t, t)) for t in sorted(_QUERYABLE_TABLES)],
        "table_label": _TABLE_LABELS.get(table, table),
        "table_note": _TABLE_NOTES.get(table),
        "columns": columns, "rows": rows,
        "date_from": date_from or "", "date_to": date_to or "",
        "page": page, "total_pages": total_pages, "total": total,
        "row_start": offset + 1 if rows else 0, "row_end": offset + len(rows),
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
        broker_rows = conn.execute(
            "SELECT broker_name, broker_type, notes FROM broker_tags "
            "ORDER BY broker_type, broker_name"
        ).fetchall()

    brokers = [
        {"name": b[0], "type": b[1],
         "type_label": _BROKER_TYPE_LABELS.get(b[1], b[1]), "notes": b[2]}
        for b in broker_rows
    ]

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
        "index_days": index_days, "brokers": brokers,
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
