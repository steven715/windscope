import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from collectors.mis import MISCollector

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "mis"


def _load_json_fixture(filename: str) -> dict:
    return json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))


@pytest.fixture
def collector() -> MISCollector:
    return MISCollector()


class TestParseQuote:
    def test_parse_index(self, collector):
        """正常解析加權指數 t00。"""
        data = _load_json_fixture("getstockinfo_t00_2330.json")
        q = collector._parse_quote(data, "t00")

        assert q is not None
        assert q["symbol"] == "t00"
        assert q["price"] == 45385.29
        assert q["prev_close"] == 44169.04
        assert q["open"] == 44447.87
        assert q["ts"] == 1781497720000

    def test_parse_stock(self, collector):
        """正常解析個股 2330。"""
        data = _load_json_fixture("getstockinfo_t00_2330.json")
        q = collector._parse_quote(data, "2330")

        assert q is not None
        assert q["price"] == 2365.0
        assert q["prev_close"] == 2310.0

    def test_price_fallback_to_open_when_no_trade(self, collector):
        """盤前/無成交（z='-'）時 price fallback 到開盤 o。"""
        data = {
            "rtcode": "0000",
            "msgArray": [{"c": "t00", "n": "加權指數",
                          "z": "-", "y": "44169.04", "o": "44447.87",
                          "h": "44447.87", "l": "44447.87", "tlong": "1781497720000"}],
        }
        q = collector._parse_quote(data, "t00")

        assert q is not None
        assert q["price"] == 44447.87  # 取開盤價

    def test_rtcode_error_returns_none(self, collector):
        """rtcode 非 0000 回 None。"""
        data = {"rtcode": "5004", "rtmessage": "invalid", "msgArray": []}
        assert collector._parse_quote(data, "t00") is None

    def test_symbol_not_found_returns_none(self, collector):
        """msgArray 無對應 symbol 回 None。"""
        data = _load_json_fixture("getstockinfo_t00_2330.json")
        assert collector._parse_quote(data, "9999") is None

    def test_missing_prev_close_returns_none(self, collector):
        """缺昨收基準回 None（無法算漲跌）。"""
        data = {
            "rtcode": "0000",
            "msgArray": [{"c": "t00", "z": "45385.29", "y": "-", "o": "-"}],
        }
        assert collector._parse_quote(data, "t00") is None


class TestCollectIndex:
    def test_collect_index_http(self, collector):
        """透過 mock 測試完整流程。"""
        data = _load_json_fixture("getstockinfo_t00_2330.json")
        mock_resp = MagicMock()
        mock_resp.json.return_value = data

        with patch("collectors.mis.http_get", return_value=mock_resp):
            q = collector.collect_index("t00")

        assert q is not None
        assert q["price"] == 45385.29

    def test_collect_index_failure(self, collector):
        """HTTP 失敗回 None，不丟例外。"""
        with patch("collectors.mis.http_get", side_effect=Exception("timeout")):
            assert collector.collect_index("t00") is None
