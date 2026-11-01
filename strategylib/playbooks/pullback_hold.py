"""Playbook `pullback_hold` p1：回測開盤區間不破再上（日盤日內，對應盤前判斷的 Trade Plan）。

規則（長；空單鏡像）：
1. 開盤區間＝日盤開盤起 range_minutes 分鐘內（依**時刻**，不是根數）的高低。
   跳空 |open/prev_close−1| > gap_no_trade_pct% → no_trade_hit。
2. 5 分 K 依時刻分桶（HH:MM 的 minute//5），桶的最後一分鐘（minute%5==4）收完才評估；缺一根 1 分 K 不會錯位。
   進場窗口 [entry.window_from, entry.window_to]，且 ≥ params.entry_skip_until、< params.no_entry_after，
   且桶的開始 ≥ 開盤區間結束：桶「回測」區間下緣（low ≤ range_low + pullback_band×寬）且不破（close > range_low）
   且收高（close > open）→ 連續 confirm_bars 個桶符合才觸發；追價 close − session_open > chase_limit_pts → chase_abandoned。
3. 進場價＝觸發那桶的 close；停損＝進場價 − risk.stop_pts（只能收緊）。
4. 持倉中每根 1 分 K：先停損（low ≤ stop），再 scaling（high ≥ 進場 + at_r×stop_pts → 平 exit_fraction、依 move_stop_to 移停），
   每個收完的桶更新「前一桶高低」；scaled_out 後 runner：桶 close < 前一桶 low → exited。
5. exit.force_flat_at 到了 force_flat；出場後 entries < max_entries_per_day 回 armed，否則 exited。
**成交價模型**：Action.price 是觸發價位；停損／分批若被跳空穿越，取該根 open（對自己不利的一邊）。
滑價、手續費由呼叫端的撮合模型加，Playbook 不管。只支援 session=regular（夜盤回 no_trade_hit）。
參數的單一真相在 DEFAULT_PARAMS；契約 trade_plan.params 覆蓋。改規則 bump VERSION。
"""

from strategylib.playbooks.base import (
    ARMED, ENTERED, EXITED, IDLE, NO_TRADE, SCALED_OUT, Action, PlanState, bar_end, hhmm,
)

PLAYBOOK_ID = "pullback_hold"
VERSION = "p1"

DEFAULT_PARAMS = {
    "range_minutes": 5,          # 開盤區間長度（分鐘，依時刻）
    "pullback_band": 0.2,        # 「回測下緣」＝low 進到區間下緣 + 此比例×區間寬以內
    "confirm_bars": 1,           # 觸發需連續幾個 5 分 K 桶符合
    "gap_no_trade_pct": 1.5,     # 開盤跳空超過此 % 不做
    "entry_skip_until": "09:05", # 此時刻前不進
    "no_entry_after": "11:30",   # 此時刻後不新進（含）
}
BUCKET_MIN = 5


def _p(plan: dict, key: str) -> object:
    """讀 trade_plan.params[key]，沒給用 DEFAULT_PARAMS（契約只傳覆蓋值）；未知鍵 raise KeyError。"""
    return (plan.get("params") or {}).get(key, DEFAULT_PARAMS[key])


def _minute_of_day(ts: str) -> int:
    return int(ts[11:13]) * 60 + int(ts[14:16])


def _add_min(hhmm_str: str, minutes: int) -> str:
    total = int(hhmm_str[:2]) * 60 + int(hhmm_str[3:5]) + minutes
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def _agg(bars: list[dict]) -> dict:
    """把連續 1 分 K 合成一根。"""
    return {"ts": bars[0]["ts"], "open": bars[0]["open"], "close": bars[-1]["close"],
            "high": max(b["high"] for b in bars), "low": min(b["low"] for b in bars)}


def _bucket_done(bars_1m: list[dict]) -> dict | None:
    """最新 1 分 K 若是 5 分桶的最後一分鐘（minute%5==4），回該桶（同桶的所有 1 分 K 合成）；否則 None。"""
    last = bars_1m[-1]
    m = _minute_of_day(last["ts"])
    if m % BUCKET_MIN != BUCKET_MIN - 1:
        return None
    key = m // BUCKET_MIN
    members = [b for b in bars_1m if _minute_of_day(b["ts"]) // BUCKET_MIN == key]
    out = _agg(members)
    start = key * BUCKET_MIN                                      # 桶的名目起點（缺第一根也不會偏）
    out["ts"] = f"{last['ts'][:11]}{start // 60:02d}:{start % 60:02d}{last['ts'][16:]}"
    return out


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
        now = hhmm(end)
        acts: list[Action] = []
        s.bars_seen = len(bars_1m)
        s.direction = s.direction or (1 if direction > 0 else -1 if direction < 0 else 0)
        d = s.direction

        if s.phase in (EXITED, NO_TRADE):
            return s, acts
        if d == 0:
            s.phase = NO_TRADE
            return s, [Action("no_trade_hit", end, reason="neutral")]
        if bar.get("session", "regular") != "regular":
            s.phase = NO_TRADE
            return s, [Action("no_trade_hit", end, reason=f"session {bar.get('session')} not supported by {VERSION}")]

        # ── 0. 開盤：跳空檢查、開盤區間（依時刻） ─────────────────────────
        if s.session_open is None:
            s.session_open = bars_1m[0]["open"]
            s.session_start = hhmm(bars_1m[0]["ts"])
            if prev_close:
                gap = abs(s.session_open / prev_close - 1) * 100
                if gap > _p(plan, "gap_no_trade_pct"):
                    s.phase = NO_TRADE
                    return s, [Action("no_trade_hit", end, reason=f"gap {gap:.2f}% > {_p(plan, 'gap_no_trade_pct')}%")]
            else:
                s.note = "prev_close missing: gap filter skipped"
        range_end = _add_min(s.session_start, int(_p(plan, "range_minutes")))
        if s.phase == IDLE:
            if now < range_end:
                return s, acts
            first = _agg([b for b in bars_1m if hhmm(b["ts"]) < range_end])
            s.range_high, s.range_low = first["high"], first["low"]
            s.phase = ARMED
            acts.append(Action("armed", end, reason=f"range {s.range_low:g}-{s.range_high:g}"))

        force_at = (plan.get("exit") or {}).get("force_flat_at")
        bucket = _bucket_done(bars_1m)

        # ── 持倉中：停損 → 分批 → 記桶 → runner → 強制平倉 ─────────────
        if s.phase in (ENTERED, SCALED_OUT):
            stop_pts = plan["risk"]["stop_pts"]
            if (bar["low"] <= s.stop_price) if d > 0 else (bar["high"] >= s.stop_price):
                # 跳空穿越停損時成交在 open（對自己不利的一邊）
                px = min(s.stop_price, bar["open"]) if d > 0 else max(s.stop_price, bar["open"])
                return self._exit(s, end, px, "stop", stop_pts, plan, acts)
            for i, sc in enumerate(plan.get("scaling") or []):
                if i in s.scaled:
                    continue
                level = s.entry_price + d * sc["at_r"] * stop_pts
                if (bar["high"] >= level) if d > 0 else (bar["low"] <= level):
                    px = max(level, bar["open"]) if d > 0 else min(level, bar["open"])   # 跳空過關卡＝拿到 open
                    frac = min(sc["exit_fraction"], s.open_fraction)
                    s.scaled.append(i)
                    s.high_water_r = max(s.high_water_r, sc["at_r"])
                    if frac >= s.open_fraction:                       # 全部出清＝一筆 exited
                        return self._exit(s, end, px, "scaled_out_all", stop_pts, plan, acts, r=sc["at_r"])
                    s.open_fraction = round(s.open_fraction - frac, 6)
                    acts.append(Action("scaled_out", end, price=px, fraction=frac, r_multiple=sc["at_r"],
                                       reason=f"+{sc['at_r']:g}R"))
                    new_stop = self._moved_stop(s, sc.get("move_stop_to"), d)
                    if new_stop is not None and ((new_stop > s.stop_price) if d > 0 else (new_stop < s.stop_price)):
                        s.stop_price = new_stop
                        acts.append(Action("stop_tightened", end, stop_price=new_stop, reason=sc.get("move_stop_to")))
                    s.phase = SCALED_OUT
            if bucket is not None:
                if s.phase == SCALED_OUT and s.prev_5m_low is not None:
                    broke = (bucket["close"] < s.prev_5m_low) if d > 0 else (bucket["close"] > s.prev_5m_high)
                    if broke:
                        return self._exit(s, end, bucket["close"], "runner", stop_pts, plan, acts)
                s.prev_5m_low, s.prev_5m_high = bucket["low"], bucket["high"]   # 每桶都更新（含 ENTERED）
            if force_at and now >= force_at:
                return self._exit(s, end, bar["close"], "force_flat", stop_pts, plan, acts, force=True)
            return s, acts

        # ── ARMED：在窗口內找觸發 ───────────────────────────────────
        if force_at and now >= force_at:
            s.phase = EXITED
            acts.append(Action("expired", end, reason="force_flat_at without entry"))
            return s, acts
        if bucket is None:
            return s, acts
        entry = plan.get("entry") or {}
        w_from, w_to = entry.get("window_from", "00:00"), entry.get("window_to", "23:59")
        if now > w_to or now >= _p(plan, "no_entry_after"):           # 窗口：window_to 含、no_entry_after 不含
            s.phase = EXITED
            acts.append(Action("expired", end, reason="entry window closed"))
            return s, acts
        bucket_start = hhmm(bucket["ts"])
        in_window = (now >= w_from and now >= _p(plan, "entry_skip_until") and bucket_start >= range_end)
        width = s.range_high - s.range_low
        band = _p(plan, "pullback_band") * width
        if d > 0:
            pulled = bucket["low"] <= s.range_low + band and bucket["close"] > s.range_low and bucket["close"] > bucket["open"]
        else:
            pulled = bucket["high"] >= s.range_high - band and bucket["close"] < s.range_high and bucket["close"] < bucket["open"]
        s.prev_5m_low, s.prev_5m_high = bucket["low"], bucket["high"]
        if not in_window:
            s.confirm_count = 0
            return s, acts
        s.confirm_count = s.confirm_count + 1 if pulled else 0
        if s.confirm_count < int(_p(plan, "confirm_bars")):
            return s, acts
        s.confirm_count = 0
        price = bucket["close"]
        chase = entry.get("chase_limit_pts")
        if chase is not None and d * (price - s.session_open) > chase:
            acts.append(Action("chase_abandoned", end, price=price,
                               reason=f"chase {d * (price - s.session_open):g} > {chase:g}"))
            return s, acts
        s.entry_price = price
        s.stop_price = price - d * plan["risk"]["stop_pts"]
        s.open_fraction = 1.0
        s.entries += 1
        s.scaled = []
        s.phase = ENTERED
        acts.append(Action("entered", end, price=price, fraction=1.0, stop_price=s.stop_price,
                           reason="pullback to range edge, held, closed " + ("up" if d > 0 else "down")))
        return s, acts

    # ── helpers ───────────────────────────────────────────────────────
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
    def _exit(s: PlanState, end: str, price: float, reason: str, stop_pts: float, plan: dict,
              acts: list[Action], r: float | None = None, force: bool = False) -> tuple[PlanState, list[Action]]:
        """全平；之後 entries < max_entries_per_day 且非強制平倉 → 回 armed，否則 exited。"""
        if r is None and s.entry_price:
            r = round((price - s.entry_price) / stop_pts * s.direction, 4)
        kind = "force_flat" if force else "exited"
        acts.append(Action(kind, end, price=price, fraction=s.open_fraction, reason=reason, r_multiple=r))
        s.open_fraction = 0.0
        s.entry_price = s.stop_price = None
        s.scaled = []
        if not force and s.entries < int(plan.get("max_entries_per_day", 1)):
            s.phase = ARMED
        else:
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
