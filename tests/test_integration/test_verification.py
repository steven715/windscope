"""Layer 4 驗證引擎測試：三分類邊界、雙基準命中、資料缺失、統計。"""

import sqlite3

import pytest

from db.schema import create_all_tables
from integration.verification import (
    _classify_change,
    get_recent_verifications,
    get_verification_stats,
    verify_signal,
)


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    return conn


def _insert_signal(conn, date="2026-06-12", direction="bullish", confidence=3):
    conn.execute(
        "INSERT INTO signals (date, direction, confidence) VALUES (?, ?, ?)",
        (date, direction, confidence),
    )


def _insert_index(conn, date, open_=None, close=None):
    conn.execute(
        "INSERT INTO raw_index (date, open, close) VALUES (?, ?, ?)",
        (date, open_, close),
    )


def _insert_prev_marker(conn, date="2026-06-11"):
    """讓 get_previous_trading_day 能從 raw_futures 找到前一交易日。"""
    conn.execute("INSERT INTO raw_futures (date) VALUES (?)", (date,))


@pytest.fixture
def db_ready():
    """前一日收盤 43000、有前日 marker 的 DB。"""
    conn = _make_db()
    _insert_prev_marker(conn, "2026-06-11")
    _insert_index(conn, "2026-06-11", open_=42900.0, close=43000.0)
    return conn


class TestClassifyChange:
    def test_up_above_band(self):
        assert _classify_change(0.31) == "up"

    def test_down_below_band(self):
        assert _classify_change(-0.31) == "down"

    def test_flat_at_positive_boundary(self):
        """剛好 +0.3% 屬於平（含邊界）。"""
        assert _classify_change(0.3) == "flat"

    def test_flat_at_negative_boundary(self):
        assert _classify_change(-0.3) == "flat"

    def test_flat_zero(self):
        assert _classify_change(0.0) == "flat"


class TestVerifySignal:
    def test_bullish_hit(self, db_ready):
        """偏多 + 收盤漲超過 0.3% → 命中。"""
        _insert_signal(db_ready, direction="bullish")
        # 收盤 43400 → +0.93%；開盤 43300 → +0.70%
        _insert_index(db_ready, "2026-06-12", open_=43300.0, close=43400.0)

        result = verify_signal("2026-06-12", db_ready)

        assert result["hit_day"] == 1
        assert result["hit_open"] == 1
        assert result["day_change_class"] == "up"
        assert result["day_change_pct"] == pytest.approx(0.9302, abs=1e-3)

    def test_bullish_miss_on_drop(self, db_ready):
        """偏多但收盤跌 → 失誤。"""
        _insert_signal(db_ready, direction="bullish")
        _insert_index(db_ready, "2026-06-12", open_=43100.0, close=42500.0)

        result = verify_signal("2026-06-12", db_ready)

        assert result["hit_day"] == 0
        assert result["day_change_class"] == "down"

    def test_neutral_hit_on_flat(self, db_ready):
        """中性 + 收盤在 ±0.3% 內 → 命中。"""
        _insert_signal(db_ready, direction="neutral")
        # 收盤 43050 → +0.116%（平）
        _insert_index(db_ready, "2026-06-12", open_=43010.0, close=43050.0)

        result = verify_signal("2026-06-12", db_ready)

        assert result["hit_day"] == 1
        assert result["day_change_class"] == "flat"

    def test_open_hit_day_miss(self, db_ready):
        """開高走低：跳空基準命中、收盤基準失誤——兩基準分開記。"""
        _insert_signal(db_ready, direction="bullish")
        # 開盤 43300 (+0.70%, up)、收盤 42700 (-0.70%, down)
        _insert_index(db_ready, "2026-06-12", open_=43300.0, close=42700.0)

        result = verify_signal("2026-06-12", db_ready)

        assert result["hit_open"] == 1
        assert result["hit_day"] == 0

    def test_no_signal_returns_none(self, db_ready):
        _insert_index(db_ready, "2026-06-12", open_=43300.0, close=43400.0)
        assert verify_signal("2026-06-12", db_ready) is None

    def test_no_index_returns_none(self, db_ready):
        _insert_signal(db_ready)
        assert verify_signal("2026-06-12", db_ready) is None

    def test_no_prev_close_returns_none(self):
        """前一交易日無 raw_index 收盤 → None。"""
        conn = _make_db()
        _insert_prev_marker(conn, "2026-06-11")  # 有 marker 但無 index
        _insert_signal(conn)
        _insert_index(conn, "2026-06-12", open_=43300.0, close=43400.0)
        assert verify_signal("2026-06-12", conn) is None

    def test_idempotent_reverify(self, db_ready):
        """重複驗證同一天覆蓋舊值，不會多一筆。"""
        _insert_signal(db_ready, direction="bullish")
        _insert_index(db_ready, "2026-06-12", open_=43300.0, close=43400.0)

        verify_signal("2026-06-12", db_ready)
        verify_signal("2026-06-12", db_ready)

        rows = db_ready.execute("SELECT COUNT(*) FROM verifications").fetchone()
        assert rows[0] == 1


class TestVerificationStats:
    def _insert_verification(self, conn, date, confidence, hit_day, hit_open=0):
        conn.execute(
            "INSERT INTO verifications (date, confidence, hit_day, hit_open) "
            "VALUES (?, ?, ?, ?)",
            (date, confidence, hit_day, hit_open),
        )

    def test_empty_stats(self):
        conn = _make_db()
        stats = get_verification_stats(conn)
        assert stats["total"] == 0
        assert stats["hit_day_rate"] is None

    def test_hit_rates(self):
        conn = _make_db()
        self._insert_verification(conn, "2026-06-08", 3, 1, 1)
        self._insert_verification(conn, "2026-06-09", 4, 1, 0)
        self._insert_verification(conn, "2026-06-10", 2, 0, 0)
        self._insert_verification(conn, "2026-06-11", 4, 1, 1)

        stats = get_verification_stats(conn)

        assert stats["total"] == 4
        assert stats["hit_day_rate"] == 75.0
        assert stats["hit_open_rate"] == 50.0
        assert stats["by_confidence"][4]["rate"] == 100.0
        assert stats["by_confidence"][2]["rate"] == 0.0

    def test_last_n_window(self):
        """last_n 只統計最近 N 筆。"""
        conn = _make_db()
        self._insert_verification(conn, "2026-06-01", 3, 0)  # 舊的，全失誤
        self._insert_verification(conn, "2026-06-10", 3, 1)
        self._insert_verification(conn, "2026-06-11", 3, 1)

        stats = get_verification_stats(conn, last_n=2)

        assert stats["total"] == 2
        assert stats["hit_day_rate"] == 100.0

    def test_recent_verifications_order(self):
        conn = _make_db()
        self._insert_verification(conn, "2026-06-10", 3, 1)
        self._insert_verification(conn, "2026-06-11", 4, 0)

        recent = get_recent_verifications(conn, last_n=5)

        assert [r["date"] for r in recent] == ["2026-06-11", "2026-06-10"]
