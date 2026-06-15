import json
import sqlite3
from datetime import datetime

import pytest

from db.schema import create_all_tables
from integration.live_verification import get_live_verification, is_market_open
from integration.verification import classify_against_benchmarks


@pytest.fixture
def db_with_signal():
    """in-memory DB，塞入一筆當日訊號（偏多，信心 3）。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    conn.execute(
        "INSERT INTO signals (date, direction, confidence, reasons) VALUES (?, ?, ?, ?)",
        ("2026-06-15", "bullish", 3, json.dumps(["匯率偏多", "期貨偏多"])),
    )
    conn.commit()
    return conn


def _quote(price, prev_close=44000.0, open_=44100.0):
    return {"symbol": "t00", "name": "加權指數", "price": price,
            "prev_close": prev_close, "open": open_, "high": price,
            "low": prev_close, "ts": 1781497720000}


# ── classify_against_benchmarks（純函式）────────────────────────────


class TestClassifyAgainstBenchmarks:
    def test_up_hits_bullish(self):
        """即時價漲 > 0.3% → up，偏多預測命中。"""
        c = classify_against_benchmarks("bullish", 44000.0, 44100.0, 44500.0)
        assert c["day_change_class"] == "up"
        assert c["hit_day"] == 1

    def test_up_misses_bearish(self):
        """即時價漲，偏空預測背離。"""
        c = classify_against_benchmarks("bearish", 44000.0, 44100.0, 44500.0)
        assert c["day_change_class"] == "up"
        assert c["hit_day"] == 0

    def test_flat_band(self):
        """漲跌在 ±0.3% 內 → flat。"""
        c = classify_against_benchmarks("neutral", 44000.0, 44000.0, 44050.0)
        assert c["day_change_class"] == "flat"
        assert c["hit_day"] == 1  # neutral 對 flat


# ── get_live_verification ──────────────────────────────────────────


class TestGetLiveVerification:
    def test_bullish_signal_index_up_is_hit(self, db_with_signal):
        d = get_live_verification("2026-06-15", db_with_signal, quote=_quote(44500.0))
        assert d["has_signal"] is True
        assert d["predicted_direction"] == "bullish"
        assert d["day_change_class"] == "up"
        assert d["hit_day_now"] is True
        assert d["quote"]["price"] == 44500.0

    def test_bullish_signal_index_down_is_miss(self, db_with_signal):
        d = get_live_verification("2026-06-15", db_with_signal, quote=_quote(43500.0))
        assert d["day_change_class"] == "down"
        assert d["hit_day_now"] is False

    def test_no_signal_for_date(self, db_with_signal):
        d = get_live_verification("2026-06-16", db_with_signal, quote=_quote(44500.0))
        assert d["has_signal"] is False

    def test_quote_unavailable(self, db_with_signal):
        """MIS 抓不到（quote=None 注入無法觸發 fetch，故顯式傳 None 走 fetch 分支）。"""
        # 直接驗證有訊號但行情缺失時的結構：用 monkeypatch 讓 collect 回 None
        from unittest.mock import patch
        with patch("integration.live_verification.MISCollector") as mock_cls:
            mock_cls.return_value.collect_index.return_value = None
            d = get_live_verification("2026-06-15", db_with_signal)
        assert d["has_signal"] is True
        assert d["quote"] is None

    def test_open_fallback_to_price_when_missing(self, db_with_signal):
        """開盤價缺失時 fallback 即時價，不丟例外。"""
        q = _quote(44500.0)
        q["open"] = None
        d = get_live_verification("2026-06-15", db_with_signal, quote=q)
        assert d["quote"] is not None
        # open 用 price 估，跳空 = (44500-44000)/44000
        assert d["open_gap_pct"] == pytest.approx(1.1364, abs=1e-3)


# ── is_market_open ─────────────────────────────────────────────────


class TestIsMarketOpen:
    def test_open_during_session(self):
        assert is_market_open(datetime(2026, 6, 15, 10, 30)) is True  # 週一 10:30

    def test_closed_after_session(self):
        assert is_market_open(datetime(2026, 6, 15, 14, 0)) is False  # 週一 14:00

    def test_closed_on_weekend(self):
        assert is_market_open(datetime(2026, 6, 13, 10, 30)) is False  # 週六
