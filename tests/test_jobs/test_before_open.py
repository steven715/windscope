"""before_open job 測試。"""

import json
from unittest.mock import patch


def test_before_open_skips_non_trading_day():
    """非交易日直接回傳 skipped。"""
    from jobs.before_open import run_before_open

    result = run_before_open("2026-04-05", db_path=":memory:")
    assert result["status"] == "skipped"
    assert result["summary"] is None


def test_before_open_full_success(memory_db, monkeypatch):
    """所有步驟成功時，status = completed，有 summary。"""
    from jobs import before_open

    def mock_step(name, fn):
        return True, None

    monkeypatch.setattr(before_open, "run_step", mock_step)
    monkeypatch.setattr(before_open, "is_trading_day", lambda d: True)

    # 為 summary 準備資料
    memory_db.execute(
        "INSERT INTO daily_metrics (date, fx_delta_twd, fx_direction) "
        "VALUES ('2026-04-08', -0.15, 'bullish')"
    )
    memory_db.commit()

    with patch("jobs.before_open.get_connection") as mock_conn:
        mock_conn.return_value.__enter__ = lambda s: memory_db
        mock_conn.return_value.__exit__ = lambda s, *a: None

        result = before_open.run_before_open("2026-04-08")

    assert result["status"] == "completed"
    # summary 有產出（可能內容有限但不為 None）
    assert result["summary"] is not None
    assert "開盤前情報" in result["summary"]


def test_before_open_holiday_collects_fx_skips_signal(memory_db, monkeypatch):
    """平日休市（端午節）：匯率步驟照跑，訊號步驟跳過、不寫 signals 表。"""
    from jobs import before_open

    # 2026-06-19 端午節（週五）→ 非交易日但非週末
    monkeypatch.setattr(before_open, "is_trading_day", lambda d: False)

    calls = []

    def mock_step(name, fn):
        calls.append(name)
        return True, None

    monkeypatch.setattr(before_open, "run_step", mock_step)

    memory_db.execute(
        "INSERT INTO daily_metrics (date, fx_delta_twd, fx_direction) "
        "VALUES ('2026-06-19', 0.0, 'neutral')"
    )
    memory_db.commit()

    with patch("jobs.before_open.get_connection") as mock_conn:
        mock_conn.return_value.__enter__ = lambda s: memory_db
        mock_conn.return_value.__exit__ = lambda s, *a: None

        result = before_open.run_before_open("2026-06-19")

    assert result["market_open"] is False
    # 匯率（休市無關）步驟有跑
    assert "fx_twd_0845" in calls
    assert "integration_fx" in calls
    # 訊號步驟完全沒跑，也不在 results
    assert "signal" not in calls
    assert "signal" not in result["results"]
    # summary 仍產出，且標註台股休市
    assert result["summary"] is not None
    assert "台股休市" in result["summary"]


def test_before_open_weekend_still_fully_skipped(monkeypatch):
    """週末仍整步略過（沿用舊行為），不收匯率。"""
    from jobs import before_open

    monkeypatch.setattr(before_open, "is_trading_day", lambda d: False)
    # 2026-06-20 為星期六
    result = before_open.run_before_open("2026-06-20", db_path=":memory:")
    assert result["status"] == "skipped"
    assert result["summary"] is None
    assert result["results"] == {}


def test_before_open_summary_shows_na_for_null(memory_db, monkeypatch):
    """有 NULL 欄位時，summary 顯示「資料不可用」。"""
    from integration.summary import generate_daily_summary

    # 塞入 oi_net_foreign = NULL 的 daily_metrics
    memory_db.execute(
        "INSERT INTO daily_metrics (date, fx_delta_twd, fx_direction, "
        "  oi_net_foreign) "
        "VALUES ('2026-04-08', -0.15, 'bullish', NULL)"
    )
    memory_db.commit()

    summary = generate_daily_summary("2026-04-08", memory_db)
    assert summary is not None
    assert "資料不可用" in summary
