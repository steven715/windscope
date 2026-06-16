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
        # 讓 taifex_oi 失敗
        if name == "taifex_oi":
            return False, "taifex_oi: stub, returning None"
        # 讓 chip 也失敗
        if name == "chip":
            return False, "chip: stub, returning None"
        return True, None

    monkeypatch.setattr(after_close, "run_step", mock_step)
    monkeypatch.setattr(after_close, "is_trading_day", lambda d: True)

    with patch("jobs.after_close.get_connection") as mock_conn:
        mock_conn.return_value.__enter__ = lambda s: memory_db
        mock_conn.return_value.__exit__ = lambda s, *a: None

        result = after_close.run_after_close("2026-04-08")

    assert result["status"] == "partial"
    assert result["results"]["taifex_oi"] is False
    assert result["results"]["chip"] is False
    assert result["results"]["twse_spot_close"] is True
    assert len(result["errors"]) == 2
    # 確認所有 11 個步驟都有被呼叫（含 CNY/KRW 收盤基準）
    assert call_count["n"] == 11


def test_collect_fx_close_foreign_saves_close_16():
    """CNY/KRW 收盤基準正確存為 close_16（供隔日亞幣同步）。"""
    from jobs.after_close import _collect_fx_close_foreign

    with patch("collectors.fx.FXCollector") as MockFX:
        inst = MockFX.return_value
        inst.collect_foreign_fx.return_value = {"currency_pair": "USD/CNY", "rate": 7.25}

        ok = _collect_fx_close_foreign("2026-06-16", None, "USD/CNY")

    assert ok is True
    inst.save_fx.assert_called_once_with("2026-06-16", "USD/CNY", 7.25, "close_16")


def test_collect_fx_close_foreign_none_returns_false():
    """Yahoo 抓不到 → 回 False，不存。"""
    from jobs.after_close import _collect_fx_close_foreign

    with patch("collectors.fx.FXCollector") as MockFX:
        inst = MockFX.return_value
        inst.collect_foreign_fx.return_value = None

        ok = _collect_fx_close_foreign("2026-06-16", None, "USD/CNY")

    assert ok is False
    inst.save_fx.assert_not_called()
