import sqlite3
from pathlib import Path

import pytest

from collectors.chip import ChipCollector
from db.schema import create_all_tables

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "chip"


@pytest.fixture
def chip_collector(tmp_path):
    """建立 ChipCollector，使用 tmp_path 的 DB。"""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.close()
    return ChipCollector(db_path=db_path)


# ── Stub 行為 ───────────────────────────────────────────────────


class TestStubBehavior:
    def test_collect_broker_trading_stub(self, chip_collector):
        """Stub 回傳 None + log warning。"""
        data = chip_collector.collect_broker_trading("2026-04-08")
        assert data is None

    def test_run_returns_dict(self, chip_collector):
        """run() 回傳 dict。"""
        results = chip_collector.run("2026-04-08")
        assert isinstance(results, dict)
        assert results["broker_trading"] is False  # stub


# ── CSV Import ──────────────────────────────────────────────────


class TestCSVImport:
    def test_import_normal(self, chip_collector):
        """正常匯入 CSV。"""
        csv_path = str(FIXTURE_DIR / "chip_sample.csv")
        count = chip_collector.import_from_csv(csv_path)

        assert count == 5  # fixture 有 5 行資料

        conn = sqlite3.connect(chip_collector.db_path)
        rows = conn.execute("SELECT * FROM raw_chip").fetchall()
        conn.close()

        assert len(rows) == 5

    def test_import_specific_values(self, chip_collector):
        """驗證匯入的具體數值。"""
        csv_path = str(FIXTURE_DIR / "chip_sample.csv")
        chip_collector.import_from_csv(csv_path)

        conn = sqlite3.connect(chip_collector.db_path)
        row = conn.execute(
            "SELECT buy_volume, sell_volume, net_volume FROM raw_chip "
            "WHERE date = '2026-04-08' AND stock_id = '2330' AND broker_name = '兆豐-嘉義'"
        ).fetchone()
        conn.close()

        assert row[0] == 500
        assert row[1] == 200
        assert row[2] == 300

    def test_import_idempotent(self, chip_collector):
        """同一份 CSV 匯入兩次不報錯，資料為最新值。"""
        csv_path = str(FIXTURE_DIR / "chip_sample.csv")
        count1 = chip_collector.import_from_csv(csv_path)
        count2 = chip_collector.import_from_csv(csv_path)

        assert count1 == 5
        assert count2 == 5

        conn = sqlite3.connect(chip_collector.db_path)
        rows = conn.execute("SELECT * FROM raw_chip").fetchall()
        conn.close()

        assert len(rows) == 5  # 不會變成 10 筆

    def test_import_empty_file(self, chip_collector, tmp_path):
        """空 CSV（只有 header）回傳 0。"""
        empty_csv = tmp_path / "empty.csv"
        empty_csv.write_text(
            "date,stock_id,stock_name,broker_name,buy_volume,sell_volume,net_volume\n",
            encoding="utf-8",
        )
        count = chip_collector.import_from_csv(str(empty_csv))
        assert count == 0

    def test_import_nonexistent_file(self, chip_collector):
        """不存在的檔案回傳 0。"""
        count = chip_collector.import_from_csv("/nonexistent/file.csv")
        assert count == 0

    def test_import_malformed_row(self, chip_collector, tmp_path):
        """格式錯誤的行被跳過，不影響其他行。"""
        csv_path = tmp_path / "bad.csv"
        csv_path.write_text(
            "date,stock_id,stock_name,broker_name,buy_volume,sell_volume,net_volume\n"
            "2026-04-08,2330,台積電,兆豐-嘉義,500,200,300\n"
            "2026-04-08,2330,台積電,凱基-台北,NOT_A_NUMBER,800,200\n"
            "2026-04-08,2409,友達,兆豐-嘉義,2000,500,1500\n",
            encoding="utf-8",
        )
        count = chip_collector.import_from_csv(str(csv_path))
        assert count == 2  # 第二行被跳過


# ── Save Broker Trading ────────────────────────────────────────


class TestSaveBrokerTrading:
    def test_save_broker_trading(self, chip_collector):
        """save_broker_trading 正確寫入 raw_chip。"""
        data_list = [
            {"broker_name": "兆豐-嘉義", "buy_volume": 500, "sell_volume": 200, "net_volume": 300},
            {"broker_name": "凱基-台北", "buy_volume": 1000, "sell_volume": 800, "net_volume": 200},
        ]
        chip_collector.save_broker_trading("2026-04-08", "2330", "台積電", data_list)

        conn = sqlite3.connect(chip_collector.db_path)
        rows = conn.execute(
            "SELECT broker_name, net_volume FROM raw_chip "
            "WHERE date = '2026-04-08' AND stock_id = '2330' "
            "ORDER BY broker_name"
        ).fetchall()
        conn.close()

        assert len(rows) == 2
        row_dict = {r[0]: r[1] for r in rows}
        assert row_dict["兆豐-嘉義"] == 300
        assert row_dict["凱基-台北"] == 200
