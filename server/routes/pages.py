"""Server-rendered 頁面：dashboard、訊號紀錄、資料查詢、觀察名單、排程狀態。"""

import json
import logging
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates

from config import settings
from db.connection import get_connection
from integration.verification import (
    get_recent_verifications,
    get_verification_stats,
)
from server.routes.api import (
    _JOB_RUN_STATUSES,
    _QUERYABLE_TABLES,
    _count_by_date_range,
    _count_job_runs,
    _delete_job_runs,
    _query_by_date_range,
    _query_job_runs,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# 排程執行狀態英文鍵 → 中文（排程頁與排程紀錄頁共用的 Jinja filter）
_STATUS_ZH = {
    "completed": "完成", "partial": "部分完成", "skipped": "略過",
    "failed": "失敗", "error": "錯誤", "done": "完成", "unknown": "未知",
}


def _status_zh(status: str | None) -> str:
    """執行狀態中文化；'error: ...' 開頭一律歸『錯誤』，未知值原樣回傳。"""
    if not status:
        return "—"
    if status.startswith("error"):
        return "錯誤"
    return _STATUS_ZH.get(status, status)


templates.env.filters["status_zh"] = _status_zh

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
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
    "market_holidays": "休市日曆（國定假日）",
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
    # market_holidays
    "name": "假日名稱",
    "source": "來源",
    "fetched_at": "抓取時間",
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


@router.get("/more", response_class=HTMLResponse)
def more_page(request: Request):
    """手機『更多』選單：收納資料/排程子頁＋設定（漲跌配色、推播說明）。桌面用側邊欄取代。"""
    return templates.TemplateResponse(request, "more.html", {
        "active": "more", "rule_version": settings.SIGNAL_RULE_VERSION,
    })


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


@router.get("/chip-import", response_class=HTMLResponse)
def chip_import_page(request: Request, msg: str | None = None):
    """分點籌碼手動匯入頁：看著看盤軟體截圖，把關鍵分點的買賣超填進表單。"""
    db_path = request.app.state.db_path
    with get_connection(db_path) as conn:
        stocks = conn.execute(
            "SELECT stock_id, stock_name FROM watchlist ORDER BY stock_id"
        ).fetchall()
        brokers = conn.execute(
            "SELECT broker_name FROM broker_tags ORDER BY broker_name"
        ).fetchall()
    from datetime import date as _date
    return templates.TemplateResponse(request, "chip_import.html", {
        "active": "chip_import", "stocks": stocks,
        "brokers": [b[0] for b in brokers],
        "today": _date.today().isoformat(), "msg": msg,
    })


@router.post("/chip-import")
def chip_import_submit(
    request: Request,
    date: str = Form(...),
    stock_id: str = Form(...),
    close_price: str = Form(""),
    broker_name: list[str] = Form(default=[]),
    buy: list[str] = Form(default=[]),
    sell: list[str] = Form(default=[]),
):
    """寫入手動填的分點資料 → raw_chip，並重算當日籌碼指標與個股訊號。"""
    from datetime import datetime

    from collectors.chip import ChipCollector
    from integration.chip_metrics import compute_chip_metrics
    from integration.signal_engine import compute_stock_signals

    db_path = request.app.state.db_path
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT stock_name FROM watchlist WHERE stock_id = ?", (stock_id,)
        ).fetchone()
    stock_name = row[0] if row else stock_id

    items = []
    for name, b, s in zip(broker_name, buy, sell):
        name = (name or "").strip()
        if not name:
            continue
        bv = int(b) if (b or "").strip().lstrip("-").isdigit() else 0
        sv = int(s) if (s or "").strip().lstrip("-").isdigit() else 0
        if bv == 0 and sv == 0:
            continue
        items.append({"broker_name": name, "buy_volume": bv,
                      "sell_volume": sv, "net_volume": bv - sv})

    if not items:
        return RedirectResponse(
            url=f"/chip-import?msg={quote('沒有有效資料列')}", status_code=303)

    ChipCollector(db_path=db_path).save_broker_trading(date, stock_id, stock_name, items)

    cp = close_price.strip()
    with get_connection(db_path) as conn:
        if cp:
            try:
                conn.execute(
                    "INSERT INTO raw_chip (date, stock_id, stock_name, broker_name, "
                    "close_price, collected_at) VALUES (?, ?, ?, '__PRICE_ONLY__', ?, ?) "
                    "ON CONFLICT(date, stock_id, broker_name) DO UPDATE SET "
                    "close_price = excluded.close_price",
                    (date, stock_id, stock_name, float(cp),
                     datetime.now().isoformat()),
                )
                conn.commit()
            except ValueError:
                pass
        compute_chip_metrics(date, conn)
        compute_stock_signals(date, conn)

    msg = f"已匯入 {stock_name}（{stock_id}）{date} 的 {len(items)} 筆分點，已重算個股訊號"
    return RedirectResponse(url=f"/chip-import?msg={quote(msg)}", status_code=303)


@router.post("/chip-import/ocr")
async def chip_import_ocr(image: UploadFile = File(...)):
    """截圖 OCR（階段二）：辨識分點明細截圖，回 JSON 供前端預填表單。"""
    from integration.chip_ocr import extract_chip_from_image, is_ocr_enabled

    if not is_ocr_enabled():
        return JSONResponse({"enabled": False,
                             "error": "截圖辨識未啟用（需設定 ANTHROPIC_API_KEY）"})
    data = await image.read()
    rows = extract_chip_from_image(data, image.content_type or "image/png")
    if rows is None:
        return JSONResponse({"enabled": True, "error": "辨識失敗，請改用手動填寫"})
    return JSONResponse({"enabled": True, "rows": rows})


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
        explain = []
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
            from integration.explain import build_explain
            explain = build_explain(signal["date"], conn)

        stats = get_verification_stats(conn)
        recent = get_recent_verifications(conn, last_n=10)

    for r in recent:
        r["direction_label"] = _DIRECTION_LABELS.get(
            r["predicted_direction"], r["predicted_direction"])
        r["day_label"] = _CLASS_LABELS.get(r["day_change_class"], "?")

    return templates.TemplateResponse(request, "dashboard.html", {
        "active": "dashboard", "signal": signal, "metrics": metrics,
        "stock_signals": stock_signals, "stats": stats, "recent": recent,
        "explain": explain,
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
            "day_change_class": r[6],
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

        # 個股籌碼解讀：每檔一張表（鏡像大盤盤前解讀），as-of 取最近訊號日
        from datetime import datetime

        from integration.explain import build_stock_explain

        asof = (conn.execute("SELECT MAX(date) FROM signals").fetchone()[0]
                or datetime.now().strftime("%Y-%m-%d"))
        stock_explain = {
            s[0]: build_stock_explain(asof, s[0], conn) for s in stocks
        }

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
        "stock_explain": stock_explain,
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


@router.post("/scheduler/save")
async def scheduler_save_route(request: Request):
    """整頁批次儲存：逐 job 比對名稱／說明／時間／通知，只套用有變動者。

    表單欄位以 job_id 為後綴：name__<id>/desc__<id>/time__<id>/notify__<id>。
    只動到的欄位才呼叫對應 setter；回報實際更新數與錯誤後導回排程頁。
    """
    from server.scheduler import (
        get_jobs_info, set_job_desc, set_job_display_name, set_job_enabled,
        set_job_notify, set_schedule_time,
    )

    form = await request.form()
    scheduler = request.app.state.scheduler
    db_path = request.app.state.db_path
    jobs = get_jobs_info(scheduler, db_path=db_path)

    changed = 0
    errors: list[str] = []
    for j in jobs:
        jid = j["id"]

        # 名稱／說明：所有 job（含基礎設施）皆可改
        new_name = (form.get(f"name__{jid}") or "").strip()
        if new_name and new_name != j["name"]:
            if set_job_display_name(jid, new_name, scheduler=scheduler, db_path=db_path):
                changed += 1
            else:
                errors.append(f"{j['name']}：名稱需 1–60 字")

        raw_desc = (form.get(f"desc__{jid}") or "").strip()
        intended_desc = raw_desc or j["default_desc"]
        if intended_desc != j["desc"]:
            if set_job_desc(jid, raw_desc, scheduler=scheduler, db_path=db_path):
                changed += 1
            else:
                errors.append(f"{j['name']}：說明需 ≤300 字")

        # 啟用/停用：所有 job（含基礎設施）皆可切。hidden0+checkbox1 取最後值。
        evals = form.getlist(f"enabled__{jid}")
        ev = evals[-1] if evals else None
        if ev is not None and (ev == "1") != j["enabled"]:
            if set_job_enabled(jid, ev == "1", scheduler=scheduler, db_path=db_path):
                changed += 1
            else:
                errors.append(f"{j['name']}：啟用設定失敗")

        # 排程時間／通知：僅每日（editable）job；基礎設施 job 不渲染這些欄位、此處也跳過
        if not j.get("editable"):
            continue

        new_time = (form.get(f"time__{jid}") or "").strip()
        if new_time and new_time != j["time_hhmm"]:
            if set_schedule_time(jid, new_time, scheduler=scheduler, db_path=db_path):
                changed += 1
            else:
                errors.append(f"{j['name']}：時間格式需為 HH:MM")

        # toggle 開時瀏覽器同時送 hidden 的 "0" 與 checkbox 的 "1"，取最後一個為準；
        # 完全沒送該欄（部分提交）→ 視為未動，不變更。
        nvals = form.getlist(f"notify__{jid}")
        nv = nvals[-1] if nvals else None
        if nv is not None and (nv == "1") != j["notify_enabled"]:
            if set_job_notify(jid, nv == "1", scheduler=scheduler, db_path=db_path):
                changed += 1
            else:
                errors.append(f"{j['name']}：通知設定失敗")

    if errors:
        msg = f"已更新 {changed} 項，{len(errors)} 項失敗：" + "；".join(errors[:3])
    elif changed:
        msg = f"已儲存 {changed} 項變更"
    else:
        msg = "沒有變更"
    return RedirectResponse(url=f"/scheduler?msg={quote(msg)}", status_code=303)


@router.get("/scheduler/runs", response_class=HTMLResponse)
def scheduler_runs_page(request: Request, job_id: str | None = None,
                        status: str | None = None, date_from: str | None = None,
                        date_to: str | None = None, page: int = 1,
                        msg: str | None = None):
    """排程執行紀錄：持久化的每次 job 執行結果，可篩 job/狀態/日期，分頁。"""
    from server.scheduler import JOB_DEFS

    db_path = request.app.state.db_path
    # 篩選白名單化：非法 job_id / status / 日期格式一律視為不篩，避免無意義查詢
    if job_id and job_id not in JOB_DEFS:
        job_id = None
    if status and status not in _JOB_RUN_STATUSES:
        status = None
    if date_from and not _DATE_RE.match(date_from):
        date_from = None
    if date_to and not _DATE_RE.match(date_to):
        date_to = None

    page_size = settings.DATA_PAGE_SIZE
    with get_connection(db_path) as conn:
        total = _count_job_runs(conn, job_id, status, date_from, date_to)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * page_size
        rows = _query_job_runs(conn, job_id, status, date_from, date_to,
                               limit=page_size, offset=offset)

    # job 下拉選項：會被記錄的 job（排除 skip_logging 的盤中刷新）
    job_options = [(jid, d["name"]) for jid, d in JOB_DEFS.items()
                   if not d.get("skip_logging")]
    return templates.TemplateResponse(request, "job_runs.html", {
        "active": "scheduler_runs",
        "rows": rows, "job_options": job_options,
        "statuses": ["completed", "partial", "skipped", "failed", "error"],
        "sel_job": job_id or "", "sel_status": status or "",
        "date_from": date_from or "", "date_to": date_to or "",
        "page": page, "total_pages": total_pages, "total": total,
        "row_start": offset + 1 if rows else 0, "row_end": offset + len(rows),
        "msg": msg,
    })


@router.post("/scheduler/runs/delete")
def scheduler_runs_delete_route(
    request: Request, mode: str = Form(...), run_id: int | None = Form(None),
    job_id: str | None = Form(None), status: str | None = Form(None),
    date_from: str | None = Form(None), date_to: str | None = Form(None),
):
    """刪除執行紀錄：mode one（單筆）/ filter（依篩選）/ all（清空）。完成導回列表。

    篩選參數白名單化；filter 模式無任何條件時拒絕（避免誤刪全表，須改用 all）。
    """
    from server.scheduler import JOB_DEFS

    if job_id and job_id not in JOB_DEFS:
        job_id = None
    if status and status not in _JOB_RUN_STATUSES:
        status = None
    if date_from and not _DATE_RE.match(date_from):
        date_from = None
    if date_to and not _DATE_RE.match(date_to):
        date_to = None

    with get_connection(request.app.state.db_path) as conn:
        n = _delete_job_runs(conn, mode, run_id, job_id, status, date_from, date_to)

    msg = "刪除失敗：請先設定篩選條件，或改用「清空全部」" if n < 0 else f"已刪除 {n} 筆執行紀錄"

    # 保留原本的篩選回到同一視圖（單筆刪除時 job_id/status/日期僅用於導回）
    params = {"job_id": job_id, "status": status,
              "date_from": date_from, "date_to": date_to, "msg": msg}
    qs = "&".join(f"{k}={quote(str(v))}" for k, v in params.items() if v)
    return RedirectResponse(url=f"/scheduler/runs?{qs}", status_code=303)
