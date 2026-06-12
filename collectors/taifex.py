import csv
import io
import logging
from datetime import datetime

from collectors.base import BaseCollector
from db.connection import get_connection
from utils.http_client import http_post

logger = logging.getLogger(__name__)

# 期貨每日交易行情下載（含一般/盤後時段），實測驗證 2026-06-12
TAIFEX_FUT_CSV_URL = "https://www.taifex.com.tw/cht/3/futDataDown"
# 三大法人—區分各期貨契約—依日期 下載，實測驗證 2026-06-12
TAIFEX_OI_CSV_URL = "https://www.taifex.com.tw/cht/3/futContractsDateDown"


def _parse_int(s: str) -> int:
    """去除逗號並轉為 int。"""
    return int(s.strip().replace(",", ""))


def _parse_float(s: str) -> float:
    """去除逗號、正負號前綴並轉為 float。"""
    s = s.strip().replace(",", "")
    if s.startswith("+"):
        s = s[1:]
    return float(s)


class TAIFEXCollector(BaseCollector):
    """期交所資料 collector：夜盤收盤、外資期貨未平倉。"""

    # ── collect 方法 ──────────────────────────────────────────────

    def collect(self, date: str) -> dict | None:
        """收集夜盤收盤資料（預設 collect 介面）。"""
        return self.collect_night_session(date)

    def collect_night_session(self, date: str) -> dict | None:
        """取得台指期夜盤收盤價和成交量。回傳 {"night_close": float, "night_volume": int} 或 None。"""
        query_date = date.replace("-", "/")  # YYYY/MM/DD
        try:
            resp = http_post(
                TAIFEX_FUT_CSV_URL,
                data={
                    "down_type": "1",
                    "commodity_id": "TX",
                    "queryStartDate": query_date,
                    "queryEndDate": query_date,
                },
                encoding="big5",
            )
        except Exception as e:
            logger.error("TAIFEX CSV request failed for %s: %s", date, e)
            return None

        return self._parse_night_csv(resp.text, date)

    def _parse_night_csv(self, csv_text: str, date: str) -> dict | None:
        """從期交所 CSV 回應解析夜盤（盤後）近月合約資料。"""
        reader = csv.reader(io.StringIO(csv_text))
        header = None

        for row in reader:
            if not row:
                continue
            # 找 header 列
            if header is None:
                cleaned = [c.strip() for c in row]
                if "交易日期" in cleaned or "契約" in cleaned:
                    header = cleaned
                continue

            if len(row) < len(header):
                continue

            row_dict = {header[i]: row[i].strip() for i in range(len(header))}

            contract = row_dict.get("契約", "")
            session = row_dict.get("交易時段", "")

            # 近月 TX 的盤後交易
            if contract == "TX" and "盤後" in session:
                # 找最近到期月份（CSV 中排第一個出現的 TX 盤後就是近月）
                try:
                    night_close = _parse_float(row_dict.get("收盤價", "0"))
                    night_volume = _parse_int(row_dict.get("成交量", "0"))
                    logger.info(
                        "TAIFEX night parsed: close=%.0f, volume=%d for %s",
                        night_close, night_volume, date,
                    )
                    return {"night_close": night_close, "night_volume": night_volume}
                except (ValueError, KeyError) as e:
                    logger.error("TAIFEX CSV parse error: %s", e)
                    return None

        logger.info("TAIFEX: no night session data found for %s", date)
        return None

    def collect_oi_foreign(self, date: str) -> dict | None:
        """取得外資台指期未平倉淨額（口數）。回傳 {"oi_net_foreign": int} 或 None。"""
        query_date = date.replace("-", "/")  # YYYY/MM/DD
        try:
            resp = http_post(
                TAIFEX_OI_CSV_URL,
                data={
                    "queryStartDate": query_date,
                    "queryEndDate": query_date,
                    "commodityId": "TXF",
                },
                encoding="big5",
            )
        except Exception as e:
            logger.error("TAIFEX OI request failed for %s: %s", date, e)
            return None

        return self.collect_oi_foreign_from_csv(resp.text, date)

    def collect_oi_foreign_from_csv(self, csv_text: str, date: str) -> dict | None:
        """從期交所三大法人留倉 CSV 解析外資未平倉淨額。供測試及未來接通真實來源使用。"""
        reader = csv.reader(io.StringIO(csv_text))
        header = None

        for row in reader:
            if not row:
                continue
            if header is None:
                cleaned = [c.strip() for c in row]
                if "身份別" in cleaned or "商品名稱" in cleaned:
                    header = cleaned
                continue

            if len(row) < len(header):
                continue

            row_dict = {header[i]: row[i].strip() for i in range(len(header))}

            product = row_dict.get("商品名稱", "")
            identity = row_dict.get("身份別", "")

            # 身份別實測為「外資及陸資」，用 in 比對涵蓋
            if "臺股期貨" in product and "外資" in identity:
                try:
                    oi_net = _parse_int(row_dict.get("多空未平倉口數淨額", "0"))
                    logger.info("TAIFEX OI parsed: oi_net_foreign=%d for %s", oi_net, date)
                    return {"oi_net_foreign": oi_net}
                except (ValueError, KeyError) as e:
                    logger.error("TAIFEX OI CSV parse error: %s", e)
                    return None

        logger.info("TAIFEX OI: no foreign data found for %s", date)
        return None

    # ── save 方法 ────────────────────────────────────────────────

    def save(self, date: str, data: dict) -> None:
        """存入夜盤資料（預設 save 介面）。"""
        self.save_night_session(date, data)

    def save_night_session(self, date: str, data: dict) -> None:
        """更新 raw_futures.night_close 和 night_volume。"""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO raw_futures (date, night_close, night_volume, collected_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                    night_close = excluded.night_close,
                    night_volume = excluded.night_volume,
                    collected_at = excluded.collected_at""",
                (date, data["night_close"], data["night_volume"], now),
            )

    def save_oi_foreign(self, date: str, data: dict) -> None:
        """更新 raw_futures.oi_net_foreign。"""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO raw_futures (date, oi_net_foreign, collected_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                    oi_net_foreign = excluded.oi_net_foreign,
                    collected_at = excluded.collected_at""",
                (date, data["oi_net_foreign"], now),
            )

    # ── run ──────────────────────────────────────────────────────

    def run(self, date: str) -> dict:
        """執行所有 TAIFEX 資料收集。"""
        logger.info("TAIFEXCollector: starting all tasks for %s", date)
        results = {}

        results["night_session"] = self._try_collect_and_save(
            lambda: self.collect_night_session(date),
            lambda data: self.save_night_session(date, data),
        )
        results["oi_foreign"] = self._try_collect_and_save(
            lambda: self.collect_oi_foreign(date),
            lambda data: self.save_oi_foreign(date, data),
        )

        logger.info("TAIFEXCollector results for %s: %s", date, results)
        return results
