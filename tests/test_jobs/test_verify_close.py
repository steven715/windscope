"""verify-close job 流程測試：graceful degradation、非交易日跳過。"""

import sqlite3
from unittest.mock import patch

from db.schema import create_all_tables
from jobs.verify_close import (
    _backfill_unverified_signals,
    _ensure_prev_index_baseline,
    run_verify_close,
)


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
    """OHLC 收集失敗 → verify 也失敗，但 job 不崩潰、不寫驗證紀錄。"""
    db_path = _setup_db(tmp_path)

    with patch(
        "collectors.twse.TWSECollector.collect_index_ohlc",
        side_effect=Exception("模擬失敗"),
    ):
        result = run_verify_close("2026-06-12", db_path=db_path)

    # 前日基準(6/11)本來就在 → prev_index_baseline 成功，整體為 partial
    assert result["status"] == "partial"
    assert result["results"]["index_ohlc"] is False
    assert result["results"]["verify"] is False
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


def _db_prev_marker(tmp_path, with_prev_index: bool) -> str:
    """建立 DB：前一交易日 marker（raw_futures 6/12），可選是否含 raw_index 基準。"""
    db_path = str(tmp_path / "b.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.execute("INSERT INTO raw_futures (date) VALUES ('2026-06-12')")
    if with_prev_index:
        conn.execute(
            "INSERT INTO raw_index (date, open, close) "
            "VALUES ('2026-06-12', 43587, 44169.04)"
        )
    conn.commit()
    conn.close()
    return db_path


class TestEnsurePrevBaseline:
    def test_present_skips_backfill(self, tmp_path):
        """前日基準已存在 → 回 True 且不打網路。"""
        db_path = _db_prev_marker(tmp_path, with_prev_index=True)
        with patch("collectors.twse.TWSECollector.collect_index_ohlc") as m:
            ok = _ensure_prev_index_baseline("2026-06-15", db_path)
        assert ok is True
        m.assert_not_called()

    def test_backfills_when_missing(self, tmp_path):
        """前日基準缺失 → 從 MI_5MINS_HIST 補上並寫入 raw_index。"""
        db_path = _db_prev_marker(tmp_path, with_prev_index=False)
        ohlc = {"open": 43587.0, "high": 44798.0, "low": 43587.0, "close": 44169.04}
        with patch("collectors.twse.TWSECollector.collect_index_ohlc",
                   return_value=ohlc) as m:
            ok = _ensure_prev_index_baseline("2026-06-15", db_path)
        assert ok is True
        m.assert_called_once_with("2026-06-12")
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT close FROM raw_index WHERE date = '2026-06-12'").fetchone()
        conn.close()
        assert row[0] == 44169.04

    def test_returns_false_when_backfill_unavailable(self, tmp_path):
        """前日缺失且補不到（假日/無資料）→ 回 False，不報錯。"""
        db_path = _db_prev_marker(tmp_path, with_prev_index=False)
        with patch("collectors.twse.TWSECollector.collect_index_ohlc",
                   return_value=None):
            ok = _ensure_prev_index_baseline("2026-06-15", db_path)
        assert ok is False


class TestBackfillUnverified:
    """自癒：補驗最近漏掉 verification 的訊號日（重現 6/25 指數延遲發布情境）。"""

    def _seed(self, tmp_path) -> str:
        """6/24、6/25 都有訊號與指數，但都還沒 verification；前日基準 6/23 在。"""
        db_path = str(tmp_path / "bf.db")
        conn = sqlite3.connect(db_path)
        create_all_tables(conn)
        conn.execute(
            "INSERT INTO raw_index (date, open, close) "
            "VALUES ('2026-06-23', 46100, 46043.6)")
        conn.execute(
            "INSERT INTO raw_index (date, open, close) "
            "VALUES ('2026-06-24', 46909.98, 46043.6)")
        conn.execute(
            "INSERT INTO raw_index (date, open, close) "
            "VALUES ('2026-06-25', 46339.68, 46255.26)")
        conn.executemany(
            "INSERT INTO signals (date, direction, confidence) VALUES (?, ?, ?)",
            [("2026-06-24", "bearish", 3), ("2026-06-25", "bullish", 2)])
        conn.commit()
        conn.close()
        return db_path

    def test_backfills_missing_verifications(self, tmp_path):
        """漏驗的 6/24、6/25 在後續日子被補上 verification 列。"""
        db_path = self._seed(tmp_path)
        conn = sqlite3.connect(db_path)
        ok = _backfill_unverified_signals("2026-06-26", conn)
        assert ok is True
        dates = [r[0] for r in conn.execute(
            "SELECT date FROM verifications ORDER BY date")]
        conn.close()
        assert dates == ["2026-06-24", "2026-06-25"]

    def test_skips_dates_still_missing_index(self, tmp_path):
        """訊號日仍缺自身指數 → 略過不補、不報錯，留待下次。"""
        db_path = str(tmp_path / "bf2.db")
        conn = sqlite3.connect(db_path)
        create_all_tables(conn)
        conn.execute(
            "INSERT INTO raw_index (date, open, close) "
            "VALUES ('2026-06-23', 46100, 46043.6)")  # 只有前日基準
        conn.execute(
            "INSERT INTO signals (date, direction, confidence) "
            "VALUES ('2026-06-24', 'bearish', 3)")  # 6/24 自身無指數
        conn.commit()
        ok = _backfill_unverified_signals("2026-06-26", conn)
        cnt = conn.execute("SELECT COUNT(*) FROM verifications").fetchone()[0]
        conn.close()
        assert ok is True
        assert cnt == 0

    def test_run_verify_close_backfills_prior_day(self, tmp_path):
        """整合：跑 6/26 的 verify_close 會順手補驗漏掉的 6/25。"""
        db_path = self._seed(tmp_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO signals (date, direction, confidence) "
            "VALUES ('2026-06-26', 'bearish', 3)")
        conn.commit()
        conn.close()

        ohlc = {"open": 46188.6, "high": 46188.6, "low": 44454.22, "close": 44571.76}
        with patch("collectors.twse.TWSECollector.collect_index_ohlc",
                   return_value=ohlc):
            result = run_verify_close("2026-06-26", db_path=db_path)

        assert result["results"]["backfill_unverified"] is True
        assert result["status"] == "completed"
        conn = sqlite3.connect(db_path)
        dates = [r[0] for r in conn.execute(
            "SELECT date FROM verifications ORDER BY date")]
        conn.close()
        assert dates == ["2026-06-24", "2026-06-25", "2026-06-26"]


def test_full_flow_backfills_prev_then_completes(tmp_path):
    """重現 6/15 情境：前日指數缺失時自動補齊後仍能完成驗證。"""
    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.execute("INSERT INTO raw_futures (date) VALUES ('2026-06-12')")  # 無 raw_index
    conn.execute(
        "INSERT INTO signals (date, direction, confidence) "
        "VALUES ('2026-06-15', 'neutral', 2)"
    )
    conn.commit()
    conn.close()

    ohlc = {"open": 44447.0, "high": 45483.0, "low": 44447.0, "close": 45396.0}
    with patch("collectors.twse.TWSECollector.collect_index_ohlc", return_value=ohlc):
        result = run_verify_close("2026-06-15", db_path=db_path)

    assert result["results"]["prev_index_baseline"] is True
    assert result["status"] == "completed"
    assert result["verification"] is not None
