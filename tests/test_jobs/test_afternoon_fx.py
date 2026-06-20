"""匯率收盤收集 job 測試：非交易日略過、交易日收 close_16、collector 失敗 graceful。"""

import sqlite3
from unittest.mock import patch

from db.schema import create_all_tables


def _db(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.close()
    return db_path


def test_non_trading_day_skipped():
    """非交易日（週末/假日）整步略過，不收任何匯率。"""
    from jobs.afternoon_fx import run_afternoon_fx

    # 2026-06-20 為星期六
    result = run_afternoon_fx("2026-06-20", db_path=":memory:")
    assert result["status"] == "skipped"
    assert result["results"] == {}


def test_collects_close_16_on_trading_day(tmp_path):
    """交易日收 4 檔匯率到 close_16 槽。"""
    from jobs.afternoon_fx import run_afternoon_fx

    db_path = _db(tmp_path)
    with patch("collectors.fx.FXCollector.collect_pair",
               side_effect=lambda date, pair: {"currency_pair": pair, "rate": 10.0}):
        # 2026-06-16 為星期二（無假日）
        result = run_afternoon_fx("2026-06-16", db_path=db_path)

    assert result["status"] == "completed"
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT currency_pair, close_16 FROM raw_fx WHERE date='2026-06-16'"
    ).fetchall()
    conn.close()
    assert len(rows) == 4
    assert all(r[1] == 10.0 for r in rows)


def test_continues_on_collector_failure(tmp_path):
    """單一幣別收不到 → 該步驟 False，其餘照常，整體 partial。"""
    from jobs.afternoon_fx import run_afternoon_fx

    db_path = _db(tmp_path)

    def fake(date, pair):
        return None if pair == "USD/KRW" else {"currency_pair": pair, "rate": 5.0}

    with patch("collectors.fx.FXCollector.collect_pair", side_effect=fake):
        result = run_afternoon_fx("2026-06-16", db_path=db_path)

    assert result["status"] == "partial"
    assert result["results"]["fx_close_krw"] is False
    assert result["results"]["fx_close_twd"] is True
