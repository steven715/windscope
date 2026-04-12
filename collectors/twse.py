import logging
from datetime import datetime

from collectors.base import BaseCollector
from db.connection import get_connection
from utils.http_client import http_get

logger = logging.getLogger(__name__)

TWSE_BFI82U_URL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"


def _parse_amount(s: str) -> float:
    """去除逗號並轉為 float。處理負數（可能帶括號或負號）。"""
    s = s.strip().replace(",", "")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    return float(s)


class TWSECollector(BaseCollector):
    """證交所三大法人買賣超 collector。"""

    def collect(self, date: str) -> dict | None:
        """收集指定日期的三大法人買賣超。date 格式：YYYY-MM-DD。"""
        date_param = date.replace("-", "")  # -> YYYYMMDD
        resp = http_get(TWSE_BFI82U_URL, params={"date": date_param, "response": "json"})
        data = resp.json()

        if data.get("stat") != "OK":
            logger.info("BFI82U: no data for %s (stat=%s)", date, data.get("stat"))
            return None

        # Check if response date matches requested date (TWSE returns latest
        # trading day data even for weekends/holidays)
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

        # 自營商 = 自行買賣 + 避險
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

    def save(self, date: str, data: dict) -> None:
        """存入 raw_institutional，用 INSERT OR REPLACE。"""
        with get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO raw_institutional
                   (date, foreign_buy, foreign_sell, foreign_net,
                    trust_buy, trust_sell, trust_net,
                    dealer_buy, dealer_sell, dealer_net,
                    total_net, collected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    date,
                    data["foreign_buy"],
                    data["foreign_sell"],
                    data["foreign_net"],
                    data["trust_buy"],
                    data["trust_sell"],
                    data["trust_net"],
                    data["dealer_buy"],
                    data["dealer_sell"],
                    data["dealer_net"],
                    data["total_net"],
                    datetime.now().isoformat(),
                ),
            )
