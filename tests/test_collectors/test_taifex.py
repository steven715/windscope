import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from collectors.taifex import TAIFEXCollector
from db.schema import create_all_tables

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "taifex"


@pytest.fixture
def taifex_collector(tmp_path):
    """建立 TAIFEXCollector，使用 tmp_path 的 DB。"""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.close()
    return TAIFEXCollector(db_path=db_path)


def _load_fixture(filename: str) -> str:
    return (FIXTURE_DIR / filename).read_text(encoding="utf-8")


# ── 夜盤收盤 ────────────────────────────────────────────────────


class TestCollectNightSession:
    def test_parse_night_csv(self, taifex_collector):
        """正常解析 CSV 取得夜盤收盤價和成交量。"""
        csv_text = _load_fixture("fut_contracts_20260408.csv")
        data = taifex_collector._parse_night_csv(csv_text, "2026-04-08")

        assert data is not None
        assert data["night_close"] == 19900.0
        assert data["night_volume"] == 45000

    def test_parse_night_csv_empty(self, taifex_collector):
        """空 CSV 回傳 None。"""
        data = taifex_collector._parse_night_csv("", "2026-04-08")
        assert data is None

    def test_collect_night_session_http(self, taifex_collector):
        """透過 http_post mock 測試完整流程。"""
        csv_text = _load_fixture("fut_contracts_20260408.csv")
        mock_resp = MagicMock()
        mock_resp.text = csv_text

        with patch("collectors.taifex.http_post", return_value=mock_resp):
            data = taifex_collector.collect_night_session("2026-04-08")

        assert data is not None
        assert data["night_close"] == 19900.0

    def test_collect_night_http_failure(self, taifex_collector):
        """HTTP 失敗回傳 None 而非 crash。"""
        with patch("collectors.taifex.http_post", side_effect=Exception("timeout")):
            data = taifex_collector.collect_night_session("2026-04-08")

        assert data is None


# ── 外資未平倉 ──────────────────────────────────────────────────


class TestCollectOiForeign:
    def test_stub_returns_none(self, taifex_collector):
        """Stub 正確回傳 None。"""
        data = taifex_collector.collect_oi_foreign("2026-04-08")
        assert data is None

    def test_parse_oi_csv(self, taifex_collector):
        """從 CSV fixture 正確解析外資未平倉淨額。"""
        csv_text = _load_fixture("oi_foreign_20260408.csv")
        data = taifex_collector.collect_oi_foreign_from_csv(csv_text, "2026-04-08")

        assert data is not None
        assert data["oi_net_foreign"] == -34800

    def test_parse_oi_csv_empty(self, taifex_collector):
        """空 CSV 回傳 None。"""
        data = taifex_collector.collect_oi_foreign_from_csv("", "2026-04-08")
        assert data is None


# ── Save + Partial Update ───────────────────────────────────────


class TestSave:
    def test_save_night_session(self, taifex_collector):
        """save_night_session 寫入 raw_futures。"""
        taifex_collector.save_night_session(
            "2026-04-08", {"night_close": 19900.0, "night_volume": 45000}
        )

        conn = sqlite3.connect(taifex_collector.db_path)
        row = conn.execute(
            "SELECT night_close, night_volume FROM raw_futures WHERE date = '2026-04-08'"
        ).fetchone()
        conn.close()

        assert row[0] == 19900.0
        assert row[1] == 45000

    def test_save_night_session_partial_update(self, taifex_collector):
        """night_session 寫入不覆蓋 spot_close。"""
        conn = sqlite3.connect(taifex_collector.db_path)
        conn.execute(
            "INSERT INTO raw_futures (date, spot_close) VALUES ('2026-04-08', 19800.5)"
        )
        conn.commit()
        conn.close()

        taifex_collector.save_night_session(
            "2026-04-08", {"night_close": 19900.0, "night_volume": 45000}
        )

        conn = sqlite3.connect(taifex_collector.db_path)
        row = conn.execute(
            "SELECT spot_close, night_close, night_volume FROM raw_futures WHERE date = '2026-04-08'"
        ).fetchone()
        conn.close()

        assert row[0] == 19800.5  # 未被覆蓋
        assert row[1] == 19900.0
        assert row[2] == 45000

    def test_save_oi_foreign(self, taifex_collector):
        """save_oi_foreign 寫入 raw_futures。"""
        taifex_collector.save_oi_foreign("2026-04-08", {"oi_net_foreign": -34800})

        conn = sqlite3.connect(taifex_collector.db_path)
        row = conn.execute(
            "SELECT oi_net_foreign FROM raw_futures WHERE date = '2026-04-08'"
        ).fetchone()
        conn.close()

        assert row[0] == -34800


# ── Run ─────────────────────────────────────────────────────────


class TestRunFlow:
    def test_run_returns_dict(self, taifex_collector):
        """run() 回傳 dict 而非 bool。"""
        csv_text = _load_fixture("fut_contracts_20260408.csv")
        mock_resp = MagicMock()
        mock_resp.text = csv_text

        with patch("collectors.taifex.http_post", return_value=mock_resp):
            results = taifex_collector.run("2026-04-08")

        assert isinstance(results, dict)
        assert results["night_session"] is True
        assert results["oi_foreign"] is False  # stub
