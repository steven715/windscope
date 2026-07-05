"""個股趨勢訊號引擎測試（spec v2：三態趨勢＋順大勢逆小勢）。

不打真實 HTTP、不用 pandas；用合成K線與 in-memory SQLite。
"""

import sqlite3

import pytest

from db.schema import create_all_tables
from integration.trend_signal import (
    DOWN,
    FLAT,
    GATE_FAIL,
    GATE_PASS,
    GATE_UNKNOWN,
    UNKNOWN,
    UP,
    TrendParams,
    _hysteresis,
    _position,
    _route,
    _sma,
    compute_trend_signal,
    evaluate,
)


def _bars(closes: list[float]) -> list[dict]:
    """把 close 序列包成 OHLC bar（high/low = ±1%）。"""
    return [{"date": f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
             "open": c, "high": c * 1.01, "low": c * 0.99,
             "close": c, "volume": 1000} for i, c in enumerate(closes)]


# ── 純函式：_sma / _hysteresis ──────────────────────────────────


class TestPrimitives:
    def test_sma_warmup_then_mean(self):
        out = _sma([1, 2, 3, 4, 5], 3)
        assert out[0] is None and out[1] is None
        assert out[2] == 2.0 and out[3] == 3.0 and out[4] == 4.0

    def test_hysteresis_needs_confirm_bars(self):
        # 單根雜訊不切換；連續 3 根才切
        raw = [UP, UP, UP, DOWN, UP, UP, DOWN, DOWN, DOWN]
        out = _hysteresis(raw, 3)
        assert out[:3] == [UP, UP, UP]
        assert out[5] == UP           # 中間單根 DOWN 不足以切換
        assert out[-1] == DOWN        # 連續 3 根 DOWN 後切換

    def test_hysteresis_empty(self):
        assert _hysteresis([], 3) == []


# ── 大勢三態分類（end-to-end evaluate）──────────────────────────


class TestBigTrend:
    def test_uptrend_up(self):
        r = evaluate(_bars([100 + i * 0.5 for i in range(300)]))
        assert r["big_trend"] == UP
        assert r["big_trend_confidence"] > 0

    def test_downtrend_down(self):
        r = evaluate(_bars([250 - i * 0.5 for i in range(300)]))
        assert r["big_trend"] == DOWN
        assert r["action_hint"] == "迴避"

    def test_ranging_flat(self):
        import math
        r = evaluate(_bars([100 + 3 * math.sin(i / 3.0) for i in range(300)]))
        assert r["big_trend"] == FLAT
        assert r["flat_redirect"] is not None

    def test_insufficient_history_unknown(self):
        """暖機不足（<260 根）回 UNKNOWN，非 FLAT。"""
        r = evaluate(_bars([100 + i for i in range(50)]))
        assert r["big_trend"] == UNKNOWN
        assert r["position_hint"] == "—"

    def test_single_bar_no_crash(self):
        r = evaluate(_bars([100]))
        assert r["big_trend"] == UNKNOWN
        assert r["value_center"] is None

    def test_empty_bars_raises(self):
        """空 bars 明確報錯（而非 IndexError），保護未來的呼叫者。"""
        with pytest.raises(ValueError):
            evaluate([])


# ── 順大勢逆小勢 路由（直接測 _route 全分支）────────────────────


class TestRoute:
    def test_unknown_big(self):
        assert _route(UNKNOWN, "資料不足", False, GATE_UNKNOWN)[0] == "資料不足待觀察"

    def test_flat_redirects_to_fundamentals(self):
        action, redirect = _route(FLAT, "貼近中樞", True, GATE_UNKNOWN)
        assert action == "震盪不操作"
        assert redirect is not None

    def test_down_avoid(self):
        assert _route(DOWN, "貼近中樞", True, GATE_PASS)[0] == "迴避"

    def test_up_fundamental_fail_avoid(self):
        assert _route(UP, "貼近中樞", True, GATE_FAIL)[0] == "迴避"

    def test_up_overbought_no_chase(self):
        assert _route(UP, "超買遠離", True, GATE_PASS)[0] == "不宜追高"

    def test_up_rebound_confirmed_with_pass_gate_watch_entry(self):
        assert _route(UP, "跌破恐慌", True, GATE_PASS)[0] == "關注建倉區"

    def test_up_rebound_unknown_gate_downgraded(self):
        """UNKNOWN gate 不放行建倉，降級為觀望（保守）。"""
        assert _route(UP, "跌破恐慌", True, GATE_UNKNOWN)[0] == "觀望"

    def test_up_panic_not_turned_up_waits(self):
        assert _route(UP, "跌破恐慌", False, GATE_PASS)[0] == "等待止穩"

    def test_up_default_neutral(self):
        assert _route(UP, "貼近中樞", False, GATE_UNKNOWN)[0] == "觀望"


# ── 加減倉提示 ──────────────────────────────────────────────────


class TestPosition:
    def test_reduce_when_overbought(self):
        p = TrendParams()
        assert _position(UP, 0.2, p) == "減倉"

    def test_add_when_below_center(self):
        assert _position(UP, -0.03, TrendParams()) == "可加倉"

    def test_hold_near_center(self):
        assert _position(UP, 0.05, TrendParams()) == "持有"

    def test_dash_when_unknown(self):
        assert _position(UNKNOWN, None, TrendParams()) == "—"


# ── compute_trend_signal：讀 DB → 寫 stock_trend_signals ────────


@pytest.fixture
def memory_db():
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    yield conn
    conn.close()


def _seed_ohlc(conn, stock_id, closes):
    bars = _bars(closes)
    for b in bars:
        conn.execute(
            "INSERT INTO raw_stock_daily (date, stock_id, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (b["date"], stock_id, b["open"], b["high"], b["low"], b["close"], b["volume"]),
        )
    conn.commit()


class TestComputeTrendSignal:
    def test_writes_signal_row(self, memory_db):
        _seed_ohlc(memory_db, "2330", [100 + i * 0.5 for i in range(300)])
        result = compute_trend_signal("2330", memory_db)

        assert result is not None
        assert result["big_trend"] == UP
        assert result["rule_version"]  # 有版本號

        row = memory_db.execute(
            "SELECT big_trend, action_hint, fundamental_gate, small_turn_up "
            "FROM stock_trend_signals WHERE stock_id = '2330'"
        ).fetchone()
        assert row[0] == UP
        assert row[2] == GATE_UNKNOWN     # 無基本面 → 一律 UNKNOWN
        assert row[3] in (0, 1)           # 存成 int

    def test_no_ohlc_returns_none(self, memory_db):
        assert compute_trend_signal("9999", memory_db) is None

    def test_upsert_is_idempotent(self, memory_db):
        _seed_ohlc(memory_db, "2330", [100 + i * 0.5 for i in range(300)])
        compute_trend_signal("2330", memory_db)
        compute_trend_signal("2330", memory_db)
        n = memory_db.execute(
            "SELECT COUNT(*) FROM stock_trend_signals WHERE stock_id = '2330'"
        ).fetchone()[0]
        assert n == 1

    def test_asof_restricts_window(self, memory_db):
        """asof 只取該日以前的K線（最新一根即為 asof 那根）。"""
        _seed_ohlc(memory_db, "2330", [100 + i * 0.5 for i in range(300)])
        asof = "2025-05-01"
        result = compute_trend_signal("2330", memory_db, asof=asof)
        assert result["date"] <= asof

    def test_compute_applies_back_adjust(self, memory_db):
        """含分割缺口（前段 3x）的序列：compute 走還原後序列 → UP，未還原則為 DOWN。"""
        from integration.price_adjust import back_adjust
        from integration.trend_signal import evaluate

        # 真實(還原後)為乾淨上升 100→250；前 150 根乘 3 模擬分割前價，index 150 缺口 >11%
        closes = ([(100 + i * 0.5) * 3 for i in range(150)]
                  + [100 + i * 0.5 for i in range(150, 300)])
        _seed_ohlc(memory_db, "3999", closes)

        res = compute_trend_signal("3999", memory_db)
        expected = evaluate(back_adjust(_bars(closes)))
        assert res["big_trend"] == expected["big_trend"] == UP     # 還原後 UP
        # 未還原原始序列評估為 DOWN（年線被分割前高價拉低）→ 證明 compute 確實套了還原
        assert evaluate(_bars(closes))["big_trend"] != res["big_trend"]
