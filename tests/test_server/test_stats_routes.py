"""判斷有效性頁與 API 測試：空狀態 200、有資料時四區塊關鍵內容、篩選生效、JSON 結構。"""

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


def test_stats_page_empty_200(client):
    r = client.get("/stats")
    assert r.status_code == 200
    assert "判斷有效性" in r.text
    assert "樣本不足" in r.text
    assert "沒有已驗證的盤前判斷" in r.text


def test_stats_page_with_data_shows_all_sections(client_with_data):
    r = client_with_data.get("/stats")
    assert r.status_code == 200
    assert "基準線對照" in r.text and "與結果無關" in r.text and "跟昨天" in r.text
    assert "信心校準" in r.text and "4/5" in r.text
    assert "判斷回測" in r.text and "開盤進 → 收盤出" in r.text and "每天做多" in r.text
    assert "單因子表" in r.text and "調整後價差" in r.text and "外資未平倉" in r.text
    assert "已驗證 <b class=\"mono\">4</b> 筆" in r.text
    assert "混算 2 個規則版本" in r.text              # v2 + v3 未篩選 → 提示


def test_stats_page_filter_by_version(client_with_data):
    r = client_with_data.get("/stats?rule_version=v3")
    assert r.status_code == 200
    assert "已驗證 <b class=\"mono\">3</b> 筆" in r.text
    assert "混算" not in r.text


def test_stats_page_date_filter_and_bad_date_ignored(client_with_data):
    r = client_with_data.get(f"/stats?date_from={_DAYS[3]}")
    assert r.status_code == 200
    assert "已驗證 <b class=\"mono\">2</b> 筆" in r.text
    r2 = client_with_data.get("/stats?date_from=not-a-date")
    assert r2.status_code == 200
    assert "已驗證 <b class=\"mono\">4</b> 筆" in r2.text    # 非法日期被忽略


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


def test_api_call_stats_empty(client):
    body = client.get("/api/call-stats").json()
    assert body["n"] == 0 and body["assessment"]["verdict"] == "insufficient"
    assert body["calibration"]["by_confidence"] == {}


# ── 規則版本不混算（verifications.rule_version；審查） ─────────────────────────────


def _db(client: TestClient) -> sqlite3.Connection:
    return sqlite3.connect(client.app.state.db_path)


def _row(html: str, date: str) -> str:
    start = html.index(date)
    return html[start:html.index("</tr>", start)]


def test_small_cards_count_only_the_current_version(client_with_data):
    """近 N 筆小卡（訊號頁、首頁）只算現行版本 v3：2/3＝66.7%；混進 v2 會變 3/4＝75.0%。"""
    for page in ("/signals", "/"):
        html = client_with_data.get(page).text
        assert "66.7%" in html, page
        assert "75.0%" not in html, page
        assert "v3 近 3" in html, page


def test_signals_page_shows_the_call_as_verified_after_a_recompute(client_with_data):
    """判斷驗證後被 recompute 成 v3 偏空：訊號頁該列顯示驗證當下的偏多／v2（命中是照它算的）。"""
    with _db(client_with_data) as conn:
        conn.execute("UPDATE signals SET direction = 'bearish', confidence = 5, rule_version = 'v3' WHERE date = ?",
                     (_DAYS[1],))
    row = _row(client_with_data.get("/signals").text, _DAYS[1])
    assert "偏多" in row and "v2" in row and "4/5" in row
    assert "偏空" not in row and "v3" not in row


def test_stats_page_lists_unknown_version_but_cannot_select_it(client_with_data):
    with _db(client_with_data) as conn:
        conn.execute("UPDATE verifications SET rule_version = NULL WHERE date = ?", (_DAYS[1],))
    html = client_with_data.get("/stats").text
    assert '<option value="" disabled>版本不明 · 1 筆' in html
    assert "目前混算 1 個規則版本＋版本不明的列" in html


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
