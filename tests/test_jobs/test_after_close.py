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
    # 收盤後現在只剩 5 個市場面步驟（個股收盤/分點/籌碼指標/匯率已拆出）
    assert call_count["n"] == 5
    # 拆出去的步驟不應再出現在 after_close
    for moved in ("twse_stock_close", "chip", "integration_chip", "fx_close"):
        assert moved not in result["results"]
