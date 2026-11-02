"""Playbook trend_pullback p1：窗口內第一根 1 分 K 收完以 close 進場、窗口邊界、跳空「今天不進」、停損（含跳空、跨日保留、
同根先停損再分批）、分批＋移停（breakeven／prev_bar_low＝前一日低、進場日退回 breakeven）、多層分批、跨日換日欄位、
非 regular K 棒略過（持倉中停損仍在）、只做多、出清即 exited、參數與一天一次的硬約束、run_days 長度、舊版狀態還原、決定性。"""

import copy

import pytest

from strategylib.playbooks import get_playbook, list_playbooks
from strategylib.playbooks.base import ARMED, ENTERED, EXITED, NO_TRADE, SCALED_OUT, PlanState
from strategylib.playbooks.trend_pullback import DEFAULT_PARAMS, TrendPullback, run_days

D1, D2 = "2026-06-15", "2026-06-16"
PLAN = {
    "playbook_id": "trend_pullback", "playbook_version": "p1",
    "params": dict(DEFAULT_PARAMS), "max_entries_per_day": 1,
    "entry": {"window_from": "09:00", "window_to": "13:00", "trigger": "x", "chase_limit_pts": None},
    "risk": {"r_pct": 1.0, "stop_pts": 10.0, "max_units": 3},
    "scaling": [{"at_r": 2.0, "exit_fraction": 0.33, "move_stop_to": "breakeven"}],
    "exit": {"runner_rule": "由新 target weight=0 表達", "force_flat_at": None},
    "no_trade": [], "discretion_allowed": ["early_exit", "reduce"],
}


def _ts(d: str, h: int, m: int) -> str:
    return f"{d}T{h:02d}:{m:02d}:00+08:00"


def _bar(d: str, h: int, m: int, o: float, hi: float, lo: float, c: float) -> dict:
    assert lo <= min(o, c) and hi >= max(o, c), "test bar must be a valid OHLC"
    return {"instrument_id": "TWSE:2330", "resolution": "1m", "session": "regular", "session_date": d,
            "ts": _ts(d, h, m), "open": o, "high": hi, "low": lo, "close": c, "volume": 1000,
            "source": "t", "published_at": _ts(d, h, m + 1), "ingested_at": _ts(d, h, m + 1), "revision": 0}


def _flat(d: str, h0: int, m0: int, n: int, px: float, wiggle: float = 1.0) -> list[dict]:
    out, h, m = [], h0, m0
    for _ in range(n):
        out.append(_bar(d, h, m, px, px + wiggle, px - wiggle, px))
        m += 1
        if m == 60:
            h, m = h + 1, 0
    return out


def test_registered_alongside_pullback_hold():
    assert ("trend_pullback", "p1") in list_playbooks() and ("pullback_hold", "p1") in list_playbooks()
    assert isinstance(get_playbook("trend_pullback", "p1"), TrendPullback)


def test_enters_at_close_of_first_completed_bar_in_window():
    day = [_bar(D1, 9, 0, 952, 953, 949, 951)] + _flat(D1, 9, 1, 2, 950)
    s, acts = run_days([day], PLAN, +1, [948.0])
    assert [a.kind for a in acts] == ["armed", "entered"]
    assert acts[1].price == 951 and acts[1].stop_price == 941 and acts[1].ts == _ts(D1, 9, 1)   # close，不是 open
    assert s.phase == ENTERED and s.entries == 1 and s.days_held == 0 and s.session_date == D1


def test_window_edges_and_rollover_to_next_day():
    plan = copy.deepcopy(PLAN); plan["entry"].update(window_from="09:30", window_to="10:00")
    day = _flat(D1, 9, 0, 29, 950) + [_bar(D1, 9, 29, 955, 957, 954, 956)]   # end 09:30 ＝ window_from → 進
    s, acts = run_days([day], plan, +1, [948.0])
    assert acts[-1].kind == "entered" and acts[-1].price == 956 and acts[-1].ts == _ts(D1, 9, 30)
    edge = [_bar(D1, 9, 59, 955, 957, 954, 956)]                              # 第一根就在 end 10:00 ＝ window_to → 還算在窗口
    s, acts = run_days([edge], plan, +1, [948.0])
    assert acts[-1].kind == "entered" and acts[-1].ts == _ts(D1, 10, 0)
    late = _flat(D1, 10, 0, 5, 950)                                             # end 10:01 起 → 今天不進、留 ARMED
    s, acts = run_days([late], plan, +1, [948.0])
    assert [a.kind for a in acts] == ["armed"] and s.phase == ARMED
    s, acts = run_days([late, _flat(D2, 9, 29, 1, 960)], plan, +1, [948.0, 950.0])
    assert acts[-1].kind == "entered" and acts[-1].price == 960 and s.session_date == D2 and s.days_held == 0


def test_gap_filter_off_by_default_skips_only_that_day_when_on():
    day = _flat(D1, 9, 0, 2, 1000)                                    # 開盤 1000 vs 前收 950 → +5.26%
    s, acts = run_days([day], PLAN, +1, [950.0])
    assert acts[-1].kind == "entered"                                 # 預設不過濾（與日 K 回測一致）
    plan = copy.deepcopy(PLAN); plan["params"]["gap_no_trade_pct"] = 3.0
    s, acts = run_days([day], plan, +1, [950.0])
    assert [a.kind for a in acts] == ["armed"] and s.phase == ARMED and "gap" in s.note   # 今天不進、不是 NO_TRADE
    s, acts = run_days([day, _flat(D2, 9, 0, 1, 1002)], plan, +1, [950.0, 1000.0])     # 隔天沒跳空 → 進
    assert acts[-1].kind == "entered" and acts[-1].ts == _ts(D2, 9, 1)
    s, acts = run_days([day], plan, +1, [None])                       # 沒前收 → 無法過濾，照進
    assert acts[-1].kind == "entered"


def test_stop_hit_gap_through_stop_and_stop_before_scaling_on_same_bar():
    day = _flat(D1, 9, 0, 3, 950) + [_bar(D1, 9, 3, 945, 946, 938, 939)]   # 停損 940，low 938
    s, acts = run_days([day], PLAN, +1, [948.0])
    assert acts[-1].kind == "exited" and acts[-1].reason == "stop" and acts[-1].price == 940
    assert acts[-1].r_multiple == -1.0 and s.phase == EXITED
    gap = _flat(D1, 9, 0, 3, 950) + [_bar(D1, 9, 3, 930, 932, 925, 928)]    # 跳空穿越 → 拿到 open 930
    s, acts = run_days([gap], PLAN, +1, [948.0])
    assert acts[-1].price == 930 and acts[-1].r_multiple == -2.0
    both = _flat(D1, 9, 0, 3, 950) + [_bar(D1, 9, 3, 950, 975, 938, 960)]   # 同根碰到 +2R 與停損 → 先停損
    s, acts = run_days([both], PLAN, +1, [948.0])
    assert [a.kind for a in acts] == ["armed", "entered", "exited"] and acts[-1].reason == "stop"


def test_entry_bar_is_not_managed_but_next_bar_is():
    """進場價是收盤，所以那根 K 的 low／high 是進場前的事；下一根起才算。"""
    day = _flat(D1, 9, 0, 1, 950) + [_bar(D1, 9, 1, 950, 975, 930, 950)]
    s, acts = run_days([day[:1]], PLAN, +1, [948.0])
    assert s.phase == ENTERED and s.stop_price == 940
    s, acts = run_days([day], PLAN, +1, [948.0])
    assert acts[-1].kind == "exited" and acts[-1].reason == "stop"       # 第二根 low 930 ≤ 940


def test_scale_out_moves_stop_breakeven_then_prev_bar_low_variants():
    day = _flat(D1, 9, 0, 3, 950) + [_bar(D1, 9, 3, 965, 972, 964, 970)]   # +2R＝970
    s, acts = run_days([day], PLAN, +1, [948.0])
    assert [a.kind for a in acts] == ["armed", "entered", "scaled_out", "stop_tightened"]
    assert acts[2].price == 970 and acts[2].fraction == 0.33 and acts[2].r_multiple == 2.0
    assert acts[3].stop_price == 950 and s.phase == SCALED_OUT and s.open_fraction == pytest.approx(0.67)
    plan = copy.deepcopy(PLAN); plan["scaling"][0]["move_stop_to"] = "prev_bar_low"   # 契約 enum 的值
    s, acts = run_days([day], plan, +1, [948.0])                                      # 進場日：沒有前一日 → breakeven
    assert acts[-1].kind == "stop_tightened" and acts[-1].stop_price == 950 and "breakeven" in acts[-1].reason
    d1 = _flat(D1, 9, 0, 5, 950)
    d2 = _flat(D2, 9, 0, 2, 955) + [_bar(D2, 9, 2, 965, 972, 964, 970)]
    s, acts = run_days([d1, d2], plan, +1, [948.0, 950.0])                             # 隔天 → 前一日最低 949
    assert acts[-1].kind == "stop_tightened" and acts[-1].stop_price == 949 and s.days_held == 1
    assert s.prev_day_low == 949 and s.prev_day_high == 951
    plan["scaling"][0]["move_stop_to"] = None                                          # 不移停
    s, acts = run_days([day], plan, +1, [948.0])
    assert acts[-1].kind == "scaled_out" and s.stop_price == 940


def test_multiple_scaling_tiers_in_one_bar():
    plan = copy.deepcopy(PLAN)
    plan["scaling"] = [{"at_r": 1.0, "exit_fraction": 0.5, "move_stop_to": "breakeven"},
                       {"at_r": 2.0, "exit_fraction": 0.5, "move_stop_to": None}]
    day = _flat(D1, 9, 0, 3, 950) + [_bar(D1, 9, 3, 955, 975, 954, 972)]       # 一根跨過 +1R(960) 與 +2R(970)
    s, acts = run_days([day], plan, +1, [948.0])
    assert [a.kind for a in acts] == ["armed", "entered", "scaled_out", "stop_tightened", "exited"]
    assert acts[2].price == 960 and acts[2].fraction == 0.5 and acts[4].price == 970 and acts[4].fraction == 0.5
    assert acts[4].reason == "scaled_out_all" and acts[4].r_multiple == 2.0 and s.phase == EXITED


def test_position_and_stop_carry_across_days_and_stop_next_day():
    d1 = _flat(D1, 9, 0, 5, 950)
    d2 = _flat(D2, 9, 0, 2, 948) + [_bar(D2, 9, 2, 944, 945, 939, 940)]     # 停損 940 保留到第二天
    s, acts = run_days([d1, d2], PLAN, +1, [948.0, 950.0])
    assert acts[-1].kind == "exited" and acts[-1].reason == "stop" and acts[-1].ts == _ts(D2, 9, 3)
    assert s.entries == 1 and s.phase == EXITED
    pb = TrendPullback()
    s1, _ = run_days([d1], PLAN, +1, [948.0])
    s2, a2 = pb.step(s1, d2[:1], PLAN, +1, 950.0)                       # 換日只重設當日欄位
    assert a2 == [] and s2.phase == ENTERED and s2.session_open == 948 and s2.day_low == 947 and s2.days_held == 1
    assert s2.stop_price == s1.stop_price == 940 and s2.entry_price == 950
    s3, _ = pb.step(s1, d2[:2], PLAN, +1, 950.0)                        # 當日高低以整天 bars_1m 計
    assert s3.day_low == 947 and s3.day_high == 949 and s3.days_held == 1


def test_non_regular_bars_are_skipped_even_while_holding():
    d1 = _flat(D1, 9, 0, 3, 950)
    s1, _ = run_days([d1], PLAN, +1, [948.0])
    pb = TrendPullback()
    night = _bar(D1, 15, 0, 900, 905, 895, 899); night["session"] = "night"
    s2, a2 = pb.step(s1, d1 + [night], PLAN, +1, 948.0)
    assert a2 == [] and s2 == s1                                        # 不動狀態、不發動作、部位仍受保護
    s3, a3 = pb.step(s2, _flat(D2, 9, 0, 1, 930), PLAN, +1, 950.0)      # 隔天 low 929 ≤ 940 → 停損照常
    assert a3[-1].kind == "exited" and a3[-1].reason == "stop"
    only_night = [dict(b, session="night") for b in _flat(D1, 15, 0, 3, 950)]
    s, acts = run_days([only_night], PLAN, +1, [948.0])
    assert acts == [] and s.phase == PlanState().phase                  # 沒有日盤 K 就什麼都不做


def test_exit_fraction_one_emits_single_exited_and_no_reentry_on_same_target():
    plan = copy.deepcopy(PLAN); plan["scaling"] = [{"at_r": 2.0, "exit_fraction": 1.0}]
    day = _flat(D1, 9, 0, 3, 950) + [_bar(D1, 9, 3, 965, 972, 964, 970)]
    s, acts = run_days([day], plan, +1, [948.0])
    assert [a.kind for a in acts] == ["armed", "entered", "exited"] and acts[-1].r_multiple == 2.0
    s2, a2 = TrendPullback().step(s, day + _flat(D1, 9, 4, 1, 950), plan, +1, 948.0)
    assert a2 == [] and s2.phase == EXITED                            # 出清後同一 target 不再進


def test_long_only_and_neutral():
    s, acts = run_days([_flat(D1, 9, 0, 2, 950)], PLAN, -1, [948.0])
    assert s.phase == NO_TRADE and "long-only" in acts[0].reason
    s, acts = run_days([_flat(D1, 9, 0, 2, 950)], PLAN, 0, [948.0])
    assert s.phase == NO_TRADE and acts[0].reason == "neutral"


def test_plan_hard_constraints_are_checked_on_every_step():
    plan = copy.deepcopy(PLAN); plan["params"]["center_ma"] = 20      # 日 K 趨勢的參數不屬於 Playbook
    with pytest.raises(KeyError):
        TrendPullback().step(PlanState(), _flat(D1, 15, 0, 1, 950), plan, -1, 948.0)   # 連窗口外／空方都擋
    plan = copy.deepcopy(PLAN); plan["max_entries_per_day"] = 2
    with pytest.raises(ValueError):
        run_days([_flat(D1, 9, 0, 2, 950)], plan, +1, [948.0])


def test_run_days_rejects_length_mismatch():
    with pytest.raises(ValueError):
        run_days([_flat(D1, 9, 0, 2, 950), _flat(D2, 9, 0, 2, 950)], PLAN, +1, [948.0])


def test_state_from_pre_0_2_dict_still_loads():
    """tradewind 會持久化 PlanState；0.1.0 時代沒有跨日欄位的 dict 必須還能還原。"""
    old = {"phase": "entered", "direction": 1, "range_high": None, "range_low": None, "session_open": 950.0,
           "session_start": "09:00", "bars_seen": 3, "confirm_count": 0, "entries": 1, "entry_price": 950.0,
           "stop_price": 940.0, "open_fraction": 1.0, "scaled": [], "prev_5m_low": None, "prev_5m_high": None,
           "high_water_r": 0.0, "note": None}
    s = PlanState.from_dict(old)
    assert s.session_date is None and s.days_held == 0 and s.prev_day_low is None
    s2, acts = TrendPullback().step(s, _flat(D2, 9, 0, 1, 930), PLAN, +1, 950.0)   # 當成新的一天接著管
    assert acts[-1].kind == "exited" and s2.days_held == 0


def test_step_is_pure_deterministic_and_serializable():
    day = _flat(D1, 9, 0, 3, 950)
    pb = get_playbook("trend_pullback", "p1")
    s0 = PlanState()
    snap = copy.deepcopy(s0.as_dict())
    s1, a1 = pb.step(s0, day, PLAN, +1, 948.0)
    assert s0.as_dict() == snap
    s2, a2 = pb.step(s0, day, PLAN, +1, 948.0)
    assert s1 == s2 and [a.as_dict() for a in a1] == [a.as_dict() for a in a2]
    assert PlanState.from_dict(s1.as_dict()) == s1
