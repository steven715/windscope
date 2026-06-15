"""證交所 MIS 即時行情 collector（盤中即時驗證觀察用）。

與其他 collector 不同：即時資料只回傳記憶體 dict，不寫入 premarket.db，
故不繼承 BaseCollector。資料來源見 docs/spec_live_verification.md。
"""

import logging

from utils.http_client import http_get

logger = logging.getLogger(__name__)

MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
MIS_REFERER = "https://mis.twse.com.tw/stock/index.jsp"

# MIS 無成交時 z 會是這些佔位值，需 fallback 到開盤價
_EMPTY_VALUES = {"-", "", None}


class MISCollector:
    """證交所 MIS 即時報價。t00 = 加權指數，個股用股票代號。"""

    def collect_index(self, symbol: str = "t00") -> dict | None:
        """取得單一標的即時報價。

        symbol：MIS 代號（"t00" 為加權指數，個股直接給代號如 "2330"）。
        回傳 {symbol, name, price, prev_close, open, high, low, ts} 或 None。
        price 取即時成交 z，盤前/無成交（z='-'）時 fallback 到開盤 o。
        """
        ex_ch = f"tse_{symbol}.tw"
        try:
            resp = http_get(
                MIS_URL,
                params={"ex_ch": ex_ch, "json": "1", "delay": "0"},
                headers={"Referer": MIS_REFERER},
            )
            data = resp.json()
        except Exception as e:
            logger.error("MIS request failed for %s: %s", symbol, e)
            return None

        return self._parse_quote(data, symbol)

    def _parse_quote(self, data: dict, symbol: str) -> dict | None:
        """從 MIS JSON 解析指定 symbol 的即時報價。"""
        if data.get("rtcode") != "0000":
            logger.warning(
                "MIS rtcode=%s msg=%s for %s",
                data.get("rtcode"), data.get("rtmessage"), symbol,
            )
            return None

        msg_array = data.get("msgArray") or []
        entry = next((m for m in msg_array if m.get("c") == symbol), None)
        if entry is None:
            logger.warning("MIS: symbol %s not in msgArray", symbol)
            return None

        # price：優先即時成交 z，無成交時 fallback 開盤 o
        raw_price = entry.get("z")
        if raw_price in _EMPTY_VALUES:
            raw_price = entry.get("o")

        price = self._to_float(raw_price)
        prev_close = self._to_float(entry.get("y"))
        open_price = self._to_float(entry.get("o"))

        if price is None or prev_close is None:
            logger.warning("MIS: %s missing price/prev_close (z=%s y=%s)",
                           symbol, entry.get("z"), entry.get("y"))
            return None

        ts = entry.get("tlong")
        ts = int(ts) if ts and str(ts).isdigit() else None

        return {
            "symbol": symbol,
            "name": entry.get("n"),
            "price": price,
            "prev_close": prev_close,
            "open": open_price,
            "high": self._to_float(entry.get("h")),
            "low": self._to_float(entry.get("l")),
            "ts": ts,
        }

    @staticmethod
    def _to_float(value: object) -> float | None:
        """安全轉 float；MIS 佔位值（'-' 等）回 None。"""
        if value in _EMPTY_VALUES:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
