"""Playbook `pullback_hold` p1：回測開盤區間不破再上（日內，對應盤前判斷的 Trade Plan）。

規則（長；空單鏡像）：
1. 開盤區間＝日盤前 range_minutes 根 1 分 K 的高低。跳空 |open/prev_close−1| > gap_no_trade_pct% → no_trade。
2. 進場窗口 [entry.window_from, entry.window_to]，且 ≥ params.entry_skip_until、< params.no_entry_after：
   每根收完的 5 分 K，若「回測」開盤區間下緣（low ≤ range_low + pullback_band × 區間寬）且不破
   （close > range_low）且收高（close > open）→ 觸發；追價＝close − session_open > chase_limit_pts → 放棄這次。
3. 進場價＝觸發那根 5 分 K 的 close；停損＝進場價 − risk.stop_pts（只能收緊）。
4. 持倉中每根 1 分 K：先檢查停損（low ≤ stop → exit at stop），再檢查 scaling（high ≥ 進場 + at_r×stop_pts
   → 平 exit_fraction、停損依 move_stop_to 移動），最後 runner：收完的 5 分 K close < 前一根 5 分 K low → exit。
5. exit.force_flat_at 到了全平；max_entries_per_day 到了就 exited（不再進）。
參數的單一真相在 DEFAULT_PARAMS；契約的 trade_plan.params 覆蓋它。改規則 bump VERSION。
"""

from strategylib.playbooks.base import (
    ARMED, ENTERED, EXITED, IDLE, NO_TRADE, SCALED_OUT, Action, PlanState, bar_end, hhmm,
)

PLAYBOOK_ID = "pullback_hold"
VERSION = "p1"

DEFAULT_PARAMS = {
    "range_minutes": 5,          # 開盤區間根數（1 分 K）
    "pullback_band": 0.2,        # 「回測下緣」＝low 進到區間下緣 + 此比例×區間寬以內
    "confirm_bars": 1,           # 觸發需連續幾根 5 分 K 符合（v0＝1）
    "gap_no_trade_pct": 1.5,     # 開盤跳空超過此 % 不做
    "entry_skip_until": "09:05", # 此時刻前不進
    "no_entry_after": "11:30",   # 此時刻後不新進
}


def _p(plan: dict, key: str):
    return (plan.get("params") or {}).get(key, DEFAULT_PARAMS[key])


def _agg(bars: list[dict]) -> dict:
    """把連續 1 分 K 合成一根（5 分 K）。"""
    return {"ts": bars[0]["ts"], "open": bars[0]["open"], "close": bars[-1]["close"],
            "high": max(b["high"] for b in bars), "low": min(b["low"] for b in bars)}


class PullbackHold:
    """pullback_hold p1 的程式；無狀態物件，狀態全在 PlanState。"""

    playbook_id = PLAYBOOK_ID
    version = VERSION

    def step(self, state: PlanState, bars_1m: list[dict], plan: dict, direction: int,
             prev_close: float | None) -> tuple[PlanState, list[Action]]:
        """處理 bars_1m[-1]（已收完的 1 分 K）。bars_1m 為本交易日日盤的 1 分 K（舊→新）。"""
        if not bars_1m:
            return state, []
        s = PlanState.from_dict(state.as_dict())            # 不改動呼叫端物件
        bar = bars_1m[-1]
        end = bar_end(bar["ts"])
        acts: list[Action] = []
        s.bars_seen = len(bars_1m)
        d = s.direction or (1 if direction > 0 else -1 if direction < 0 else 0)
        s.direction = d

        if s.phase in (EXITED, NO_TRADE):
            return s, acts
        if d == 0:
            s.phase = NO_TRADE
            return s, [Action("no_trade", end, reason="neutral")]

        # ── 0. 開盤：跳空檢查、開盤區間 ────────────────────────────────
        if s.session_open is None:
            s.session_open = bars_1m[0]["open"]
            if prev_close:
                gap = abs(s.session_open / prev_close - 1) * 100
                if gap > _p(plan, "gap_no_trade_pct"):
                    s.phase = NO_TRADE
                    return s, [Action("no_trade", end, reason=f"gap {gap:.2f}% > {_p(plan, 'gap_no_trade_pct')}%")]
        rng = int(_p(plan, "range_minutes"))
        if s.phase == IDLE:
            if len(bars_1m) < rng:
                return s, acts
            first = _agg(bars_1m[:rng])
            s.range_high, s.range_low = first["high"], first["low"]
            s.phase = ARMED
            acts.append(Action("armed", bar_end(bars_1m[rng - 1]["ts"]), reason=f"range {s.range_low:g}-{s.range_high:g}"))
            if len(bars_1m) == rng:
                return s, acts

        force_at = (plan.get("exit") or {}).get("force_flat_at")
        now = hhmm(end)

        # ── 持倉中：停損 → 分批 → runner → 強制平倉 ──────────────────
        if s.phase in (ENTERED, SCALED_OUT):
            stop_pts = plan["risk"]["stop_pts"]
            hit = (bar["low"] <= s.stop_price) if d > 0 else (bar["high"] >= s.stop_price)
            if hit:
                return self._exit(s, end, s.stop_price, "stop", stop_pts, acts)
            for i, sc in enumerate(plan.get("scaling") or []):
                if i in s.scaled:
                    continue
                level = s.entry_price + d * sc["at_r"] * stop_pts
                reached = bar["high"] >= level if d > 0 else bar["low"] <= level
                if reached:
                    frac = min(sc["exit_fraction"], s.open_fraction)
                    s.open_fraction = round(s.open_fraction - frac, 6)
                    s.scaled.append(i)
                    s.high_water_r = max(s.high_water_r, sc["at_r"])
                    acts.append(Action("scale_out", end, price=level, fraction=frac, r_multiple=sc["at_r"],
                                       reason=f"+{sc['at_r']:g}R"))
                    new_stop = self._moved_stop(s, sc.get("move_stop_to"), d)
                    if new_stop is not None and (new_stop > s.stop_price if d > 0 else new_stop < s.stop_price):
                        s.stop_price = new_stop
                        acts.append(Action("tighten_stop", end, stop_price=new_stop, reason=sc.get("move_stop_to")))
                    s.phase = SCALED_OUT if s.open_fraction > 0 else EXITED
                    if s.phase == EXITED:
                        acts.append(Action("exit", end, price=level, fraction=0.0, reason="scaled_out_all",
                                           r_multiple=sc["at_r"]))
                        return s, acts
            if s.phase == SCALED_OUT and self._five_done(bars_1m):
                five = _agg(bars_1m[-5:])
                if s.prev_5m_low is not None:
                    broke = five["close"] < s.prev_5m_low if d > 0 else five["close"] > s.prev_5m_high
                    if broke:
                        return self._exit(s, end, five["close"], "runner", stop_pts, acts)
                s.prev_5m_low, s.prev_5m_high = five["low"], five["high"]
            if force_at and now >= force_at:
                return self._exit(s, end, bar["close"], "force_flat", stop_pts, acts)
            return s, acts

        # ── ARMED：在窗口內找觸發 ───────────────────────────────────
        if s.phase == ARMED:
            if force_at and now >= force_at:
                s.phase = EXITED
                acts.append(Action("expired", end, reason="force_flat_at without entry"))
                return s, acts
            if s.entries >= int(plan.get("max_entries_per_day", 1)):
                s.phase = EXITED
                acts.append(Action("expired", end, reason="max_entries_per_day"))
                return s, acts
            if not self._five_done(bars_1m):
                return s, acts
            five = _agg(bars_1m[-5:])
            entry = plan.get("entry") or {}
            w_from, w_to = entry.get("window_from", "00:00"), entry.get("window_to", "23:59")
            if now > w_to or now >= _p(plan, "no_entry_after"):
                s.phase = EXITED
                acts.append(Action("expired", end, reason="entry window closed"))
                return s, acts
            if now < w_from or now < _p(plan, "entry_skip_until"):
                s.prev_5m_low, s.prev_5m_high = five["low"], five["high"]
                return s, acts
            width = (s.range_high - s.range_low)
            band = _p(plan, "pullback_band") * width
            if d > 0:
                pulled = five["low"] <= s.range_low + band and five["close"] > s.range_low and five["close"] > five["open"]
            else:
                pulled = five["high"] >= s.range_high - band and five["close"] < s.range_high and five["close"] < five["open"]
            s.prev_5m_low, s.prev_5m_high = five["low"], five["high"]
            if not pulled:
                return s, acts
            price = five["close"]
            chase = entry.get("chase_limit_pts")
            if chase is not None and d * (price - s.session_open) > chase:
                acts.append(Action("no_trade", end, price=price, reason=f"chase {d * (price - s.session_open):g} > {chase:g}"))
                return s, acts
            s.entry_price = price
            s.stop_price = price - d * plan["risk"]["stop_pts"]
            s.open_fraction = 1.0
            s.entries += 1
            s.scaled = []
            s.phase = ENTERED
            acts.append(Action("enter", end, price=price, fraction=1.0, stop_price=s.stop_price,
                               reason="pullback to range edge, held, closed up" if d > 0 else
                               "pullback to range edge, held, closed down"))
            return s, acts
        return s, acts

    # ── helpers ───────────────────────────────────────────────────────
    @staticmethod
    def _five_done(bars_1m: list[dict]) -> bool:
        """最新 1 分 K 是否剛好收完一根 5 分 K（以日盤第一根為起點，每 5 根）。"""
        return len(bars_1m) % 5 == 0

    @staticmethod
    def _moved_stop(s: PlanState, rule: str | None, d: int) -> float | None:
        if rule == "breakeven":
            return s.entry_price
        if rule == "prev_bar_low" and d > 0:
            return s.prev_5m_low
        if rule == "prev_bar_high" and d < 0:
            return s.prev_5m_high
        return None

    @staticmethod
    def _exit(s: PlanState, end: str, price: float, reason: str, stop_pts: float,
              acts: list[Action]) -> tuple[PlanState, list[Action]]:
        r = (price - s.entry_price) / stop_pts * s.direction if s.entry_price else None
        acts.append(Action("exit", end, price=price, fraction=s.open_fraction, reason=reason,
                           r_multiple=round(r, 4) if r is not None else None))
        s.open_fraction = 0.0
        s.phase = EXITED
        return s, acts


def run_session(bars_1m: list[dict], plan: dict, direction: int, prev_close: float | None,
                state: PlanState | None = None) -> tuple[PlanState, list[Action]]:
    """對整個交易日的 1 分 K 逐根重放（回測／測試用），回傳最終狀態與全部動作。"""
    pb = PullbackHold()
    s = state or PlanState()
    acts: list[Action] = []
    for i in range(1, len(bars_1m) + 1):
        s, a = pb.step(s, bars_1m[:i], plan, direction, prev_close)
        acts += a
        if s.phase in (EXITED, NO_TRADE):
            break
    return s, acts
