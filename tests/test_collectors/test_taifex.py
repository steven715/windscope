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


def _mock_resp(text: str) -> MagicMock:
    resp = MagicMock()
    resp.text = text
    return resp


# 假日 / 無資料時期交所回傳的 HTML 錯誤頁（實測 2026-06-12）
HOLIDAY_HTML = "<!DOCTYPE HTML><html><body>查無資料</body></html>"


# ── 夜盤收盤 ────────────────────────────────────────────────────


class TestCollectNightSession:
    def test_parse_night_csv(self, taifex_collector):
        """正常解析 CSV 取得近月夜盤收盤價和成交量（真實 fixture）。"""
        csv_text = _load_fixture("fut_data_20260611.csv")
        data = taifex_collector._parse_night_csv(csv_text, "2026-06-11")

        assert data is not None
        assert data["night_close"] == 42615.0
        assert data["night_volume"] == 117269

    def test_parse_night_csv_empty(self, taifex_collector):
        """空 CSV 回傳 None。"""
        data = taifex_collector._parse_night_csv("", "2026-06-11")
        assert data is None

    def test_parse_night_csv_holiday_html(self, taifex_collector):
        """假日回傳 HTML 錯誤頁時應回 None 而非 crash。"""
        data = taifex_collector._parse_night_csv(HOLIDAY_HTML, "2026-06-07")
        assert data is None

    def test_collect_night_session_http(self, taifex_collector):
        """透過 http_post mock 測試完整流程與 POST 參數。"""
        csv_text = _load_fixture("fut_data_20260611.csv")

        with patch("collectors.taifex.http_post", return_value=_mock_resp(csv_text)) as mock_post:
            data = taifex_collector.collect_night_session("2026-06-11")

        assert data is not None
        assert data["night_close"] == 42615.0
        sent = mock_post.call_args.kwargs.get("data") or mock_post.call_args.args[1]
        assert sent["queryStartDate"] == "2026/06/11"
        assert sent["commodity_id"] == "TX"

    def test_collect_night_http_failure(self, taifex_collector):
        """HTTP 失敗回傳 None 而非 crash。"""
        with patch("collectors.taifex.http_post", side_effect=Exception("timeout")):
            data = taifex_collector.collect_night_session("2026-06-11")

        assert data is None


# ── 外資未平倉 ──────────────────────────────────────────────────


class TestCollectOiForeign:
    def test_parse_oi_csv(self, taifex_collector):
        """從真實 CSV fixture 正確解析外資未平倉淨額。"""
        csv_text = _load_fixture("oi_foreign_20260611.csv")
        data = taifex_collector.collect_oi_foreign_from_csv(csv_text, "2026-06-11")

        assert data is not None
        assert data["oi_net_foreign"] == -63168

    def test_parse_oi_csv_empty(self, taifex_collector):
        """空 CSV 回傳 None。"""
        data = taifex_collector.collect_oi_foreign_from_csv("", "2026-06-11")
        assert data is None

    def test_parse_oi_csv_holiday_html(self, taifex_collector):
        """假日回傳 HTML 錯誤頁時應回 None。"""
        data = taifex_collector.collect_oi_foreign_from_csv(HOLIDAY_HTML, "2026-06-07")
        assert data is None

    def test_collect_oi_foreign_http(self, taifex_collector):
        """透過 http_post mock 測試完整流程與 POST 參數。"""
        csv_text = _load_fixture("oi_foreign_20260611.csv")

        with patch("collectors.taifex.http_post", return_value=_mock_resp(csv_text)) as mock_post:
            data = taifex_collector.collect_oi_foreign("2026-06-11")

        assert data is not None
        assert data["oi_net_foreign"] == -63168
        sent = mock_post.call_args.kwargs.get("data") or mock_post.call_args.args[1]
        assert sent["queryStartDate"] == "2026/06/11"
        assert sent["commodityId"] == "TXF"

    def test_collect_oi_foreign_http_failure(self, taifex_collector):
        """HTTP 失敗回傳 None 而非 crash。"""
        with patch("collectors.taifex.http_post", side_effect=Exception("timeout")):
            data = taifex_collector.collect_oi_foreign("2026-06-11")

        assert data is None


# ── Save + Partial Update ───────────────────────────────────────


class TestSave:
    def test_save_night_session(self, taifex_collector):
        """save_night_session 寫入 raw_futures。"""
        taifex_collector.save_night_session(
            "2026-06-11", {"night_close": 42615.0, "night_volume": 117269}
        )

        conn = sqlite3.connect(taifex_collector.db_path)
        row = conn.execute(
            "SELECT night_close, night_volume FROM raw_futures WHERE date = '2026-06-11'"
        ).fetchone()
        conn.close()

        assert row[0] == 42615.0
        assert row[1] == 117269

    def test_save_night_session_partial_update(self, taifex_collector):
        """night_session 寫入不覆蓋 spot_close。"""
        conn = sqlite3.connect(taifex_collector.db_path)
        conn.execute(
            "INSERT INTO raw_futures (date, spot_close) VALUES ('2026-06-11', 42500.5)"
        )
        conn.commit()
        conn.close()

        taifex_collector.save_night_session(
            "2026-06-11", {"night_close": 42615.0, "night_volume": 117269}
        )

        conn = sqlite3.connect(taifex_collector.db_path)
        row = conn.execute(
            "SELECT spot_close, night_close, night_volume FROM raw_futures WHERE date = '2026-06-11'"
        ).fetchone()
        conn.close()

        assert row[0] == 42500.5  # 未被覆蓋
        assert row[1] == 42615.0
        assert row[2] == 117269

    def test_save_oi_foreign(self, taifex_collector):
        """save_oi_foreign 寫入 raw_futures。"""
        taifex_collector.save_oi_foreign("2026-06-11", {"oi_net_foreign": -63168})

        conn = sqlite3.connect(taifex_collector.db_path)
        row = conn.execute(
            "SELECT oi_net_foreign FROM raw_futures WHERE date = '2026-06-11'"
        ).fetchone()
        conn.close()

        assert row[0] == -63168


# ── Run ─────────────────────────────────────────────────────────


class TestRunFlow:
    def test_run_returns_dict(self, taifex_collector):
        """run() 回傳 dict，夜盤與 OI 都成功。"""
        night_csv = _load_fixture("fut_data_20260611.csv")
        oi_csv = _load_fixture("oi_foreign_20260611.csv")

        with patch(
            "collectors.taifex.http_post",
            side_effect=[_mock_resp(night_csv), _mock_resp(oi_csv)],
        ):
            results = taifex_collector.run("2026-06-11")

        assert isinstance(results, dict)
        assert results["night_session"] is True
        assert results["oi_foreign"] is True

    def test_run_oi_failure_does_not_block(self, taifex_collector):
        """OI 收集失敗時夜盤仍成功。"""
        night_csv = _load_fixture("fut_data_20260611.csv")

        with patch(
            "collectors.taifex.http_post",
            side_effect=[_mock_resp(night_csv), Exception("timeout")],
        ):
            results = taifex_collector.run("2026-06-11")

        assert results["night_session"] is True
        assert results["oi_foreign"] is False
