"""籌碼分點收集 job 測試：非交易日略過、自給自足(個股收盤+分點+算指標)、token 行為。"""

from unittest.mock import patch


def test_chip_collect_skips_non_trading_day():
    """非交易日直接 skipped。"""
    from jobs.chip_collect import run_chip_collect

    # 2026-04-05 為星期日
    result = run_chip_collect("2026-04-05", db_path=":memory:")
    assert result["status"] == "skipped"
    assert result["results"] == {}


def test_chip_collect_skips_chip_without_token(memory_db, monkeypatch):
    """未設 FINMIND_TOKEN → 分點步驟整步略過、不計失敗，可達 completed。"""
    from jobs import chip_collect

    monkeypatch.setattr(chip_collect, "run_step", lambda name, fn: (True, None))
    monkeypatch.setattr(chip_collect, "is_trading_day", lambda d: True)
    monkeypatch.setattr(chip_collect.settings, "FINMIND_TOKEN", "")

    with patch("jobs.chip_collect.get_connection") as mock_conn:
        mock_conn.return_value.__enter__ = lambda s: memory_db
        mock_conn.return_value.__exit__ = lambda s, *a: None
        result = chip_collect.run_chip_collect("2026-04-08")

    assert "chip" not in result["results"]            # 分點被略過
    assert result["results"]["twse_stock_close"] is True
    assert result["results"]["integration_chip"] is True
    assert result["status"] == "completed"


def test_chip_collect_runs_chip_with_token(memory_db, monkeypatch):
    """設了 FINMIND_TOKEN → 個股收盤 + 分點 + 算指標 三步都跑。"""
    from jobs import chip_collect

    calls = []
    monkeypatch.setattr(chip_collect, "run_step",
                        lambda name, fn: calls.append(name) or (True, None))
    monkeypatch.setattr(chip_collect, "is_trading_day", lambda d: True)
    monkeypatch.setattr(chip_collect.settings, "FINMIND_TOKEN", "test-token")

    with patch("jobs.chip_collect.get_connection") as mock_conn:
        mock_conn.return_value.__enter__ = lambda s: memory_db
        mock_conn.return_value.__exit__ = lambda s, *a: None
        result = chip_collect.run_chip_collect("2026-04-08")

    assert calls == ["twse_stock_close", "chip", "integration_chip"]
    assert result["status"] == "completed"
