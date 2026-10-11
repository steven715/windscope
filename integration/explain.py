"""盤前解讀層：把每個指標的「原數據 + 判讀 + 為什麼」組成一張表，供學習用面板顯示。
純函數，只讀 raw_*、daily_metrics 與 signals，不寫 DB。

分層原則（見使用者要求）：
- 原數據 = 事實；判讀 = 依門檻得出的級別措辭（詮釋）。盤前判斷維度的級別與
  對票／信心的實際效果（effect）取自 integration/premarket_call，門檻不在本檔重比。
- 「為什麼」= 詮釋/評註，**外部化在 config/explain_notes.json**、標來源、可放自己的觀點，
  與判斷邏輯完全分離。改觀點只改 JSON，不動程式、不影響任何訊號。
"""

import json
import logging
import sqlite3
from pathlib import Path

from config import settings
from integration.premarket_call import (
    DIRECTION_LABELS,
    SKIP_ALIGNED,
    SKIP_DIRECTION_NEUTRAL,
    SKIP_FUTURES_VOTE_NEUTRAL,
    SKIP_FX_VOTE_NEUTRAL,
    Adjustment,
    evaluate_premarket_call,
    load_daily_metrics,
)
from integration.premarket_inputs import SPOT_FROM_INDEX, PremarketInputs, load_premarket_inputs
from integration.stock_chip import (
    BrokerReading,
    ForeignReading,
    StockChip,
    ZoneReading,
    load_stock_chip,
)

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


def _row(dim, raw, verdict, css, notes, effect=None):
    why, why_source = _note(notes, dim)
    return {"dim": dim, "raw": raw, "verdict": verdict, "css": css,
            "effect": effect, "why": why, "why_source": why_source}


_SKIP_ZH = {
    SKIP_FX_VOTE_NEUTRAL: "匯率票中性",
    SKIP_FUTURES_VOTE_NEUTRAL: "期貨票中性",
    SKIP_DIRECTION_NEUTRAL: "判斷方向中性",
    SKIP_ALIGNED: "與判斷同向",
}


def _effect_text(adj: Adjustment) -> str | None:
    """信心調整的實際效果文字：+1／-1／未計入（原因）；無級別或級別本不調整回 None。"""
    if adj.effect > 0:
        return f"信心 +{adj.effect}"
    if adj.effect < 0:
        return f"信心 {adj.effect}"
    if adj.skip:
        return f"未計入：{_SKIP_ZH[adj.skip]}"
    return None


def _vote_text(name: str, vote: str) -> str:
    return f"{name} {DIRECTION_LABELS.get(vote, vote)}"


def build_explain(date: str, conn: sqlite3.Connection) -> list[dict]:
    """組出 date 的盤前解讀表。回傳每維度 {dim, raw, verdict, css, effect, why} 的 list。
    盤前判斷維度的級別與 effect（對票／信心的實際效果）取自 premarket_call，本檔只負責措辭。
    """
    metrics = load_daily_metrics(date, conn)
    if metrics is None:
        return []
    call = evaluate_premarket_call(metrics)
    fx_delta_twd, fx_direction = metrics.fx_delta_twd, metrics.fx_direction
    spread, spread_adj = metrics.futures_spread, metrics.futures_spread_adjusted
    vol_ratio = metrics.futures_volume_ratio
    oi_net, oi_delta = metrics.oi_net_foreign, metrics.oi_delta

    notes = _load_notes()
    inp = load_premarket_inputs(date, conn)
    rows = []

    # 1. 匯率（台幣）：離岸 USDTWD=X 晨對晨（在岸 08:45 牌價開盤前未更新、失真）
    twd_now, twd_prev = inp.twd_offshore_today, inp.twd_offshore_prev
    if twd_now is not None and twd_prev is not None and fx_delta_twd is not None:
        raw = f"今晨離岸 {twd_now:.3f} vs 昨晨 {twd_prev:.3f} → Δ{fx_delta_twd:+.4f}"
    else:
        raw = f"Δ{fx_delta_twd:+.4f}" if fx_delta_twd is not None else "資料不足"
    verdict, css = {
        "bullish": ("升值（外資錢進來，權值股有機會）", "up"),
        "bearish": ("貶值（外資匯出，今天別衝）", "down"),
        "neutral": ("平盤（回到個股籌碼判斷）", "flat"),
    }.get(fx_direction, ("資料不足", "flat"))
    rows.append(_row("匯率（台幣）", raw, verdict, css, notes,
                     _vote_text("匯率票", call.fx_vote)))

    # 1b. 匯率節奏（跳空 + 緩步/急拉，用盤前 5 分序列）
    rows.append(_fx_rhythm(date, conn, fx_delta_twd, notes))

    # 2. 亞幣同步
    # 亞幣同步與韓元背離是兩個信心調整，原數據同一行 → 一列、兩個效果都列
    detail = metrics.fx_asia_detail or {}
    raw = (f"台幣{_ASIA_ZH.get(detail.get('TWD'))}／"
           f"人民幣{_ASIA_ZH.get(detail.get('CNY'))}／"
           f"韓元{_ASIA_ZH.get(detail.get('KRW'))}")
    asia, krw = call.asia_sync, call.krw_divergence
    if krw.level == "divergence":
        verdict, css = "警示：台幣人民幣貶但韓元升，外資恐賣台買韓", "down"
    elif asia.level == "sync" and detail.get("TWD") == "bearish":
        verdict, css = "三幣同步貶（資金撤出亞洲）", "down"
    elif asia.level == "sync" and detail.get("TWD") == "bullish":
        verdict, css = "三幣同步（國際資金流入亞洲，大盤安全）", "up"
    elif asia.level == "sync":
        verdict, css = "三幣同步持平（無明顯資金流向）", "flat"
    elif asia.level == "diverge":
        verdict, css = "分歧（只有部分亞幣動，買盤恐不持續）", "flat"
    else:
        verdict, css = "資料不足（亞幣收盤基準累積中）", "flat"
    effects = []
    if krw.effect:
        effects.append(f"韓元背離 {_effect_text(krw)}")
    asia_effect = _effect_text(asia)
    if asia_effect:
        label = "亞幣同步" if asia.level == "sync" else "亞幣分歧"
        effects.append(f"{label} {asia_effect}")
    rows.append(_row("亞幣同步", raw, verdict, css, notes, "；".join(effects) or None))

    # 3. 期貨價差
    night, prev_spot = inp.night_close, inp.prev_spot
    if night is not None and prev_spot is not None and spread is not None:
        spot_label = "前日現貨（指數收盤補）" if inp.prev_spot_source == SPOT_FROM_INDEX else "前日現貨"
        raw = f"夜盤{night:.0f} − {spot_label}{prev_spot:.0f} = {spread:+.0f}"
        if spread_adj is not None and spread_adj != spread:
            raw += f"（扣除息後 {spread_adj:+.0f}）"
    else:
        raw = f"{spread_adj:+.0f}" if spread_adj is not None else "資料不足"
    threshold = f"{settings.FUTURES_SPREAD_THRESHOLD:g}"
    if spread_adj is None:
        verdict, css = "資料不足", "flat"
    elif call.futures_vote == "bullish":
        verdict, css = f"正價差≥{threshold}（開高機率高）", "up"
    elif call.futures_vote == "bearish":
        verdict, css = f"逆價差≥{threshold}（開低機率高）", "down"
    else:
        verdict, css = f"價差未達±{threshold}（中性）", "flat"
    rows.append(_row("期貨價差", raw, verdict, css, notes,
                     _vote_text("期貨票", call.futures_vote)))

    # 4. 夜盤量比（原數據顯示今夜實際口數 + 近 N 日均量，比單一倍數有感）
    if vol_ratio is not None:
        n = settings.FUTURES_VOLUME_LOOKBACK
        night_vol, avg = inp.night_volume, inp.night_volume_avg
        if night_vol is not None and avg:
            raw = f"今夜 {night_vol:,} 口 ｜ 近{n}日均 {avg:,.0f} 口 → {vol_ratio:.2f}倍"
        else:
            raw = f"{vol_ratio:.2f}x（vs 近{n}日均量）"
        verdict, css = {
            "high": ("爆量（大戶提前佈局，順著做）", "up"),
            "low": ("量縮（市場在等，開盤波動大）", "flat"),
        }.get(call.night_volume.level, ("量能正常", "flat"))
    else:
        raw, verdict, css = "資料不足", "資料不足", "flat"
    rows.append(_row("夜盤量比", raw, verdict, css, notes,
                     _effect_text(call.night_volume)))

    # 5. 外資未平倉
    if oi_net is not None:
        raw = f"{oi_net:+,} 口"
        if oi_delta is not None:
            raw += f"（較前日 {oi_delta:+,}）"
        # 籌碼沒有票：只在與判斷方向相逆時扣信心（見 effect）
        verdict, css = {
            "net_short": ("外資淨空", "down"),
            "net_long": ("外資淨多", "up"),
        }.get(call.foreign_oi.level, ("部位中性", "flat"))
    else:
        raw, verdict, css = "尚未收集（收盤後更新）", "—", "flat"
    rows.append(_row("外資未平倉", raw, verdict, css, notes,
                     _effect_text(call.foreign_oi)))

    # 6. 美股對照（看異常）
    sp_now, sp_prev = inp.sp500_today, inp.sp500_prev
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
    rows.append(_jpy_risk_gauge(inp, notes))

    return rows


def premarket_drift_notice(date: str, conn: sqlite3.Connection) -> str | None:
    """解讀即時重算的盤前判斷與 signals 已存那筆不同（結果、兩票或規則版本）時回一行提示。
    相同或缺資料回 None；正常交易日兩者相同，事後補資料或規則更新後回看舊日子才會不同。
    """
    stored = conn.execute(
        "SELECT direction, confidence, fx_vote, futures_vote, rule_version "
        "FROM signals WHERE date = ?", (date,)
    ).fetchone()
    metrics = load_daily_metrics(date, conn)
    if stored is None or metrics is None:
        return None
    call = evaluate_premarket_call(metrics)
    direction, confidence, fx_vote, futures_vote, rule_version = stored
    stored_version = rule_version or "未記錄"
    same_result = ((call.direction, call.confidence, call.fx_vote, call.futures_vote)
                   == (direction, confidence, fx_vote, futures_vote))
    if same_result and call.rule_version == rule_version:
        return None
    if same_result:
        return (f"08:50 的盤前判斷用規則 {stored_version}，解讀以現行規則 "
                f"{call.rule_version} 重算（結果相同）")
    return (f"解讀以目前資料／規則 {call.rule_version} 重算"
            f"（{DIRECTION_LABELS.get(call.direction, call.direction)} 信心 {call.confidence}，"
            f"匯率票 {DIRECTION_LABELS.get(call.fx_vote, call.fx_vote)}、"
            f"期貨票 {DIRECTION_LABELS.get(call.futures_vote, call.futures_vote)}），"
            f"與 08:50 的盤前判斷不同（{DIRECTION_LABELS.get(direction, direction)} "
            f"信心 {confidence}，規則 {stored_version}）")


def _jpy_risk_gauge(inp: PremarketInputs, notes: dict) -> dict:
    """日圓避險溫度計：USD/JPY 今日報價 vs 前一交易日收盤。日圓急升=risk-off=偏空警示。"""
    now = inp.fx_quote_0845.get("USD/JPY")
    prev = inp.fx_prev_close_16.get("USD/JPY")

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


# ── 個股籌碼解讀（鏡像大盤 build_explain，供 watchlist 每檔顯示）─────────
# 維度→原數據(事實)→判讀(分類規則在 integration/stock_chip)→為什麼(觀點，走 explain_notes)。
# 純讀、不寫 DB、不改任何訊號；缺資料的維度優雅顯示「資料不可用」。

def _fmt_amt(amount: float | None) -> str:
    """金額(元)格式：>=1億用億、>=1萬用萬、否則整數。"""
    if amount is None:
        return "—"
    if abs(amount) >= 1e8:
        return f"{amount / 1e8:+.2f}億"
    if abs(amount) >= 1e4:
        return f"{amount / 1e4:+.0f}萬"
    return f"{amount:+,.0f}"


def _stock_foreign_row(f: ForeignReading, notes: dict) -> dict:
    """外資動向維度。"""
    if not f.available:
        return _row("外資動向", "尚未取得外資個股買賣超（T86）",
                    "資料不可用", "flat", notes)
    if f.streak >= settings.FOREIGN_CONSECUTIVE_MIN:
        raw = f"連{'買' if f.latest_net > 0 else '賣'} {f.streak} 天，累計 {f.cum:+,.0f} 張"
    else:
        raw = f"前一交易日淨{'買' if f.latest_net > 0 else '賣'}超 {abs(f.latest_net):,.0f} 張"
    if f.category is None:
        return _row("外資動向", raw, "外資無明顯方向", "flat", notes)
    return _row("外資動向", raw, f.reason, f.direction, notes)


def _stock_broker_row(b: BrokerReading, date: str, notes: dict) -> dict:
    """主力分點維度（最顯著的一個分點）。"""
    if not b.available:
        return _row("主力分點", "尚未匯入分點資料（可用『分點匯入』補）",
                    "資料不可用", "flat", notes)
    raw = f"{b.broker} 淨額 {_fmt_amt(b.net)}"
    if b.data_date != date:       # 非當前日的分點：標出資料日期，免得誤以為是最新
        raw += f"（{b.data_date}）"
    if b.category is None:
        return _row("主力分點", raw, "無明顯分點訊號", "flat", notes)
    return _row("主力分點", raw, b.reason, b.direction, notes)


_ZONE_ZH = {"low": "低檔（月線下方）", "consolidation": "盤整（貼近月線）",
            "high": "高檔（月線上方）", "other": "月線上下之間"}


def _stock_price_zone_row(z: ZoneReading, notes: dict) -> dict:
    """股價位置維度（分點指標優先，無分點時由 K線還原後乖離補）。"""
    if not z.available:
        return _row("股價位置", "尚無股價位置資料（回補K線即可算）", "資料不可用", "flat", notes)
    if z.from_kline:
        return _row("股價位置", f"股價 vs 月線 {z.pct:+.1f}%（由K線還原後算）",
                    _ZONE_ZH.get(z.zone, "—"), "flat", notes)
    return _row("股價位置", f"股價 vs MA20 {z.pct:+.1f}%",
                _ZONE_ZH.get(z.zone, z.zone or "—"), "flat", notes)


def format_stock_explain(chip: StockChip) -> list[dict]:
    """把籌碼判讀排成個股解讀表（外資動向／主力分點／股價位置），鏡像大盤 build_explain。

    每維度回 {dim, raw, verdict, css, why}；觀點走 explain_notes。
    """
    notes = _load_notes()
    return [
        _stock_foreign_row(chip.foreign, notes),
        _stock_broker_row(chip.broker, chip.date, notes),
        _stock_price_zone_row(chip.zone, notes),
    ]


def build_stock_explain(date: str, stock_id: str,
                        conn: sqlite3.Connection) -> list[dict]:
    """組出個股籌碼解讀表（讀 load_stock_chip 再排版）。純讀不寫。"""
    return format_stock_explain(load_stock_chip(date, stock_id, conn))


# ── 個股趨勢解讀（技術面 + 基本面預留）──────────────────────────
# 鏡像籌碼解讀的三欄：原數據(事實)→判讀(綁 TREND_RULE_VERSION 門檻)→為什麼(觀點)。
# 技術面讀 stock_trend_signals（由個股日K導出）；基本面資料源未接，先預留占位。

def _stock_trend_row(date: str, stock_id: str, conn: sqlite3.Connection,
                     notes: dict) -> dict:
    """趨勢（技術面）維度：讀 date 以前最新一筆趨勢訊號，組原數據＋三態判讀。"""
    from integration.trend_signal import load_stock_trend

    t = load_stock_trend(stock_id, conn, asof=date)
    if t is None:
        return _row("趨勢（技術面）", "尚未回補歷史K線（可用『K線匯入』補）",
                    "資料不可用", "flat", notes)

    slope, center, bias, small = t.big_trend_confidence, t.value_center, t.bias_pct, t.small_state
    if t.big_trend == "UNKNOWN":
        return _row("趨勢（技術面）", "歷史K線不足，年線暖機未完成（需~260根）",
                    "資料不足", "flat", notes)

    parts = []
    if center is not None:
        parts.append(f"月線{center:.1f}")
    if slope is not None:
        parts.append(f"年線斜率{slope * 100:+.1f}%")
    if bias is not None:
        parts.append(f"乖離{bias * 100:+.1f}%")
    parts.append(f"小勢{small}" + ("·MA5轉升" if t.small_turn_up else ""))
    raw = "｜".join(parts)
    if t.adjust_note:                  # 還原提示：公司行為缺口偵測（供人工核對）
        raw += f"｜{t.adjust_note}"

    verdict = f"{t.label}·{t.action_hint}"
    if t.position_hint and t.position_hint != "—":
        verdict += f"（{t.position_hint}）"
    return _row("趨勢（技術面）", raw, verdict, t.css, notes)


def _stock_fundamental_row(notes: dict) -> dict:
    """基本面維度：資料源未接，預留占位（判讀恆為資料不可用）。"""
    return _row("基本面", "尚未接入基本面資料（營收/財報）",
                "資料不可用（預留）", "flat", notes)


def build_stock_trend_explain(date: str, stock_id: str,
                              conn: sqlite3.Connection) -> list[dict]:
    """組出個股趨勢解讀表：趨勢（技術面）＋基本面（預留）。與籌碼解讀同一 row 結構。"""
    notes = _load_notes()
    return [
        _stock_trend_row(date, stock_id, conn, notes),
        _stock_fundamental_row(notes),
    ]
