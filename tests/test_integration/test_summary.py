"""daily summary 文字摘要測試。"""

import json

import pytest

from integration.summary import generate_daily_summary, _format_amount


def test_summary_with_full_data(memory_db):
    """資料完整時，摘要包含所有區塊。"""
    # 準備 daily_metrics
    memory_db.execute(
        "INSERT INTO daily_metrics "
        "(date, fx_delta_twd, fx_delta_cny, fx_delta_krw, "
        " fx_direction, fx_asia_sync, fx_asia_detail, "
        " futures_spread, futures_spread_adjusted, "
        " futures_volume_ratio, oi_net_foreign, oi_delta) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "2026-04-08", -0.15, -0.002, -10.0,
            "bullish", 0,
            json.dumps({"TWD": "bullish", "CNY": "neutral", "KRW": "bullish"}),
            100.0, 70.0, 1.44, 25000, 1500,
        ),
    )

    # 準備 raw_fx：前一交易日(04-07)收盤 + 當日(04-08)報價
    for pair, c16, q0845 in [
        ("USD/TWD", 31.50, 31.35),
        ("USD/CNY", 7.250, 7.248),
        ("USD/KRW", 1380.0, 1370.0),
    ]:
        memory_db.execute("INSERT INTO raw_fx (date, currency_pair, close_16) "
                          "VALUES ('2026-04-07', ?, ?)", (pair, c16))
        memory_db.execute("INSERT INTO raw_fx (date, currency_pair, quote_0845) "
                          "VALUES ('2026-04-08', ?, ?)", (pair, q0845))

    # 準備 raw_futures：前日現貨(基準) + 當日夜盤
    memory_db.execute("INSERT INTO raw_futures (date, spot_close) "
                      "VALUES ('2026-04-07', 20050.0)")
    memory_db.execute("INSERT INTO raw_futures (date, night_close, ex_dividend_points) "
                      "VALUES ('2026-04-08', 20150.0, 30.0)")

    # 準備 watchlist + 個股觀察訊號
    memory_db.execute(
        "INSERT INTO watchlist (stock_id, stock_name, added_date, reason) "
        "VALUES ('2330', '台積電', '2026-04-01', '權值股')"
    )
    memory_db.execute(
        "INSERT INTO stock_signals (date, stock_id, broker_name, category, reasons) "
        "VALUES ('2026-04-08', '2330', '外資', '外資連買', '外資連買 2 天（累計 13,573 張）')"
    )

    memory_db.commit()

    summary = generate_daily_summary("2026-04-08", memory_db)
    assert summary is not None
    assert "匯率" in summary
    assert "期貨" in summary
    assert "籌碼" in summary
    assert "台積電" in summary
    assert "外資連買" in summary       # 個股訊號有顯示
    assert "20150" in summary          # 今日夜盤
    assert "20050" in summary          # 前日現貨（基準）
    assert "31.5000" in summary        # 前日 USD/TWD 收盤


def test_summary_with_null_fields(memory_db):
    """有 NULL 欄位時，顯示「資料不可用」而非空白或報錯。"""
    memory_db.execute(
        "INSERT INTO daily_metrics (date, fx_delta_twd, fx_direction, "
        "  oi_net_foreign) "
        "VALUES ('2026-04-08', -0.15, 'bullish', NULL)"
    )
    memory_db.commit()

    summary = generate_daily_summary("2026-04-08", memory_db)
    assert summary is not None
    assert "資料不可用" in summary


def test_summary_no_data(memory_db):
    """無 daily_metrics 時回傳 None。"""
    summary = generate_daily_summary("2026-04-08", memory_db)
    assert summary is None


def test_summary_empty_watchlist(memory_db):
    """觀察名單為空時顯示提示。"""
    memory_db.execute(
        "INSERT INTO daily_metrics (date, fx_delta_twd, fx_direction) "
        "VALUES ('2026-04-08', -0.15, 'bullish')"
    )
    memory_db.commit()

    summary = generate_daily_summary("2026-04-08", memory_db)
    assert "觀察名單為空" in summary


def test_summary_watchlist_no_signal(memory_db):
    """watchlist 中沒有個股訊號時顯示「無觀察訊號」。"""
    memory_db.execute(
        "INSERT INTO daily_metrics (date, fx_delta_twd, fx_direction) "
        "VALUES ('2026-04-08', -0.15, 'bullish')"
    )
    memory_db.execute(
        "INSERT INTO watchlist (stock_id, stock_name, added_date, reason) "
        "VALUES ('2409', '友達', '2026-04-08', '外資反手大買')"
    )
    memory_db.commit()

    summary = generate_daily_summary("2026-04-08", memory_db)
    assert "無觀察訊號" in summary


def test_format_amount_billion():
    """金額格式化：億。"""
    assert "4.48億" == _format_amount(448000000)


def test_format_amount_ten_million():
    """金額格式化：萬（千萬級）。"""
    assert "1500萬" == _format_amount(15000000)


def test_format_amount_million():
    """金額格式化：萬（百萬級）。"""
    assert "120萬" == _format_amount(1200000)


def test_format_amount_none():
    """金額格式化：None。"""
    assert "資料不可用" == _format_amount(None)
