"""GET /api/calendar：區間內的休市日（含名稱）、休市表有資料的年份、參數檢查。"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from db.schema import create_all_tables
from server.app import create_app


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    for d, n in (("2026-06-19", "端午節"), ("2026-10-09", "國慶日補假"), ("2027-01-01", "開國紀念日")):
        conn.execute("INSERT INTO market_holidays (date, name, source, fetched_at) VALUES (?, ?, 'test', '2026-01-01T00:00:00')", (d, n))
    conn.commit(); conn.close()
    return TestClient(create_app(db_path=db_path, enable_scheduler=False))


def test_calendar_lists_holidays_in_range_and_covered_years(client):
    body = client.get("/api/calendar?date_from=2026-06-01&date_to=2026-12-31").json()
    assert body["holidays"] == [{"date": "2026-06-19", "name": "端午節"}, {"date": "2026-10-09", "name": "國慶日補假"}]
    assert body["years_covered"] == ["2026", "2027"]
    assert client.get("/api/calendar?date_from=2026-07-01&date_to=2026-07-31").json()["holidays"] == []


def test_calendar_rejects_bad_ranges(client):
    assert client.get("/api/calendar?date_from=2026-6-1&date_to=2026-12-31").status_code == 422
    assert client.get("/api/calendar?date_from=2026-12-31&date_to=2026-01-01").status_code == 422
    assert client.get("/api/calendar?date_from=2021-01-01&date_to=2026-12-31").status_code == 422   # 6 個年份
    assert client.get("/api/calendar?date_from=2022-01-01&date_to=2026-12-31").status_code == 200   # 5 個年份
    assert client.get("/api/calendar?date_from=2026-01-01").status_code == 422
