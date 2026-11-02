"""時點正確性：還原是尺度變換，t 之後才知道的配息事件不會改變 ≤t 的趨勢輸出。

回測一次用「全部事件」還原整段再逐根評估；嚴格的 point-in-time 是在每個 t 只用 published_at ≤ t
的事件。這裡證明兩者對趨勢引擎的輸出完全相同（類別欄位相等、數值欄位在四捨五入容忍內），
所以 integration/backtest 用整段還原序列不構成前視偏差。
"""

import json
from pathlib import Path

import pytest

from strategylib.adjust import back_adjust_with_events
from strategylib.trend import GATE_PASS, TrendParams, evaluate_series

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "strategylib" / "trend_golden.json"
_CATEGORICAL = ("big_trend", "small_state", "small_turn_up", "fundamental_gate", "action_hint",
                "position_hint", "flat_redirect", "reasons")
_RATIO = ("big_trend_confidence", "bias_pct")


@pytest.fixture(scope="module")
def data() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _pit_signals_at(bars: list[dict], divs: list[dict], t: int) -> dict:
    """只用 ≤t 的 K 線與 ex_date ≤ t 日期的事件，還原後評估，回傳第 t 根的輸出。"""
    known = [d for d in divs if d["ex_date"] <= bars[t]["date"]]
    adj, _ = back_adjust_with_events(bars[:t + 1], known)
    return evaluate_series(adj, TrendParams(), GATE_PASS)[-1]


def test_future_dividends_do_not_change_past_outputs(data):
    bars, divs = data["bars"], data["div_events"]
    full_adj, _ = back_adjust_with_events(bars, divs)
    full = evaluate_series(full_adj, TrendParams(), GATE_PASS)
    # 檢查點：每個配息事件前後各幾根、暖機後第一根、最後一根
    idx = {bars.index(next(b for b in bars if b["date"] == d["ex_date"])) for d in divs}
    checks = sorted({i + k for i in idx for k in (-3, -1, 0, 1, 3)} | {261, 300, 420, len(bars) - 1})
    for t in checks:
        pit = _pit_signals_at(bars, divs, t)
        for k in _CATEGORICAL:
            assert pit[k] == full[t][k], f"t={t} {k}: {pit[k]} != {full[t][k]}"
        for k in _RATIO:
            a, b = pit[k], full[t][k]
            assert (a is None and b is None) or abs(a - b) <= 1e-4, f"t={t} {k}: {a} != {b}"
        # value_center 是絕對價位，會隨尺度變（這正是「只用當時已知事件」的差異），不比


def test_value_center_scales_by_future_factors_and_nothing_else(data):
    """反證：用整段（含未來事件）還原 vs 只用 ≤t 事件還原，第 t 根的 value_center 恰差「t 之後所有 factor 的乘積」，
    其餘欄位不變——證明未來事件對 ≤t 的影響只是一個尺度常數。"""
    bars, divs = data["bars"], data["div_events"]
    full_adj, _ = back_adjust_with_events(bars, divs)
    full = evaluate_series(full_adj, TrendParams(), GATE_PASS)
    differed = 0
    for t in (300, 420, 600):                           # 都在最後一次配息（2025-11-27）之前
        pit = _pit_signals_at(bars, divs, t)
        future = [d["factor"] for d in divs if d["ex_date"] > bars[t]["date"]]
        assert future, f"t={t} 應有未來事件才算得上反證"
        scale = 1.0
        for f in future:
            scale *= f
        assert scale != 1.0
        if pit["value_center"] is not None and full[t]["value_center"] is not None:
            assert full[t]["value_center"] == pytest.approx(pit["value_center"] * scale, abs=0.011)   # 兩邊各四捨五入到 0.01
            assert full[t]["value_center"] != pit["value_center"]
            differed += 1
        for k in _CATEGORICAL:
            assert pit[k] == full[t][k]
        for k in _RATIO:
            a, b = pit[k], full[t][k]
            assert (a is None and b is None) or abs(a - b) <= 1e-4
    assert differed >= 1, "至少一個檢查點要看得到尺度差，否則測試是空的"
