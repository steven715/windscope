"""after_close job 測試。"""

from unittest.mock import patch


def test_after_close_skips_non_trading_day():
    """非交易日直接回傳 skipped。"""
    from jobs.after_close import run_after_close

    # 2026-04-05 是星期日
    result = run_after_close("2026-04-05", db_path=":memory:")
    assert result["status"] == "skipped"
    assert result["results"] == {}


def test_after_close_full_success(memory_db, monkeypatch):
    """所有 collector 成功時，status = completed。"""
    from jobs import after_close

    # Mock 所有步驟都成功
    def mock_step(name, fn):
        return True, None

    monkeypatch.setattr(after_close, "run_step", mock_step)
    monkeypatch.setattr(after_close, "is_trading_day", lambda d: True)

    with patch("jobs.after_close.get_connection") as mock_conn:
        mock_conn.return_value.__enter__ = lambda s: memory_db
        mock_conn.return_value.__exit__ = lambda s, *a: None

        result = after_close.run_after_close("2026-04-08")

    assert result["status"] == "completed"
    assert all(v is True for v in result["results"].values())
    assert result["errors"] == []


def test_after_close_partial_failure(memory_db, monkeypatch):
    """部分 collector 失敗時，status = partial，其他步驟仍執行。"""
    from jobs import after_close

    call_count = {"n": 0}

    def mock_step(name, fn):
        call_count["n"] += 1
        if name == "taifex_oi":
            return False, "taifex_oi: stub, returning None"
        return True, None

    monkeypatch.setattr(after_close, "run_step", mock_step)
    monkeypatch.setattr(after_close, "is_trading_day", lambda d: True)

    with patch("jobs.after_close.get_connection") as mock_conn:
        mock_conn.return_value.__enter__ = lambda s: memory_db
        mock_conn.return_value.__exit__ = lambda s, *a: None

        result = after_close.run_after_close("2026-04-08")

    assert result["status"] == "partial"
    assert result["results"]["taifex_oi"] is False
    assert result["results"]["twse_spot_close"] is True
    assert len(result["errors"]) == 1
    # 收盤後 6 步：5 個市場面步驟 + 個股日K趨勢刷新（stock_trends）
    assert call_count["n"] == 6
    # 拆出去的步驟不應再出現在 after_close
    for moved in ("twse_stock_close", "chip", "integration_chip", "fx_close"):
        assert moved not in result["results"]


def test_collectors_receive_injected_db_path(monkeypatch):
    """回歸：job 注入 db_path 時，各收集步驟的 collector 也要寫進同一個 DB（舊版用預設正式 DB）。"""
    import collectors.chip as chip
    import collectors.fx as fx
    import collectors.taifex as taifex
    import collectors.twse as twse
    from jobs import after_close, after_night, before_open, chip_collect

    seen = []

    class FakeCollector:
        def __init__(self, db_path=None):
            seen.append(db_path)

        def __getattr__(self, name):
            return lambda *a, **k: None  # collect_* 回 None → 步驟 no-data，不寫入

    monkeypatch.setattr(twse, "TWSECollector", FakeCollector)
    monkeypatch.setattr(taifex, "TAIFEXCollector", FakeCollector)
    monkeypatch.setattr(fx, "FXCollector", FakeCollector)
    monkeypatch.setattr(chip, "ChipCollector", FakeCollector)

    db = "/tmp/isolated.db"
    for fn in (after_close._collect_twse_spot_close, after_close._collect_twse_institutional,
               after_close._collect_twse_foreign_stock, after_close._collect_twse_ex_dividend,
               after_close._collect_taifex_oi, after_night._collect_taifex_night,
               after_night._collect_sp500, chip_collect._collect_twse_stock_close,
               chip_collect._collect_chip, before_open._collect_fx_intraday):
        assert fn("2026-04-08", db) is False

    assert seen == [db] * 10
