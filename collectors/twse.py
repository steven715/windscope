import json
import logging
from datetime import datetime
from pathlib import Path

from collectors.base import BaseCollector
from db.connection import get_connection
from utils.http_client import http_get

logger = logging.getLogger(__name__)

TWSE_BFI82U_URL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"
TWSE_FMTQIK_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK"
TWSE_STOCK_DAY_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
TWSE_T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TWSE_TWT49U_URL = "https://www.twse.com.tw/rwd/zh/exRight/TWT49U"

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "config" / "watchlist.json"


def _parse_amount(s: str) -> float:
    """去除逗號並轉為 float。處理負數（可能帶括號或負號）。"""
    s = s.strip().replace(",", "")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    return float(s)


def _to_roc_date(date: str) -> str:
    """西元日期 YYYY-MM-DD 轉民國年 YYY/MM/DD。"""
    year, month, day = date.split("-")
    roc_year = int(year) - 1911
    return f"{roc_year}/{month}/{day}"


def _load_watchlist() -> list[dict]:
    """讀取 watchlist.json。"""
    with open(WATCHLIST_PATH, encoding="utf-8") as f:
        return json.load(f)


class TWSECollector(BaseCollector):
    """證交所資料 collector：三大法人、加權指數、個股收盤、外資個股、除息點數。"""

    def __init__(self, db_path: str | None = None):
        super().__init__(db_path)
        self._watchlist = _load_watchlist()

    # ── collect 方法 ──────────────────────────────────────────────

    def collect(self, date: str) -> dict | None:
        """收集指定日期的三大法人買賣超（保留原有介面）。"""
        return self.collect_institutional(date)

    def collect_institutional(self, date: str) -> dict | None:
        """取得三大法人買賣超。date 格式：YYYY-MM-DD。"""
        date_param = date.replace("-", "")
        resp = http_get(TWSE_BFI82U_URL, params={"date": date_param, "response": "json"})
        data = resp.json()

        if data.get("stat") != "OK":
            logger.info("BFI82U: no data for %s (stat=%s)", date, data.get("stat"))
            return None

        resp_date = data.get("date", "")
        if resp_date != date_param:
            logger.info(
                "BFI82U: date mismatch for %s (got %s), likely non-trading day",
                date, resp_date,
            )
            return None

        rows = data.get("data", [])
        if not rows:
            logger.warning("BFI82U: stat=OK but data is empty for %s", date)
            return None

        result = {}
        dealer_self_buy = dealer_self_sell = dealer_self_net = 0.0
        dealer_hedge_buy = dealer_hedge_sell = dealer_hedge_net = 0.0

        for row in rows:
            name = row[0].strip()
            buy = _parse_amount(row[1])
            sell = _parse_amount(row[2])
            net = _parse_amount(row[3])

            if "外資及陸資" in name and "不含外資自營商" in name:
                result["foreign_buy"] = buy
                result["foreign_sell"] = sell
                result["foreign_net"] = net
            elif name == "投信":
                result["trust_buy"] = buy
                result["trust_sell"] = sell
                result["trust_net"] = net
            elif "自營商" in name and "自行買賣" in name:
                dealer_self_buy = buy
                dealer_self_sell = sell
                dealer_self_net = net
            elif "自營商" in name and "避險" in name:
                dealer_hedge_buy = buy
                dealer_hedge_sell = sell
                dealer_hedge_net = net
            elif name == "合計":
                result["total_net"] = net

        result["dealer_buy"] = dealer_self_buy + dealer_hedge_buy
        result["dealer_sell"] = dealer_self_sell + dealer_hedge_sell
        result["dealer_net"] = dealer_self_net + dealer_hedge_net

        logger.info(
            "BFI82U parsed: foreign_net=%.0f, trust_net=%.0f, dealer_net=%.0f",
            result.get("foreign_net", 0),
            result.get("trust_net", 0),
            result.get("dealer_net", 0),
        )
        return result

    def collect_spot_close(self, date: str) -> dict | None:
        """取得加權指數收盤價。回傳 {"spot_close": float} 或 None。"""
        date_param = date.replace("-", "")
        resp = http_get(TWSE_FMTQIK_URL, params={"date": date_param, "response": "json"})
        data = resp.json()

        if data.get("stat") != "OK":
            logger.info("FMTQIK: no data for %s (stat=%s)", date, data.get("stat"))
            return None

        target_roc = _to_roc_date(date)
        for row in data.get("data", []):
            if row[0].strip() == target_roc:
                close_str = row[4].strip().replace(",", "")
                spot_close = float(close_str)
                logger.info("FMTQIK parsed: spot_close=%.2f for %s", spot_close, date)
                return {"spot_close": spot_close}

        logger.info("FMTQIK: target date %s not found in response", date)
        return None

    def collect_stock_close(self, date: str, stock_id: str) -> dict | None:
        """取得單一個股收盤價。回傳 {"stock_id": str, "close_price": float} 或 None。"""
        date_param = date.replace("-", "")
        resp = http_get(
            TWSE_STOCK_DAY_URL,
            params={"date": date_param, "stockNo": stock_id, "response": "json"},
        )
        data = resp.json()

        if data.get("stat") != "OK":
            logger.info("STOCK_DAY: no data for %s/%s (stat=%s)", stock_id, date, data.get("stat"))
            return None

        target_roc = _to_roc_date(date)
        for row in data.get("data", []):
            if row[0].strip() == target_roc:
                close_str = row[6].strip().replace(",", "")
                close_price = float(close_str)
                logger.info("STOCK_DAY parsed: %s close=%.2f for %s", stock_id, close_price, date)
                return {"stock_id": stock_id, "close_price": close_price}

        logger.info("STOCK_DAY: target date %s not found for %s", date, stock_id)
        return None

    def collect_all_stock_close(self, date: str) -> list[dict]:
        """針對 watchlist 中所有個股取得收盤價。回傳成功的列表。"""
        results = []
        for stock in self._watchlist:
            stock_id = stock["stock_id"]
            try:
                data = self.collect_stock_close(date, stock_id)
                if data:
                    results.append(data)
                else:
                    logger.warning("No close price for %s on %s", stock_id, date)
            except Exception as e:
                logger.error("Failed to get close for %s: %s", stock_id, e)
        return results

    def collect_foreign_stock(self, date: str) -> list[dict] | None:
        """取得外資個股買賣超，篩選 watchlist 中的個股。"""
        date_param = date.replace("-", "")
        resp = http_get(TWSE_T86_URL, params={"date": date_param, "selectType": "ALL", "response": "json"})
        data = resp.json()

        if data.get("stat") != "OK":
            logger.info("T86: no data for %s (stat=%s)", date, data.get("stat"))
            return None

        rows = data.get("data", [])
        if not rows:
            logger.warning("T86: stat=OK but data is empty for %s", date)
            return None

        watchlist_ids = {s["stock_id"] for s in self._watchlist}
        results = []

        for row in rows:
            sid = row[0].strip()
            if sid in watchlist_ids:
                net_shares = _parse_amount(row[4])
                # 股 → 張（除以 1000）
                foreign_net_volume = int(net_shares / 1000)
                results.append({
                    "stock_id": sid,
                    "foreign_net_volume": foreign_net_volume,
                })

        logger.info("T86 parsed: %d watchlist stocks found for %s", len(results), date)
        return results if results else None

    def collect_ex_dividend_points(self, date: str) -> dict | None:
        """取得當日除息對加權指數的預估影響點數。"""
        date_param = date.replace("-", "")
        try:
            resp = http_get(TWSE_TWT49U_URL, params={"date": date_param, "response": "json"})
            data = resp.json()
        except Exception as e:
            logger.warning("TWT49U: request failed for %s: %s, using stub", date, e)
            return {"ex_dividend_points": 0.0}

        if data.get("stat") != "OK":
            # 非除息日，回傳 0 是正常的
            logger.info("TWT49U: no data for %s (likely no ex-dividend), returning 0", date)
            return {"ex_dividend_points": 0.0}

        # 嘗試從 notes 中解析預估點數
        notes = data.get("notes", [])
        for note in notes:
            if "影響加權指數" in note and "點" in note:
                import re
                match = re.search(r"約\s*([\d.]+)\s*點", note)
                if match:
                    points = float(match.group(1))
                    logger.info("TWT49U parsed: ex_dividend_points=%.2f for %s", points, date)
                    return {"ex_dividend_points": points}

        # 有資料但解析不到點數，回傳 0
        logger.info("TWT49U: data exists but couldn't parse points for %s", date)
        return {"ex_dividend_points": 0.0}

    # ── save 方法（全部使用 ON CONFLICT DO UPDATE）──────────────

    def save(self, date: str, data: dict) -> None:
        """存入 raw_institutional（保留原有介面）。"""
        self.save_institutional(date, data)

    def save_institutional(self, date: str, data: dict) -> None:
        """存入 raw_institutional，用 ON CONFLICT DO UPDATE。"""
        with get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO raw_institutional
                   (date, foreign_buy, foreign_sell, foreign_net,
                    trust_buy, trust_sell, trust_net,
                    dealer_buy, dealer_sell, dealer_net,
                    total_net, collected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                    foreign_buy = excluded.foreign_buy,
                    foreign_sell = excluded.foreign_sell,
                    foreign_net = excluded.foreign_net,
                    trust_buy = excluded.trust_buy,
                    trust_sell = excluded.trust_sell,
                    trust_net = excluded.trust_net,
                    dealer_buy = excluded.dealer_buy,
                    dealer_sell = excluded.dealer_sell,
                    dealer_net = excluded.dealer_net,
                    total_net = excluded.total_net,
                    collected_at = excluded.collected_at""",
                (
                    date,
                    data["foreign_buy"], data["foreign_sell"], data["foreign_net"],
                    data["trust_buy"], data["trust_sell"], data["trust_net"],
                    data["dealer_buy"], data["dealer_sell"], data["dealer_net"],
                    data["total_net"],
                    datetime.now().isoformat(),
                ),
            )

    def save_spot_close(self, date: str, data: dict) -> None:
        """更新 raw_futures.spot_close，不覆蓋其他欄位。"""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO raw_futures (date, spot_close, collected_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                    spot_close = excluded.spot_close,
                    collected_at = excluded.collected_at""",
                (date, data["spot_close"], now),
            )

    def save_stock_close(self, date: str, data_list: list[dict]) -> None:
        """把個股收盤價更新到 raw_chip 表（更新 close_price 欄位）。"""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            for item in data_list:
                # 先看這支股票在 raw_chip 有沒有任何該日期的紀錄
                existing = conn.execute(
                    "SELECT broker_name FROM raw_chip WHERE date = ? AND stock_id = ?",
                    (date, item["stock_id"]),
                ).fetchall()

                if existing:
                    # 有分點資料，更新 close_price
                    conn.execute(
                        """UPDATE raw_chip SET close_price = ?, collected_at = ?
                           WHERE date = ? AND stock_id = ?""",
                        (item["close_price"], now, date, item["stock_id"]),
                    )
                else:
                    # 無分點資料，插一筆佔位（broker_name 用 placeholder）
                    conn.execute(
                        """INSERT INTO raw_chip
                           (date, stock_id, stock_name, broker_name, close_price, collected_at)
                           VALUES (?, ?, ?, '__PRICE_ONLY__', ?, ?)
                           ON CONFLICT(date, stock_id, broker_name) DO UPDATE SET
                            close_price = excluded.close_price,
                            collected_at = excluded.collected_at""",
                        (date, item["stock_id"], "", item["close_price"], now),
                    )

    def save_foreign_stock(self, date: str, data_list: list[dict]) -> None:
        """外資個股買賣超存入 raw_institutional_stock（使用 raw_chip 暫存）。"""
        # 外資個股買賣超欄位不同於分點資料，存到 raw_chip 的特殊 broker_name
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            for item in data_list:
                conn.execute(
                    """INSERT INTO raw_chip
                       (date, stock_id, broker_name, net_volume, collected_at)
                       VALUES (?, ?, '__FOREIGN__', ?, ?)
                       ON CONFLICT(date, stock_id, broker_name) DO UPDATE SET
                        net_volume = excluded.net_volume,
                        collected_at = excluded.collected_at""",
                    (date, item["stock_id"], item["foreign_net_volume"], now),
                )

    def save_ex_dividend(self, date: str, data: dict) -> None:
        """更新 raw_futures.ex_dividend_points。"""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO raw_futures (date, ex_dividend_points, collected_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                    ex_dividend_points = excluded.ex_dividend_points,
                    collected_at = excluded.collected_at""",
                (date, data["ex_dividend_points"], now),
            )

    # ── run：執行所有子任務 ─────────────────────────────────────

    def run(self, date: str) -> dict:
        """執行所有 TWSE 資料收集，回傳各子任務成功與否。"""
        logger.info("TWSECollector: starting all tasks for %s", date)
        results = {}

        results["institutional"] = self._try_collect_and_save(
            lambda: self.collect_institutional(date),
            lambda data: self.save_institutional(date, data),
        )
        results["spot_close"] = self._try_collect_and_save(
            lambda: self.collect_spot_close(date),
            lambda data: self.save_spot_close(date, data),
        )

        # 個股收盤
        try:
            stock_data = self.collect_all_stock_close(date)
            if stock_data:
                self.save_stock_close(date, stock_data)
                results["stock_close"] = True
            else:
                results["stock_close"] = False
        except Exception as e:
            logger.error("TWSECollector stock_close failed: %s", e)
            results["stock_close"] = False

        results["foreign_stock"] = self._try_collect_and_save(
            lambda: self.collect_foreign_stock(date),
            lambda data: self.save_foreign_stock(date, data),
        )
        results["ex_dividend"] = self._try_collect_and_save(
            lambda: self.collect_ex_dividend_points(date),
            lambda data: self.save_ex_dividend(date, data),
        )

        logger.info("TWSECollector results for %s: %s", date, results)
        return results
