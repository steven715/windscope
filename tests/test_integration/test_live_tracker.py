from datetime import datetime
from unittest.mock import patch

import pytest

from integration import live_tracker
from integration.live_tracker import (
    get_cached_quote,
    is_market_open,
    refresh_live_quote,
)
from utils import trading_calendar


@pytest.fixture(autouse=True)
def clean_cache():
    """每個測試前後清空報價快取；休市日曆快取設空集合（僅排除週末，確保測試不讀真實 DB）。"""
    live_tracker._reset_cache()
    trading_calendar._holiday_cache = set()
    yield
    live_tracker._reset_cache()
    trading_calendar._holiday_cache = None


def _quote(price=44500.0):
    return {"symbol": "t00", "name": "加權指數", "price": price,
            "prev_close": 44000.0, "open": 44100.0, "high": price,
            "low": 44000.0, "ts": 1781497720000}


class TestRefresh:
    def test_refresh_populates_cache(self):
        with patch("integration.live_tracker.MISCollector") as mock_cls:
            mock_cls.return_value.collect_index.return_value = _quote(44500.0)
            refresh_live_quote(now=datetime(2026, 6, 15, 10, 0))
        q, as_of = get_cached_quote()
        assert q["price"] == 44500.0
        assert as_of == "10:00:00"

    def test_refresh_no_network_outside_window_when_cached(self):
        """非刷新時段且已有快取 → 不再打網路。"""
        live_tracker._cache["quote"] = _quote(44000.0)
        live_tracker._cache["as_of"] = "13:25:00"
        with patch("integration.live_tracker.MISCollector") as mock_cls:
            refresh_live_quote(now=datetime(2026, 6, 15, 20, 0))  # 晚上 20:00
            mock_cls.return_value.collect_index.assert_not_called()
        # 快取維持原值
        assert get_cached_quote()[0]["price"] == 44000.0

    def test_refresh_fetches_when_cache_empty_even_off_hours(self):
        """快取為空時即使非時段也抓一次（讓盤後也有最後狀態）。"""
        with patch("integration.live_tracker.MISCollector") as mock_cls:
            mock_cls.return_value.collect_index.return_value = _quote(44200.0)
            refresh_live_quote(now=datetime(2026, 6, 15, 20, 0))
            mock_cls.return_value.collect_index.assert_called_once()
        assert get_cached_quote()[0]["price"] == 44200.0

    def test_refresh_ignores_none_quote(self):
        """MIS 回 None 時不覆寫快取、不報錯。"""
        with patch("integration.live_tracker.MISCollector") as mock_cls:
            mock_cls.return_value.collect_index.return_value = None
            refresh_live_quote(now=datetime(2026, 6, 15, 10, 0))
        assert get_cached_quote() == (None, None)

    def test_refresh_skips_on_holiday_even_cache_empty(self):
        """國定假日（平日休市）→ 即使快取為空、即使盤中時段也不打網路。"""
        trading_calendar._holiday_cache = {"2026-06-15"}  # 把這個週一標成休市日
        with patch("integration.live_tracker.MISCollector") as mock_cls:
            refresh_live_quote(now=datetime(2026, 6, 15, 10, 0))  # 盤中時段
            mock_cls.return_value.collect_index.assert_not_called()
        assert get_cached_quote() == (None, None)

    def test_refresh_skips_on_weekend_even_cache_empty(self):
        """週末 → 即使快取為空也不打網路（修正前會空打一次）。"""
        with patch("integration.live_tracker.MISCollector") as mock_cls:
            refresh_live_quote(now=datetime(2026, 6, 13, 10, 0))  # 週六
            mock_cls.return_value.collect_index.assert_not_called()
        assert get_cached_quote() == (None, None)


class TestIsMarketOpen:
    def test_open(self):
        assert is_market_open(datetime(2026, 6, 15, 10, 30)) is True

    def test_closed_after(self):
        assert is_market_open(datetime(2026, 6, 15, 14, 0)) is False

    def test_closed_weekend(self):
        assert is_market_open(datetime(2026, 6, 13, 10, 30)) is False

    def test_closed_on_holiday(self):
        """平日國定假日盤中時段也視為休市。"""
        trading_calendar._holiday_cache = {"2026-06-15"}
        assert is_market_open(datetime(2026, 6, 15, 10, 30)) is False
