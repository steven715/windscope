"""趨勢三態引擎（spec v2：三態趨勢＋順大勢逆小勢）——純函式，windscope／tradewind 共用。

輸入還原日K（舊→新，每根 {date, open, high, low, close}），輸出三態趨勢 UP/DOWN/FLAT
（年線斜率為主、FLAT 三症狀投票、遲滯確認）＋順大勢逆小勢的行動／持倉提示。
不用 pandas：以 stdlib list 忠實重寫。**門檻的單一真相在 DEFAULTS**；改任一條 bump RULE_VERSION。
fundamental_gate（內功閘門）由呼叫端給，無資料時 UNKNOWN（保守：不放行「關注建倉區」）。
"""

from dataclasses import dataclass, fields

# t1→t2 (2026-07-05): 改用還原股價。門檻皆為起始值須回測。
RULE_VERSION = "t2"

# 三態 + 資料不足態（UNKNOWN 表暖機不足，與真正 FLAT 區分）
UP, DOWN, FLAT, UNKNOWN = "UP", "DOWN", "FLAT", "UNKNOWN"
# fundamental_gate 取值
GATE_PASS, GATE_FAIL, GATE_UNKNOWN = "PASS", "FAIL", "UNKNOWN"
# 大勢顯示標籤與配色（與規則同處；windscope 的 explain／pages 取用）
TREND_LABELS = {UP: "上升趨勢", DOWN: "下降趨勢", FLAT: "震盪盤整", UNKNOWN: "資料不足"}
TREND_CSS = {UP: "up", DOWN: "down", FLAT: "flat", UNKNOWN: "flat"}

# 門檻單一真相（windscope settings 的 TREND_* 由此取值）
DEFAULTS = {
    "ma_big": 240,            # 年線（大勢方向）；台股慣例 240、A股 250
    "ma_center": 20,          # 月線＝價值中樞（源修正，非季線）
    "ma_fast": 5,             # 小勢回升偵測（5日）
    "slope_lookback": 20,     # 年線斜率量測窗
    "slope_deadband": 0.02,   # 走平死區
    "cross_window": 20,       # 穿越/箱型量測窗
    "cross_thresh": 4,        # 反復穿越月線次數門檻
    "box_eps": 0.06,          # 箱型寬門檻（不創新高低）
    "confirm_bars": 3,        # 遲滯確認（連續同態幾根才切換 state）
    "overbought_bias": 0.15,  # 超買乖離（源留白，須自訂+回測）
}


@dataclass
class TrendParams:
    """趨勢分類可調參數，預設＝DEFAULTS（可注入覆寫做回測）。"""

    ma_big: int = DEFAULTS["ma_big"]
    ma_center: int = DEFAULTS["ma_center"]
    ma_fast: int = DEFAULTS["ma_fast"]
    slope_lookback: int = DEFAULTS["slope_lookback"]
    slope_deadband: float = DEFAULTS["slope_deadband"]
    cross_window: int = DEFAULTS["cross_window"]
    cross_thresh: int = DEFAULTS["cross_thresh"]
    box_eps: float = DEFAULTS["box_eps"]
    confirm_bars: int = DEFAULTS["confirm_bars"]
    overbought_bias: float = DEFAULTS["overbought_bias"]

    @property
    def warmup(self) -> int:
        """年線斜率成立所需根數＝ma_big + slope_lookback。"""
        return self.ma_big + self.slope_lookback


assert {f.name for f in fields(TrendParams)} == set(DEFAULTS), "TrendParams 欄位須與 DEFAULTS 一致"


def sma(values: list[float], n: int) -> list[float | None]:
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

    ma_big = sma(closes, p.ma_big)
    ma_center = sma(closes, p.ma_center)

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
    供回測逐 bar 重放 production 訊號。O(n) 單趟。
    """
    if not bars:
        return []
    p = params or TrendParams()
    n = len(bars)
    closes = [b["close"] for b in bars]
    ma_center = sma(closes, p.ma_center)
    ma_fast = sma(closes, p.ma_fast)
    states, slopes = _classify_series(bars, p)
    warmup = p.warmup

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
