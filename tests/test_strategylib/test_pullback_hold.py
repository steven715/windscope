"""Playbook pullback_hold p1：觸發、追價放棄、跳空不做、停損、分批＋移停、runner 出場、強制平倉、
一天一次、窗口、空單鏡像、決定性。全部用手刻 1 分 K（契約 bars 形狀）。"""

import copy

import pytest

from strategylib.playbooks import get_playbook, list_playbooks
from strategylib.playbooks.base import ARMED, ENTERED, EXITED, NO_TRADE, SCALED_OUT, PlanState
from strategylib.playbooks.pullback_hold import DEFAULT_PARAMS, PullbackHold, run_session

D = "2026-06-12"
PLAN = {
    "playbook_id": "pullback_hold", "playbook_version": "p1",
    "params": dict(DEFAULT_PARAMS), "max_entries_per_day": 1,
    "entry": {"window_from": "09:05", "window_to": "10:30", "trigger": "x", "chase_limit_pts": 40},
    "risk": {"r_pct": 1.0, "stop_pts": 30, "max_units": 10},
    "scaling": [{"at_r": 1.0, "exit_fraction": 0.5, "move_stop_to": "breakeven"}],
    "exit": {"runner_rule": "x", "force_flat_at": "13:25"},
    "no_trade": [], "discretion_allowed": ["early_exit"],
}


def _ts(h: int, m: int) -> str:
    return f"{D}T{h:02d}:{m:02d}:00+08:00"


def _bar(h: int, m: int, o: float, hi: float, lo: float, c: float) -> dict:
    return {"instrument_id": "TAIFEX:TMF", "resolution": "1m", "session": "regular", "session_date": D,
            "ts": _ts(h, m), "open": o, "high": hi, "low": lo, "close": c, "volume": 10,
            "source": "t", "published_at": _ts(h, m + 1), "ingested_at": _ts(h, m + 1), "revision": 0}


def _flat(h0: int, m0: int, n: int, px: float, wiggle: float = 2.0) -> list[dict]:
    """n 根橫盤 1 分 K。"""
    out = []
    h, m = h0, m0
    for _ in range(n):
        out.append(_bar(h, m, px, px + wiggle, px - wiggle, px))
        m += 1
        if m == 60:
            h, m = h + 1, 0
    return out


def _day_long_entry() -> tuple[list[dict], float]:
    """08:45 開盤區間 23000–23020；09:05–09:10 回測下緣不破收高 → 觸發 enter @ 23012。"""
    bars = _flat(8, 45, 5, 23010, 10)               # 區間 23000–23020（08:45–08:49）
    bars += _flat(8, 50, 15, 23030, 3)              # 08:50–09:04：遠離下緣，不觸發
    pull = _flat(9, 5, 5, 23004, 1)                 # 09:05–09:09：回測到下緣
    pull[0]["open"], pull[0]["low"] = 23008, 23001  # low 進到 23000+0.2×20=23004 以內
    pull[-1]["close"] = 23012                       # 5 分 K 收高、不破
    bars += pull
    return bars, 23000.0


def test_registry():
    assert ("pullback_hold", "p1") in list_playbooks()
    assert isinstance(get_playbook("pullback_hold", "p1"), PullbackHold)
    with pytest.raises(KeyError):
        get_playbook("pullback_hold", "p9")


def test_long_entry_then_scale_out_breakeven_and_runner_exit():
    bars, prev_close = _day_long_entry()
    s, acts = run_session(bars, PLAN, +1, prev_close)
    kinds = [a.kind for a in acts]
    assert kinds == ["armed", "enter"] and s.phase == ENTERED
    enter = acts[-1]
    assert enter.price == 23012 and enter.stop_price == 23012 - 30 and enter.ts == _ts(9, 10)
    # +1R（23042）：分批 0.5、停損移成本
    more = _flat(9, 10, 4, 23030, 3) + [_bar(9, 14, 23035, 23045, 23030, 23040)]
    s, acts = run_session(bars + more, PLAN, +1, prev_close)
    kinds = [a.kind for a in acts]
    assert kinds == ["armed", "enter", "scale_out", "tighten_stop"]
    assert acts[2].price == 23042 and acts[2].fraction == 0.5 and acts[2].r_multiple == 1.0
    assert acts[3].stop_price == 23012 and s.phase == SCALED_OUT and s.open_fraction == 0.5
    # runner：下一根 5 分 K（09:15–09:19）收在前一根 5 分 K 低點（09:10–09:14 的 low＝23027）之下 → exit
    runner = _flat(9, 15, 5, 23032, 2)
    runner[-1]["close"] = 23020
    s, acts = run_session(bars + more + runner, PLAN, +1, prev_close)
    assert [a.kind for a in acts][-1] == "exit" and acts[-1].reason == "runner"
    assert acts[-1].price == 23020 and acts[-1].fraction == 0.5 and s.phase == EXITED
    assert acts[-1].r_multiple == pytest.approx((23020 - 23012) / 30, abs=1e-4)
    # 沒跌破前一根 5 分 K 低點就不出
    runner[-1]["close"] = 23028
    s, acts = run_session(bars + more + runner, PLAN, +1, prev_close)
    assert acts[-1].kind == "tighten_stop" and s.phase == SCALED_OUT


def test_stop_hit_exits_at_stop_price():
    bars, prev_close = _day_long_entry()
    crash = _flat(9, 10, 3, 23000, 1) + [_bar(9, 13, 22995, 22996, 22975, 22980)]
    s, acts = run_session(bars + crash, PLAN, +1, prev_close)
    assert acts[-1].kind == "exit" and acts[-1].reason == "stop" and acts[-1].price == 22982
    assert acts[-1].r_multiple == -1.0 and s.phase == EXITED and s.open_fraction == 0


def test_chase_limit_abandons_trigger_but_stays_armed():
    bars = _flat(8, 45, 5, 23010, 10) + _flat(8, 50, 15, 23030, 3)
    pull = _flat(9, 5, 5, 23004, 1)
    pull[0]["low"] = 23001
    pull[-1]["close"] = 23060                       # 比開盤 23010 高 50 > 40
    s, acts = run_session(bars + pull, PLAN, +1, 23000.0)
    assert [a.kind for a in acts] == ["armed", "no_trade"] and "chase" in acts[-1].reason
    assert s.phase == ARMED and s.entries == 0


def test_gap_no_trade():
    bars = _flat(8, 45, 5, 23400, 5)                # 開盤 23400 vs 前收 23000 → +1.74%
    s, acts = run_session(bars, PLAN, +1, 23000.0)
    assert s.phase == NO_TRADE and acts[0].kind == "no_trade" and "gap" in acts[0].reason


def test_neutral_direction_is_no_trade():
    s, acts = run_session(_flat(8, 45, 6, 23000), PLAN, 0, 23000.0)
    assert s.phase == NO_TRADE and acts[0].reason == "neutral"


def test_entry_window_and_skip_until():
    bars = _flat(8, 45, 5, 23010, 10)
    early = _flat(8, 50, 5, 23004, 1); early[0]["low"] = 23001; early[-1]["close"] = 23012   # 09:05 前
    s, acts = run_session(bars + early, PLAN, +1, 23000.0)
    assert [a.kind for a in acts] == ["armed"] and s.phase == ARMED                           # 不觸發
    late = _flat(8, 55, 160, 23030, 3)                                                        # 到 11:35
    s, acts = run_session(bars + late, PLAN, +1, 23000.0)
    assert s.phase == EXITED and acts[-1].kind == "expired" and "window" in acts[-1].reason


def test_force_flat_and_single_entry_per_day():
    bars, prev_close = _day_long_entry()
    hold = _flat(9, 10, 50, 23020, 3)                                      # 09:10–09:59 持有
    hold += _flat(10, 0, 60, 23020, 3) + _flat(11, 0, 60, 23020, 3) + _flat(12, 0, 60, 23020, 3) + _flat(13, 0, 26, 23020, 3)
    s, acts = run_session(bars + hold, PLAN, +1, prev_close)
    # 13:24–13:25 那根收完（end 13:25）就平，不會拖到 13:26
    assert acts[-1].kind == "exit" and acts[-1].reason == "force_flat" and acts[-1].ts == _ts(13, 25)
    assert s.phase == EXITED and s.entries == 1
    # 出場後再來一次觸發：已 EXITED，不再動作
    pb = PullbackHold()
    s2, a2 = pb.step(s, bars + hold + _flat(13, 26, 1, 23020), PLAN, +1, prev_close)
    assert a2 == [] and s2.phase == EXITED


def test_short_side_mirrors_long():
    bars = _flat(8, 45, 5, 23010, 10) + _flat(8, 50, 15, 22990, 3)
    pull = _flat(9, 5, 5, 23016, 1)
    pull[0]["high"] = 23019                         # 回測上緣（23020−4=23016 以上）
    pull[-1]["close"] = 23008                       # 收低、不破上緣
    s, acts = run_session(bars + pull, PLAN, -1, 23000.0)
    assert acts[-1].kind == "enter" and acts[-1].stop_price == 23008 + 30
    crash_up = _flat(9, 10, 3, 23010, 1) + [_bar(9, 13, 23030, 23045, 23025, 23040)]
    s, acts = run_session(bars + pull + crash_up, PLAN, -1, 23000.0)
    assert acts[-1].reason == "stop" and acts[-1].price == 23038 and acts[-1].r_multiple == -1.0


def test_step_is_pure_and_deterministic():
    bars, prev_close = _day_long_entry()
    pb = get_playbook("pullback_hold", "p1")
    s0 = PlanState()
    snapshot = copy.deepcopy(s0.as_dict())
    s1, a1 = pb.step(s0, bars, PLAN, +1, prev_close)
    assert s0.as_dict() == snapshot                 # 不改動輸入狀態
    s2, a2 = pb.step(s0, bars, PLAN, +1, prev_close)
    assert s1.as_dict() == s2.as_dict() and [a.as_dict() for a in a1] == [a.as_dict() for a in a2]
    assert PlanState.from_dict(s1.as_dict()) == s1  # 可序列化往返
