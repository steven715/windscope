"""Playbook `trend_pullback` p1：個股趨勢訊號（strategy_id=trend_signal）的波段執行程式——開盤進場、停損、分批、跨日持有。

分工：**哪一天該進／加／減／出**由上游的日 K 趨勢引擎決定（strategylib.trend → windscope 的 target；
與 integration/backtest.py 同一套：關注建倉區→進、可加倉→加、減倉→減、大勢離開 UP→weight 0），
每一次變動都是一筆**新 target**（新 weight）。本 Playbook 只負責「拿到 target 之後，盤中怎麼成交、怎麼保護」：
1. 進場：target 生效後第一個日盤、進場窗口內的第一根收完的 1 分 K，以該根 **open** 成交（＝回測的「次日開盤成交」）。
   params.gap_no_trade_pct 有值且 |open/prev_close−1| 超過它 → no_trade_hit（預設 None＝不過濾，與回測一致）。
   只做多（direction ≤ 0 → no_trade_hit）；現股放空不在 p1。
2. 停損：進場價 − risk.stop_pts（現股單位＝元），每根 1 分 K 檢查 low；跳空穿越取該根 open；只能收緊；**跨日保留**。
3. 分批：high ≥ 進場 + at_r×stop_pts → 平 exit_fraction、依 move_stop_to 移停（breakeven／prev_day_low＝前一交易日最低）。
4. 出場：沒有 force_flat（波段抱過夜）。大勢轉向、失效條件（invalidation）都由**新 target weight=0** 表達，
   呼叫端（Trade Management）平倉並丟掉本狀態；Playbook 不自行判趨勢。停損或分批出清後 → exited，
   同一 target 不再進（要再進是上游發新 target 的事）。
5. 換日：bars_1m 是「本交易日日盤」的 1 分 K；session_date 變了就重設當日欄位、days_held+1，部位與停損不動。
**成交價模型**同 pullback_hold：Action.price 是觸發價位，滑價／手續費／股數由呼叫端處理。只支援 session=regular。
參數的單一真相在 DEFAULT_PARAMS；日 K 趨勢的門檻（月線、企穩根數、閘門）在 strategylib.trend.DEFAULTS，不在這裡。
改規則 bump VERSION。
"""

from strategylib.playbooks.base import (
    ARMED, ENTERED, EXITED, IDLE, NO_TRADE, SCALED_OUT, Action, PlanState, bar_end, hhmm,
)

PLAYBOOK_ID = "trend_pullback"
VERSION = "p1"

DEFAULT_PARAMS = {
    "gap_no_trade_pct": None,    # 開盤相對前收跳空超過此 % 不進；None＝不過濾（與日 K 回測一致）
}


def _p(plan: dict, key: str) -> object:
    """讀 trade_plan.params[key]，沒給用 DEFAULT_PARAMS；未知鍵 raise KeyError（參數鍵由本 playbook 定義並版本化）。"""
    params = plan.get("params") or {}
    for k in params:
        if k not in DEFAULT_PARAMS:
            raise KeyError(f"{PLAYBOOK_ID}@{VERSION} does not know param {k!r}; known: {sorted(DEFAULT_PARAMS)}")
    return params.get(key, DEFAULT_PARAMS[key])


class TrendPullback:
    """trend_pullback p1 的程式；無狀態物件，狀態全在 PlanState（跨日由呼叫端保存）。"""

    playbook_id = PLAYBOOK_ID
    version = VERSION

    def step(self, state: PlanState, bars_1m: list[dict], plan: dict, direction: int,
             prev_close: float | None) -> tuple[PlanState, list[Action]]:
        """處理 bars_1m[-1]（已收完的 1 分 K）。bars_1m 為**本交易日**日盤的 1 分 K（舊→新）。"""
        if not bars_1m:
            return state, []
        s = PlanState.from_dict(state.as_dict())
        bar = bars_1m[-1]
        end = bar_end(bar["ts"])
        now = hhmm(end)
        acts: list[Action] = []
        s.bars_seen = len(bars_1m)
        s.direction = s.direction or (1 if direction > 0 else -1 if direction < 0 else 0)

        if s.phase in (EXITED, NO_TRADE):
            return s, acts
        if s.direction <= 0:
            s.phase = NO_TRADE
            return s, [Action("no_trade_hit", end, reason="neutral" if s.direction == 0 else f"{VERSION} is long-only")]
        if bar.get("session", "regular") != "regular":
            s.phase = NO_TRADE
            return s, [Action("no_trade_hit", end, reason=f"session {bar.get('session')} not supported by {VERSION}")]

        # ── 換日／第一天：重設當日欄位，部位與停損不動 ────────────────────
        day = bar.get("session_date") or bar["ts"][:10]
        if s.session_date != day:
            if s.session_date is not None and s.phase in (ENTERED, SCALED_OUT):
                s.days_held += 1
            s.prev_day_low, s.prev_day_high = s.day_low, s.day_high
            s.session_date = day
            s.session_open = bars_1m[0]["open"]
            s.session_start = hhmm(bars_1m[0]["ts"])
            s.day_low, s.day_high = None, None
            if s.phase == IDLE:
                s.phase = ARMED
                acts.append(Action("armed", end, reason="target active; enter at first bar in window"))
        s.day_low = bar["low"] if s.day_low is None else min(s.day_low, bar["low"])
        s.day_high = bar["high"] if s.day_high is None else max(s.day_high, bar["high"])

        # ── 持倉中：停損 → 分批（＋移停） ───────────────────────────────
        if s.phase in (ENTERED, SCALED_OUT):
            stop_pts = plan["risk"]["stop_pts"]
            if bar["low"] <= s.stop_price:
                px = min(s.stop_price, bar["open"])                       # 跳空穿越＝拿到 open
                return self._exit(s, end, px, "stop", stop_pts, acts)
            for i, sc in enumerate(plan.get("scaling") or []):
                if i in s.scaled:
                    continue
                level = s.entry_price + sc["at_r"] * stop_pts
                if bar["high"] >= level:
                    px = max(level, bar["open"])
                    frac = min(sc["exit_fraction"], s.open_fraction)
                    s.scaled.append(i)
                    s.high_water_r = max(s.high_water_r, sc["at_r"])
                    if frac >= s.open_fraction:
                        return self._exit(s, end, px, "scaled_out_all", stop_pts, acts, r=sc["at_r"])
                    s.open_fraction = round(s.open_fraction - frac, 6)
                    acts.append(Action("scaled_out", end, price=px, fraction=frac, r_multiple=sc["at_r"],
                                       reason=f"+{sc['at_r']:g}R"))
                    new_stop = self._moved_stop(s, sc.get("move_stop_to"))
                    if new_stop is not None and new_stop > s.stop_price:
                        s.stop_price = new_stop
                        acts.append(Action("stop_tightened", end, stop_price=new_stop, reason=sc.get("move_stop_to")))
                    s.phase = SCALED_OUT
            return s, acts

        # ── ARMED：進場窗口內第一根收完的 1 分 K，以 open 成交 ──────────────
        entry = plan.get("entry") or {}
        w_from, w_to = entry.get("window_from", "00:00"), entry.get("window_to", "23:59")
        if not (w_from <= now <= w_to):
            return s, acts                                                # 今天窗口已過就等下一個交易日
        gap_pct = _p(plan, "gap_no_trade_pct")
        if gap_pct is not None and prev_close:
            gap = abs(s.session_open / prev_close - 1) * 100
            if gap > gap_pct:
                s.phase = NO_TRADE
                return s, acts + [Action("no_trade_hit", end, reason=f"gap {gap:.2f}% > {gap_pct}%")]
        price = bar["open"]
        s.entry_price = price
        s.stop_price = price - plan["risk"]["stop_pts"]
        s.open_fraction = 1.0
        s.entries += 1
        s.scaled = []
        s.days_held = 0
        s.phase = ENTERED
        acts.append(Action("entered", end, price=price, fraction=1.0, stop_price=s.stop_price,
                           reason="trend target active: buy at open of first bar in window"))
        return s, acts

    # ── helpers ───────────────────────────────────────────────────────
    @staticmethod
    def _moved_stop(s: PlanState, rule: str | None) -> float | None:
        if rule == "breakeven":
            return s.entry_price
        if rule == "prev_day_low":
            return s.prev_day_low
        return None

    @staticmethod
    def _exit(s: PlanState, end: str, price: float, reason: str, stop_pts: float,
              acts: list[Action], r: float | None = None) -> tuple[PlanState, list[Action]]:
        """全平 → exited（同一 target 不再進；再進是上游發新 target 的事）。"""
        if r is None and s.entry_price:
            r = round((price - s.entry_price) / stop_pts, 4)
        acts.append(Action("exited", end, price=price, fraction=s.open_fraction, reason=reason, r_multiple=r))
        s.open_fraction = 0.0
        s.entry_price = s.stop_price = None
        s.scaled = []
        s.phase = EXITED
        return s, acts


def run_days(days: list[list[dict]], plan: dict, direction: int, prev_closes: list[float | None],
             state: PlanState | None = None) -> tuple[PlanState, list[Action]]:
    """對多個交易日的 1 分 K 逐日逐根重放（回測／測試用）；prev_closes[i] 是第 i 天的前收。"""
    pb = TrendPullback()
    s = state or PlanState()
    acts: list[Action] = []
    for bars_1m, prev_close in zip(days, prev_closes):
        for i in range(1, len(bars_1m) + 1):
            s, a = pb.step(s, bars_1m[:i], plan, direction, prev_close)
            acts += a
            if s.phase in (EXITED, NO_TRADE):
                return s, acts
    return s, acts
