"""verify-close job 流程測試：graceful degradation、非交易日跳過。"""

import sqlite3
from unittest.mock import patch

from db.schema import create_all_tables
from jobs.verify_close import run_verify_close


def _setup_db(tmp_path) -> str:
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    # 前一交易日 marker + 指數收盤
    conn.execute("INSERT INTO raw_futures (date) VALUES ('2026-06-11')")
    conn.execute(
        "INSERT INTO raw_index (date, open, close) VALUES ('2026-06-11', 42900, 43000)"
    )
    # 當日早上的訊號
    conn.execute(
        "INSERT INTO signals (date, direction, confidence) "
        "VALUES ('2026-06-12', 'bullish', 3)"
    )
    conn.commit()
    conn.close()
    return db_path


def test_full_flow_hit(tmp_path):
    """收集 OHLC 成功 + 訊號命中 → completed。"""
    db_path = _setup_db(tmp_path)
    ohlc = {"open": 43300.0, "high": 43500.0, "low": 43200.0, "close": 43400.0}

    with patch("collectors.twse.TWSECollector.collect_index_ohlc", return_value=ohlc):
        result = run_verify_close("2026-06-12", db_path=db_path)

    assert result["status"] == "completed"
    assert result["results"]["index_ohlc"] is True
    assert result["results"]["verify"] is True
    assert result["verification"]["hit_day"] == 1


def test_collector_failure_does_not_crash(tmp_path):
    """OHLC 收集失敗 → verify 也失敗，但 job 正常回傳 failed。"""
    db_path = _setup_db(tmp_path)

    with patch(
        "collectors.twse.TWSECollector.collect_index_ohlc",
        side_effect=Exception("模擬失敗"),
    ):
        result = run_verify_close("2026-06-12", db_path=db_path)

    assert result["status"] == "failed"
    assert result["results"]["index_ohlc"] is False
    assert result["verification"] is None
    assert len(result["errors"]) > 0


def test_no_signal_partial(tmp_path):
    """有 OHLC 但當天沒訊號 → partial。"""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.execute("INSERT INTO raw_futures (date) VALUES ('2026-06-11')")
    conn.execute(
        "INSERT INTO raw_index (date, open, close) VALUES ('2026-06-11', 42900, 43000)"
    )
    conn.commit()
    conn.close()

    ohlc = {"open": 43300.0, "high": 43500.0, "low": 43200.0, "close": 43400.0}
    with patch("collectors.twse.TWSECollector.collect_index_ohlc", return_value=ohlc):
        result = run_verify_close("2026-06-12", db_path=db_path)

    assert result["status"] == "partial"
    assert result["results"]["index_ohlc"] is True
    assert result["results"]["verify"] is False


def test_skips_non_trading_day(tmp_path):
    """週末直接跳過。"""
    result = run_verify_close("2026-06-13", db_path=str(tmp_path / "t.db"))  # 週六
    assert result["status"] == "skipped"
    assert result["results"] == {}
