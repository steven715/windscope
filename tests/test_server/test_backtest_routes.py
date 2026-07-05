"""回測頁路由測試：空狀態 200、觸發回測 → 存 run → 頁面呈現、找不到股票。"""

import sqlite3
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from db.schema import create_all_tables
from server.app import create_app


def _seed(conn: sqlite3.Connection, stock_id: str, n: int = 300) -> None:
    """塞 watchlist + n 根上升日K（足夠年線暖機、確保能建立 run）。"""
    conn.execute("INSERT INTO watchlist (stock_id, stock_name, added_date, reason) "
                 "VALUES (?, ?, '2020-01-01', 't')", (stock_id, "測試股"))
    d0 = date(2018, 1, 1)
    for i in range(n):
        d = (d0 + timedelta(days=i)).isoformat()
        px = 100 + i * 0.5
        conn.execute(
            "INSERT INTO raw_stock_daily (date, stock_id, open, high, low, close, "
            "volume, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, '')",
            (d, stock_id, px, px * 1.01, px * 0.99, px, 1000))
    conn.commit()


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    _seed(conn, "8888")
    conn.close()
    return TestClient(create_app(db_path=db_path, enable_scheduler=False))


def test_backtest_page_empty_200(client):
    r = client.get("/backtest")
    assert r.status_code == 200
    assert "回測" in r.text
    assert "8888" in r.text                      # watchlist 出現在下拉
    assert "尚無回測紀錄" in r.text               # 空狀態


def test_run_then_view(client):
    r = client.post("/backtest/run", data={"stock_id": "8888", "entry_mode": "single"},
                    follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "run_id=" in loc

    page = client.get(loc)
    assert page.status_code == 200
    assert "總報酬" in page.text                  # 指標卡片
    assert "權益曲線" in page.text                 # 有 run 就畫圖
    assert "歷次回測" in page.text                 # run 對照表


def test_run_both_modes_creates_two_runs(client, tmp_path):
    client.post("/backtest/run", data={"stock_id": "8888", "entry_mode": "both"},
                follow_redirects=False)
    # 直接查 DB 確認兩個 run
    import glob
    db_path = glob.glob(str(tmp_path / "t.db"))[0]
    conn = sqlite3.connect(db_path)
    modes = [r[0] for r in conn.execute("SELECT entry_mode FROM backtest_runs").fetchall()]
    conn.close()
    assert sorted(modes) == ["scaled", "single"]


def test_run_unknown_stock_redirects_with_msg(client):
    r = client.post("/backtest/run", data={"stock_id": "0000", "entry_mode": "single"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "msg=" in r.headers["location"]
    assert "run_id=" not in r.headers["location"]


def test_compare_two_runs(client, tmp_path):
    client.post("/backtest/run", data={"stock_id": "8888", "entry_mode": "both"},
                follow_redirects=False)
    import glob
    conn = sqlite3.connect(glob.glob(str(tmp_path / "t.db"))[0])
    ids = [r[0] for r in conn.execute("SELECT id FROM backtest_runs ORDER BY id").fetchall()]
    conn.close()
    assert len(ids) == 2

    r = client.get(f"/backtest/compare?a={ids[0]}&b={ids[1]}")
    assert r.status_code == 200
    assert "回測比較" in r.text
    assert "參數差異" in r.text and "績效變化" in r.text
    assert "進出場模式" in r.text          # single vs scaled → 模式差異被標出


def test_compare_missing_run_redirects(client):
    r = client.get("/backtest/compare?a=999&b=998", follow_redirects=False)
    assert r.status_code == 303
    assert "/backtest?msg=" in r.headers["location"]


def test_history_page_lists_runs_and_picker(client):
    client.post("/backtest/run", data={"stock_id": "8888", "entry_mode": "both"},
                follow_redirects=False)
    r = client.get("/backtest/history")
    assert r.status_code == 200
    assert "歷次回測" in r.text
    assert "比較兩筆" in r.text          # 兩筆 run → 比較選單出現
    assert "8888" in r.text


def test_history_page_empty(client):
    r = client.get("/backtest/history")
    assert r.status_code == 200
    assert "尚無回測紀錄" in r.text
