"""Layer 3 籌碼觀察測試：個股分點過濾與分類、日期窗口。"""

import sqlite3

from db.schema import create_all_tables
from integration.signal_engine import compute_stock_signals


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    return conn


def _insert_stock_metric(conn, date="2026-06-11", stock_id="2330",
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


class TestStockSignalsDateWindow:
    """第 T 日的分點籌碼觀察只看 T 之前的分點指標（與外資一致：08:50 時 T 日未收）。"""

    def test_reads_previous_day_metrics(self):
        """chip_collect／匯入寫在 T-1 → before_open 的 T 日要讀得到。"""
        conn = _make_db()
        _insert_stock_metric(conn, date="2026-06-11", both_sides=1)
        results = compute_stock_signals("2026-06-12", conn)
        assert [r["category"] for r in results] == ["fake_volume"]
        row = conn.execute(
            "SELECT date FROM stock_signals WHERE stock_id = '2330'").fetchone()
        assert row[0] == "2026-06-12"

    def test_ignores_same_day_metrics(self):
        """T 日當天的分點指標不算進第 T 日的籌碼觀察（重算歷史日才與 08:50 一致）。"""
        conn = _make_db()
        _insert_stock_metric(conn, date="2026-06-12", both_sides=1)
        assert compute_stock_signals("2026-06-12", conn) == []

    def test_latest_date_per_stock(self):
        """每檔各取 T 之前最近一個有分點指標的日期，舊日的列不混進來。"""
        conn = _make_db()
        _insert_stock_metric(conn, date="2026-06-08", stock_id="2330",
                             broker_name="舊分點", both_sides=1)
        _insert_stock_metric(conn, date="2026-06-11", stock_id="2330",
                             broker_name="新分點", both_sides=1)
        _insert_stock_metric(conn, date="2026-06-05", stock_id="2454",
                             broker_name="久遠分點", both_sides=1)
        results = compute_stock_signals("2026-06-12", conn)
        got = {(r["stock_id"], r["broker_name"]) for r in results}
        assert got == {("2330", "新分點"), ("2454", "久遠分點")}

    def test_recompute_clears_stale_broker_observations(self):
        """重算先清當日舊的分點籌碼觀察；外資那列不動。"""
        conn = _make_db()
        _insert_stock_metric(conn, both_sides=1)
        assert compute_stock_signals("2026-06-12", conn)
        conn.execute(
            "INSERT INTO stock_signals (date, stock_id, broker_name, category, "
            "reasons, rule_version, created_at) "
            "VALUES ('2026-06-12', '2330', '外資', 'foreign_buy', 'r', 'v', 'now')")
        conn.execute("UPDATE daily_stock_metrics SET both_sides_flag = 0, "
                     "net_amount = 1e6")
        conn.commit()

        assert compute_stock_signals("2026-06-12", conn) == []
        rows = conn.execute(
            "SELECT broker_name FROM stock_signals WHERE date = '2026-06-12'"
        ).fetchall()
        assert rows == [("外資",)]
