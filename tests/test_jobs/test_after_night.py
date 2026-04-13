"""after_night job 測試。"""

from unittest.mock import patch


def test_after_night_skips_non_trading_day():
    """非交易日直接回傳 skipped。"""
    from jobs.after_night import run_after_night

    result = run_after_night("2026-04-05", db_path=":memory:")
    assert result["status"] == "skipped"
    assert result["results"] == {}


def test_after_night_full_success(memory_db, monkeypatch):
    """所有步驟成功時，status = completed。"""
    from jobs import after_night

    def mock_step(name, fn):
        return True, None

    monkeypatch.setattr(after_night, "run_step", mock_step)
    monkeypatch.setattr(after_night, "is_trading_day", lambda d: True)

    with patch("jobs.after_night.get_connection") as mock_conn:
        mock_conn.return_value.__enter__ = lambda s: memory_db
        mock_conn.return_value.__exit__ = lambda s, *a: None

        result = after_night.run_after_night("2026-04-08")

    assert result["status"] == "completed"
    assert all(v is True for v in result["results"].values())


def test_after_night_partial_failure(memory_db, monkeypatch):
    """部分步驟失敗時，status = partial。"""
    from jobs import after_night

    def mock_step(name, fn):
        if name == "sp500_close":
            return False, "sp500_close: stub"
        return True, None

    monkeypatch.setattr(after_night, "run_step", mock_step)
    monkeypatch.setattr(after_night, "is_trading_day", lambda d: True)

    with patch("jobs.after_night.get_connection") as mock_conn:
        mock_conn.return_value.__enter__ = lambda s: memory_db
        mock_conn.return_value.__exit__ = lambda s, *a: None

        result = after_night.run_after_night("2026-04-08")

    assert result["status"] == "partial"
    assert result["results"]["sp500_close"] is False
    assert result["results"]["taifex_night"] is True
