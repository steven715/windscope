"""盤前判斷有效性統計測試（Layer 4b）：基準線、信心校準、單因子分桶、判斷回測、結論。

全部用 in-memory SQLite 塞假 signals/verifications/daily_metrics/raw_index，不依賴網路與 data/。
"""

import math
import sqlite3

import pytest

from config import settings
from db.schema import create_all_tables
from integration.call_stats import (
    CallRow,
    _assess,
    _trade_metrics,
    backtest_calls,
    build_call_stats,
    compute_baselines,
    confidence_calibration,
    factor_table,
    list_rule_versions,
    load_verified_calls,
    wilson_interval,
)

# 2026-06-08(一)～06-12(五) 連續五個交易日（conftest 預設無休市日）
_DAYS = ["2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11", "2026-06-12"]


def _row(date="2026-06-10", direction="bullish", confidence=3, prev_close=100.0,
         open_=100.5, close=101.0, rule_version="v3") -> CallRow:
    """手刻 CallRow；漲跌幅與三分類照 verification 口徑算。"""
    from integration.verification import classify_against_benchmarks

    c = classify_against_benchmarks(direction, prev_close, open_, close)
    return CallRow(date=date, direction=direction, confidence=confidence,
                   rule_version=rule_version, prev_close=prev_close, open=open_, close=close,
                   open_gap_pct=c["open_gap_pct"], day_change_pct=c["day_change_pct"],
                   open_gap_class=c["open_gap_class"], day_change_class=c["day_change_class"],
                   hit_day=c["hit_day"], hit_open=c["hit_open"])


def _seed_day(conn, date, direction, confidence, prev_close, open_, close,
              rule_version="v3", metrics: dict | None = None) -> None:
    """一天份：signals + verifications（照 verify_signal 口徑）+ raw_index + 可選 daily_metrics。"""
    from integration.verification import classify_against_benchmarks

    c = classify_against_benchmarks(direction, prev_close, open_, close)
    conn.execute("INSERT INTO signals (date, direction, confidence, rule_version) "
                 "VALUES (?, ?, ?, ?)", (date, direction, confidence, rule_version))
    conn.execute(
        "INSERT INTO verifications (date, predicted_direction, confidence, prev_close, open, "
        "close, open_gap_pct, day_change_pct, open_gap_class, day_change_class, hit_day, hit_open) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (date, direction, confidence, prev_close, open_, close, c["open_gap_pct"],
         c["day_change_pct"], c["open_gap_class"], c["day_change_class"],
         c["hit_day"], c["hit_open"]))
    conn.execute("INSERT OR REPLACE INTO raw_index (date, open, close) VALUES (?, ?, ?)",
                 (date, open_, close))
    if metrics is not None:
        cols = ", ".join(metrics)
        conn.execute(f"INSERT INTO daily_metrics (date, {cols}) VALUES (?, "
                     f"{', '.join('?' * len(metrics))})", (date, *metrics.values()))


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    return conn


# ── 統計工具 ──────────────────────────────────────────────────────


class TestWilson:
    def test_zero_n_is_none(self):
        assert wilson_interval(0, 0) is None

    def test_known_value(self):
        # 6/10：Wilson 95% ≈ [0.313, 0.832]
        lo, hi = wilson_interval(6, 10)
        assert math.isclose(lo, 0.3127, abs_tol=1e-3)
        assert math.isclose(hi, 0.8318, abs_tol=1e-3)

    def test_bounded_and_narrows_with_n(self):
        lo1, hi1 = wilson_interval(0, 5)
        assert lo1 == 0.0 and 0 < hi1 < 1
        lo, hi = wilson_interval(600, 1000)
        assert hi - lo < wilson_interval(6, 10)[1] - wilson_interval(6, 10)[0]


class TestTradeMetrics:
    def test_empty(self):
        m = _trade_metrics([])
        assert m["n"] == 0 and m["win_rate"] is None and m["max_losing_streak"] == 0

    def test_basic_metrics(self):
        m = _trade_metrics([0.02, -0.01, 0.02, -0.01])
        assert m["n"] == 4 and m["wins"] == 2 and m["win_rate"] == 0.5
        assert math.isclose(m["avg_win"], 0.02) and math.isclose(m["avg_loss"], -0.01)
        assert math.isclose(m["payoff"], 2.0)
        assert math.isclose(m["expectancy"], 0.005)
        assert math.isclose(m["profit_factor"], 2.0)
        assert math.isclose(m["total_return"], 1.02 * 0.99 * 1.02 * 0.99 - 1)

    def test_drawdown_and_streaks(self):
        m = _trade_metrics([0.10, -0.10, -0.10, 0.05, 0.05, 0.05])
        # 峰 1.1 → 1.1*0.9*0.9=0.891 → 回撤 -19%
        assert math.isclose(m["max_drawdown"], 0.891 / 1.1 - 1)
        assert m["max_losing_streak"] == 2 and m["max_winning_streak"] == 3

    def test_no_loss_profit_factor_none(self):
        assert _trade_metrics([0.01, 0.02])["profit_factor"] is None


# ── 資料載入 ──────────────────────────────────────────────────────


def test_load_filters_by_version_and_range(db):
    _seed_day(db, _DAYS[0], "bullish", 3, 100, 100, 101, rule_version="v2")
    _seed_day(db, _DAYS[1], "bullish", 3, 101, 101, 102, rule_version="v3")
    _seed_day(db, _DAYS[2], "bearish", 2, 102, 102, 101, rule_version="v3")
    assert [r.date for r in load_verified_calls(db)] == _DAYS[:3]           # 升冪
    assert [r.rule_version for r in load_verified_calls(db, rule_version="v3")] == ["v3", "v3"]
    assert [r.date for r in load_verified_calls(db, date_from=_DAYS[1], date_to=_DAYS[1])] \
        == [_DAYS[1]]
    assert list_rule_versions(db) == [{"rule_version": "v3", "n": 2},
                                      {"rule_version": "v2", "n": 1}]
    # 依最近使用日排序而非字串：v10 用在最新一天就排最前
    _seed_day(db, _DAYS[3], "bullish", 3, 101, 101, 102, rule_version="v10")
    assert [v["rule_version"] for v in list_rule_versions(db)] == ["v10", "v3", "v2"]


def test_load_skips_null_or_out_of_range_confidence(db):
    _seed_day(db, _DAYS[0], "bullish", 3, 100, 100, 101)
    _seed_day(db, _DAYS[1], "bullish", 9, 101, 101, 102)          # 超出 1–5
    db.execute("INSERT INTO signals (date, direction, confidence) VALUES (?, 'bullish', NULL)",
               (_DAYS[2],))
    db.execute("INSERT INTO verifications (date, predicted_direction, confidence, prev_close, "
               "open, close, day_change_class, open_gap_class) "
               "VALUES (?, 'bullish', NULL, 102, 102, 103, 'up', 'flat')", (_DAYS[2],))
    rows = load_verified_calls(db)
    assert [r.date for r in rows] == [_DAYS[0]]
    assert build_call_stats(db)["n"] == 1                         # 不會 500


def test_load_recomputes_hit_from_verification_stance(db):
    """方向與 hit 都以 verifications 當下立場為準，signals 事後被覆寫也不影響。"""
    _seed_day(db, _DAYS[0], "bullish", 3, 100, 100, 101)
    db.execute("UPDATE signals SET direction = 'bearish' WHERE date = ?", (_DAYS[0],))
    db.execute("UPDATE verifications SET hit_day = 0 WHERE date = ?", (_DAYS[0],))
    r = load_verified_calls(db)[0]
    assert r.direction == "bullish" and r.hit_day == 1 and r.hit_open == 0


def test_load_skips_unverified_signal(db):
    db.execute("INSERT INTO signals (date, direction, confidence) VALUES (?, 'bullish', 3)",
               (_DAYS[0],))
    assert load_verified_calls(db) == []


# ── ① 基準線 ──────────────────────────────────────────────────────


def test_baselines_always_and_independence():
    # 實際：漲 漲 跌 平；預測：多 多 多 中 → 系統命中 3/4
    rows = [_row(_DAYS[0], "bullish", close=101), _row(_DAYS[1], "bullish", close=101),
            _row(_DAYS[2], "bullish", close=99), _row(_DAYS[3], "neutral", close=100.1)]
    b = compute_baselines(rows)
    assert b["system"]["n"] == 4 and b["system"]["hits"] == 3
    by = {x["key"]: x for x in b["baselines"]}
    assert by["always_up"]["rate"] == 0.5
    assert by["always_down"]["rate"] == 0.25
    assert by["always_flat"]["rate"] == 0.25
    # 獨立期望 = P(預多)·P(漲)+P(預中)·P(平) = .75*.5 + .25*.25 = 0.4375
    assert math.isclose(by["independence"]["rate"], 0.4375)
    assert math.isclose(by["independence"]["edge_pp"], (0.75 - 0.4375) * 100)
    assert by["follow_yesterday"]["n"] == 0            # 未給 index_changes
    assert b["actual_dist"]["up"]["share"] == 0.5


def test_baseline_follow_yesterday_uses_previous_trading_day():
    # 06-08 是週一：前一交易日是 06-05（週五），不是日曆上的 06-07
    rows = [_row("2026-06-08", "bullish", close=101),      # 預測漲、實際漲 → 命中
            _row(_DAYS[1], "bullish", close=99),            # 06-08 漲 → 喊漲、實際跌
            _row(_DAYS[2], "bullish", close=99)]            # 06-09 無前日漲跌 → 不計
    changes = {"2026-06-05": 1.0, "2026-06-07": -5.0, "2026-06-08": 1.0}
    b = compute_baselines(rows, changes)
    fy = {x["key"]: x for x in b["baselines"]}["follow_yesterday"]
    assert fy["n"] == 2 and fy["hits"] == 1
    # edge_pp 用同一子集：系統在這兩天命中 1/2 → 0 pp
    assert fy["system_rate_same_sample"] == 0.5 and fy["edge_pp"] == 0.0


def test_baselines_empty():
    b = compute_baselines([])
    assert b["system"]["n"] == 0 and b["system"]["rate"] is None
    assert all(x["rate"] is None for x in b["baselines"])


# ── ② 信心校準 ────────────────────────────────────────────────────


def test_calibration_buckets_and_monotonic():
    rows = ([_row(confidence=2, close=101)] * 2 + [_row(confidence=2, close=99)] * 2   # 50%
            + [_row(confidence=4, close=101)] * 3 + [_row(confidence=4, close=99)])    # 75%
    cal = confidence_calibration(rows, min_bucket=2)
    assert cal["by_confidence"][2]["rate"] == 0.5
    assert cal["by_confidence"][4]["rate"] == 0.75
    assert cal["monotonic"] is True and cal["monotonic_buckets"] == [2, 4]
    # 偏多命中時順向報酬為正、失誤為負 → 平均 (1+1-1-1)/4 = 0
    assert math.isclose(cal["by_confidence"][2]["avg_directional_return"], 0.0, abs_tol=1e-9)
    assert cal["by_direction"]["bullish"]["n"] == 8


def test_calibration_non_monotonic_and_insufficient():
    rows = ([_row(confidence=1, close=101)] * 3                          # 100%
            + [_row(confidence=3, close=99)] * 3                         # 0%
            + [_row(confidence=5, close=101)])                           # n=1 → 不足
    cal = confidence_calibration(rows, min_bucket=2)
    assert cal["monotonic"] is False
    assert cal["by_confidence"][5]["insufficient"] is True
    assert cal["monotonic_buckets"] == [1, 3]


def test_calibration_flat_is_not_monotonic():
    rows = ([_row(confidence=2, close=101), _row(confidence=2, close=99)]
            + [_row(confidence=4, close=101), _row(confidence=4, close=99)])   # 50% / 50%
    assert confidence_calibration(rows, min_bucket=2)["monotonic"] is False


def test_calibration_neutral_has_no_directional_return():
    cal = confidence_calibration([_row(direction="neutral", close=100.1)], min_bucket=1)
    nb = cal["by_direction"]["neutral"]
    assert nb["n_directional"] == 0 and nb["avg_directional_return"] is None
    assert nb["rate"] == 1.0                              # 平 → 命中
    assert cal["monotonic"] is None                       # 桶數 <2


# ── ③ 單因子表 ────────────────────────────────────────────────────


def test_factor_table_buckets_by_thresholds(db, monkeypatch):
    monkeypatch.setattr(settings, "FUTURES_SPREAD_THRESHOLD", 100)
    monkeypatch.setattr(settings, "OI_BULLISH_THRESHOLD", 30000)
    monkeypatch.setattr(settings, "OI_BEARISH_THRESHOLD", -30000)
    # 06-08 當作前收基準（無 metrics）；06-09~06-11 三天各一組輸入
    db.execute("INSERT INTO raw_index (date, close) VALUES (?, 100)", (_DAYS[0],))
    _seed_day(db, _DAYS[1], "bullish", 3, 100, 100, 101,
              metrics={"fx_direction": "bullish", "fx_asia_sync": 1,
                       "futures_spread_adjusted": 150.0, "futures_volume_ratio": 1.6,
                       "oi_net_foreign": 35000, "oi_delta": 500})
    _seed_day(db, _DAYS[2], "bearish", 3, 101, 101, 99,
              metrics={"fx_direction": "bearish", "fx_asia_sync": 0,
                       "futures_spread_adjusted": -100.0, "futures_volume_ratio": 0.7,
                       "oi_net_foreign": -30000, "oi_delta": -500})
    _seed_day(db, _DAYS[3], "neutral", 2, 99, 99, 99.1,
              metrics={"fx_direction": "neutral", "fx_asia_sync": None,
                       "futures_spread_adjusted": None, "futures_volume_ratio": 1.0,
                       "oi_net_foreign": 0, "oi_delta": 0})
    table = {f["key"]: f for f in factor_table(db, min_bucket=1)}

    spread = {b["key"]: b for b in table["futures_spread_adjusted"]["buckets"]}
    assert spread["pos"]["n"] == 1 and spread["pos"]["implied"] == "bullish"
    assert spread["pos"]["implied_hit"]["rate"] == 1.0                 # 漲 → 命中
    assert spread["neg"]["n"] == 1                   # −100 剛好達門檻＝與期貨票同邊界（≤）
    assert "mid" not in spread
    assert spread["na"]["n"] == 1 and spread["na"]["implied_hit"] is None

    oi = {b["key"]: b for b in table["oi_net_foreign"]["buckets"]}
    assert oi["net_long"]["n"] == 1 and oi["net_short"]["n"] == 1 and oi["mid"]["n"] == 1
    assert oi["net_short"]["implied_hit"]["rate"] == 1.0               # 跌 → 命中

    vol = {b["key"]: b for b in table["futures_volume_ratio"]["buckets"]}
    assert {k: v["n"] for k, v in vol.items()} == {"high": 1, "normal": 1, "low": 1}
    sync = {b["key"]: b for b in table["fx_asia_sync"]["buckets"]}
    assert sync["sync"]["dist"]["up"]["n"] == 1 and sync["na"]["n"] == 1
    assert math.isclose(table["fx_direction"]["buckets"][0]["avg_pct"], 1.0)


def test_factor_table_counts_days_without_signal(db):
    """因子表只需 daily_metrics + raw_index，無判斷的回補日也算。"""
    db.execute("INSERT INTO raw_index (date, close) VALUES (?, 100)", (_DAYS[0],))
    db.execute("INSERT INTO raw_index (date, close) VALUES (?, 102)", (_DAYS[1],))
    db.execute("INSERT INTO daily_metrics (date, fx_direction) VALUES (?, 'bullish')", (_DAYS[1],))
    fx = factor_table(db)[0]
    assert fx["key"] == "fx_direction" and fx["n"] == 1


def test_factor_table_skips_day_without_prev_close(db):
    db.execute("INSERT INTO raw_index (date, close) VALUES (?, 102)", (_DAYS[1],))
    db.execute("INSERT INTO daily_metrics (date, fx_direction) VALUES (?, 'bullish')", (_DAYS[1],))
    assert factor_table(db)[0]["n"] == 0


# ── ④ 判斷回測 ────────────────────────────────────────────────────


def test_backtest_modes_and_cost():
    rows = [_row(_DAYS[0], "bullish", prev_close=100, open_=101, close=102),   # 多：o→c +0.99%
            _row(_DAYS[1], "bearish", prev_close=102, open_=102, close=100),   # 空：+2%
            _row(_DAYS[2], "neutral", prev_close=100, open_=100, close=100.1)]  # 空手
    rows[0].confidence, rows[1].confidence = 1, 3     # 平均 2 → 權重 0.5 / 1.5
    bt = backtest_calls(rows, cost_bps=100)         # 1% 來回
    oc = bt["modes"]["open_to_close"]
    assert oc["n_days"] == 3 and oc["n_neutral"] == 1
    assert oc["equal"]["n"] == 2                     # 中性日不計
    rets = [t["ret"] for t in oc["trades"]]
    assert math.isclose(rets[0], 102 / 101 - 1 - 0.01)
    assert math.isclose(rets[1], -(100 / 102 - 1) - 0.01)
    # 信心加權：權重正規化成平均 1（0.5、1.5），總曝險與等權相同
    assert [t["weight"] for t in oc["trades"]] == [0.5, 1.5]
    assert math.isclose(oc["weighted"]["expectancy"], (0.5 * rets[0] + 1.5 * rets[1]) / 2)
    # 對照列含中性日；同口徑指數 = 每天做多零成本的逐日複利
    assert oc["always_long"]["n"] == 3 and oc["always_short"]["n"] == 3
    assert math.isclose(oc["index_return"], (102 / 101) * (100 / 102) * (100.1 / 100) - 1)
    assert math.isclose(oc["index_return"],
                        backtest_calls(rows, cost_bps=0)["modes"]["open_to_close"]
                        ["always_long"]["total_return"])

    cc = bt["modes"]["close_to_close"]
    cc_rets = [t["ret"] for t in cc["trades"]]
    assert math.isclose(cc_rets[0], 102 / 100 - 1 - 0.01)   # 前收口徑與 o→c 不同
    assert cc_rets[0] != rets[0]


def test_backtest_skips_rows_without_prices():
    rows = [_row(_DAYS[0], "bullish")]
    rows[0].close = None
    bt = backtest_calls(rows)
    assert bt["modes"]["open_to_close"]["n_days"] == 0
    assert bt["modes"]["open_to_close"]["equal"]["n"] == 0
    assert bt["modes"]["open_to_close"]["index_return"] is None


def test_backtest_weighting_equal_confidence_matches_equal_weight():
    rows = [_row(_DAYS[0], "bullish", close=101, confidence=5),
            _row(_DAYS[1], "bearish", close=99, confidence=5)]
    bt = backtest_calls(rows, cost_bps=0)["modes"]["close_to_close"]
    assert math.isclose(bt["weighted"]["expectancy"], bt["equal"]["expectancy"])


def test_backtest_single_trade_sharpe_none():
    bt = backtest_calls([_row(close=101)], cost_bps=0)["modes"]["close_to_close"]
    assert bt["equal"]["n"] == 1 and bt["equal"]["sharpe"] is None


def test_backtest_default_cost_from_settings():
    assert backtest_calls([])["cost_bps"] == settings.STATS_BT_COST_BPS


# ── 結論與總表 ────────────────────────────────────────────────────


def _fake_baselines(sys_hits, n, best_rate):
    rows = [_row(close=101)] * sys_hits + [_row(close=99)] * (n - sys_hits)
    b = compute_baselines(rows)
    b["baselines"] = [{"key": "x", "label": "基準X", "rate": best_rate, "n": n,
                       "hits": None, "ci_low": None, "ci_high": None, "edge_pp": None}]
    return b


def test_assess_verdicts():
    assert _assess(5, _fake_baselines(4, 5, 0.5), 30)["verdict"] == "insufficient"
    assert _assess(100, _fake_baselines(80, 100, 0.5), 30)["verdict"] == "edge"
    assert _assess(100, _fake_baselines(52, 100, 0.5), 30)["verdict"] == "unclear"
    assert _assess(100, _fake_baselines(40, 100, 0.5), 30)["verdict"] == "no_edge"
    assert _assess(100, _fake_baselines(50, 100, 0.5), 30)["verdict"] == "no_edge"   # 恰好相等


def test_build_call_stats_end_to_end(db, monkeypatch):
    monkeypatch.setattr(settings, "STATS_MIN_SAMPLE", 2)
    db.execute("INSERT INTO raw_index (date, close) VALUES (?, 99)", (_DAYS[0],))
    _seed_day(db, _DAYS[1], "bullish", 4, 99, 99.5, 101,
              metrics={"fx_direction": "bullish", "futures_spread_adjusted": 120.0})
    _seed_day(db, _DAYS[2], "bearish", 3, 101, 101, 99,
              metrics={"fx_direction": "bearish", "futures_spread_adjusted": -120.0})
    s = build_call_stats(db)
    assert s["n"] == 2 and s["date_range"] == [_DAYS[1], _DAYS[2]]
    assert s["assessment"]["verdict"] in {"edge", "unclear", "no_edge"}
    assert s["baselines"]["system"]["rate"] == 1.0
    assert s["calibration"]["by_confidence"][4]["n"] == 1
    assert s["backtest"]["modes"]["open_to_close"]["equal"]["n"] == 2
    assert s["versions"] == [{"rule_version": "v3", "n": 2}]
    # 跟昨天：06-10 用 06-09 的漲（+2.02%）喊漲，實際跌 → 0/1
    fy = next(b for b in s["baselines"]["baselines"] if b["key"] == "follow_yesterday")
    assert fy["n"] == 1 and fy["hits"] == 0


def test_build_call_stats_empty_db(db):
    s = build_call_stats(db)
    assert s["n"] == 0 and s["assessment"]["verdict"] == "insufficient"
    assert s["calibration"]["by_confidence"] == {} and s["backtest"]["modes"]
    assert all(f["n"] == 0 for f in s["factors"])
