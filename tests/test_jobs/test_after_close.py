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
    monkeypatch.setattr(after_close.settings, "FINMIND_TOKEN", "test-token")  # 讓 chip 步驟執行

    with patch("jobs.after_close.get_connection") as mock_conn:
        mock_conn.return_value.__enter__ = lambda s: memory_db
        mock_conn.return_value.__exit__ = lambda s, *a: None

        result = after_close.run_after_close("2026-04-08")

    assert result["status"] == "partial"
    assert result["results"]["taifex_oi"] is False
    assert result["results"]["chip"] is False
    assert result["results"]["twse_spot_close"] is True
    assert len(result["errors"]) == 2
    # 設了 token → 12 個步驟都有被呼叫（含 CNY/KRW/JPY 收盤基準 + chip）
    assert call_count["n"] == 12


def test_after_close_skips_chip_without_token(memory_db, monkeypatch):
    """未設 FINMIND_TOKEN → 分點步驟整步略過、不計失敗，可達 completed。"""
    from jobs import after_close

    monkeypatch.setattr(after_close, "run_step", lambda name, fn: (True, None))
    monkeypatch.setattr(after_close, "is_trading_day", lambda d: True)
    monkeypatch.setattr(after_close.settings, "FINMIND_TOKEN", "")

    with patch("jobs.after_close.get_connection") as mock_conn:
        mock_conn.return_value.__enter__ = lambda s: memory_db
        mock_conn.return_value.__exit__ = lambda s, *a: None

        result = after_close.run_after_close("2026-04-08")

    assert "chip" not in result["results"]   # 分點步驟被略過
    assert result["status"] == "completed"   # 不再因分點而 partial


def test_after_close_fx_steps_use_close_16(memory_db, monkeypatch):
    """收盤後 4 個 FX 步驟改走 collect_and_save_pair，且 slot 為 close_16。"""
    from jobs import after_close

    monkeypatch.setattr(after_close, "is_trading_day", lambda d: True)
    monkeypatch.setattr(after_close.settings, "FINMIND_TOKEN", "")

    calls = []
    monkeypatch.setattr(
        "collectors.fx.FXCollector.collect_and_save_pair",
        lambda self, date, pair, slot: calls.append((pair, slot)) or True)
    # 其餘 collector 都 stub 成功，避免真實 HTTP
    monkeypatch.setattr(after_close, "_collect_twse_spot_close", lambda d, c: True)
    monkeypatch.setattr(after_close, "_collect_twse_institutional", lambda d, c: True)
    monkeypatch.setattr(after_close, "_collect_twse_foreign_stock", lambda d, c: True)
    monkeypatch.setattr(after_close, "_collect_twse_stock_close", lambda d, c: True)
    monkeypatch.setattr(after_close, "_collect_twse_ex_dividend", lambda d, c: True)
    monkeypatch.setattr(after_close, "_collect_taifex_oi", lambda d, c: True)
    monkeypatch.setattr(after_close, "_compute_chip", lambda d, c: True)

    with patch("jobs.after_close.get_connection") as mock_conn:
        mock_conn.return_value.__enter__ = lambda s: memory_db
        mock_conn.return_value.__exit__ = lambda s, *a: None
        after_close.run_after_close("2026-06-16")

    assert calls == [("USD/TWD", "close_16"), ("USD/CNY", "close_16"),
                     ("USD/KRW", "close_16"), ("USD/JPY", "close_16")]
