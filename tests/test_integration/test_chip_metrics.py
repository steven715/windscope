"""Chip metrics integration 測試。"""

import sqlite3

import pytest

from db.schema import create_all_tables
from integration.chip_metrics import compute_chip_metrics


@pytest.fixture
def chip_db():
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    # broker_tags
    conn.execute(
        "INSERT INTO broker_tags VALUES ('兆豐-嘉義', 'swing', '長線主力')"
    )
    conn.execute(
        "INSERT INTO broker_tags VALUES ('凱基-台北', 'day_trade', '隔日沖高手')"
    )
    conn.commit()
    yield conn
    conn.close()


def _insert_chip(conn, date, stock_id, stock_name, broker_name,
                 buy_vol, sell_vol, net_vol, close_price=None):
    conn.execute(
        "INSERT INTO raw_chip "
        "(date, stock_id, stock_name, broker_name, "
        " buy_volume, sell_volume, net_volume, close_price) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (date, stock_id, stock_name, broker_name,
         buy_vol, sell_vol, net_vol, close_price),
    )
    conn.commit()


def _insert_price_only(conn, date, stock_id, close_price):
    """模擬 TWSE collector 寫入的 __PRICE_ONLY__ row。"""
    conn.execute(
        "INSERT INTO raw_chip "
        "(date, stock_id, stock_name, broker_name, close_price) "
        "VALUES (?, ?, '', '__PRICE_ONLY__', ?)",
        (date, stock_id, close_price),
    )
    conn.commit()


class TestNetAmount:
    def test_normal(self, chip_db):
        """net_amount = net_volume * close_price * 1000。"""
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     600, 100, 500, 895.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert len(results) == 1
        assert results[0]["net_amount"] == 500 * 895.0 * 1000

    def test_close_price_null_uses_price_only(self, chip_db):
        """broker row 無 close_price → 從 __PRICE_ONLY__ row 取得。"""
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     600, 100, 500, None)
        _insert_price_only(chip_db, "2026-04-08", "2330", 895.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert results[0]["net_amount"] == 500 * 895.0 * 1000

    def test_no_close_price_at_all(self, chip_db):
        """完全無 close_price → net_amount = None。"""
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     600, 100, 500, None)

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert results[0]["net_amount"] is None


class TestConsecutiveDays:
    def test_consecutive_buy_4_days(self, chip_db):
        """連買 4 天（含今天）→ 回傳 4。"""
        for d in ["2026-04-03", "2026-04-04", "2026-04-07"]:
            _insert_chip(chip_db, d, "2330", "台積電", "兆豐-嘉義",
                         500, 200, 300, 890.0)
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     600, 100, 500, 895.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert results[0]["consecutive_days"] == 4

    def test_consecutive_interrupted(self, chip_db):
        """中間插入賣超 → 只算中斷後的天數。"""
        _insert_chip(chip_db, "2026-04-03", "2330", "台積電", "兆豐-嘉義",
                     500, 200, 300, 890.0)
        _insert_chip(chip_db, "2026-04-04", "2330", "台積電", "兆豐-嘉義",
                     100, 500, -400, 885.0)  # sell day
        _insert_chip(chip_db, "2026-04-07", "2330", "台積電", "兆豐-嘉義",
                     600, 200, 400, 890.0)
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     600, 100, 500, 895.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert results[0]["consecutive_days"] == 2  # only 04-07 + 04-08

    def test_consecutive_sell(self, chip_db):
        """連續賣超 → 回傳負數。"""
        _insert_chip(chip_db, "2026-04-07", "2330", "台積電", "兆豐-嘉義",
                     100, 600, -500, 880.0)
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     50, 400, -350, 875.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert results[0]["consecutive_days"] == -2

    def test_net_volume_zero(self, chip_db):
        """net_volume = 0 → consecutive_days = 0。"""
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     300, 300, 0, 895.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert results[0]["consecutive_days"] == 0

    def test_no_history(self, chip_db):
        """無歷史 → consecutive_days = 1（只有今天）。"""
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     600, 100, 500, 895.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert results[0]["consecutive_days"] == 1


class TestPriceVsMa20:
    def test_sufficient_history(self, chip_db):
        """歷史 >= MA_PERIOD → 正常計算。"""
        # 20 days of close_price history via __PRICE_ONLY__
        dates = [f"2026-03-{d:02d}" for d in range(5, 25)]
        for d in dates:
            _insert_price_only(chip_db, d, "2330", 880.0)
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     600, 100, 500, 900.0)
        _insert_price_only(chip_db, "2026-04-08", "2330", 900.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        # MA20 from these 20 dates at 880 + today at 900:
        # Actually LIMIT 20 ordered by date DESC includes today
        # The 20 most recent: 04-08(900) + 19 days of 880
        # MA = (900 + 19*880) / 20 = (900 + 16720) / 20 = 17620/20 = 881
        expected_ma = (900.0 + 19 * 880.0) / 20
        expected_pct = round((900.0 - expected_ma) / expected_ma * 100, 2)
        assert results[0]["price_vs_ma20"] == pytest.approx(expected_pct, abs=0.1)

    def test_insufficient_history(self, chip_db):
        """歷史 < 5 天 → price_vs_ma20 = None。"""
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     600, 100, 500, 900.0)
        _insert_price_only(chip_db, "2026-04-08", "2330", 900.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert results[0]["price_vs_ma20"] is None

    def test_partial_history_above_min(self, chip_db):
        """5 <= 歷史 < 20 → 用現有資料算。"""
        for d in [f"2026-04-0{i}" for i in range(1, 8)]:
            _insert_price_only(chip_db, d, "2330", 880.0)
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     600, 100, 500, 900.0)
        _insert_price_only(chip_db, "2026-04-08", "2330", 900.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert results[0]["price_vs_ma20"] is not None


class TestPriceZone:
    def test_low_zone(self, chip_db):
        """price_vs_ma20 < -20 → low。"""
        # Create enough history with high prices, then current low
        for d in [f"2026-04-0{i}" for i in range(1, 8)]:
            _insert_price_only(chip_db, d, "2330", 1000.0)
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     600, 100, 500, 700.0)
        _insert_price_only(chip_db, "2026-04-08", "2330", 700.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert results[0]["price_zone"] == "low"

    def test_consolidation_zone(self, chip_db):
        """-5 <= price_vs_ma20 <= 5 → consolidation。"""
        for d in [f"2026-04-0{i}" for i in range(1, 8)]:
            _insert_price_only(chip_db, d, "2330", 900.0)
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     600, 100, 500, 900.0)
        _insert_price_only(chip_db, "2026-04-08", "2330", 900.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert results[0]["price_zone"] == "consolidation"

    def test_high_zone(self, chip_db):
        """price_vs_ma20 > 20 → high。"""
        for d in [f"2026-04-0{i}" for i in range(1, 8)]:
            _insert_price_only(chip_db, d, "2330", 700.0)
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     600, 100, 500, 900.0)
        _insert_price_only(chip_db, "2026-04-08", "2330", 900.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert results[0]["price_zone"] == "high"

    def test_other_zone(self, chip_db):
        """介於 consolidation 和 high 之間 → other。"""
        for d in [f"2026-04-0{i}" for i in range(1, 8)]:
            _insert_price_only(chip_db, d, "2330", 850.0)
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     600, 100, 500, 950.0)
        _insert_price_only(chip_db, "2026-04-08", "2330", 950.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        # (950 - ~864) / ~864 * 100 ≈ 10% → other (between 5 and 20)
        assert results[0]["price_zone"] == "other"


class TestBothSidesFlag:
    def test_both_sides(self, chip_db):
        """buy > 0 且 sell > 0 → flag = 1。"""
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     600, 100, 500, 895.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert results[0]["both_sides_flag"] == 1

    def test_buy_only(self, chip_db):
        """sell = 0 → flag = 0。"""
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     600, 0, 600, 895.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert results[0]["both_sides_flag"] == 0


class TestBrokerType:
    def test_known_broker(self, chip_db):
        """已知分點 → 回傳 broker_type。"""
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     600, 100, 500, 895.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert results[0]["broker_type"] == "swing"

    def test_unknown_broker(self, chip_db):
        """未知分點 → broker_type = None。"""
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "元大-中壢",
                     600, 100, 500, 895.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert results[0]["broker_type"] is None


class TestEdgeCases:
    def test_no_chip_data(self, chip_db):
        """無籌碼資料 → 回傳空 list。"""
        results = compute_chip_metrics("2026-04-08", chip_db)
        assert results == []

    def test_excludes_price_only_and_foreign_rows(self, chip_db):
        """__PRICE_ONLY__ 和 __FOREIGN__ 不算入籌碼計算。"""
        _insert_price_only(chip_db, "2026-04-08", "2330", 895.0)
        chip_db.execute(
            "INSERT INTO raw_chip (date, stock_id, stock_name, broker_name, "
            "net_volume) VALUES (?, ?, ?, ?, ?)",
            ("2026-04-08", "2330", "台積電", "__FOREIGN__", 1000),
        )
        chip_db.commit()

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert results == []

    def test_multiple_brokers(self, chip_db):
        """同股票多個分點 → 每個分點各一筆結果。"""
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     600, 100, 500, 895.0)
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "凱基-台北",
                     200, 50, 150, 895.0)

        results = compute_chip_metrics("2026-04-08", chip_db)

        assert len(results) == 2
        brokers = {r["broker_name"] for r in results}
        assert brokers == {"兆豐-嘉義", "凱基-台北"}

    def test_writes_to_daily_stock_metrics(self, chip_db):
        """確認寫入 daily_stock_metrics。"""
        _insert_chip(chip_db, "2026-04-08", "2330", "台積電", "兆豐-嘉義",
                     600, 100, 500, 895.0)

        compute_chip_metrics("2026-04-08", chip_db)

        row = chip_db.execute(
            "SELECT net_amount, consecutive_days, broker_type "
            "FROM daily_stock_metrics "
            "WHERE date = ? AND stock_id = ? AND broker_name = ?",
            ("2026-04-08", "2330", "兆豐-嘉義"),
        ).fetchone()
        assert row is not None
        assert row[0] == 500 * 895.0 * 1000
        assert row[1] == 1
        assert row[2] == "swing"
