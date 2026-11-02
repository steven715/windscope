"""golden：共用庫的趨勢引擎與還原，輸出必須與重構前（tests/fixtures/strategylib/trend_golden.json）逐根相同。

fixture 由重構前的 integration.trend_signal／price_adjust 產生（700 根合成日K，含分割缺口與三次配息，
三態都出現）。任何改變輸出的修改都會讓這裡紅——那就是該 bump RULE_VERSION 並重產 golden 的時候。
"""

import json
import math
from pathlib import Path

import pytest

from strategylib.adjust import back_adjust_with_events
from strategylib.trend import GATE_PASS, GATE_UNKNOWN, TrendParams, evaluate, evaluate_series

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "strategylib" / "trend_golden.json"


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_golden_covers_all_states(golden):
    states = {s["big_trend"] for s in golden["signals_gate_pass"]}
    assert states == {"UNKNOWN", "FLAT", "UP", "DOWN"}
    assert "關注建倉區" in {s["action_hint"] for s in golden["signals_gate_pass"]}


def test_adjust_matches_golden(golden):
    adj, events = back_adjust_with_events(golden["bars"], golden["div_events"])
    assert [(e["date"], e["kind"]) for e in events] == [(e["date"], e["kind"]) for e in golden["adjust_events"]]
    for got, want in zip(adj, golden["adjusted_closes"]):
        assert math.isclose(got["close"], want, abs_tol=1e-5)   # fixture 存 6 位小數


@pytest.mark.parametrize("key,gate", [("signals_gate_unknown", GATE_UNKNOWN), ("signals_gate_pass", GATE_PASS)])
def test_trend_matches_golden(golden, key, gate):
    adj, _ = back_adjust_with_events(golden["bars"], golden["div_events"])
    assert TrendParams().__dict__ == golden["params"]           # 門檻沒變
    got = evaluate_series(adj, TrendParams(), gate)
    assert len(got) == len(golden[key])
    for i, (g, w) in enumerate(zip(got, golden[key])):
        assert g == w, f"bar {i} ({golden['bars'][i]['date']}) differs: {g} != {w}"
    assert evaluate(adj, TrendParams(), gate) == golden[key][-1]


def test_integration_layer_reexports_same_objects():
    """windscope 的 integration.trend_signal／price_adjust 只是薄層：同一個函式物件。"""
    import integration.price_adjust as pa
    import integration.trend_signal as ts
    import strategylib.adjust as sa
    import strategylib.trend as st

    assert ts.evaluate_series is st.evaluate_series and ts.TrendParams is st.TrendParams
    assert pa.back_adjust_with_events is sa.back_adjust_with_events
