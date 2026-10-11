"""after_night job 測試。"""

from unittest.mock import patch


def test_after_night_skips_non_trading_day():
    """非交易日直接回傳 skipped。"""
    from jobs.after_night import run_after_night

    result = run_after_night("2026-04-05", db_path=":memory:")
    assert result["status"] == "skipped"
    assert result["results"] == {}


def test_after_night_full_success(memory_db, monkeypatch, step_seam):
    """所有步驟成功時，status = completed。"""
    from jobs import after_night

    def mock_step(name, fn):
        return True, None

    step_seam(mock_step)
    monkeypatch.setattr(after_night, "is_trading_day", lambda d: True)

    with patch("jobs.after_night.get_connection") as mock_conn:
        mock_conn.return_value.__enter__ = lambda s: memory_db
        mock_conn.return_value.__exit__ = lambda s, *a: None

        result = after_night.run_after_night("2026-04-08")

    assert result["status"] == "completed"
    assert all(v is True for v in result["results"].values())


def test_after_night_partial_failure(memory_db, monkeypatch, step_seam):
    """部分步驟失敗時，status = partial。"""
    from jobs import after_night

    def mock_step(name, fn):
        if name == "sp500_close":
            return False, "sp500_close: failed"
        return True, None

    step_seam(mock_step)
    monkeypatch.setattr(after_night, "is_trading_day", lambda d: True)

    with patch("jobs.after_night.get_connection") as mock_conn:
        mock_conn.return_value.__enter__ = lambda s: memory_db
        mock_conn.return_value.__exit__ = lambda s, *a: None

        result = after_night.run_after_night("2026-04-08")

    assert result["status"] == "partial"
    assert result["results"]["sp500_close"] is False
    assert result["results"]["taifex_night"] is True


def test_after_night_saturday_targets_next_monday(memory_db, monkeypatch, step_seam):
    """週六觸發（前日週五有夜盤）→ 夜盤歸屬下週一，補上週一早盤的期貨缺口。"""
    from jobs import after_night

    step_seam(lambda name, fn: (True, None))

    with patch("jobs.after_night.get_connection") as mock_conn:
        mock_conn.return_value.__enter__ = lambda s: memory_db
        mock_conn.return_value.__exit__ = lambda s, *a: None

        result = after_night.run_after_night("2026-06-13")  # 週六

    assert result["status"] == "completed"
    assert result["date"] == "2026-06-15"  # 歸屬下週一


def test_after_night_sunday_skips_no_session():
    """週日觸發（前日週六無夜盤）→ skipped。"""
    from jobs.after_night import run_after_night

    result = run_after_night("2026-06-14", db_path=":memory:")  # 週日
    assert result["status"] == "skipped"
    assert result["results"] == {}


def test_saturday_wiring_saves_to_target_monday(monkeypatch, tmp_path):
    """接線：週六觸發 → 夜盤與 S&P 收盤都以下週一（target）存檔；S&P 取 data["close"]。"""
    import sqlite3

    import collectors.fx as fx
    import collectors.taifex as taifex
    from db.schema import create_all_tables
    from jobs import after_night

    saved = []

    class FakeTaifex:
        def __init__(self, db_path=None):
            pass

        def collect_night_session(self, date):
            return {"night_close": 23000.0, "for": date}

        def save_night_session(self, date, data):
            saved.append(("night", date, data["for"]))

    class FakeFX:
        def __init__(self, db_path=None):
            pass

        def collect_sp500(self):
            return {"close": 5800.5}

        def save_sp500(self, date, close):
            saved.append(("sp500", date, close))

    monkeypatch.setattr(taifex, "TAIFEXCollector", FakeTaifex)
    monkeypatch.setattr(fx, "FXCollector", FakeFX)
    db = str(tmp_path / "n.db")
    conn = sqlite3.connect(db)
    create_all_tables(conn)
    conn.close()

    result = after_night.run_after_night("2026-04-11", db)  # 週六 → 下週一 04-13

    assert result["date"] == "2026-04-13"
    assert saved == [("night", "2026-04-13", "2026-04-13"), ("sp500", "2026-04-13", 5800.5)]
    assert result["results"]["taifex_night"] is True
    assert result["results"]["sp500_close"] is True
