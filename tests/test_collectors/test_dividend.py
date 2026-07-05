"""除權息還原因子 collector 測試（FinMind TaiwanStockDividendResult）。不打真實 HTTP。"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

from collectors.dividend import (
    collect_dividend_factors,
    load_dividend_factors,
    save_dividend_factors,
)
from db.schema import create_all_tables

FIXTURE = (Path(__file__).resolve().parent.parent / "fixtures" / "finmind"
           / "dividend_result_0050.json")


def _resp(data):
    m = MagicMock()
    m.json.return_value = data
    return m


def test_collect_parses_factors():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with patch("collectors.dividend.http_get", return_value=_resp(fixture)):
        events = collect_dividend_factors("0050")

    assert len(events) == 3
    assert events[0]["ex_date"] == "2025-01-17"
    # before 198.05 / after 195.35
    assert abs(events[0]["factor"] - 195.35 / 198.05) < 1e-9
    assert all(0 < e["factor"] <= 1.05 for e in events)


def test_collect_non_200_returns_empty():
    """付費等級被擋（400）等非 200 → 回 []，不炸。"""
    with patch("collectors.dividend.http_get",
               return_value=_resp({"status": 400, "msg": "Your level is free"})):
        assert collect_dividend_factors("0050") == []


def test_collect_request_error_returns_empty():
    with patch("collectors.dividend.http_get", side_effect=Exception("net down")):
        assert collect_dividend_factors("0050") == []


def test_collect_skips_malformed_rows():
    bad = {"status": 200, "data": [
        {"date": "2025-01-17", "before_price": 198.05, "after_price": 195.35},
        {"date": "2025-07-21", "before_price": 0, "after_price": 50},      # before=0 跳過
        {"date": "", "before_price": 10, "after_price": 9},                # 無日期跳過
        {"date": "2026-01-22", "before_price": "x", "after_price": 70},    # 非數字跳過
    ]}
    with patch("collectors.dividend.http_get", return_value=_resp(bad)):
        events = collect_dividend_factors("0050")
    assert [e["ex_date"] for e in events] == ["2025-01-17"]


def test_save_and_load_roundtrip(tmp_path):
    db = str(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    create_all_tables(conn)
    conn.close()

    n = save_dividend_factors(
        "0050", [{"ex_date": "2025-07-21", "factor": 0.993}], db_path=db)
    assert n == 1

    conn = sqlite3.connect(db)
    assert load_dividend_factors(conn, "0050") == [
        {"ex_date": "2025-07-21", "factor": 0.993}]
    conn.close()
