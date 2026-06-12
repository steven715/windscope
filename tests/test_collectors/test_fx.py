import json
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from collectors.fx import FXCollector
from db.schema import create_all_tables

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "fx"


@pytest.fixture
def fx_collector(tmp_path):
    """建立 FXCollector，使用 tmp_path 的 DB。"""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.close()
    return FXCollector(db_path=db_path)


def _load_fixture(filename: str) -> str:
    return (FIXTURE_DIR / filename).read_text(encoding="utf-8")


def _load_json_fixture(filename: str) -> dict:
    return json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))


# ── USD/TWD（台銀 CSV）──────────────────────────────────────────


class TestCollectTWD:
    def test_parse_bot_csv(self, fx_collector):
        """正常解析台銀 CSV 取得 USD 即期買入匯率。"""
        csv_text = _load_fixture("bot_tw_csv.csv")
        data = fx_collector._parse_bot_csv(csv_text)

        assert data is not None
        assert data["currency_pair"] == "USD/TWD"
        assert data["rate"] == 31.27

    def test_parse_bot_csv_empty(self, fx_collector):
        """空 CSV 回傳 None。"""
        data = fx_collector._parse_bot_csv("")
        assert data is None

    def test_collect_twd_http(self, fx_collector):
        """透過 mock 測試完整收集流程。"""
        csv_text = _load_fixture("bot_tw_csv.csv")
        mock_resp = MagicMock()
        mock_resp.text = csv_text

        with patch("collectors.fx.http_get", return_value=mock_resp):
            data = fx_collector.collect_twd("2026-04-08")

        assert data is not None
        assert data["rate"] == 31.27

    def test_collect_twd_failure(self, fx_collector):
        """HTTP 失敗回傳 None。"""
        with patch("collectors.fx.http_get", side_effect=Exception("timeout")):
            data = fx_collector.collect_twd("2026-04-08")

        assert data is None


# ── USD/CNY、USD/KRW（Yahoo Finance）────────────────────────────


class TestCollectForeignFX:
    def test_parse_yahoo_usdcny(self, fx_collector):
        """正常解析 Yahoo Finance USD/CNY。"""
        fixture = _load_json_fixture("yahoo_usdcny.json")
        data = fx_collector._parse_yahoo_chart(fixture, "USD/CNY")

        assert data is not None
        assert data["currency_pair"] == "USD/CNY"
        assert data["rate"] == 7.25

    def test_parse_yahoo_usdkrw(self, fx_collector):
        """正常解析 Yahoo Finance USD/KRW。"""
        fixture = _load_json_fixture("yahoo_usdkrw.json")
        data = fx_collector._parse_yahoo_chart(fixture, "USD/KRW")

        assert data is not None
        assert data["currency_pair"] == "USD/KRW"
        assert data["rate"] == 1365.50

    def test_collect_foreign_fx_http(self, fx_collector):
        """透過 mock 測試完整收集流程。"""
        fixture = _load_json_fixture("yahoo_usdcny.json")
        mock_resp = MagicMock()
        mock_resp.json.return_value = fixture

        with patch("collectors.fx.http_get", return_value=mock_resp):
            data = fx_collector.collect_foreign_fx("USD/CNY")

        assert data is not None
        assert data["rate"] == 7.25

    def test_collect_foreign_fx_failure(self, fx_collector):
        """HTTP 失敗回傳 None。"""
        with patch("collectors.fx.http_get", side_effect=Exception("timeout")):
            data = fx_collector.collect_foreign_fx("USD/CNY")

        assert data is None

    def test_unknown_currency_pair(self, fx_collector):
        """不支援的幣對回傳 None。"""
        data = fx_collector.collect_foreign_fx("USD/JPY")
        assert data is None

    def test_parse_yahoo_empty_result(self, fx_collector):
        """Yahoo 回傳空結果回傳 None。"""
        data = fx_collector._parse_yahoo_chart({"chart": {"result": []}}, "USD/CNY")
        assert data is None


# ── Save ────────────────────────────────────────────────────────


class TestSave:
    def test_save_fx_close_16(self, fx_collector):
        """save_fx 正確寫入 close_16 欄位。"""
        fx_collector.save_fx("2026-04-08", "USD/TWD", 31.27, "close_16")

        conn = sqlite3.connect(fx_collector.db_path)
        row = conn.execute(
            "SELECT close_16 FROM raw_fx WHERE date = '2026-04-08' AND currency_pair = 'USD/TWD'"
        ).fetchone()
        conn.close()

        assert row[0] == 31.27

    def test_save_fx_quote_0845(self, fx_collector):
        """save_fx 正確寫入 quote_0845 欄位。"""
        fx_collector.save_fx("2026-04-08", "USD/TWD", 31.35, "quote_0845")

        conn = sqlite3.connect(fx_collector.db_path)
        row = conn.execute(
            "SELECT quote_0845 FROM raw_fx WHERE date = '2026-04-08' AND currency_pair = 'USD/TWD'"
        ).fetchone()
        conn.close()

        assert row[0] == 31.35

    def test_save_fx_partial_update(self, fx_collector):
        """寫入 quote_0845 不覆蓋已有的 close_16。"""
        fx_collector.save_fx("2026-04-08", "USD/TWD", 31.27, "close_16")
        fx_collector.save_fx("2026-04-08", "USD/TWD", 31.35, "quote_0845")

        conn = sqlite3.connect(fx_collector.db_path)
        row = conn.execute(
            "SELECT close_16, quote_0845 FROM raw_fx "
            "WHERE date = '2026-04-08' AND currency_pair = 'USD/TWD'"
        ).fetchone()
        conn.close()

        assert row[0] == 31.27  # 未被覆蓋
        assert row[1] == 31.35


# ── Run ─────────────────────────────────────────────────────────


class TestRunFlow:
    def test_run_returns_dict(self, fx_collector):
        """run() 回傳 dict。"""
        csv_text = _load_fixture("bot_tw_csv.csv")
        cny_fixture = _load_json_fixture("yahoo_usdcny.json")
        krw_fixture = _load_json_fixture("yahoo_usdkrw.json")

        def mock_get(url, **kwargs):
            resp = MagicMock()
            if "bot.com.tw" in url:
                resp.text = csv_text
                return resp
            elif "USDCNY" in url:
                resp.json.return_value = cny_fixture
                return resp
            elif "USDKRW" in url:
                resp.json.return_value = krw_fixture
                return resp
            raise Exception("unexpected URL")

        with patch("collectors.fx.http_get", side_effect=mock_get):
            results = fx_collector.run("2026-04-08")

        assert isinstance(results, dict)
        assert results["usd_twd"] is True
        assert results["usd_cny"] is True
        assert results["usd_krw"] is True

    def test_run_partial_failure(self, fx_collector):
        """一個來源失敗不影響其他。"""
        csv_text = _load_fixture("bot_tw_csv.csv")

        def mock_get(url, **kwargs):
            resp = MagicMock()
            if "bot.com.tw" in url:
                resp.text = csv_text
                return resp
            raise Exception("timeout")

        with patch("collectors.fx.http_get", side_effect=mock_get):
            results = fx_collector.run("2026-04-08")

        assert results["usd_twd"] is True
        assert results["usd_cny"] is False
        assert results["usd_krw"] is False


# ── S&P 500（Yahoo Finance ^GSPC）───────────────────────────────


class TestCollectSP500:
    def test_collect_sp500(self, fx_collector):
        """從真實 fixture 解析 S&P 500 收盤價。"""
        fixture = _load_json_fixture("yahoo_gspc_20260612.json")
        mock_resp = MagicMock()
        mock_resp.json.return_value = fixture

        with patch("collectors.fx.http_get", return_value=mock_resp):
            data = fx_collector.collect_sp500()

        assert data is not None
        assert data["close"] == pytest.approx(7381.14, abs=0.01)

    def test_collect_sp500_http_failure(self, fx_collector):
        """HTTP 失敗回傳 None 而非 crash。"""
        with patch("collectors.fx.http_get", side_effect=Exception("timeout")):
            data = fx_collector.collect_sp500()

        assert data is None

    def test_collect_sp500_malformed(self, fx_collector):
        """格式異常回傳 None。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"chart": {"result": []}}

        with patch("collectors.fx.http_get", return_value=mock_resp):
            data = fx_collector.collect_sp500()

        assert data is None

    def test_save_sp500(self, fx_collector):
        """save_sp500 寫入 raw_futures.sp500_close，不覆蓋其他欄位。"""
        conn = sqlite3.connect(fx_collector.db_path)
        conn.execute(
            "INSERT INTO raw_futures (date, night_close) VALUES ('2026-06-12', 42615.0)"
        )
        conn.commit()
        conn.close()

        fx_collector.save_sp500("2026-06-12", 7381.14)

        conn = sqlite3.connect(fx_collector.db_path)
        row = conn.execute(
            "SELECT night_close, sp500_close FROM raw_futures WHERE date = '2026-06-12'"
        ).fetchone()
        conn.close()

        assert row[0] == 42615.0  # 未被覆蓋
        assert row[1] == pytest.approx(7381.14)
