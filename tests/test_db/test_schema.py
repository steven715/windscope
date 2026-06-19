import json
import sqlite3
from pathlib import Path

from db.schema import create_all_tables, import_broker_tags, import_watchlist


class TestCreateAllTables:
    def test_creates_all_expected_tables(self, memory_db):
        """create_all_tables 應建出所有預期的表。"""
        tables = memory_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t[0] for t in tables}

        expected = {
            "raw_fx",
            "raw_futures",
            "raw_chip",
            "raw_institutional",
            "raw_index",
            "broker_tags",
            "watchlist",
            "daily_metrics",
            "daily_stock_metrics",
            "signals",
            "stock_signals",
            "verifications",
            "stock_info",
        }
        assert expected.issubset(table_names)

    def test_import_watchlist_seeds_stock_info(self, memory_db, tmp_path):
        """匯入 watchlist 時同步建立 stock_info。"""
        stocks = [{"stock_id": "2330", "stock_name": "台積電",
                   "added_date": "2026-04-08", "reason": "權值股"}]
        json_path = tmp_path / "watchlist.json"
        json_path.write_text(json.dumps(stocks, ensure_ascii=False), encoding="utf-8")

        from db.schema import import_watchlist
        import_watchlist(memory_db, str(json_path))

        row = memory_db.execute(
            "SELECT stock_name FROM stock_info WHERE stock_id = '2330'"
        ).fetchone()
        assert row[0] == "台積電"

    def test_upsert_stock_info_skips_empty_name(self, memory_db):
        """空名稱不應覆蓋既有的股名。"""
        from db.schema import upsert_stock_info
        upsert_stock_info(memory_db, "2330", "台積電")
        upsert_stock_info(memory_db, "2330", None)
        row = memory_db.execute(
            "SELECT stock_name FROM stock_info WHERE stock_id = '2330'"
        ).fetchone()
        assert row[0] == "台積電"

    def test_idempotent_insert_raw_index(self, memory_db):
        """raw_index 同一天重複寫入不報錯，且資料為最新值。"""
        memory_db.execute(
            "INSERT OR REPLACE INTO raw_index (date, open, close) "
            "VALUES ('2026-06-11', 43172.21, 43149.46)"
        )
        memory_db.execute(
            "INSERT OR REPLACE INTO raw_index (date, open, close) "
            "VALUES ('2026-06-11', 43172.21, 43200.00)"
        )
        row = memory_db.execute(
            "SELECT close FROM raw_index WHERE date = '2026-06-11'"
        ).fetchone()
        assert row[0] == 43200.00

    def test_idempotent_insert_signals(self, memory_db):
        """signals 同一天重複寫入不報錯，且資料為最新值。"""
        memory_db.execute(
            "INSERT OR REPLACE INTO signals (date, direction, confidence) "
            "VALUES ('2026-06-11', 'bullish', 3)"
        )
        memory_db.execute(
            "INSERT OR REPLACE INTO signals (date, direction, confidence) "
            "VALUES ('2026-06-11', 'neutral', 2)"
        )
        row = memory_db.execute(
            "SELECT direction, confidence FROM signals WHERE date = '2026-06-11'"
        ).fetchone()
        assert row == ("neutral", 2)

    def test_idempotent(self):
        """重複執行 create_all_tables 不應報錯。"""
        conn = sqlite3.connect(":memory:")
        create_all_tables(conn)
        create_all_tables(conn)  # 第二次不應 raise
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert len(tables) >= 8
        conn.close()

    def test_migration_adds_quote_pm_to_existing_raw_fx(self):
        """既有(舊 schema)raw_fx 無 quote_pm → create_all_tables 應 ALTER 補上。"""
        conn = sqlite3.connect(":memory:")
        # 模擬舊版 raw_fx（無 quote_pm 欄）
        conn.execute(
            "CREATE TABLE raw_fx (date TEXT NOT NULL, currency_pair TEXT NOT NULL, "
            "close_16 REAL, quote_0845 REAL, ny_close REAL, collected_at TEXT, "
            "PRIMARY KEY (date, currency_pair))"
        )
        cols_before = {r[1] for r in conn.execute("PRAGMA table_info(raw_fx)")}
        assert "quote_pm" not in cols_before

        create_all_tables(conn)  # 應觸發 _migrate_columns

        cols_after = {r[1] for r in conn.execute("PRAGMA table_info(raw_fx)")}
        assert "quote_pm" in cols_after
        # 再跑一次不應重複 ALTER 或報錯
        create_all_tables(conn)
        conn.close()


class TestImportBrokerTags:
    def test_import_broker_tags(self, memory_db, tmp_path):
        """import_broker_tags 能正確匯入 JSON 資料。"""
        tags = [
            {"broker_name": "兆豐-嘉義", "broker_type": "swing", "notes": "長線"},
            {"broker_name": "凱基-台北", "broker_type": "day_trade", "notes": "隔日沖"},
        ]
        json_path = tmp_path / "broker_tags.json"
        json_path.write_text(json.dumps(tags, ensure_ascii=False), encoding="utf-8")

        count = import_broker_tags(memory_db, str(json_path))
        assert count == 2

        rows = memory_db.execute("SELECT * FROM broker_tags").fetchall()
        assert len(rows) == 2

    def test_import_broker_tags_idempotent(self, memory_db, tmp_path):
        """重複匯入不應報錯，且資料為最新值。"""
        tags = [{"broker_name": "測試券商", "broker_type": "swing", "notes": "v1"}]
        json_path = tmp_path / "broker_tags.json"
        json_path.write_text(json.dumps(tags, ensure_ascii=False), encoding="utf-8")

        import_broker_tags(memory_db, str(json_path))

        tags[0]["notes"] = "v2"
        json_path.write_text(json.dumps(tags, ensure_ascii=False), encoding="utf-8")
        import_broker_tags(memory_db, str(json_path))

        row = memory_db.execute(
            "SELECT notes FROM broker_tags WHERE broker_name = '測試券商'"
        ).fetchone()
        assert row[0] == "v2"


class TestImportWatchlist:
    def test_import_watchlist(self, memory_db, tmp_path):
        """import_watchlist 能正確匯入 JSON 資料。"""
        stocks = [
            {
                "stock_id": "2330",
                "stock_name": "台積電",
                "added_date": "2026-04-08",
                "reason": "權值股",
            }
        ]
        json_path = tmp_path / "watchlist.json"
        json_path.write_text(json.dumps(stocks, ensure_ascii=False), encoding="utf-8")

        count = import_watchlist(memory_db, str(json_path))
        assert count == 1

        rows = memory_db.execute("SELECT * FROM watchlist").fetchall()
        assert len(rows) == 1

    def test_import_watchlist_idempotent(self, memory_db, tmp_path):
        """重複匯入是冪等的。"""
        stocks = [
            {
                "stock_id": "2330",
                "stock_name": "台積電",
                "added_date": "2026-04-08",
                "reason": "v1",
            }
        ]
        json_path = tmp_path / "watchlist.json"
        json_path.write_text(json.dumps(stocks, ensure_ascii=False), encoding="utf-8")

        import_watchlist(memory_db, str(json_path))

        stocks[0]["reason"] = "v2"
        json_path.write_text(json.dumps(stocks, ensure_ascii=False), encoding="utf-8")
        import_watchlist(memory_db, str(json_path))

        row = memory_db.execute(
            "SELECT reason FROM watchlist WHERE stock_id = '2330'"
        ).fetchone()
        assert row[0] == "v2"
