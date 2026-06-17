"""盤前解讀層測試：原數據 + 判讀 + 為什麼。"""

import json
import sqlite3

from db.schema import create_all_tables
from integration.explain import build_explain


def _setup(conn):
    conn.execute(
        "INSERT INTO daily_metrics (date, fx_delta_twd, fx_direction, fx_asia_detail, "
        "fx_asia_sync, futures_spread, futures_spread_adjusted, futures_volume_ratio, "
        "oi_net_foreign, oi_delta) VALUES "
        "('2026-06-16', 0.0, 'neutral', ?, 0, 849.0, 849.0, 0.46, -66734, -1695)",
        (json.dumps({"TWD": "neutral", "CNY": "bullish", "KRW": "neutral"}),),
    )
    conn.execute("INSERT INTO raw_fx (date, currency_pair, close_16) "
                 "VALUES ('2026-06-15', 'USD/TWD', 31.485)")
    conn.execute("INSERT INTO raw_fx (date, currency_pair, quote_0845) "
                 "VALUES ('2026-06-16', 'USD/TWD', 31.485)")
    conn.execute("INSERT INTO raw_futures (date, spot_close, sp500_close) "
                 "VALUES ('2026-06-15', 45396.99, 7431.46)")
    conn.execute("INSERT INTO raw_futures (date, night_close, sp500_close) "
                 "VALUES ('2026-06-16', 46246.0, 7554.29)")
    # 盤前 5 分序列：台幣緩步升（USD/TWD 31.55→31.49，最大單根 0.02 < 0.03）
    for ts, close in [(1, 31.55), (2, 31.53), (3, 31.51), (4, 31.49)]:
        conn.execute("INSERT INTO intraday_fx (date, currency_pair, ts, close) "
                     "VALUES ('2026-06-16', 'USD/TWD', ?, ?)", (ts, close))
    conn.commit()


def test_build_explain_rows():
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    _setup(conn)

    rows = build_explain("2026-06-16", conn)
    by_dim = {r["dim"]: r for r in rows}

    # 八個維度都在，且都有原數據 + 判讀 + 為什麼
    assert len(rows) == 8
    for r in rows:
        assert r["raw"] and r["verdict"] and r["why"]

    assert "平盤" in by_dim["匯率（台幣）"]["verdict"]
    assert "緩步" in by_dim["匯率節奏"]["verdict"]  # 台幣緩步升
    assert "正價差" in by_dim["期貨價差"]["verdict"]
    assert "夜盤46246" in by_dim["期貨價差"]["raw"]
    assert "淨空" in by_dim["外資未平倉"]["verdict"]
    assert "量縮" in by_dim["夜盤量比"]["verdict"]
    # 美股 +1.65% / 夜盤 +1.87% 同向 → 同步
    assert "同步" in by_dim["美股對照（看異常）"]["verdict"]


def test_build_explain_no_metrics_returns_empty():
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    assert build_explain("2026-06-16", conn) == []


def test_fx_rhythm_surge_flagged():
    """盤前 5 分序列出現急拉（單根 ≥ 0.03）→ 標『急拉、別追』。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    conn.execute("INSERT INTO daily_metrics (date, fx_delta_twd, fx_direction) "
                 "VALUES ('2026-06-16', -0.02, 'neutral')")
    for ts, close in [(1, 31.55), (2, 31.55), (3, 31.51)]:  # 單根 0.04 急拉
        conn.execute("INSERT INTO intraday_fx (date, currency_pair, ts, close) "
                     "VALUES ('2026-06-16', 'USD/TWD', ?, ?)", (ts, close))
    conn.commit()

    rhythm = next(r for r in build_explain("2026-06-16", conn) if r["dim"] == "匯率節奏")
    assert "急拉" in rhythm["verdict"]


def test_jpy_riskoff_flagged():
    """日圓急升(USD/JPY 大跌)→ 標避險 risk-off。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    conn.execute("INSERT INTO daily_metrics (date, fx_direction) VALUES ('2026-06-17', 'neutral')")
    conn.execute("INSERT INTO raw_futures (date, spot_close) VALUES ('2026-06-16', 45000)")  # 讓 prev_day=6/16
    conn.execute("INSERT INTO raw_fx (date, currency_pair, close_16) VALUES ('2026-06-16', 'USD/JPY', 150.0)")
    conn.execute("INSERT INTO raw_fx (date, currency_pair, quote_0845) VALUES ('2026-06-17', 'USD/JPY', 148.5)")
    conn.commit()

    jpy = next(r for r in build_explain("2026-06-17", conn) if r["dim"] == "日圓避險情緒")
    assert "避險" in jpy["verdict"] and "risk-off" in jpy["verdict"]


def test_anomaly_divergence_flagged():
    """美股大漲但夜盤沒動 → 標『異常：台股相對弱』。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    conn.execute(
        "INSERT INTO daily_metrics (date, futures_spread, futures_spread_adjusted) "
        "VALUES ('2026-06-16', 5.0, 5.0)")
    # 前日現貨 45000、今日夜盤 45010（幾乎沒動，+0.02%）
    conn.execute("INSERT INTO raw_futures (date, spot_close, sp500_close) "
                 "VALUES ('2026-06-15', 45000.0, 7000.0)")
    # 美股大漲 +2%
    conn.execute("INSERT INTO raw_futures (date, night_close, sp500_close) "
                 "VALUES ('2026-06-16', 45010.0, 7140.0)")
    conn.commit()

    rows = build_explain("2026-06-16", conn)
    anomaly = next(r for r in rows if r["dim"] == "美股對照（看異常）")
    assert "異常" in anomaly["verdict"]
