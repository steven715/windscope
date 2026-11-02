"""Playbook trend_pullback p1：開盤進場、窗口、跳空過濾（預設不過濾）、停損（含跳空、跨日保留）、分批＋移停
（breakeven／prev_day_low）、跨日換日欄位、只做多、出清即 exited、未知參數、決定性。手刻契約形狀的 1 分 K。"""

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


def test_enters_at_open_of_first_bar_in_window():
    day = _flat(D1, 9, 0, 3, 950)
    day[0]["open"] = 952
    s, acts = run_days([day], PLAN, +1, [948.0])
    assert [a.kind for a in acts] == ["armed", "entered"]
    assert acts[1].price == 952 and acts[1].stop_price == 942 and acts[1].ts == _ts(D1, 9, 1)
    assert s.phase == ENTERED and s.entries == 1 and s.days_held == 0 and s.session_date == D1


def test_window_waits_for_window_from_and_rolls_to_next_day_after_window_to():
    plan = copy.deepcopy(PLAN); plan["entry"].update(window_from="09:30", window_to="10:00")
    day = _flat(D1, 9, 0, 29, 950) + _flat(D1, 9, 29, 1, 955)      # 09:29 那根 end=09:30 → 第一根在窗口內
    s, acts = run_days([day], plan, +1, [948.0])
    assert acts[-1].kind == "entered" and acts[-1].price == 955 and acts[-1].ts == _ts(D1, 9, 30)
    late = _flat(D1, 10, 30, 5, 950)                                  # 今天窗口已過 → 不進、留 ARMED
    s, acts = run_days([late], plan, +1, [948.0])
    assert [a.kind for a in acts] == ["armed"] and s.phase == ARMED
    s, acts = run_days([late, _flat(D2, 9, 29, 1, 960)], plan, +1, [948.0, 950.0])   # 隔天窗口內進
    assert acts[-1].kind == "entered" and acts[-1].price == 960 and s.session_date == D2


def test_gap_filter_off_by_default_and_on_when_given():
    day = _flat(D1, 9, 0, 2, 1000)                                    # 開盤 1000 vs 前收 950 → +5.26%
    s, acts = run_days([day], PLAN, +1, [950.0])
    assert acts[-1].kind == "entered"                                 # 預設不過濾（與日 K 回測一致）
    plan = copy.deepcopy(PLAN); plan["params"]["gap_no_trade_pct"] = 3.0
    s, acts = run_days([day], plan, +1, [950.0])
    assert s.phase == NO_TRADE and acts[-1].kind == "no_trade_hit" and "gap" in acts[-1].reason
    s, acts = run_days([day], plan, +1, [None])                       # 沒前收 → 無法過濾，照進
    assert acts[-1].kind == "entered"


def test_stop_hit_and_gap_through_stop():
    day = _flat(D1, 9, 0, 3, 950) + [_bar(D1, 9, 3, 945, 946, 938, 939)]   # 停損 940，low 938
    s, acts = run_days([day], PLAN, +1, [948.0])
    assert acts[-1].kind == "exited" and acts[-1].reason == "stop" and acts[-1].price == 940
    assert acts[-1].r_multiple == -1.0 and s.phase == EXITED
    gap = _flat(D1, 9, 0, 3, 950) + [_bar(D1, 9, 3, 930, 932, 925, 928)]    # 跳空穿越 → 拿到 open 930
    s, acts = run_days([gap], PLAN, +1, [948.0])
    assert acts[-1].price == 930 and acts[-1].r_multiple == -2.0


def test_scale_out_at_2r_moves_stop_to_breakeven_then_prev_day_low_variant():
    day = _flat(D1, 9, 0, 3, 950) + [_bar(D1, 9, 3, 965, 972, 964, 970)]   # +2R＝970
    s, acts = run_days([day], PLAN, +1, [948.0])
    kinds = [a.kind for a in acts]
    assert kinds == ["armed", "entered", "scaled_out", "stop_tightened"]
    assert acts[2].price == 970 and acts[2].fraction == 0.33 and acts[2].r_multiple == 2.0
    assert acts[3].stop_price == 950 and s.phase == SCALED_OUT and s.open_fraction == pytest.approx(0.67)
    # 隔天拿到 2R → 移停到前一日最低（949）
    plan = copy.deepcopy(PLAN); plan["scaling"][0]["move_stop_to"] = "prev_day_low"
    d1 = _flat(D1, 9, 0, 5, 950)
    d2 = _flat(D2, 9, 0, 2, 955) + [_bar(D2, 9, 2, 965, 972, 964, 970)]
    s, acts = run_days([d1, d2], plan, +1, [948.0, 950.0])
    assert acts[-1].kind == "stop_tightened" and acts[-1].stop_price == 949 and s.days_held == 1
    assert s.prev_day_low == 949 and s.prev_day_high == 951


def test_position_and_stop_carry_across_days_and_stop_next_day():
    d1 = _flat(D1, 9, 0, 5, 950)
    d2 = _flat(D2, 9, 0, 2, 948) + [_bar(D2, 9, 2, 944, 945, 939, 940)]     # 停損 940 保留到第二天
    s, acts = run_days([d1, d2], PLAN, +1, [948.0, 950.0])
    assert acts[-1].kind == "exited" and acts[-1].reason == "stop" and acts[-1].ts == _ts(D2, 9, 3)
    assert s.entries == 1 and s.phase == EXITED
    # 換日只重設當日欄位：第二天第一根之後 session_open／day_low 是第二天的
    pb = TrendPullback()
    s1, _ = run_days([d1], PLAN, +1, [948.0])
    s2, a2 = pb.step(s1, d2[:1], PLAN, +1, 950.0)
    assert a2 == [] and s2.phase == ENTERED and s2.session_open == 948 and s2.day_low == 947 and s2.days_held == 1
    assert s2.stop_price == s1.stop_price == 940 and s2.entry_price == 950


def test_exit_fraction_one_emits_single_exited_and_no_reentry_on_same_target():
    plan = copy.deepcopy(PLAN); plan["scaling"] = [{"at_r": 2.0, "exit_fraction": 1.0}]
    day = _flat(D1, 9, 0, 3, 950) + [_bar(D1, 9, 3, 965, 972, 964, 970)]
    s, acts = run_days([day], plan, +1, [948.0])
    assert [a.kind for a in acts] == ["armed", "entered", "exited"] and acts[-1].r_multiple == 2.0
    s2, a2 = TrendPullback().step(s, day + _flat(D1, 9, 4, 1, 950), plan, +1, 948.0)
    assert a2 == [] and s2.phase == EXITED                            # 出清後同一 target 不再進


def test_long_only_neutral_and_night_session():
    s, acts = run_days([_flat(D1, 9, 0, 2, 950)], PLAN, -1, [948.0])
    assert s.phase == NO_TRADE and "long-only" in acts[0].reason
    s, acts = run_days([_flat(D1, 9, 0, 2, 950)], PLAN, 0, [948.0])
    assert s.phase == NO_TRADE and acts[0].reason == "neutral"
    night = _flat(D1, 15, 0, 2, 950)
    for b in night:
        b["session"] = "night"
    s, acts = run_days([night], PLAN, +1, [948.0])
    assert s.phase == NO_TRADE and "session" in acts[0].reason


def test_unknown_param_key_is_rejected():
    plan = copy.deepcopy(PLAN); plan["params"]["center_ma"] = 20      # 日 K 趨勢的參數不屬於 Playbook
    with pytest.raises(KeyError):
        run_days([_flat(D1, 9, 0, 2, 950)], plan, +1, [948.0])


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
