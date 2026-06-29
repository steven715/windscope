"""盤前解讀層：把每個指標的「原數據 + 判讀 + 為什麼」組成一張表，供學習用面板顯示。
純函數，只讀 raw_* 與 daily_metrics，不寫 DB。

分層原則（見使用者要求）：
- 原數據 = 事實；判讀 = 系統立場（綁 settings 門檻 + rule_version，本檔內定）。
- 「為什麼」= 詮釋/評註，**外部化在 config/explain_notes.json**、標來源、可放自己的觀點，
  與判斷邏輯完全分離。改觀點只改 JSON，不動程式、不影響任何訊號。
"""

import json
import logging
import sqlite3
from pathlib import Path

from config import settings
from utils.trading_calendar import get_previous_trading_day

logger = logging.getLogger(__name__)

_ASIA_ZH = {"bullish": "升", "bearish": "貶", "neutral": "平", None: "—"}
_NOTES_PATH = Path(__file__).resolve().parent.parent / "config" / "explain_notes.json"


def _load_notes() -> dict:
    """讀取『為什麼』觀點檔。每次呼叫重讀，方便編輯後不需重啟即生效。"""
    try:
        return json.loads(_NOTES_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("explain_notes 讀取失敗: %s", e)
        return {}


def _note(notes: dict, dim: str) -> tuple[str, str]:
    """回傳 (觀點文字, 來源)。my_note 非空時優先、來源標『你』。"""
    e = notes.get(dim, {})
    my = (e.get("my_note") or "").strip()
    if my:
        return my, "你"
    return e.get("note", ""), e.get("source", "")


def _row(dim, raw, verdict, css, notes):
    why, why_source = _note(notes, dim)
    return {"dim": dim, "raw": raw, "verdict": verdict, "css": css,
            "why": why, "why_source": why_source}


def build_explain(date: str, conn: sqlite3.Connection) -> list[dict]:
    """組出 date 的盤前解讀表。回傳每維度 {dim, raw, verdict, css, why} 的 list。"""
    m = conn.execute(
        "SELECT fx_delta_twd, fx_direction, fx_asia_detail, fx_asia_sync, "
        "       futures_spread, futures_spread_adjusted, futures_volume_ratio, "
        "       oi_net_foreign, oi_delta "
        "FROM daily_metrics WHERE date = ?",
        (date,),
    ).fetchone()
    if m is None:
        return []
    (fx_delta_twd, fx_direction, asia_json, asia_sync,
     spread, spread_adj, vol_ratio, oi_net, oi_delta) = m

    notes = _load_notes()
    prev = get_previous_trading_day(date, conn)
    rows = []

    # 1. 匯率（台幣）：離岸 USDTWD=X 晨對晨（在岸 08:45 牌價開盤前未更新、失真）
    from integration.fx_metrics import offshore_twd_morning
    twd_now = offshore_twd_morning(conn, date)
    twd_prev = offshore_twd_morning(conn, prev) if prev else None
    if twd_now is not None and twd_prev is not None:
        raw = f"今晨離岸 {twd_now:.3f} vs 昨晨 {twd_prev:.3f} → Δ{fx_delta_twd:+.4f}"
    else:
        raw = f"Δ{fx_delta_twd:+.4f}" if fx_delta_twd is not None else "資料不足"
    verdict, css = {
        "bullish": ("升值（外資錢進來，權值股有機會）", "up"),
        "bearish": ("貶值（外資匯出，今天別衝）", "down"),
        "neutral": ("平盤（回到個股籌碼判斷）", "flat"),
    }.get(fx_direction, ("資料不足", "flat"))
    rows.append(_row("匯率（台幣）", raw, verdict, css, notes))

    # 1b. 匯率節奏（跳空 + 緩步/急拉，用盤前 5 分序列）
    rows.append(_fx_rhythm(date, conn, fx_delta_twd, notes))

    # 2. 亞幣同步
    detail = json.loads(asia_json) if asia_json else {}
    raw = (f"台幣{_ASIA_ZH.get(detail.get('TWD'))}／"
           f"人民幣{_ASIA_ZH.get(detail.get('CNY'))}／"
           f"韓元{_ASIA_ZH.get(detail.get('KRW'))}")
    if detail.get("TWD") == "bearish" and detail.get("CNY") == "bearish" and detail.get("KRW") == "bullish":
        verdict, css = "警示：台幣人民幣貶但韓元升，外資恐賣台買韓", "down"
    elif asia_sync == 1:
        verdict, css = "三幣同步（國際資金流入亞洲，大盤安全）", "up"
    elif asia_sync == 0:
        verdict, css = "分歧（只有部分亞幣動，買盤恐不持續）", "flat"
    else:
        verdict, css = "資料不足（亞幣收盤基準累積中）", "flat"
    rows.append(_row("亞幣同步", raw, verdict, css, notes))

    # 3. 期貨價差
    night = _scalar(conn, "SELECT night_close FROM raw_futures WHERE date=?", (date,))
    prev_spot = _scalar(conn, "SELECT spot_close FROM raw_futures WHERE date=?", (prev,)) if prev else None
    if night is not None and prev_spot is not None:
        raw = f"夜盤{night:.0f} − 前日現貨{prev_spot:.0f} = {spread:+.0f}"
        if spread_adj is not None and spread_adj != spread:
            raw += f"（扣除息後 {spread_adj:+.0f}）"
    else:
        raw = f"{spread_adj:+.0f}" if spread_adj is not None else "資料不足"
    if spread_adj is None:
        verdict, css = "資料不足", "flat"
    elif spread_adj >= settings.FUTURES_SPREAD_THRESHOLD:
        verdict, css = "正價差>100（開高機率高）", "up"
    elif spread_adj <= -settings.FUTURES_SPREAD_THRESHOLD:
        verdict, css = "逆價差>100（開低機率高）", "down"
    else:
        verdict, css = "價差<100（中性）", "flat"
    rows.append(_row("期貨價差", raw, verdict, css, notes))

    # 4. 夜盤量比（原數據顯示今夜實際口數 + 近 N 日均量，比單一倍數有感）
    if vol_ratio is not None:
        n = settings.FUTURES_VOLUME_LOOKBACK
        night_vol = _scalar(conn, "SELECT night_volume FROM raw_futures WHERE date=?", (date,))
        recent = conn.execute(
            "SELECT night_volume FROM raw_futures WHERE date < ? AND night_volume IS NOT NULL "
            "ORDER BY date DESC LIMIT ?", (date, n)).fetchall()
        avg = sum(r[0] for r in recent) / len(recent) if recent else None
        if night_vol is not None and avg:
            raw = f"今夜 {night_vol:,} 口 ｜ 近{n}日均 {avg:,.0f} 口 → {vol_ratio:.2f}倍"
        else:
            raw = f"{vol_ratio:.2f}x（vs 近{n}日均量）"
        if vol_ratio >= settings.VOLUME_RATIO_HIGH:
            verdict, css = "爆量（大戶提前佈局，順著做）", "up"
        elif vol_ratio <= settings.VOLUME_RATIO_LOW:
            verdict, css = "量縮（市場在等，開盤波動大）", "flat"
        else:
            verdict, css = "量能正常", "flat"
    else:
        raw, verdict, css = "資料不足", "資料不足", "flat"
    rows.append(_row("夜盤量比", raw, verdict, css, notes))

    # 5. 外資未平倉
    if oi_net is not None:
        raw = f"{oi_net:+,} 口"
        if oi_delta is not None:
            raw += f"（較前日 {oi_delta:+,}）"
        if oi_net <= settings.OI_BEARISH_THRESHOLD:
            verdict, css = "外資淨空（方向偏空）", "down"
        elif oi_net >= settings.OI_BULLISH_THRESHOLD:
            verdict, css = "外資淨多（方向偏多）", "up"
        else:
            verdict, css = "部位中性", "flat"
    else:
        raw, verdict, css = "尚未收集（收盤後更新）", "—", "flat"
    rows.append(_row("外資未平倉", raw, verdict, css, notes))

    # 6. 美股對照（看異常）
    sp_now = _scalar(conn, "SELECT sp500_close FROM raw_futures WHERE date=?", (date,))
    sp_prev = _scalar(conn, "SELECT sp500_close FROM raw_futures WHERE date=?", (prev,)) if prev else None
    if sp_now is not None and sp_prev and prev_spot and night is not None:
        sp_chg = (sp_now - sp_prev) / sp_prev * 100
        night_chg = (night - prev_spot) / prev_spot * 100
        raw = f"S&P {sp_now:.0f}（{sp_chg:+.2f}%）vs 台指夜盤 {night_chg:+.2f}%"
        if abs(sp_chg) >= 1.0 and abs(night_chg) < 0.3:
            verdict, css = "異常：美股動但夜盤沒跟，台股相對弱→不追高", "down"
        elif (sp_chg > 0) == (night_chg > 0):
            verdict, css = "與美股同步", "up" if sp_chg > 0 else "down"
        else:
            verdict, css = "與美股反向（留意）", "flat"
    else:
        raw, verdict, css = "資料累積中（需美股與夜盤同時有資料）", "—", "flat"
    rows.append(_row("美股對照（看異常）", raw, verdict, css, notes))

    # 7. 日圓避險情緒（獨立的反向風險溫度計，不進亞幣同步/訊號）
    rows.append(_jpy_risk_gauge(date, conn, prev, notes))

    return rows


def _jpy_risk_gauge(date: str, conn: sqlite3.Connection, prev_day: str | None,
                    notes: dict) -> dict:
    """日圓避險溫度計：USD/JPY 今日報價 vs 前一交易日收盤。日圓急升=risk-off=偏空警示。"""
    now = _scalar(conn, "SELECT quote_0845 FROM raw_fx WHERE date=? AND currency_pair='USD/JPY'", (date,))
    prev = _scalar(conn, "SELECT close_16 FROM raw_fx WHERE date=? AND currency_pair='USD/JPY'", (prev_day,)) if prev_day else None

    if now is None or prev is None:
        return _row("日圓避險情緒", "資料累積中（需 USD/JPY 報價與前日收盤）", "—", "flat", notes)

    delta = round(now - prev, 3)  # USD/JPY 下跌(負)=日圓升值
    raw = f"USD/JPY {now:.2f}（前日 {prev:.2f}）Δ{delta:+.2f}"
    if delta <= -settings.JPY_RISKOFF_DELTA:
        verdict, css = "日圓急升 → 避險(risk-off)，對股市偏空警示", "down"
    elif delta >= settings.JPY_RISKOFF_DELTA:
        verdict, css = "日圓走弱 → 風險偏好(risk-on)，市場較平靜", "up"
    else:
        verdict, css = "日圓平穩 → 無明顯避險訊號", "flat"
    return _row("日圓避險情緒", raw, verdict, css, notes)


def _fx_rhythm(date: str, conn: sqlite3.Connection, fx_delta_twd: float | None,
               notes: dict) -> dict:
    """升貶節奏：跳空（離岸晨對晨 Δ ≥ 門檻）+ 緩步/急拉（盤前 5 分序列）。"""
    # 跳空（台幣升＝USD/TWD delta 為負）
    gap = None
    if fx_delta_twd is not None and abs(fx_delta_twd) >= settings.FX_GAP_THRESHOLD:
        gap = "跳空升" if fx_delta_twd <= 0 else "跳空貶"

    bars = conn.execute(
        "SELECT close FROM intraday_fx WHERE date=? AND currency_pair='USD/TWD' "
        "ORDER BY ts", (date,),
    ).fetchall()
    closes = [b[0] for b in bars if b[0] is not None]

    if len(closes) < 2:
        raw = "盤前序列無" + ("" if gap is None else f"（{gap}）")
        verdict = gap or "資料累積中"
        css = "flat" if gap is None else ("up" if gap == "跳空升" else "down")
        return _row("匯率節奏", raw, verdict, css, notes)

    steps = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    max_step = max(abs(s) for s in steps)
    total = closes[-1] - closes[0]
    css = "up" if total < 0 else "down" if total > 0 else "flat"  # 台幣升=USD跌
    raw = f"近{len(closes)}根5分 {closes[0]:.3f}→{closes[-1]:.3f}（最大單根 {max_step:.3f}）"

    if max_step >= settings.FX_INTRADAY_SURGE:
        verdict = "急拉（恐央行/單一鉅額，隔天易回貶，別追）"
        css = "flat"
    elif gap:
        verdict = f"{gap}（外資半夜已動，小心開高走低）"
    elif abs(total) >= settings.FX_GAP_THRESHOLD:
        verdict = ("緩步台幣升（外資分批匯入，常連買）" if total < 0
                   else "緩步台幣貶（資金流出）")
    else:
        verdict = "盤前無明顯節奏"
        css = "flat"
    return _row("匯率節奏", raw, verdict, css, notes)


def _scalar(conn, sql, params):
    r = conn.execute(sql, params).fetchone()
    return r[0] if r else None


# ── 個股籌碼解讀（鏡像大盤 build_explain，供 watchlist 每檔顯示）─────────
# 維度→原數據(事實)→判讀(綁 signal_engine 既有門檻)→為什麼(觀點，走 explain_notes)。
# 純讀、不寫 DB、不改任何訊號；缺資料的維度優雅顯示「資料不可用」。

# 分類 category → 解讀表 css（多空語意；對敲/隔日沖等警示類走 flat）
_STOCK_CSS = {
    "外資連買": "up", "外資大買": "up", "外資連賣": "down", "外資大賣": "down",
    "bottom_watch": "up", "accumulation": "up",
    "distribution_warning": "down", "avoid": "down",
    "fake_volume": "flat", "day_trade_no_chase": "flat",
}


def _fmt_amt(amount: float | None) -> str:
    """金額(元)格式：>=1億用億、>=1萬用萬、否則整數。"""
    if amount is None:
        return "—"
    if abs(amount) >= 1e8:
        return f"{amount / 1e8:+.2f}億"
    if abs(amount) >= 1e4:
        return f"{amount / 1e4:+.0f}萬"
    return f"{amount:+,.0f}"


def _stock_foreign_row(date, stock_id, conn, notes) -> dict:
    """外資動向維度（raw_chip __FOREIGN__，復用 signal_engine 連買連賣分類）。"""
    from integration.signal_engine import _classify_foreign

    rows = conn.execute(
        "SELECT net_volume FROM raw_chip "
        "WHERE stock_id = ? AND broker_name = '__FOREIGN__' "
        "      AND date < ? AND net_volume IS NOT NULL "
        "ORDER BY date DESC LIMIT 30",
        (stock_id, date),
    ).fetchall()
    if not rows:
        return _row("外資動向", "尚未取得外資個股買賣超（T86）",
                    "資料不可用", "flat", notes)

    latest_net = rows[0][0]
    direction = 1 if latest_net > 0 else -1 if latest_net < 0 else 0
    streak, cum = 0, 0.0
    if direction != 0:
        for (net,) in rows:
            if net is None or net == 0 or (1 if net > 0 else -1) != direction:
                break
            streak += 1
            cum += net

    if streak >= 2:
        raw = f"連{'買' if direction > 0 else '賣'} {streak} 天，累計 {cum:+,.0f} 張"
    else:
        raw = f"前一交易日淨{'買' if latest_net > 0 else '賣'}超 {abs(latest_net):,.0f} 張"

    classified = _classify_foreign(direction, streak, cum, latest_net) if direction else None
    if classified is None:
        return _row("外資動向", raw, "外資無明顯方向", "flat", notes)
    category, reason = classified
    return _row("外資動向", raw, reason, _STOCK_CSS.get(category, "flat"), notes)


def _stock_broker_row(date, stock_id, conn, notes) -> dict:
    """主力分點維度（daily_stock_metrics；對敲/隔日沖警示 + 復用 _classify_stock）。"""
    from integration.signal_engine import _classify_stock

    # 取該股 <= date 最近一個有分點資料的日期（forgiving：剛匯入的也顯示得到）
    row_date = conn.execute(
        "SELECT MAX(date) FROM daily_stock_metrics WHERE stock_id = ? AND date <= ?",
        (stock_id, date),
    ).fetchone()[0]
    if row_date is None:
        return _row("主力分點", "尚未匯入分點資料（可用『分點匯入』補）",
                    "資料不可用", "flat", notes)

    rows = conn.execute(
        "SELECT broker_name, net_amount, consecutive_days, price_zone, "
        "       both_sides_flag, broker_type "
        "FROM daily_stock_metrics WHERE stock_id = ? AND date = ?",
        (stock_id, row_date),
    ).fetchall()

    # 對每個分點算 (category, reason)，挑最顯著者（有訊號優先，再比 |淨額|）
    best = None  # (key, broker, net, reason, category)
    for broker, net, consec, zone, both_sides, btype in rows:
        if both_sides == 1:
            cat, reason = "fake_volume", "同分點兩邊都有量，疑似對敲假量，不碰"
        elif btype == "day_trade" and net is not None and net > 0:
            cat, reason = "day_trade_no_chase", "隔日沖分點買超，明天多半賣，不追"
        else:
            classified = _classify_stock(net, consec, zone)
            cat, reason = classified if classified else (None, None)
        key = (1 if cat else 0, abs(net) if net is not None else 0)
        if best is None or key > best[0]:
            best = (key, broker, net, reason, cat)

    _, broker, net, reason, cat = best
    raw = f"{broker} 淨額 {_fmt_amt(net)}"
    if row_date != date:          # 非當前日的分點：標出資料日期，免得誤以為是最新
        raw += f"（{row_date}）"
    if cat is None:
        return _row("主力分點", raw, "無明顯分點訊號", "flat", notes)
    return _row("主力分點", raw, reason, _STOCK_CSS.get(cat, "flat"), notes)


def _stock_price_zone_row(date, stock_id, conn, notes) -> dict:
    """股價位置維度（daily_stock_metrics.price_vs_ma20 / price_zone）。"""
    row = conn.execute(
        "SELECT price_vs_ma20, price_zone FROM daily_stock_metrics "
        "WHERE stock_id = ? AND date <= ? AND price_vs_ma20 IS NOT NULL "
        "ORDER BY date DESC LIMIT 1",
        (stock_id, date),
    ).fetchone()
    if row is None or row[0] is None:
        return _row("股價位置", "尚無股價位置資料", "資料不可用", "flat", notes)
    pct, zone = row
    zone_zh = {"low": "低檔（月線下方）", "consolidation": "盤整（貼近月線）",
               "high": "高檔（月線上方）"}.get(zone, zone or "—")
    return _row("股價位置", f"股價 vs MA20 {pct:+.1f}%", zone_zh, "flat", notes)


def build_stock_explain(date: str, stock_id: str,
                        conn: sqlite3.Connection) -> list[dict]:
    """組出個股籌碼解讀表（外資動向／主力分點／股價位置），鏡像大盤 build_explain。

    每維度回 {dim, raw, verdict, css, why}。純讀不寫；判讀復用 signal_engine 門檻、
    觀點走 explain_notes；缺資料的維度顯示「資料不可用」。
    """
    notes = _load_notes()
    return [
        _stock_foreign_row(date, stock_id, conn, notes),
        _stock_broker_row(date, stock_id, conn, notes),
        _stock_price_zone_row(date, stock_id, conn, notes),
    ]
