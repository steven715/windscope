"""盤前解讀層：把每個指標的「原數據 + 判讀 + 為什麼(原文依據)」組成一張表，
供學習用的 Web 面板顯示。純函數，只讀 raw_* 與 daily_metrics，不寫 DB。

「為什麼」文字來自發想原文〈開盤前三件事〉，見 docs/logic_article.md。
"""

import json
import logging
import sqlite3

from config import settings
from utils.trading_calendar import get_previous_trading_day

logger = logging.getLogger(__name__)

_ASIA_ZH = {"bullish": "升", "bearish": "貶", "neutral": "平", None: "—"}


def _row(dim, raw, verdict, css, why):
    return {"dim": dim, "raw": raw, "verdict": verdict, "css": css, "why": why}


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

    prev = get_previous_trading_day(date, conn)
    rows = []

    # 1. 匯率（台幣）
    twd_now = _scalar(conn, "SELECT quote_0845 FROM raw_fx WHERE date=? AND currency_pair='USD/TWD'", (date,))
    twd_prev = _scalar(conn, "SELECT close_16 FROM raw_fx WHERE date=? AND currency_pair='USD/TWD'", (prev,)) if prev else None
    if twd_now is not None and twd_prev is not None:
        raw = f"08:45 {twd_now:.3f} vs 前日16:00 {twd_prev:.3f} → Δ{fx_delta_twd:+.4f}"
    else:
        raw = f"Δ{fx_delta_twd:+.4f}" if fx_delta_twd is not None else "資料不足"
    verdict, css = {
        "bullish": ("升值（外資錢進來，權值股有機會）", "up"),
        "bearish": ("貶值（外資匯出，今天別衝）", "down"),
        "neutral": ("平盤（回到個股籌碼判斷）", "flat"),
    }.get(fx_direction, ("資料不足", "flat"))
    rows.append(_row("匯率（台幣）", raw, verdict, css,
                     "跟前一天16:00收盤比，升貶超過0.1元才算明顯。台幣是外資的腳印——"
                     "先看匯率才不會被新聞帶風向。"))

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
    rows.append(_row("亞幣同步", raw, verdict, css,
                     "三個一起升＝國際資金真的流入；只有台幣升＝壽險/出口商拋匯、買盤不持續；"
                     "台貶人貶但韓元升＝外資在賣台買韓，要小心。"))

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
    rows.append(_row("期貨價差", raw, verdict, css,
                     "正價差>100→開高、逆價差>100→開低。但6–8月除息旺季本來就逆價差100–300點，"
                     "是正常現象，要扣掉除息點數再判斷。"))

    # 4. 夜盤量比
    if vol_ratio is not None:
        raw = f"{vol_ratio:.2f}x（vs 近5日均量）"
        if vol_ratio >= settings.VOLUME_RATIO_HIGH:
            verdict, css = "爆量（大戶提前佈局，順著做）", "up"
        elif vol_ratio <= settings.VOLUME_RATIO_LOW:
            verdict, css = "量縮（市場在等，開盤波動大）", "flat"
        else:
            verdict, css = "量能正常", "flat"
    else:
        raw, verdict, css = "資料不足", "資料不足", "flat"
    rows.append(_row("夜盤量比", raw, verdict, css,
                     "爆量(>1.5倍)且價差擴大＝大戶佈局、勝率高；量縮＝市場在等、開盤先看5分鐘再動作。"))

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
    rows.append(_row("外資未平倉", raw, verdict, css,
                     "外資期貨空單超過3萬口→方向偏空。這是外資的期貨部位，用前一交易日收盤的數字。"))

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
    rows.append(_row("美股對照（看異常）", raw, verdict, css,
                     "重點不是看漲跌、是看有沒有異常：美股大漲但台指夜盤沒怎麼動→台股相對弱勢，不建議追高。"))

    return rows


def _scalar(conn, sql, params):
    r = conn.execute(sql, params).fetchone()
    return r[0] if r else None
