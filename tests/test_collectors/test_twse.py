import json
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from collectors.twse import TWSECollector
from db.schema import create_all_tables

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "twse"


@pytest.fixture
def twse_collector(tmp_path):
    """建立 TWSECollector，使用 tmp_path 的 DB。"""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.close()
    return TWSECollector(db_path=db_path)


def _load_fixture(filename: str) -> dict:
    return json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))


class TestCollectInstitutional:
    def test_parse_normal_response(self, twse_collector):
        """正常回應能正確 parse 出三大法人買賣超。"""
        fixture = _load_fixture("bfi82u_20260410.json")
        mock_resp = MagicMock()
        mock_resp.json.return_value = fixture

        with patch("collectors.twse.http_get", return_value=mock_resp):
            data = twse_collector.collect("2026-04-10")

        assert data is not None
        assert "foreign_buy" in data
        assert "foreign_sell" in data
        assert "foreign_net" in data
        assert "trust_net" in data
        assert "dealer_net" in data
        assert "total_net" in data
        # Verify specific values from the real fixture (after removing commas)
        assert data["foreign_buy"] == 324941653192
        assert data["foreign_sell"] == 296133381984
        assert data["foreign_net"] == 28808271208
        assert data["trust_net"] == -4301105584
        assert data["total_net"] == 36765321462

    def test_holiday_returns_none(self, twse_collector):
        """非交易日 (stat != OK) 回傳 None。"""
        fixture = _load_fixture("bfi82u_20260412_holiday.json")
        mock_resp = MagicMock()
        mock_resp.json.return_value = fixture

        with patch("collectors.twse.http_get", return_value=mock_resp):
            data = twse_collector.collect("2026-04-12")

        assert data is None

    def test_date_mismatch_returns_none(self, twse_collector):
        """API 回傳的日期與請求日期不符時回傳 None（週末/假日行為）。"""
        fixture = _load_fixture("bfi82u_20260410.json")
        mock_resp = MagicMock()
        mock_resp.json.return_value = fixture

        with patch("collectors.twse.http_get", return_value=mock_resp):
            # Request 2026-04-12 (Sunday) but fixture has date 20260410
            data = twse_collector.collect("2026-04-12")

        assert data is None

    def test_comma_number_parsing(self, twse_collector):
        """帶逗號的金額字串能正確轉成數字。"""
        fixture = _load_fixture("bfi82u_20260410.json")
        mock_resp = MagicMock()
        mock_resp.json.return_value = fixture

        with patch("collectors.twse.http_get", return_value=mock_resp):
            data = twse_collector.collect("2026-04-10")

        for key in [
            "foreign_buy", "foreign_sell", "foreign_net",
            "trust_buy", "trust_sell", "trust_net",
            "dealer_buy", "dealer_sell", "dealer_net",
            "total_net",
        ]:
            assert isinstance(data[key], (int, float)), f"{key} should be numeric"


class TestSaveAndIdempotent:
    def test_save_writes_to_db(self, twse_collector):
        """save 能正確寫入 raw_institutional。"""
        data = {
            "foreign_buy": 324941653192,
            "foreign_sell": 296133381984,
            "foreign_net": 28808271208,
            "trust_buy": 9480174170,
            "trust_sell": 13781279754,
            "trust_net": -4301105584,
            "dealer_buy": 41316774350,
            "dealer_sell": 29058618512,
            "dealer_net": 12258155838,
            "total_net": 36765321462,
        }
        twse_collector.save("2026-04-10", data)

        conn = sqlite3.connect(twse_collector.db_path)
        row = conn.execute(
            "SELECT foreign_net, total_net FROM raw_institutional WHERE date = '2026-04-10'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == 28808271208
        assert row[1] == 36765321462

    def test_save_idempotent(self, twse_collector):
        """同一天存兩次不報錯，且資料為最新值。"""
        data1 = {
            "foreign_buy": 100, "foreign_sell": 50, "foreign_net": 50,
            "trust_buy": 100, "trust_sell": 50, "trust_net": 50,
            "dealer_buy": 100, "dealer_sell": 50, "dealer_net": 50,
            "total_net": 150,
        }
        data2 = {
            "foreign_buy": 200, "foreign_sell": 60, "foreign_net": 140,
            "trust_buy": 200, "trust_sell": 60, "trust_net": 140,
            "dealer_buy": 200, "dealer_sell": 60, "dealer_net": 140,
            "total_net": 420,
        }

        twse_collector.save("2026-04-10", data1)
        twse_collector.save("2026-04-10", data2)

        conn = sqlite3.connect(twse_collector.db_path)
        row = conn.execute(
            "SELECT total_net FROM raw_institutional WHERE date = '2026-04-10'"
        ).fetchone()
        conn.close()

        assert row[0] == 420


class TestRunFlow:
    def test_http_failure_returns_false(self, twse_collector):
        """HTTP 失敗時 run() 回傳 False 而非 crash。"""
        with patch(
            "collectors.twse.http_get",
            side_effect=Exception("Connection timeout"),
        ):
            result = twse_collector.run("2026-04-10")

        assert result is False

    def test_run_success(self, twse_collector):
        """run() 正常流程：collect -> save -> True。"""
        fixture = _load_fixture("bfi82u_20260410.json")
        mock_resp = MagicMock()
        mock_resp.json.return_value = fixture

        with patch("collectors.twse.http_get", return_value=mock_resp):
            result = twse_collector.run("2026-04-10")

        assert result is True

        conn = sqlite3.connect(twse_collector.db_path)
        row = conn.execute(
            "SELECT foreign_net FROM raw_institutional WHERE date = '2026-04-10'"
        ).fetchone()
        conn.close()
        assert row is not None
