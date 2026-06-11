"""Layer 3 訊號引擎測試：兩票合成、加減分項、夾限、個股過濾與分類。"""

import json
import sqlite3

import pytest

from db.schema import create_all_tables
from integration.signal_engine import (
    compute_market_signal,
    compute_stock_signals,
    format_signal_text,
)


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    return conn


def _insert_metrics(conn, date="2026-06-12", fx_direction="neutral",
                    fx_asia_sync=None, asia_detail=None,
                    spread_adjusted=None, volume_ratio=None, oi_net=None):
    """塞一筆 daily_metrics 測試資料。"""
    detail_json = json.dumps(asia_detail, ensure_ascii=False) if asia_detail else None
    conn.execute(
        """INSERT INTO daily_metrics
               (date, fx_direction, fx_asia_sync, fx_asia_detail,
                futures_spread_adjusted, futures_volume_ratio, oi_net_foreign)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (date, fx_direction, fx_asia_sync, detail_json,
         spread_adjusted, volume_ratio, oi_net),
    )
    conn.commit()


class TestVoteSynthesis:
    def test_both_bullish(self):
        """兩票同向偏多 → bullish 信心 3。"""
        conn = _make_db()
        _insert_metrics(conn, fx_direction="bullish", spread_adjusted=150)
        result = compute_market_signal("2026-06-12", conn)
        assert result["direction"] == "bullish"
        assert result["confidence"] == 3

    def test_both_bearish(self):
        conn = _make_db()
        _insert_metrics(conn, fx_direction="bearish", spread_adjusted=-150)
        result = compute_market_signal("2026-06-12", conn)
        assert result["direction"] == "bearish"
        assert result["confidence"] == 3

    def test_conflict_forces_neutral(self):
        """兩票反向 → 強制中性，信心 1。"""
        conn = _make_db()
        _insert_metrics(conn, fx_direction="bullish", spread_adjusted=-150)
        result = compute_market_signal("2026-06-12", conn)
        assert result["direction"] == "neutral"
        assert result["confidence"] == 1
        assert any("分歧" in r for r in result["reasons"])

    def test_one_directional_one_neutral(self):
        """一票方向一票中性 → 該方向，信心 2。"""
        conn = _make_db()
        _insert_metrics(conn, fx_direction="bullish", spread_adjusted=50)
        result = compute_market_signal("2026-06-12", conn)
        assert result["direction"] == "bullish"
        assert result["confidence"] == 2

    def test_both_neutral(self):
        conn = _make_db()
        _insert_metrics(conn, fx_direction="neutral", spread_adjusted=20)
        result = compute_market_signal("2026-06-12", conn)
        assert result["direction"] == "neutral"
        assert result["confidence"] == 2

    def test_spread_exactly_at_threshold(self):
        """價差剛好 +100 → 偏多票（含邊界）。"""
        conn = _make_db()
        _insert_metrics(conn, fx_direction="neutral", spread_adjusted=100)
        result = compute_market_signal("2026-06-12", conn)
        assert result["futures_vote"] == "bullish"

    def test_no_metrics_returns_none(self):
        conn = _make_db()
        assert compute_market_signal("2026-06-12", conn) is None

    def test_missing_fx_treated_neutral(self):
        """匯率資料缺失 → 匯率票中性，理由有記錄。"""
        conn = _make_db()
        _insert_metrics(conn, fx_direction=None, spread_adjusted=150)
        result = compute_market_signal("2026-06-12", conn)
        assert result["direction"] == "bullish"
        assert result["confidence"] == 2
        assert any("匯率資料不可用" in r for r in result["reasons"])


class TestModifiers:
    def test_asia_sync_aligned_plus_one(self):
        """亞幣同步且與訊號同向 → +1。"""
        conn = _make_db()
        _insert_metrics(conn, fx_direction="bullish", fx_asia_sync=1,
                        spread_adjusted=150)
        result = compute_market_signal("2026-06-12", conn)
        assert result["confidence"] == 4

    def test_twd_only_minus_one(self):
        """只有台幣動（不同步）→ -1。"""
        conn = _make_db()
        _insert_metrics(conn, fx_direction="bullish", fx_asia_sync=0,
                        spread_adjusted=150)
        result = compute_market_signal("2026-06-12", conn)
        assert result["confidence"] == 2

    def test_volume_high_plus_one(self):
        """量比 >= 1.5 且期貨有方向 → +1。"""
        conn = _make_db()
        _insert_metrics(conn, fx_direction="bullish", spread_adjusted=150,
                        volume_ratio=1.6)
        result = compute_market_signal("2026-06-12", conn)
        assert result["confidence"] == 4

    def test_volume_high_without_futures_direction_no_bonus(self):
        """量比高但期貨票中性 → 不加分。"""
        conn = _make_db()
        _insert_metrics(conn, fx_direction="bullish", spread_adjusted=50,
                        volume_ratio=1.6)
        result = compute_market_signal("2026-06-12", conn)
        assert result["confidence"] == 2

    def test_volume_low_minus_one(self):
        """量比 <= 0.7 → -1。"""
        conn = _make_db()
        _insert_metrics(conn, fx_direction="bullish", spread_adjusted=150,
                        volume_ratio=0.6)
        result = compute_market_signal("2026-06-12", conn)
        assert result["confidence"] == 2

    def test_oi_against_bullish_minus_one(self):
        """偏多訊號但外資淨空單超過 3 萬口 → -1。"""
        conn = _make_db()
        _insert_metrics(conn, fx_direction="bullish", spread_adjusted=150,
                        oi_net=-34800)
        result = compute_market_signal("2026-06-12", conn)
        assert result["confidence"] == 2

    def test_oi_against_bearish_minus_one(self):
        conn = _make_db()
        _insert_metrics(conn, fx_direction="bearish", spread_adjusted=-150,
                        oi_net=35000)
        result = compute_market_signal("2026-06-12", conn)
        assert result["confidence"] == 2

    def test_oi_small_no_penalty(self):
        """空單未達 3 萬口 → 不扣分。"""
        conn = _make_db()
        _insert_metrics(conn, fx_direction="bullish", spread_adjusted=150,
                        oi_net=-20000)
        result = compute_market_signal("2026-06-12", conn)
        assert result["confidence"] == 3

    def test_krw_divergence_minus_one(self):
        """台幣貶人民幣貶但韓元升 → -1 + 警示理由。"""
        conn = _make_db()
        _insert_metrics(conn, fx_direction="bearish", spread_adjusted=-150,
                        asia_detail={"TWD": "bearish", "CNY": "bearish",
                                     "KRW": "bullish"})
        result = compute_market_signal("2026-06-12", conn)
        assert result["confidence"] == 2
        assert any("賣台買韓" in r for r in result["reasons"])

    def test_confidence_clamped_to_max(self):
        """所有加分項同時成立也不超過 5。"""
        conn = _make_db()
        _insert_metrics(conn, fx_direction="bullish", fx_asia_sync=1,
                        spread_adjusted=200, volume_ratio=2.0, oi_net=10000)
        result = compute_market_signal("2026-06-12", conn)
        assert result["confidence"] == 5

    def test_confidence_clamped_to_min(self):
        """扣到底也不低於 1。"""
        conn = _make_db()
        _insert_metrics(conn, fx_direction="bullish", fx_asia_sync=0,
                        spread_adjusted=150, volume_ratio=0.5, oi_net=-40000)
        result = compute_market_signal("2026-06-12", conn)
        assert result["confidence"] == 1


class TestSignalPersistence:
    def test_written_to_signals_table(self):
        conn = _make_db()
        _insert_metrics(conn, fx_direction="bullish", spread_adjusted=150)
        compute_market_signal("2026-06-12", conn)

        row = conn.execute(
            "SELECT direction, confidence, rule_version, reasons "
            "FROM signals WHERE date = '2026-06-12'"
        ).fetchone()
        assert row[0] == "bullish"
        assert row[1] == 3
        assert row[2] == "v1"
        assert isinstance(json.loads(row[3]), list)

    def test_idempotent_recompute(self):
        """重算同一天覆蓋舊值。"""
        conn = _make_db()
        _insert_metrics(conn, fx_direction="bullish", spread_adjusted=150)
        compute_market_signal("2026-06-12", conn)
        conn.execute(
            "UPDATE daily_metrics SET fx_direction = 'neutral' "
            "WHERE date = '2026-06-12'"
        )
        compute_market_signal("2026-06-12", conn)

        rows = conn.execute("SELECT direction FROM signals").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "bullish"  # 期貨票仍偏多，僅匯率轉中性


def _insert_stock_metric(conn, date="2026-06-12", stock_id="2330",
                         broker_name="兆豐-嘉義", net_amount=1.2e8,
                         consecutive=3, price_zone="low",
                         both_sides=0, broker_type="swing"):
    conn.execute(
        """INSERT INTO daily_stock_metrics
               (date, stock_id, broker_name, net_amount, consecutive_days,
                price_zone, both_sides_flag, broker_type)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (date, stock_id, broker_name, net_amount, consecutive,
         price_zone, both_sides, broker_type),
    )
    conn.commit()


class TestStockSignals:
    def test_fake_volume_filtered_first(self):
        """對敲假量優先於其他分類。"""
        conn = _make_db()
        _insert_stock_metric(conn, both_sides=1)
        results = compute_stock_signals("2026-06-12", conn)
        assert len(results) == 1
        assert results[0]["category"] == "fake_volume"

    def test_day_trade_broker_flagged(self):
        """隔日沖分點買超 → 不追。"""
        conn = _make_db()
        _insert_stock_metric(conn, broker_name="凱基-台北",
                             broker_type="day_trade")
        results = compute_stock_signals("2026-06-12", conn)
        assert results[0]["category"] == "day_trade_no_chase"

    def test_bottom_watch(self):
        """低檔連買 3 天且金額達標 → 摸底觀察。"""
        conn = _make_db()
        _insert_stock_metric(conn, price_zone="low", consecutive=3)
        results = compute_stock_signals("2026-06-12", conn)
        assert results[0]["category"] == "bottom_watch"

    def test_distribution_warning(self):
        conn = _make_db()
        _insert_stock_metric(conn, price_zone="high", consecutive=3)
        results = compute_stock_signals("2026-06-12", conn)
        assert results[0]["category"] == "distribution_warning"

    def test_accumulation_needs_five_days(self):
        """盤整區連買 5 天 → 吸籌；3 天不夠。"""
        conn = _make_db()
        _insert_stock_metric(conn, stock_id="2330",
                             price_zone="consolidation", consecutive=5)
        _insert_stock_metric(conn, stock_id="2454",
                             price_zone="consolidation", consecutive=3)
        results = compute_stock_signals("2026-06-12", conn)
        categories = {r["stock_id"]: r["category"] for r in results}
        assert categories.get("2330") == "accumulation"
        assert "2454" not in categories

    def test_avoid_on_consecutive_sell(self):
        """連賣 3 天 → 避開。"""
        conn = _make_db()
        _insert_stock_metric(conn, net_amount=-8e7, consecutive=-3)
        results = compute_stock_signals("2026-06-12", conn)
        assert results[0]["category"] == "avoid"

    def test_amount_below_threshold_skipped(self):
        """金額未達 5,000 萬 → 不產生訊號。"""
        conn = _make_db()
        _insert_stock_metric(conn, net_amount=3e7)
        assert compute_stock_signals("2026-06-12", conn) == []

    def test_consecutive_below_threshold_skipped(self):
        conn = _make_db()
        _insert_stock_metric(conn, consecutive=2)
        assert compute_stock_signals("2026-06-12", conn) == []

    def test_no_metrics_empty_list(self):
        conn = _make_db()
        assert compute_stock_signals("2026-06-12", conn) == []


class TestFormatSignalText:
    def test_contains_direction_and_reasons(self):
        text = format_signal_text({
            "direction": "bullish", "confidence": 4,
            "rule_version": "v1",
            "reasons": ["匯率與期貨同向", "亞幣同步且與訊號同向 +1"],
        })
        assert "【訊號判斷】" in text
        assert "偏多" in text
        assert "信心 4/5" in text
        assert "亞幣同步" in text
