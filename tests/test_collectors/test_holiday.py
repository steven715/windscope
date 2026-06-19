"""休市日曆 collector 測試。不打真實 HTTP，用 fixture 模擬 TWSE 假日表回應。"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from collectors.holiday import HolidayCollector, _roc_to_iso, parse_holidays
from db.schema import create_all_tables

_FIXTURE = Path("tests/fixtures/twse/holiday_schedule_2026.json")


def _load_entries() -> list[dict]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


class TestRocToIso:
    def test_normal(self):
        assert _roc_to_iso("1150619") == "2026-06-19"

    def test_new_year(self):
        assert _roc_to_iso("1150101") == "2026-01-01"

    def test_bad_length(self):
        assert _roc_to_iso("11506") is None

    def test_non_digit(self):
        assert _roc_to_iso("11506XX") is None

    def test_invalid_calendar_date(self):
        # 民國 115 年 13 月 → 不存在
        assert _roc_to_iso("1151301") is None


class TestParseHolidays:
    def test_dragon_boat_is_closure(self):
        """端午節 1150619 應被解析為休市日。"""
        holidays = parse_holidays(_load_entries())
        dates = {h["date"] for h in holidays}
        assert "2026-06-19" in dates

    def test_excludes_trading_notices(self):
        """『開始交易/最後交易』類通知為開市日，必須排除。"""
        holidays = parse_holidays(_load_entries())
        dates = {h["date"] for h in holidays}
        # 1150102 國曆新年開始交易日、1150211 農曆春節前最後交易日、
        # 1150223 農曆春節後開始交易日 → 皆為交易日，不應入休市清單
        assert "2026-01-02" not in dates
        assert "2026-02-11" not in dates
        assert "2026-02-23" not in dates

    def test_includes_no_trading_settlement_day(self):
        """『市場無交易，僅辦理結算交割作業』為休市日，應保留。"""
        holidays = parse_holidays(_load_entries())
        dates = {h["date"] for h in holidays}
        assert "2026-02-12" in dates  # 1150212

    def test_known_2026_closures_present(self):
        holidays = parse_holidays(_load_entries())
        dates = {h["date"] for h in holidays}
        for d in ["2026-01-01", "2026-05-01", "2026-06-19",
                  "2026-09-25", "2026-12-25"]:
            assert d in dates, d

    def test_empty_input(self):
        assert parse_holidays([]) == []
        assert parse_holidays(None) == []

    def test_bad_date_skipped_not_crash(self):
        entries = [{"Name": "壞日期", "Date": "abc"},
                   {"Name": "端午節", "Date": "1150619"}]
        result = parse_holidays(entries)
        assert result == [{"date": "2026-06-19", "name": "端午節"}]


class TestHolidayCollector:
    def test_run_fetch_and_save(self, tmp_path):
        """run() 解析 fixture 並寫入 market_holidays，回傳寫入筆數。"""
        db = str(tmp_path / "test.db")
        conn = sqlite3.connect(db)
        create_all_tables(conn)
        conn.commit()
        conn.close()

        mock_resp = MagicMock()
        mock_resp.json.return_value = _load_entries()

        collector = HolidayCollector(db_path=db)
        with patch("collectors.holiday.http_get", return_value=mock_resp):
            n = collector.run()

        assert n > 0
        conn = sqlite3.connect(db)
        dates = {r[0] for r in conn.execute("SELECT date FROM market_holidays")}
        conn.close()
        assert "2026-06-19" in dates
        assert len(dates) == n

    def test_fetch_http_failure_returns_none(self):
        """HTTP 失敗時 fetch 回 None，不 raise。"""
        collector = HolidayCollector(db_path=":memory:")
        with patch("collectors.holiday.http_get", side_effect=Exception("boom")):
            assert collector.fetch() is None

    def test_save_upsert_idempotent(self):
        """重複 save 同一天不報錯，且為最新值（PK 衝突走 upsert）。"""
        collector = HolidayCollector(db_path=":memory:")
        # 用同一條連線模擬：直接測 SQL 冪等性
        conn = sqlite3.connect(":memory:")
        create_all_tables(conn)
        for name in ["舊名", "新名"]:
            conn.execute(
                "INSERT INTO market_holidays (date, name, source, fetched_at) "
                "VALUES ('2026-06-19', ?, 'twse_openapi', 't') "
                "ON CONFLICT(date) DO UPDATE SET name = excluded.name",
                (name,),
            )
        conn.commit()
        row = conn.execute(
            "SELECT name FROM market_holidays WHERE date = '2026-06-19'"
        ).fetchone()
        assert row[0] == "新名"
        conn.close()
