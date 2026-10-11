"""個股籌碼判讀測試：三維度判讀、燈號、籌碼傾向規則、與籌碼觀察共用同一套分類。"""

import sqlite3

import pytest

from db.schema import create_all_tables
from integration.explain import build_stock_explain
from integration.chip_observation import refresh_chip_observations
from integration.stock_chip import (
    BrokerReading,
    ForeignReading,
    StockChip,
    ZoneReading,
    foreign_streak,
    load_stock_chip,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    create_all_tables(c)
    yield c
    c.close()


def _chip(f_cat=None, b_cat=None, f_ok=True, b_ok=True, z_ok=True) -> StockChip:
    return StockChip(
        "2330", "2026-06-18",
        ForeignReading(f_ok, latest_net=1.0, category=f_cat),
        BrokerReading(b_ok, data_date="2026-06-18", category=b_cat),
        ZoneReading(z_ok, pct=-5.0, zone="low"),
    )


def _foreign(c, stock_id, nets):
    """依序寫入 06-15、06-16、06-17 的外資淨張數。"""
    for d, n in zip(("2026-06-15", "2026-06-16", "2026-06-17"), nets):
        c.execute("INSERT INTO raw_chip (date, stock_id, broker_name, net_volume) "
                  "VALUES (?, ?, '__FOREIGN__', ?)", (d, stock_id, n))
    c.commit()


class TestLightsAndLean:
    def test_missing_data_is_na_not_flat(self):
        assert _chip(f_ok=False, b_ok=False, z_ok=False).lights == ["na", "na", "na"]
        assert _chip(b_ok=False).lights == ["flat", "na", "flat"]  # 有資料但無方向＝flat

    def test_lean_foreign_first(self):
        assert _chip(f_cat="外資連買").lean == "bull"
        assert _chip(f_cat="外資連賣", b_cat="bottom_watch").lean == "bear"  # 外資賣壓過分點買

    def test_lean_broker_assists(self):
        assert _chip(b_cat="bottom_watch").lean == "bull"
        assert _chip(b_cat="avoid").lean == "bear"
        assert _chip(b_cat="fake_volume").lean == "none"  # 警示類不算方向
        assert _chip().lean == "none"

    def test_zone_never_drives_lean(self):
        assert _chip(f_ok=False, b_ok=False).lean == "none"


def test_foreign_streak(conn):
    _foreign(conn, "2330", [-100, 2000, 3000])
    assert foreign_streak(conn, "2330", "2026-06-18") == (3000, 2, 5000.0)
    assert foreign_streak(conn, "9999", "2026-06-18") is None


def test_load_stock_chip_foreign_and_zone_fallback(conn):
    _foreign(conn, "2330", [-100, 2000, 3000])
    conn.execute(
        "INSERT INTO stock_trend_signals (date, stock_id, big_trend, bias_pct, rule_version) "
        "VALUES ('2026-06-17', '2330', 'UP', 0.03, 't2')")
    conn.commit()
    chip = load_stock_chip("2026-06-18", "2330", conn)
    assert chip.foreign.category == "外資連買" and chip.foreign.direction == "up"
    assert not chip.broker.available
    assert chip.zone.from_kline and chip.zone.zone == "consolidation"
    assert chip.lights == ["up", "na", "flat"] and chip.lean == "bull"


def test_explain_and_stock_signals_share_broker_wording(conn):
    """回歸：解讀與籌碼觀察（stock_signals）用同一句分點文案（舊版兩份各自漂移）。"""
    conn.execute("INSERT INTO watchlist (stock_id, stock_name) VALUES ('2454', '聯發科')")
    conn.execute(
        "INSERT INTO daily_stock_metrics (date, stock_id, broker_name, net_amount, "
        " consecutive_days, price_zone, both_sides_flag, broker_type) "
        "VALUES ('2026-06-18', '2454', '某分點', 80000000, 1, 'high', 1, NULL)")
    conn.commit()

    # 06-18 的分點影響下一交易日（06-19）的籌碼觀察
    stored = {r["reasons"] for r in refresh_chip_observations("2026-06-19", conn)}
    broker = next(r for r in build_stock_explain("2026-06-18", "2454", conn)
                  if r["dim"] == "主力分點")
    assert stored == {broker["verdict"]}
    assert "對敲" in broker["verdict"]


def test_explain_and_foreign_signals_agree(conn):
    """外資連買：籌碼觀察的理由與解讀的判讀為同一句。"""
    conn.execute("INSERT INTO watchlist (stock_id, stock_name) VALUES ('2330', '台積電')")
    _foreign(conn, "2330", [-100, 2000, 3000])
    stored = [r["reasons"] for r in refresh_chip_observations("2026-06-18", conn)]
    foreign = next(r for r in build_stock_explain("2026-06-18", "2330", conn)
                   if r["dim"] == "外資動向")
    assert stored == [foreign["verdict"]]
