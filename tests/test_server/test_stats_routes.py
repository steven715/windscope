"""判斷有效性 API 測試（/stats 頁已下線、搬到前端 windwatch：見 test_retired_pages）：JSON 結構、篩選、空狀態；首頁小卡不混版本。"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from db.schema import create_all_tables
from integration.verification import classify_against_benchmarks
from server.app import create_app

_DAYS = ["2026-06-08", "2026-06-09", "2026-06-10", "2026-06-11", "2026-06-12"]


def _seed_day(conn, date, direction, confidence, prev_close, open_, close, rule_version="v3"):
    c = classify_against_benchmarks(direction, prev_close, open_, close)
    conn.execute("INSERT INTO signals (date, direction, confidence, rule_version) "
                 "VALUES (?, ?, ?, ?)", (date, direction, confidence, rule_version))
    conn.execute(
        "INSERT INTO verifications (date, predicted_direction, confidence, prev_close, open, "
        "close, open_gap_pct, day_change_pct, open_gap_class, day_change_class, hit_day, hit_open, rule_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (date, direction, confidence, prev_close, open_, close, c["open_gap_pct"],
         c["day_change_pct"], c["open_gap_class"], c["day_change_class"],
         c["hit_day"], c["hit_open"], rule_version))
    conn.execute("INSERT OR REPLACE INTO raw_index (date, open, close) VALUES (?, ?, ?)",
                 (date, open_, close))
    conn.execute("INSERT INTO daily_metrics (date, fx_direction, futures_spread_adjusted, "
                 "oi_net_foreign) VALUES (?, ?, ?, ?)",
                 (date, direction, 150.0 if direction == "bullish" else -150.0, 0))


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.close()
    return TestClient(create_app(db_path=db_path, enable_scheduler=False))


@pytest.fixture
def client_with_data(tmp_path):
    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.execute("INSERT INTO raw_index (date, close) VALUES (?, 100)", (_DAYS[0],))
    _seed_day(conn, _DAYS[1], "bullish", 4, 100, 100.2, 101, rule_version="v2")
    _seed_day(conn, _DAYS[2], "bearish", 3, 101, 101, 99.5)
    _seed_day(conn, _DAYS[3], "bullish", 2, 99.5, 99.5, 99)      # 失誤
    _seed_day(conn, _DAYS[4], "neutral", 2, 99, 99, 99.1)
    conn.commit()
    conn.close()
    return TestClient(create_app(db_path=db_path, enable_scheduler=False))


def test_api_call_stats_structure(client_with_data):
    r = client_with_data.get("/api/call-stats")
    assert r.status_code == 200
    body = r.json()
    assert body["n"] == 4
    assert set(body) >= {"assessment", "baselines", "calibration", "factors", "backtest",
                         "versions", "filters"}
    assert body["assessment"]["verdict"] in {"insufficient", "edge", "unclear", "no_edge"}
    assert body["baselines"]["system"]["hits"] == 3
    keys = {b["key"] for b in body["baselines"]["baselines"]}
    assert keys == {"always_up", "always_down", "always_flat", "follow_yesterday", "independence"}
    assert set(body["backtest"]["modes"]) == {"open_to_close", "close_to_close"}
    assert body["backtest"]["modes"]["open_to_close"]["equal"]["n"] == 3   # 中性不計
    assert [f["key"] for f in body["factors"]][:2] == ["fx_direction", "fx_asia_sync"]


def test_api_call_stats_filters(client_with_data):
    r = client_with_data.get("/api/call-stats?rule_version=v2")
    assert r.json()["n"] == 1
    r = client_with_data.get(f"/api/call-stats?date_to={_DAYS[2]}&cost_bps=0")
    body = r.json()
    assert body["n"] == 2 and body["backtest"]["cost_bps"] == 0


def test_api_call_stats_lists_unknown_version_as_empty_string(client_with_data):
    """驗證時版本不明（NULL）的列在 versions 以 "" 列出（前端拿它說「另有 N 筆沒有規則版本」，不讓人選）。"""
    conn = sqlite3.connect(client_with_data.app.state.db_path)
    conn.execute("UPDATE verifications SET rule_version = NULL WHERE date = ?", (_DAYS[1],))
    conn.commit()
    conn.close()
    versions = client_with_data.get("/api/call-stats").json()["versions"]
    assert {"rule_version": "", "n": 1} in versions
    assert sum(v["n"] for v in versions) == 4


def test_api_call_stats_empty(client):
    body = client.get("/api/call-stats").json()
    assert body["n"] == 0 and body["assessment"]["verdict"] == "insufficient"
    assert body["calibration"]["by_confidence"] == {}


# ── 規則版本不混算（verifications.rule_version；審查） ─────────────────────────────


def test_small_cards_count_only_the_current_version(client_with_data):
    """近 N 筆小卡（首頁）只算現行版本 v3：2/3＝66.7%；混進 v2 會變 3/4＝75.0%。"""
    for page in ("/",):
        html = client_with_data.get(page).text
        assert "66.7%" in html, page
        assert "75.0%" not in html, page
        assert "v3 近 3" in html, page


def test_dashboard_strip_without_verifications_for_the_current_version(tmp_path):
    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.execute("INSERT INTO raw_index (date, close) VALUES (?, 100)", (_DAYS[0],))
    _seed_day(conn, _DAYS[1], "bullish", 4, 100, 100.2, 101, rule_version="v2")   # 只有舊版本的驗證
    conn.commit()
    conn.close()
    html = TestClient(create_app(db_path=db_path, enable_scheduler=False)).get("/").text
    assert "v3 還沒有驗證紀錄" in html
    assert "None%" not in html
