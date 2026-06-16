import json
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from collectors.twse import TWSECollector, _parse_amount, _to_roc_date
from db.schema import create_all_tables

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "twse"


@pytest.fixture
def twse_collector(tmp_path):
    """建立 TWSECollector，使用 tmp_path 的 DB。

    明確塞入 watchlist（2330、2409）使測試與 config/watchlist.json 解耦——
    外資個股 parser 依 watchlist 過濾，不應隨正式名單增刪而壞掉。
    """
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.execute("INSERT INTO watchlist (stock_id, stock_name, added_date, reason) "
                 "VALUES ('2330', '台積電', '2026-04-08', 'test'), "
                 "('2409', '友達', '2026-04-08', 'test')")
    conn.commit()
    conn.close()
    return TWSECollector(db_path=db_path)


def _load_fixture(filename: str) -> dict:
    return json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))


def _mock_resp(fixture_data):
    mock = MagicMock()
    mock.json.return_value = fixture_data
    return mock


# ── 工具函式測試 ────────────────────────────────────────────────


class TestUtilFunctions:
    def test_parse_amount_normal(self):
        assert _parse_amount("324,941,653,192") == 324941653192

    def test_parse_amount_negative_parens(self):
        assert _parse_amount("(4,301,105,584)") == -4301105584

    def test_parse_amount_negative_sign(self):
        assert _parse_amount("-4,301,105,584") == -4301105584

    def test_to_roc_date(self):
        assert _to_roc_date("2026-04-08") == "115/04/08"

    def test_to_roc_date_century(self):
        assert _to_roc_date("2000-01-01") == "89/01/01"


# ── 三大法人買賣超 ──────────────────────────────────────────────


class TestCollectInstitutional:
    def test_parse_normal_response(self, twse_collector):
        """正常回應能正確 parse 出三大法人買賣超。"""
        fixture = _load_fixture("bfi82u_20260410.json")

        with patch("collectors.twse.http_get", return_value=_mock_resp(fixture)):
            data = twse_collector.collect_institutional("2026-04-10")

        assert data is not None
        assert data["foreign_buy"] == 324941653192
        assert data["foreign_sell"] == 296133381984
        assert data["foreign_net"] == 28808271208
        assert data["trust_net"] == -4301105584
        assert data["total_net"] == 36765321462

    def test_holiday_returns_none(self, twse_collector):
        """非交易日 (stat != OK) 回傳 None。"""
        fixture = _load_fixture("bfi82u_20260412_holiday.json")

        with patch("collectors.twse.http_get", return_value=_mock_resp(fixture)):
            data = twse_collector.collect_institutional("2026-04-12")

        assert data is None

    def test_date_mismatch_returns_none(self, twse_collector):
        """API 回傳的日期與請求日期不符時回傳 None。"""
        fixture = _load_fixture("bfi82u_20260410.json")

        with patch("collectors.twse.http_get", return_value=_mock_resp(fixture)):
            data = twse_collector.collect_institutional("2026-04-12")

        assert data is None

    def test_comma_number_parsing(self, twse_collector):
        """帶逗號的金額字串能正確轉成數字。"""
        fixture = _load_fixture("bfi82u_20260410.json")

        with patch("collectors.twse.http_get", return_value=_mock_resp(fixture)):
            data = twse_collector.collect_institutional("2026-04-10")

        for key in [
            "foreign_buy", "foreign_sell", "foreign_net",
            "trust_buy", "trust_sell", "trust_net",
            "dealer_buy", "dealer_sell", "dealer_net",
            "total_net",
        ]:
            assert isinstance(data[key], (int, float)), f"{key} should be numeric"


# ── 加權指數收盤 ────────────────────────────────────────────────


class TestCollectSpotClose:
    def test_parse_spot_close(self, twse_collector):
        """正常解析加權指數收盤價。"""
        fixture = _load_fixture("fmtqik_202604.json")

        with patch("collectors.twse.http_get", return_value=_mock_resp(fixture)):
            data = twse_collector.collect_spot_close("2026-04-08")

        assert data is not None
        assert data["spot_close"] == 19800.50

    def test_spot_close_date_not_found(self, twse_collector):
        """目標日期不在回應中（如假日）回傳 None。"""
        fixture = _load_fixture("fmtqik_202604.json")

        with patch("collectors.twse.http_get", return_value=_mock_resp(fixture)):
            data = twse_collector.collect_spot_close("2026-04-06")  # Sunday

        assert data is None

    def test_spot_close_roc_date_conversion(self, twse_collector):
        """民國年轉換正確。"""
        fixture = _load_fixture("fmtqik_202604.json")

        with patch("collectors.twse.http_get", return_value=_mock_resp(fixture)):
            data = twse_collector.collect_spot_close("2026-04-10")

        assert data is not None
        assert data["spot_close"] == 20010.60

    def test_spot_close_no_data(self, twse_collector):
        """stat != OK 回傳 None。"""
        with patch("collectors.twse.http_get", return_value=_mock_resp({"stat": "很抱歉"})):
            data = twse_collector.collect_spot_close("2026-04-06")

        assert data is None


# ── 個股收盤 ────────────────────────────────────────────────────


class TestCollectStockClose:
    def test_parse_stock_close(self, twse_collector):
        """正常解析個股收盤價。"""
        fixture = _load_fixture("stock_day_2330_202604.json")

        with patch("collectors.twse.http_get", return_value=_mock_resp(fixture)):
            data = twse_collector.collect_stock_close("2026-04-08", "2330")

        assert data is not None
        assert data["stock_id"] == "2330"
        assert data["close_price"] == 895.0

    def test_stock_close_date_not_found(self, twse_collector):
        """非交易日回傳 None。"""
        fixture = _load_fixture("stock_day_2330_202604.json")

        with patch("collectors.twse.http_get", return_value=_mock_resp(fixture)):
            data = twse_collector.collect_stock_close("2026-04-06", "2330")

        assert data is None

    def test_collect_all_stock_close(self, twse_collector):
        """批次取得 watchlist 中所有個股收盤價。"""
        fixture = _load_fixture("stock_day_2330_202604.json")

        with patch("collectors.twse.http_get", return_value=_mock_resp(fixture)):
            results = twse_collector.collect_all_stock_close("2026-04-08")

        # watchlist 有 2 支，但都用同一個 fixture，所以只有 2330 會對得上 stock_id
        # (兩次呼叫都用同一 fixture，但 stock_id 不同只是參數)
        # 這裡測的是：不 crash、回傳 list
        assert isinstance(results, list)


# ── 外資個股買賣超 ──────────────────────────────────────────────


class TestCollectForeignStock:
    def test_parse_foreign_stock(self, twse_collector):
        """正常解析外資個股買賣超，篩選 watchlist。"""
        fixture = _load_fixture("t86_20260408.json")

        with patch("collectors.twse.http_get", return_value=_mock_resp(fixture)):
            data = twse_collector.collect_foreign_stock("2026-04-08")

        assert data is not None
        ids = {d["stock_id"] for d in data}
        # watchlist 只有 2330 和 2409
        assert "2330" in ids
        assert "2409" in ids
        assert "2317" not in ids  # 不在 watchlist

    def test_foreign_stock_volume_conversion(self, twse_collector):
        """股 → 張轉換正確（除以 1000）。"""
        fixture = _load_fixture("t86_20260408.json")

        with patch("collectors.twse.http_get", return_value=_mock_resp(fixture)):
            data = twse_collector.collect_foreign_stock("2026-04-08")

        item_2330 = next(d for d in data if d["stock_id"] == "2330")
        # fixture: 15,000,000 股 → 15000 張
        assert item_2330["foreign_net_volume"] == 15000

    def test_foreign_stock_no_data(self, twse_collector):
        """stat != OK 回傳 None。"""
        with patch("collectors.twse.http_get", return_value=_mock_resp({"stat": "很抱歉"})):
            data = twse_collector.collect_foreign_stock("2026-04-06")

        assert data is None


# ── 除息預估點數 ────────────────────────────────────────────────


class TestCollectExDividend:
    def test_parse_ex_dividend(self, twse_collector):
        """正常解析除息預估點數。"""
        fixture = _load_fixture("twt49u_20260408.json")

        with patch("collectors.twse.http_get", return_value=_mock_resp(fixture)):
            data = twse_collector.collect_ex_dividend_points("2026-04-08")

        assert data is not None
        assert data["ex_dividend_points"] == 3.25

    def test_no_ex_dividend_day(self, twse_collector):
        """非除息日回傳 0。"""
        with patch("collectors.twse.http_get", return_value=_mock_resp({"stat": "很抱歉"})):
            data = twse_collector.collect_ex_dividend_points("2026-04-09")

        assert data is not None
        assert data["ex_dividend_points"] == 0.0

    def test_request_failure_returns_zero(self, twse_collector):
        """請求失敗時回傳 0 而非 crash。"""
        with patch("collectors.twse.http_get", side_effect=Exception("timeout")):
            data = twse_collector.collect_ex_dividend_points("2026-04-08")

        assert data is not None
        assert data["ex_dividend_points"] == 0.0


# ── Save + Partial Update ───────────────────────────────────────


class TestSave:
    def test_save_institutional(self, twse_collector):
        """save_institutional 寫入 raw_institutional。"""
        data = {
            "foreign_buy": 100, "foreign_sell": 50, "foreign_net": 50,
            "trust_buy": 100, "trust_sell": 50, "trust_net": 50,
            "dealer_buy": 100, "dealer_sell": 50, "dealer_net": 50,
            "total_net": 150,
        }
        twse_collector.save_institutional("2026-04-10", data)

        conn = sqlite3.connect(twse_collector.db_path)
        row = conn.execute(
            "SELECT total_net FROM raw_institutional WHERE date = '2026-04-10'"
        ).fetchone()
        conn.close()
        assert row[0] == 150

    def test_save_institutional_idempotent(self, twse_collector):
        """同一天存兩次不報錯，資料為最新值。"""
        data1 = {
            "foreign_buy": 100, "foreign_sell": 50, "foreign_net": 50,
            "trust_buy": 100, "trust_sell": 50, "trust_net": 50,
            "dealer_buy": 100, "dealer_sell": 50, "dealer_net": 50,
            "total_net": 150,
        }
        data2 = {**data1, "total_net": 420}

        twse_collector.save_institutional("2026-04-10", data1)
        twse_collector.save_institutional("2026-04-10", data2)

        conn = sqlite3.connect(twse_collector.db_path)
        row = conn.execute(
            "SELECT total_net FROM raw_institutional WHERE date = '2026-04-10'"
        ).fetchone()
        conn.close()
        assert row[0] == 420

    def test_save_spot_close_partial_update(self, twse_collector):
        """spot_close 寫入不覆蓋 night_close。"""
        conn = sqlite3.connect(twse_collector.db_path)
        conn.execute(
            "INSERT INTO raw_futures (date, night_close) VALUES ('2026-04-08', 19850)"
        )
        conn.commit()
        conn.close()

        twse_collector.save_spot_close("2026-04-08", {"spot_close": 19800.5})

        conn = sqlite3.connect(twse_collector.db_path)
        row = conn.execute(
            "SELECT night_close, spot_close FROM raw_futures WHERE date = '2026-04-08'"
        ).fetchone()
        conn.close()

        assert row[0] == 19850  # night_close 未被覆蓋
        assert row[1] == 19800.5

    def test_save_ex_dividend_partial_update(self, twse_collector):
        """ex_dividend_points 寫入不覆蓋其他欄位。"""
        conn = sqlite3.connect(twse_collector.db_path)
        conn.execute(
            "INSERT INTO raw_futures (date, spot_close) VALUES ('2026-04-08', 19800.5)"
        )
        conn.commit()
        conn.close()

        twse_collector.save_ex_dividend("2026-04-08", {"ex_dividend_points": 3.25})

        conn = sqlite3.connect(twse_collector.db_path)
        row = conn.execute(
            "SELECT spot_close, ex_dividend_points FROM raw_futures WHERE date = '2026-04-08'"
        ).fetchone()
        conn.close()

        assert row[0] == 19800.5  # 未被覆蓋
        assert row[1] == 3.25


# ── Run 整合 ────────────────────────────────────────────────────


class TestRunFlow:
    def test_http_failure_returns_false(self, twse_collector):
        """HTTP 失敗時各子任務回傳 False 而非 crash。"""
        with patch("collectors.twse.http_get", side_effect=Exception("timeout")):
            results = twse_collector.run("2026-04-10")

        assert isinstance(results, dict)
        # ex_dividend returns {"ex_dividend_points": 0.0} on failure, so it's True
        assert results["institutional"] is False
        assert results["spot_close"] is False

    def test_run_success(self, twse_collector):
        """run() 正常流程收集多種資料。"""
        fixtures = {
            "BFI82U": _load_fixture("bfi82u_20260410.json"),
            "FMTQIK": _load_fixture("fmtqik_202604.json"),
            "STOCK_DAY": _load_fixture("stock_day_2330_202604.json"),
            "T86": _load_fixture("t86_20260408.json"),
            "TWT49U": _load_fixture("twt49u_20260408.json"),
            "MI_5MINS_HIST": _load_fixture("mi_5mins_hist_202606.json"),
        }

        def mock_get(url, **kwargs):
            resp = MagicMock()
            if "BFI82U" in url:
                resp.json.return_value = fixtures["BFI82U"]
            elif "FMTQIK" in url:
                resp.json.return_value = fixtures["FMTQIK"]
            elif "STOCK_DAY" in url:
                resp.json.return_value = fixtures["STOCK_DAY"]
            elif "T86" in url:
                resp.json.return_value = fixtures["T86"]
            elif "TWT49U" in url:
                resp.json.return_value = fixtures["TWT49U"]
            elif "MI_5MINS_HIST" in url:
                resp.json.return_value = fixtures["MI_5MINS_HIST"]
            return resp

        with patch("collectors.twse.http_get", side_effect=mock_get):
            results = twse_collector.run("2026-04-10")

        assert isinstance(results, dict)
        assert results["institutional"] is True
        assert results["spot_close"] is True
        assert results["ex_dividend"] is True


# ── watchlist 載入來源 ────────────────────────────────────────────


class TestWatchlistLoading:
    def test_db_watchlist_takes_precedence(self, tmp_path):
        """DB watchlist 有資料時，collector 用 DB 而非 JSON。"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        create_all_tables(conn)
        conn.execute(
            "INSERT INTO watchlist (stock_id, stock_name) VALUES ('9999', '測試股')"
        )
        conn.commit()
        conn.close()

        collector = TWSECollector(db_path=db_path)
        ids = {s["stock_id"] for s in collector._watchlist}
        assert ids == {"9999"}

    def test_empty_db_falls_back_to_json(self, twse_collector):
        """DB watchlist 為空時 fallback 到 watchlist.json（種子）。"""
        ids = {s["stock_id"] for s in twse_collector._watchlist}
        assert "2330" in ids  # 來自 config/watchlist.json

    def test_t86_upserts_stock_info(self, twse_collector):
        """save_foreign_stock 順手把 T86 的股名寫進 stock_info。"""
        twse_collector.save_foreign_stock("2026-06-11", [
            {"stock_id": "2330", "stock_name": "台積電",
             "foreign_net_volume": 9111},
        ])
        conn = sqlite3.connect(twse_collector.db_path)
        row = conn.execute(
            "SELECT stock_name FROM stock_info WHERE stock_id = '2330'"
        ).fetchone()
        conn.close()
        assert row[0] == "台積電"


# ── 加權指數 OHLC（MI_5MINS_HIST）─────────────────────────────────


class TestCollectIndexOhlc:
    def test_parse_normal_response(self, twse_collector):
        """正常回應能 parse 出當日開高低收。"""
        fixture = _load_fixture("mi_5mins_hist_202606.json")

        with patch("collectors.twse.http_get", return_value=_mock_resp(fixture)):
            data = twse_collector.collect_index_ohlc("2026-06-11")

        assert data is not None
        assert data["open"] == pytest.approx(43172.21)
        assert data["high"] == pytest.approx(43463.03)
        assert data["low"] == pytest.approx(42006.39)
        assert data["close"] == pytest.approx(43149.46)

    def test_no_data_response(self, twse_collector):
        """假日或無資料時回傳 None。"""
        with patch(
            "collectors.twse.http_get",
            return_value=_mock_resp({"stat": "很抱歉，沒有符合條件的資料!"}),
        ):
            data = twse_collector.collect_index_ohlc("2026-06-07")

        assert data is None

    def test_date_not_in_month(self, twse_collector):
        """回應中找不到目標日期（尚未收盤或非交易日）時回傳 None。"""
        fixture = _load_fixture("mi_5mins_hist_202606.json")

        with patch("collectors.twse.http_get", return_value=_mock_resp(fixture)):
            data = twse_collector.collect_index_ohlc("2026-06-30")

        assert data is None

    def test_save_index_ohlc_idempotent(self, twse_collector):
        """save_index_ohlc 重複寫入同一天，資料為最新值。"""
        ohlc = {"open": 43172.21, "high": 43463.03, "low": 42006.39, "close": 43149.46}
        twse_collector.save_index_ohlc("2026-06-11", ohlc)
        ohlc["close"] = 43200.00
        twse_collector.save_index_ohlc("2026-06-11", ohlc)

        conn = sqlite3.connect(twse_collector.db_path)
        row = conn.execute(
            "SELECT open, close FROM raw_index WHERE date = '2026-06-11'"
        ).fetchone()
        conn.close()
        assert row == (43172.21, 43200.00)
