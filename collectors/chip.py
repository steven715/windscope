import csv
import json
import logging
from datetime import datetime
from pathlib import Path

from collectors.base import BaseCollector
from db.connection import get_connection
from utils.http_client import http_get

logger = logging.getLogger(__name__)

TWSE_TWT43U_URL = "https://www.twse.com.tw/rwd/zh/fund/TWT43U"
WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "config" / "watchlist.json"


def _parse_amount(s: str) -> int:
    """去除逗號並轉為 int。"""
    return int(s.strip().replace(",", ""))


def _load_watchlist() -> list[dict]:
    """讀取 watchlist.json。"""
    with open(WATCHLIST_PATH, encoding="utf-8") as f:
        return json.load(f)


class ChipCollector(BaseCollector):
    """分點籌碼 collector：券商買賣日報 + CSV 手動匯入。"""

    def __init__(self, db_path: str | None = None):
        super().__init__(db_path)
        self._watchlist = _load_watchlist()

    # ── collect 方法 ──────────────────────────────────────────────

    def collect(self, date: str) -> dict | None:
        """嘗試自動收集分點資料（目前為 stub）。"""
        return self.collect_broker_trading(date)

    def collect_broker_trading(self, date: str) -> dict | None:
        """嘗試取得個股券商進出明細。目前為 STUB。"""
        # TODO: 待驗證
        # 來源：證交所券商買賣日報 https://www.twse.com.tw/rwd/zh/fund/TWT43U
        # 已知問題：此 URL 可能回傳大盤資料而非個股券商明細
        # 狀態：STUB
        logger.warning("collect_broker_trading is a stub, returning None")
        return None

    def import_from_csv(self, csv_path: str) -> int:
        """從 CSV 匯入分點籌碼資料。回傳匯入筆數。"""
        path = Path(csv_path)
        if not path.exists():
            logger.error("CSV file not found: %s", csv_path)
            return 0

        now = datetime.now().isoformat()
        count = 0

        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)

            with get_connection(self.db_path) as conn:
                for row in reader:
                    try:
                        conn.execute(
                            """INSERT INTO raw_chip
                               (date, stock_id, stock_name, broker_name,
                                buy_volume, sell_volume, net_volume, collected_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(date, stock_id, broker_name) DO UPDATE SET
                                stock_name = excluded.stock_name,
                                buy_volume = excluded.buy_volume,
                                sell_volume = excluded.sell_volume,
                                net_volume = excluded.net_volume,
                                collected_at = excluded.collected_at""",
                            (
                                row["date"],
                                row["stock_id"],
                                row["stock_name"],
                                row["broker_name"],
                                int(row["buy_volume"]),
                                int(row["sell_volume"]),
                                int(row["net_volume"]),
                                now,
                            ),
                        )
                        count += 1
                    except (KeyError, ValueError) as e:
                        logger.warning("Skipping malformed CSV row: %s — %s", row, e)

        logger.info("Imported %d rows from %s", count, csv_path)
        return count

    # ── save 方法 ────────────────────────────────────────────────

    def save(self, date: str, data: dict) -> None:
        """存入分點資料（預設 save 介面）。預留給自動來源。"""
        pass

    def save_broker_trading(self, date: str, stock_id: str, stock_name: str,
                            data_list: list[dict]) -> None:
        """存入 raw_chip。PK 是 date+stock_id+broker_name。"""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            for item in data_list:
                conn.execute(
                    """INSERT INTO raw_chip
                       (date, stock_id, stock_name, broker_name,
                        buy_volume, sell_volume, net_volume, collected_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(date, stock_id, broker_name) DO UPDATE SET
                        stock_name = excluded.stock_name,
                        buy_volume = excluded.buy_volume,
                        sell_volume = excluded.sell_volume,
                        net_volume = excluded.net_volume,
                        collected_at = excluded.collected_at""",
                    (
                        date, stock_id, stock_name,
                        item["broker_name"],
                        item["buy_volume"],
                        item["sell_volume"],
                        item["net_volume"],
                        now,
                    ),
                )

    # ── run ──────────────────────────────────────────────────────

    def run(self, date: str) -> dict:
        """執行分點籌碼收集（目前自動來源為 stub）。"""
        logger.info("ChipCollector: starting all tasks for %s", date)
        results = {}

        results["broker_trading"] = self._try_collect_and_save(
            lambda: self.collect_broker_trading(date),
            lambda data: None,
        )

        logger.info("ChipCollector results for %s: %s", date, results)
        return results
