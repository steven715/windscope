import csv
import json
import logging
from datetime import datetime
from pathlib import Path

from collectors.base import BaseCollector
from config import settings
from db.connection import get_connection
from utils.http_client import http_get

logger = logging.getLogger(__name__)

# 分點明細來源：FinMind TaiwanStockTradingDailyReport（需 Sponsor token）。
# 官方 bsr.twse.com.tw 有 CAPTCHA 無法自動化；TWSE TWT43U 實測為自營商彙總表
# 而非分點明細（2026-06-12 驗證），故不使用。
FINMIND_BROKER_DATASET = "TaiwanStockTradingDailyReport"
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
        """自動收集 watchlist 個股的分點資料（預設 collect 介面）。"""
        return self.collect_broker_trading(date)

    def collect_broker_trading(self, date: str) -> dict | None:
        """透過 FinMind 取得 watchlist 個股的券商分點進出。

        回傳 {stock_id: {"stock_name": str, "brokers": [{broker_name, buy_volume,
        sell_volume, net_volume}, ...]}}；未設定 FINMIND_TOKEN 或全部失敗時回傳 None。
        """
        if not settings.FINMIND_TOKEN:
            logger.warning(
                "collect_broker_trading: FINMIND_TOKEN not set, "
                "auto chip collection disabled (use `import-chip` CSV instead)"
            )
            return None

        results: dict = {}
        for stock in self._watchlist:
            stock_id = stock["stock_id"]
            brokers = self._fetch_finmind_broker(stock_id, date)
            if not brokers:
                continue
            results[stock_id] = {
                "stock_name": stock.get("stock_name", ""),
                "brokers": brokers,
            }

        if not results:
            logger.info("collect_broker_trading: no broker data for %s", date)
            return None
        return results

    def _fetch_finmind_broker(self, stock_id: str, date: str) -> list[dict] | None:
        """呼叫 FinMind API 取得單一個股的分點明細並彙總。失敗回傳 None。"""
        try:
            resp = http_get(
                settings.FINMIND_API_URL,
                params={
                    "dataset": FINMIND_BROKER_DATASET,
                    "data_id": stock_id,
                    "start_date": date,
                    "end_date": date,
                    "token": settings.FINMIND_TOKEN,
                },
            )
            payload = resp.json()
        except Exception as e:
            logger.error("FinMind request failed for %s %s: %s", stock_id, date, e)
            return None

        if payload.get("status") != 200:
            logger.error(
                "FinMind error for %s %s: %s",
                stock_id, date, str(payload.get("msg", ""))[:200],
            )
            return None

        return self.aggregate_broker_rows(payload.get("data", []))

    @staticmethod
    def aggregate_broker_rows(rows: list[dict]) -> list[dict]:
        """把 FinMind 的逐價位明細彙總成每券商一筆 buy/sell/net（股數）。"""
        agg: dict[str, dict] = {}
        for row in rows:
            try:
                name = row["securities_trader"]
                buy = int(row["buy"])
                sell = int(row["sell"])
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("Skipping malformed FinMind row: %s — %s", row, e)
                continue
            entry = agg.setdefault(
                name, {"broker_name": name, "buy_volume": 0, "sell_volume": 0}
            )
            entry["buy_volume"] += buy
            entry["sell_volume"] += sell

        result = []
        for entry in agg.values():
            entry["net_volume"] = entry["buy_volume"] - entry["sell_volume"]
            result.append(entry)
        return result

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
        """存入 collect_broker_trading 回傳的多檔個股分點資料。"""
        for stock_id, item in data.items():
            self.save_broker_trading(date, stock_id, item["stock_name"], item["brokers"])

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
        """執行分點籌碼收集（FinMind 自動來源，無 token 時為 no-op）。"""
        logger.info("ChipCollector: starting all tasks for %s", date)
        results = {}

        results["broker_trading"] = self._try_collect_and_save(
            lambda: self.collect_broker_trading(date),
            lambda data: self.save(date, data),
        )

        logger.info("ChipCollector results for %s: %s", date, results)
        return results
