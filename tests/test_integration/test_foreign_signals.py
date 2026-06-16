"""外資流向個股訊號測試（用 T86 每檔外資買賣超）。"""

import sqlite3

from db.schema import create_all_tables
from integration.signal_engine import compute_foreign_stock_signals


def _setup(conn, nets):
    """watchlist 放 2330，塞入 __FOREIGN__ 每日淨額 nets=[(date, net), ...]。"""
    conn.execute("INSERT INTO watchlist (stock_id, stock_name, added_date, reason) "
                 "VALUES ('2330', '台積電', '2026-04-08', 't')")
    for d, net in nets:
        conn.execute(
            "INSERT INTO raw_chip (date, stock_id, broker_name, net_volume) "
            "VALUES (?, '2330', '__FOREIGN__', ?)", (d, net))
    conn.commit()


def test_consecutive_buy_signal():
    """外資連買 2 天且累計過門檻 → 外資連買訊號。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    # 6/11 賣(中斷)、6/12 +3927、6/15 +9646 → 截至 6/15 連買 2 天，累計 13573
    _setup(conn, [("2026-06-11", -4904), ("2026-06-12", 3927), ("2026-06-15", 9646)])

    res = compute_foreign_stock_signals("2026-06-16", conn)

    assert len(res) == 1
    assert res[0]["category"] == "外資連買"
    assert "連買 2 天" in res[0]["reason"]
    row = conn.execute(
        "SELECT broker_name, category FROM stock_signals "
        "WHERE date='2026-06-16' AND stock_id='2330'").fetchone()
    assert row == ("外資", "外資連買")


def test_single_big_buy_signal():
    """外資單日大買（>1萬張）即使沒連續也標『外資大買』。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    _setup(conn, [("2026-06-12", -2000), ("2026-06-15", 18230)])  # 反手大買

    res = compute_foreign_stock_signals("2026-06-16", conn)
    assert res[0]["category"] == "外資大買"
    assert "18,230" in res[0]["reason"]


def test_below_threshold_no_signal():
    """連買但累計未達門檻 → 不產生訊號。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    _setup(conn, [("2026-06-12", 500), ("2026-06-15", 800)])  # 累計 1300 < 3000

    assert compute_foreign_stock_signals("2026-06-16", conn) == []


def test_today_data_excluded():
    """只用『今日之前』的外資資料（今日 T86 未收）。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    # 只有今天(6/16)有資料 → date < 6/16 查無 → 無訊號
    _setup(conn, [("2026-06-16", 50000)])

    assert compute_foreign_stock_signals("2026-06-16", conn) == []
