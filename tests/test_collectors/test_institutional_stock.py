"""個股外資買賣超歷史 collector 測試（FinMind institutional）。不打真實 HTTP。"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

from collectors.institutional_stock import (
    collect_foreign_history,
    save_foreign_history,
)
from db.schema import create_all_tables

FIXTURE = (Path(__file__).resolve().parent.parent / "fixtures" / "finmind"
           / "institutional_2454.json")


def _resp(data):
    m = MagicMock()
    m.json.return_value = data
    return m


def test_collect_parses_foreign_only_to_lots():
    """只取 Foreign_Investor、(買−賣)股/1000=張。"""
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with patch("collectors.institutional_stock.http_get", return_value=_resp(fixture)):
        rows = collect_foreign_history("2454")

    assert len(rows) == 4                       # fixture 4 個交易日的外資列
    d = {r["date"]: r["net_volume"] for r in rows}
    assert d["2026-06-30"] == 814               # buy 8424167 − sell 7609922 → 814 張
    assert d["2026-07-03"] == -3366             # 淨賣超


def test_collect_non_200_returns_empty():
    with patch("collectors.institutional_stock.http_get",
               return_value=_resp({"status": 402, "msg": "limit reached"})):
        assert collect_foreign_history("2454") == []


def test_collect_request_error_returns_empty():
    with patch("collectors.institutional_stock.http_get",
               side_effect=Exception("net down")):
        assert collect_foreign_history("2454") == []


def test_save_roundtrip_to_raw_chip_foreign(tmp_path):
    db = str(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    create_all_tables(conn)
    conn.close()

    n = save_foreign_history(
        "2454", [{"date": "2026-07-03", "net_volume": -3366}], db_path=db)
    assert n == 1

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT net_volume FROM raw_chip WHERE stock_id='2454' "
        "AND broker_name='__FOREIGN__'").fetchone()
    assert row[0] == -3366
    conn.close()
