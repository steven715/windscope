"""盤前解讀層測試：原數據 + 判讀 + 為什麼。"""

import json
import sqlite3

import pytest

from db.schema import create_all_tables
from integration.explain import (
    _note,
    build_explain,
    build_stock_explain,
    build_stock_trend_explain,
)


def test_note_prefers_my_note_with_source():
    """my_note 非空時優先且署名『你』；否則用原文、署名來源。"""
    notes = {"X": {"source": "原文", "note": "原文觀點", "my_note": "我的看法"}}
    assert _note(notes, "X") == ("我的看法", "你")
    notes2 = {"X": {"source": "原文", "note": "原文觀點", "my_note": ""}}
    assert _note(notes2, "X") == ("原文觀點", "原文")
    assert _note({}, "缺") == ("", "")


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
    # 每列都帶觀點來源（從 config/explain_notes.json 載入）
    assert by_dim["匯率（台幣）"]["why_source"] == "原文"
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


def _setup_stock(conn, stock_id="2330"):
    """外資連買 3 天 + 主力分點低檔連買 + 股價位置(月線下)。"""
    for d in ("2026-06-15", "2026-06-16", "2026-06-17"):
        conn.execute(
            "INSERT INTO raw_chip (date, stock_id, broker_name, net_volume) "
            "VALUES (?, ?, '__FOREIGN__', 2000)", (d, stock_id))
    conn.execute(
        "INSERT INTO daily_stock_metrics (date, stock_id, broker_name, net_amount, "
        " consecutive_days, price_vs_ma20, price_zone, both_sides_flag, broker_type) "
        "VALUES ('2026-06-18', ?, '兆豐-嘉義', 60000000, 3, -5.0, 'low', 0, NULL)",
        (stock_id,))
    conn.commit()


def test_build_stock_explain_happy():
    """有外資+分點資料 → 三維度判讀正確（外資連買/主力摸底/低檔）。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    _setup_stock(conn)

    rows = build_stock_explain("2026-06-18", "2330", conn)
    by_dim = {r["dim"]: r for r in rows}
    assert set(by_dim) == {"外資動向", "主力分點", "股價位置"}

    assert by_dim["外資動向"]["css"] == "up"
    assert "連買" in by_dim["外資動向"]["verdict"]
    assert by_dim["主力分點"]["css"] == "up"
    assert "摸底" in by_dim["主力分點"]["verdict"]
    assert "低檔" in by_dim["股價位置"]["verdict"]
    # 觀點從 explain_notes 帶入（立場 vs 觀點分離）
    assert by_dim["外資動向"]["why"] and by_dim["外資動向"]["why_source"] == "原文"
    conn.close()


def test_build_stock_explain_no_data_graceful():
    """無任何籌碼資料 → 三維度都優雅顯示『資料不可用』，不報錯。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)

    rows = build_stock_explain("2026-06-18", "9999", conn)
    assert len(rows) == 3
    assert all(r["verdict"] == "資料不可用" for r in rows)
    assert all(r["css"] == "flat" for r in rows)
    conn.close()


def test_price_zone_falls_back_to_kline_bias():
    """無分點資料但有趨勢訊號 → 股價位置由 K線還原後乖離補上（非資料不可用）。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    # 無 daily_stock_metrics；趨勢 bias +0.03（+3% → 盤整）
    conn.execute(
        "INSERT INTO stock_trend_signals (date, stock_id, big_trend, value_center, "
        "bias_pct, small_state, action_hint, position_hint, rule_version) VALUES "
        "('2026-07-01', '2454', 'UP', 105.6, 0.03, '貼近中樞', '觀望', '持有', 't2')")
    conn.commit()

    zone = next(r for r in build_stock_explain("2026-07-05", "2454", conn)
                if r["dim"] == "股價位置")
    assert zone["verdict"] != "資料不可用"
    assert "盤整" in zone["verdict"]
    assert "K線" in zone["raw"]
    conn.close()


def test_price_zone_prefers_chip_metrics_over_kline():
    """有分點的 daily_stock_metrics 時仍優先用它（不被 K線 fallback 蓋掉）。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    conn.execute(
        "INSERT INTO daily_stock_metrics (date, stock_id, broker_name, price_vs_ma20, "
        "price_zone) VALUES ('2026-07-01', '2454', '某分點', -25.0, 'low')")
    conn.execute(
        "INSERT INTO stock_trend_signals (date, stock_id, big_trend, bias_pct, "
        "rule_version) VALUES ('2026-07-01', '2454', 'UP', 0.03, 't2')")
    conn.commit()

    zone = next(r for r in build_stock_explain("2026-07-05", "2454", conn)
                if r["dim"] == "股價位置")
    assert "低檔" in zone["verdict"]          # 用分點的 -25% low，非 K線的 +3%
    assert "K線" not in zone["raw"]
    conn.close()


def test_build_stock_explain_broker_picks_signal_over_size():
    """主力分點挑『有訊號』優先於『淨額大但無訊號』。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    # 甲：淨額大(1億)但無訊號（連買僅 1 天 < 門檻）
    conn.execute(
        "INSERT INTO daily_stock_metrics (date, stock_id, broker_name, net_amount, "
        " consecutive_days, price_zone, both_sides_flag, broker_type) "
        "VALUES ('2026-06-18', '2454', '甲分點', 100000000, 1, 'consolidation', 0, NULL)")
    # 乙：淨額較小(6千萬)但有訊號（低檔連買 3 天 → 摸底）
    conn.execute(
        "INSERT INTO daily_stock_metrics (date, stock_id, broker_name, net_amount, "
        " consecutive_days, price_zone, both_sides_flag, broker_type) "
        "VALUES ('2026-06-18', '2454', '乙分點', 60000000, 3, 'low', 0, NULL)")
    conn.commit()

    broker = next(r for r in build_stock_explain("2026-06-18", "2454", conn)
                  if r["dim"] == "主力分點")
    assert "乙分點" in broker["raw"]   # 有訊號者勝出，非淨額大者
    assert "摸底" in broker["verdict"]
    conn.close()


def test_build_stock_explain_day_trade_warning():
    """隔日沖分點買超 → 標『不追』，警示走 flat。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    conn.execute(
        "INSERT INTO daily_stock_metrics (date, stock_id, broker_name, net_amount, "
        " consecutive_days, price_zone, both_sides_flag, broker_type) "
        "VALUES ('2026-06-18', '2454', '隔日沖分點', 80000000, 1, 'high', 0, 'day_trade')")
    conn.commit()

    broker = next(r for r in build_stock_explain("2026-06-18", "2454", conn)
                  if r["dim"] == "主力分點")
    assert "不追" in broker["verdict"]
    assert broker["css"] == "flat"
    conn.close()


# ── 趨勢解讀（技術面 + 基本面預留）──────────────────────────────


def _seed_trend_signal(conn, stock_id="2330", big_trend="UP"):
    conn.execute(
        "INSERT INTO stock_trend_signals (date, stock_id, big_trend, "
        "big_trend_confidence, value_center, bias_pct, small_state, small_turn_up, "
        "fundamental_gate, action_hint, position_hint, flat_redirect, reasons, "
        "rule_version) VALUES ('2026-07-01', ?, ?, 0.031, 812.4, -0.018, "
        "'跌破恐慌', 1, 'UNKNOWN', '觀望', '可加倉', NULL, '[]', 't1')",
        (stock_id, big_trend),
    )
    conn.commit()


def test_build_stock_trend_explain_happy():
    """有趨勢訊號 → 趨勢（技術面）判讀含三態+行動；基本面固定為預留。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    _seed_trend_signal(conn, "2330", "UP")

    rows = build_stock_trend_explain("2026-07-05", "2330", conn)
    by_dim = {r["dim"]: r for r in rows}
    assert list(by_dim) == ["趨勢（技術面）", "基本面"]

    trend = by_dim["趨勢（技術面）"]
    assert trend["css"] == "up"
    assert "上升趨勢" in trend["verdict"] and "觀望" in trend["verdict"]
    assert "月線812.4" in trend["raw"] and "年線斜率+3.1%" in trend["raw"]
    assert trend["why"] and trend["why_source"] == "原文"  # 觀點走 explain_notes

    fund = by_dim["基本面"]
    assert "資料不可用" in fund["verdict"]
    assert fund["css"] == "flat" and fund["why"]
    conn.close()


def test_build_stock_trend_explain_no_signal_graceful():
    """尚無趨勢訊號 → 趨勢維度顯示『資料不可用』，基本面仍為預留，不報錯。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)

    rows = build_stock_trend_explain("2026-07-05", "9999", conn)
    assert len(rows) == 2
    assert rows[0]["dim"] == "趨勢（技術面）"
    assert rows[0]["verdict"] == "資料不可用"
    assert rows[1]["dim"] == "基本面"
    conn.close()


def test_build_stock_trend_explain_unknown_state():
    """暖機不足(UNKNOWN) → 趨勢維度標『資料不足』而非三態。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    _seed_trend_signal(conn, "2330", "UNKNOWN")

    trend = build_stock_trend_explain("2026-07-05", "2330", conn)[0]
    assert trend["verdict"] == "資料不足"
    conn.close()


def test_build_stock_trend_explain_shows_adjust_note():
    """有還原提示(adjust_note) → 出現在原數據，供人工核對誤還原。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    conn.execute(
        "INSERT INTO stock_trend_signals (date, stock_id, big_trend, value_center, "
        "bias_pct, small_state, action_hint, position_hint, adjust_note, rule_version) "
        "VALUES ('2026-07-01', '0050', 'UP', 105.6, 0.026, '貼近中樞', '觀望', "
        "'可加倉', '已還原公司行為缺口 1 次（最近 2025-06-18 ×0.25，請核對是否分割/大除權）', 't2')")
    conn.commit()

    trend = build_stock_trend_explain("2026-07-05", "0050", conn)[0]
    assert "已還原公司行為缺口 1 次" in trend["raw"]
    assert "2025-06-18" in trend["raw"]
    conn.close()
