"""Layer 3b 個股趨勢訊號引擎（spec v2：三態趨勢＋順大勢逆小勢）。

純技術面：輸入個股還原日K OHLC（raw_stock_daily），輸出三態趨勢 UP/DOWN/FLAT
（以年線斜率為主、FLAT 三症狀投票）＋順大勢逆小勢的進場/加減倉提示。

不用 pandas——spec 的參考骨架是 pandas，這裡以 stdlib list 忠實重寫（rolling 均線、
斜率、遲滯確認）。門檻全部來自 config/settings.py，調整門檻請 bump TREND_RULE_VERSION。
fundamental_gate 目前無基本面資料源，一律 UNKNOWN（保守：不放行「關注建倉區」）。
"""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from config import settings

logger = logging.getLogger(__name__)

# 三態 + 資料不足態（UNKNOWN 表暖機不足，與真正 FLAT 區分）
UP, DOWN, FLAT, UNKNOWN = "UP", "DOWN", "FLAT", "UNKNOWN"

# fundamental_gate 取值
GATE_PASS, GATE_FAIL, GATE_UNKNOWN = "PASS", "FAIL", "UNKNOWN"


@dataclass
class TrendParams:
    """趨勢分類可調參數，預設取自 settings（可注入覆寫做回測）。"""

    ma_big: int = settings.TREND_MA_BIG
    ma_center: int = settings.TREND_MA_CENTER
    ma_fast: int = settings.TREND_MA_FAST
    slope_lookback: int = settings.TREND_SLOPE_LOOKBACK
    slope_deadband: float = settings.TREND_SLOPE_DEADBAND
    cross_window: int = settings.TREND_CROSS_WINDOW
    cross_thresh: int = settings.TREND_CROSS_THRESH
    box_eps: float = settings.TREND_BOX_EPS
    confirm_bars: int = settings.TREND_CONFIRM_BARS
    overbought_bias: float = settings.TREND_OVERBOUGHT_BIAS


def _sma(values: list[float], n: int) -> list[float | None]:
    """簡單移動平均：index i = 最近 n 根均值，暖機不足回 None（對應 pandas rolling(n).mean()）。"""
    out: list[float | None] = []
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= n:
            running -= values[i - n]
        out.append(running / n if i >= n - 1 else None)
    return out


def _hysteresis(raw: list[str], n: int) -> list[str]:
    """遲滯確認：raw 需連續 n 根同態才切換 state，抑制每日抖動（忠實對應 spec 骨架）。"""
    if not raw:
        return []
    out = []
    state = raw[0]
    val = raw[0]
    streak = 1
    for i, v in enumerate(raw):
        if i == 0:
            out.append(state)
            continue
        if v != val:
            val, streak = v, 1
        else:
            streak += 1
        if streak >= n and v != state:
            state = v
        out.append(state)
    return out


def _classify_series(bars: list[dict], p: TrendParams) -> tuple[list[str], list[float | None]]:
    """對整個序列算三態（遲滯後）與年線斜率。回傳 (states, slopes)，長度＝len(bars)。"""
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    n = len(bars)

    ma_big = _sma(closes, p.ma_big)
    ma_center = _sma(closes, p.ma_center)

    # 年線斜率：ma_big[i] / ma_big[i-L] - 1（兩端皆需暖機完成）
    slopes: list[float | None] = [None] * n
    for i in range(n):
        j = i - p.slope_lookback
        if j >= 0 and ma_big[i] is not None and ma_big[j] not in (None, 0):
            slopes[i] = ma_big[i] / ma_big[j] - 1.0

    # close 對月線的上下側 → 反復穿越次數（FLAT 症狀 2）
    side: list[int | None] = [None] * n
    for i in range(n):
        if ma_center[i] is not None:
            side[i] = 1 if closes[i] > ma_center[i] else 0
    crossed = [0] * n  # 該根是否穿越（相對前一根）
    for i in range(1, n):
        if side[i] is not None and side[i - 1] is not None:
            crossed[i] = abs(side[i] - side[i - 1])
    w = p.cross_window

    raw: list[str] = [FLAT] * n
    for i in range(n):
        s = slopes[i]

        # FLAT 三症狀（無法計算的症狀不投票＝False）
        flat_slope = s is not None and abs(s) <= p.slope_deadband
        choppy = False
        if i >= w - 1 and all(side[k] is not None for k in range(i - w + 1, i + 1)):
            choppy = sum(crossed[i - w + 1:i + 1]) >= p.cross_thresh
        rangebound = False
        if i >= w - 1 and closes[i]:
            box = (max(highs[i - w + 1:i + 1]) - min(lows[i - w + 1:i + 1])) / closes[i]
            rangebound = box <= p.box_eps
        is_flat = (int(flat_slope) + int(choppy) + int(rangebound)) >= 2

        if s is not None and not is_flat and ma_big[i] is not None:
            if s > p.slope_deadband and closes[i] > ma_big[i]:
                raw[i] = UP
            elif s < -p.slope_deadband and closes[i] < ma_big[i]:
                raw[i] = DOWN

    return _hysteresis(raw, p.confirm_bars), slopes


def _small_state(bias: float | None, p: TrendParams) -> str:
    """小勢（對月線支點的相對位置）。bias 不可用時回『資料不足』。"""
    if bias is None:
        return "資料不足"
    if bias > p.overbought_bias:
        return "超買遠離"
    if bias < 0:
        return "跌破恐慌"
    return "貼近中樞"


def _route(big: str, small: str, turn_up: bool, gate: str) -> tuple[str, str | None]:
    """決策路由（忠實對應 spec 五步）。回傳 (action_hint, flat_redirect)。"""
    if big == UNKNOWN:
        return "資料不足待觀察", None
    if big == FLAT:                                   # 第五步：震盪自動失效
        return "震盪不操作", "轉去修內功(研究財報)"
    if big == DOWN:                                    # 第一步：只搭往上的火車
        return "迴避", None
    # UP（第一步：大勢昂首向上）
    if gate == GATE_FAIL:                              # 內功否決
        return "迴避", None
    if small == "超買遠離":                            # 第二步：狗跑太快
        return "不宜追高", None
    if small in ("跌破恐慌", "貼近中樞") and turn_up and gate == GATE_PASS:
        return "關注建倉區", None                       # 第三步：回擺+企穩+內功驗證
    if small == "跌破恐慌" and not turn_up:            # 第三步防禦：不能剛跌就買
        return "等待止穩", None
    return "觀望", None


def _position(big: str, bias: float | None, p: TrendParams) -> str:
    """加減倉提示（獨立於 action 的連續提示：狗跑太快減倉、太慢加倉）。"""
    if big == UNKNOWN or bias is None:
        return "—"
    if bias > p.overbought_bias:
        return "減倉"
    if bias < 0:
        return "可加倉"
    return "持有"


def _reasons(big: str, slope: float | None, small: str, turn_up: bool,
             bias: float | None, gate: str) -> list[str]:
    """組出人看得懂的理由清單（對應每日輸出 schema 的 reasons）。"""
    reasons: list[str] = []
    if big == UNKNOWN:
        reasons.append("歷史K線不足，年線暖機未完成，趨勢待觀察")
        return reasons

    if big == UP and slope is not None:
        reasons.append(f"年線斜率{slope:+.1%}且價在年線上（大勢向上）")
    elif big == DOWN and slope is not None:
        reasons.append(f"年線斜率{slope:+.1%}且價在年線下（大勢向下）")
    elif big == FLAT:
        reasons.append("震盪市（走平/反復穿越/箱型三症狀投票 ≥2），趨勢自動失效")

    if bias is not None:
        turn = "MA5轉升" if turn_up else "MA5未轉升"
        if small == "超買遠離":
            reasons.append(f"乖離+{bias:.1%} 超買遠離月線（狗跑太快），{turn}")
        elif small == "跌破恐慌":
            reasons.append(f"跌破月線支點（乖離{bias:.1%}），{turn}")
        else:
            reasons.append(f"貼近月線支點（乖離{bias:+.1%}），{turn}")

    if big == UP and gate == GATE_UNKNOWN and small in ("跌破恐慌", "貼近中樞") and turn_up:
        reasons.append("回擺企穩但內功未驗證（無基本面資料）→ 僅觀望，不放行建倉")
    return reasons


def evaluate_series(bars: list[dict], params: TrendParams | None = None,
                    fundamental_gate: str = GATE_UNKNOWN) -> list[dict]:
    """對整段日K（舊→新）逐根算趨勢訊號，回傳與 evaluate 同 schema 的每根 dict 列表。

    第 i 根的結果 ＝ 只用 bars[:i+1] 時 evaluate 的輸出（point-in-time，無未來函數），
    供回測逐 bar 重放 production 訊號。O(n) 單趟（共用 _classify_series 等 helper）。
    """
    if not bars:
        return []
    p = params or TrendParams()
    n = len(bars)
    closes = [b["close"] for b in bars]
    ma_center = _sma(closes, p.ma_center)
    ma_fast = _sma(closes, p.ma_fast)
    states, slopes = _classify_series(bars, p)
    warmup = p.ma_big + p.slope_lookback

    out: list[dict] = []
    for i in range(n):
        slope = slopes[i]
        # 暖機門檻：年線斜率需 ma_big + slope_lookback 根才成立；不足＝UNKNOWN（非 FLAT）
        enough_big = (i + 1) >= warmup and slope is not None
        big = states[i] if enough_big else UNKNOWN
        center = ma_center[i]
        bias = ((closes[i] - center) / center) if center else None
        turn_up = bool(i >= 1 and ma_fast[i] is not None and ma_fast[i - 1] is not None
                       and ma_fast[i] > ma_fast[i - 1])
        small = _small_state(bias, p)
        action, flat_redirect = _route(big, small, turn_up, fundamental_gate)
        position = _position(big, bias, p)
        reasons = _reasons(big, slope, small, turn_up, bias, fundamental_gate)
        out.append({
            "big_trend": big,
            "big_trend_confidence": round(slope, 4) if slope is not None else None,
            "value_center": round(center, 2) if center is not None else None,
            "bias_pct": round(bias, 4) if bias is not None else None,
            "small_state": small,
            "small_turn_up": turn_up,
            "fundamental_gate": fundamental_gate,
            "action_hint": action,
            "position_hint": position,
            "flat_redirect": flat_redirect,
            "reasons": reasons,
        })
    return out


def evaluate(bars: list[dict], params: TrendParams | None = None,
             fundamental_gate: str = GATE_UNKNOWN) -> dict:
    """對一段日K（舊→新排序）算最新一根的趨勢訊號，回傳 spec §4 的每日輸出 schema。"""
    if not bars:
        raise ValueError("evaluate: bars 不可為空")
    return evaluate_series(bars, params, fundamental_gate)[-1]


def _save_trend_signal(conn: sqlite3.Connection, result: dict) -> None:
    """把 evaluate 結果 upsert 進 stock_trend_signals。"""
    conn.execute(
        """INSERT INTO stock_trend_signals
               (date, stock_id, big_trend, big_trend_confidence, value_center,
                bias_pct, small_state, small_turn_up, fundamental_gate,
                action_hint, position_hint, flat_redirect, reasons,
                adjust_note, rule_version, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(date, stock_id) DO UPDATE SET
               big_trend = excluded.big_trend,
               big_trend_confidence = excluded.big_trend_confidence,
               value_center = excluded.value_center,
               bias_pct = excluded.bias_pct,
               small_state = excluded.small_state,
               small_turn_up = excluded.small_turn_up,
               fundamental_gate = excluded.fundamental_gate,
               action_hint = excluded.action_hint,
               position_hint = excluded.position_hint,
               flat_redirect = excluded.flat_redirect,
               reasons = excluded.reasons,
               adjust_note = excluded.adjust_note,
               rule_version = excluded.rule_version,
               created_at = excluded.created_at""",
        (result["date"], result["stock_id"], result["big_trend"],
         result["big_trend_confidence"], result["value_center"],
         result["bias_pct"], result["small_state"],
         1 if result["small_turn_up"] else 0, result["fundamental_gate"],
         result["action_hint"], result["position_hint"], result["flat_redirect"],
         json.dumps(result["reasons"], ensure_ascii=False),
         result.get("adjust_note"),
         settings.TREND_RULE_VERSION, datetime.now().isoformat()),
    )
    conn.commit()


def compute_trend_signal(stock_id: str, conn: sqlite3.Connection,
                         asof: str | None = None,
                         params: TrendParams | None = None) -> dict | None:
    """讀 raw_stock_daily 算個股趨勢訊號並寫入 stock_trend_signals。無K線回 None。

    asof 給定時只取該日（含）以前的K線（供回測/歷史重算）；否則用全部歷史的最新一根。
    """
    q = ("SELECT date, open, high, low, close, volume "
         "FROM raw_stock_daily WHERE stock_id = ? AND open IS NOT NULL "
         "AND high IS NOT NULL AND low IS NOT NULL AND close IS NOT NULL")
    args: list = [stock_id]
    if asof:
        q += " AND date <= ?"
        args.append(asof)
    q += " ORDER BY date ASC"
    rows = conn.execute(q, args).fetchall()

    bars = [{"date": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "volume": r[5]} for r in rows]
    if not bars:
        logger.info("compute_trend_signal: no OHLC for %s", stock_id)
        return None

    # 還原股價：偵測分割/大除權缺口 + 配息比例回推（spec §6.1 需還原股價）
    from collectors.dividend import load_dividend_factors
    from integration.price_adjust import adjust_note, back_adjust_with_events
    bars, adj_events = back_adjust_with_events(
        bars, load_dividend_factors(conn, stock_id))
    note = adjust_note(adj_events)

    result = evaluate(bars, params)
    result["stock_id"] = stock_id
    result["date"] = bars[-1]["date"]
    result["rule_version"] = settings.TREND_RULE_VERSION
    result["adjust_note"] = note
    _save_trend_signal(conn, result)

    # 公司行為缺口還原可能誤判（±10% 限制外的槓桿/反向ETF、停牌跳空）→ 記 log 供核對
    for e in adj_events:
        if e["kind"] == "gap":
            logger.info("Trend %s: back-adjusted gap @%s ×%.3f (verify split/ex-rights)",
                        stock_id, e["date"], e["factor"])

    logger.info("Trend signal for %s@%s: %s / %s (bias=%s)",
                stock_id, result["date"], result["big_trend"],
                result["action_hint"], result["bias_pct"])
    return result
