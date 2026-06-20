import csv
import io
import logging
from datetime import datetime

from collectors.base import BaseCollector
from config import settings
from db.connection import get_connection
from utils.http_client import http_get

logger = logging.getLogger(__name__)

BOT_CSV_URL = "https://rate.bot.com.tw/xrt/flcsv/0/day"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
SP500_SYMBOL = "^GSPC"  # 實測驗證 2026-06-12，回應格式與匯率 chart 相同


class FXCollector(BaseCollector):
    """匯率 collector：USD/TWD（台銀）、USD/CNY/KRW/JPY（Yahoo Finance）。"""

    # 走 Yahoo 的外幣對；USD/TWD 走台銀。collect_pair 依此自動路由。
    _FOREIGN_PAIRS = ("USD/CNY", "USD/KRW", "USD/JPY")

    # ── collect 方法 ──────────────────────────────────────────────

    def collect_pair(self, date: str, pair: str) -> dict | None:
        """依幣別自動路由來源收一檔匯率：USD/TWD→台銀、CNY/KRW/JPY→Yahoo。

        回 {"currency_pair", "rate"} 或 None。把散落各 job 的來源判斷收斂於此。
        """
        if pair == "USD/TWD":
            return self.collect_twd(date)
        if pair in self._FOREIGN_PAIRS:
            return self.collect_foreign_fx(pair)
        logger.error("collect_pair: unknown pair %s", pair)
        return None

    def collect_and_save_pair(self, date: str, pair: str, time_slot: str) -> bool:
        """收一檔匯率並存入指定 time_slot 欄位。成功 True；無資料或 slot 非法 False。"""
        data = self.collect_pair(date, pair)
        if data is None:
            return False
        return self.save_fx(date, data["currency_pair"], data["rate"], time_slot)

    def collect(self, date: str) -> dict | None:
        """收集 USD/TWD（預設 collect 介面）。"""
        return self.collect_twd(date, time_slot="close_16")

    def collect_twd(self, date: str, time_slot: str = "close_16") -> dict | None:
        """取得 USD/TWD 即期買入匯率。回傳 {"currency_pair": "USD/TWD", "rate": float} 或 None。"""
        try:
            resp = http_get(BOT_CSV_URL, encoding="utf-8")
        except Exception as e:
            logger.error("BOT CSV request failed: %s", e)
            return None

        return self._parse_bot_csv(resp.text)

    def _parse_bot_csv(self, csv_text: str) -> dict | None:
        """從台銀 flcsv 解析 USD 即期買入匯率。

        flcsv 表頭為「幣別,匯率,現金,即期,遠期10天…遠期180天」買入區塊，
        後接相同欄位的賣出區塊，故「即期」出現兩次：第一個是即期買入。
        回傳 {"currency_pair": "USD/TWD", "rate": float} 或 None。
        """
        # 台銀 CSV 帶 UTF-8 BOM，會讓表頭第一格變成 "﻿幣別"，
        # 導致 "幣別" 比對失敗。先去除 BOM。
        csv_text = csv_text.lstrip("﻿")
        reader = csv.reader(io.StringIO(csv_text))
        header = None
        cur_idx = None
        spot_idx = None

        for row in reader:
            cleaned = [c.strip() for c in row]
            if header is None:
                # 表頭需同時含「幣別」與「即期」才視為有效表頭（跳過註解行）
                if "幣別" not in cleaned or "即期" not in cleaned:
                    continue
                header = cleaned
                cur_idx = header.index("幣別")
                spot_idx = header.index("即期")  # 第一個即期 = 即期買入
                continue

            if len(cleaned) <= spot_idx or cleaned[cur_idx] != "USD":
                continue

            rate_str = cleaned[spot_idx]
            if not rate_str:
                logger.warning("BOT CSV: USD found but no 即期 value")
                return None
            rate = float(rate_str)
            logger.info("BOT CSV parsed: USD/TWD=%.4f", rate)
            return {"currency_pair": "USD/TWD", "rate": rate}

        logger.warning("BOT CSV: USD row not found")
        return None

    def collect_foreign_fx(self, currency_pair: str) -> dict | None:
        """取得 USD/CNY、USD/KRW、USD/JPY（Yahoo Finance）。回傳 {"currency_pair", "rate"} 或 None。"""
        symbol_map = {
            "USD/CNY": "USDCNY=X",
            "USD/KRW": "USDKRW=X",
            "USD/JPY": "USDJPY=X",
        }
        symbol = symbol_map.get(currency_pair)
        if not symbol:
            logger.error("Unknown currency pair: %s", currency_pair)
            return None

        try:
            resp = http_get(
                f"{YAHOO_CHART_URL}/{symbol}",
                params={"interval": "1d", "range": "2d"},
            )
            data = resp.json()
        except Exception as e:
            logger.error("Yahoo Finance request failed for %s: %s", currency_pair, e)
            return None

        return self._parse_yahoo_chart(data, currency_pair)

    def _parse_yahoo_chart(self, data: dict, currency_pair: str) -> dict | None:
        """從 Yahoo Finance chart API 解析最新收盤價。"""
        try:
            result = data["chart"]["result"][0]
            closes = result["indicators"]["quote"][0]["close"]
            # 取最後一筆非 None 的收盤價
            rate = None
            for c in reversed(closes):
                if c is not None:
                    rate = float(c)
                    break
            if rate is None:
                logger.warning("Yahoo chart: no valid close for %s", currency_pair)
                return None
            logger.info("Yahoo chart parsed: %s=%.4f", currency_pair, rate)
            return {"currency_pair": currency_pair, "rate": rate}
        except (KeyError, IndexError, TypeError) as e:
            logger.error("Yahoo chart parse error for %s: %s", currency_pair, e)
            return None

    def collect_sp500(self) -> dict | None:
        """取得 S&P 500 最新收盤價（Yahoo Finance ^GSPC）。回傳 {"close": float} 或 None。"""
        try:
            resp = http_get(
                f"{YAHOO_CHART_URL}/{SP500_SYMBOL}",
                params={"interval": "1d", "range": "2d"},
            )
            data = resp.json()
        except Exception as e:
            logger.error("Yahoo Finance request failed for SP500: %s", e)
            return None

        parsed = self._parse_yahoo_chart(data, "SP500")
        if parsed is None:
            return None
        return {"close": parsed["rate"]}

    def collect_twd_intraday(self) -> list[dict] | None:
        """取得 USD/TWD 盤前 5 分鐘序列（Yahoo USDTWD=X，離岸報價，作節奏形狀參考）。

        回傳最近 settings.FX_INTRADAY_BARS 根有效 K 的 [{"ts": int, "close": float}]，
        由舊到新；失敗回 None。
        """
        try:
            resp = http_get(
                f"{YAHOO_CHART_URL}/USDTWD=X",
                params={"interval": "5m", "range": "1d"},
            )
            data = resp.json()
        except Exception as e:
            logger.error("Yahoo USDTWD intraday request failed: %s", e)
            return None
        try:
            result = data["chart"]["result"][0]
            ts_list = result["timestamp"]
            closes = result["indicators"]["quote"][0]["close"]
        except (KeyError, IndexError, TypeError) as e:
            logger.error("Yahoo USDTWD intraday parse error: %s", e)
            return None

        bars = [{"ts": int(t), "close": float(c)}
                for t, c in zip(ts_list, closes) if c is not None]
        if not bars:
            logger.warning("Yahoo USDTWD intraday: no valid bars")
            return None
        return bars[-settings.FX_INTRADAY_BARS:]

    # ── save 方法 ────────────────────────────────────────────────

    def save_intraday_fx(self, date: str, currency_pair: str,
                         bars: list[dict]) -> None:
        """存入 intraday_fx（盤前匯率 5 分序列）。冪等覆寫。"""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            for b in bars:
                conn.execute(
                    "INSERT INTO intraday_fx (date, currency_pair, ts, close, collected_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(date, currency_pair, ts) DO UPDATE SET "
                    "close = excluded.close, collected_at = excluded.collected_at",
                    (date, currency_pair, b["ts"], b["close"], now),
                )

    def save_sp500(self, date: str, close: float) -> None:
        """更新 raw_futures.sp500_close（S&P 500 雖由 FX collector 收集，但欄位在期貨表）。"""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO raw_futures (date, sp500_close, collected_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                    sp500_close = excluded.sp500_close,
                    collected_at = excluded.collected_at""",
                (date, close, now),
            )

    def save(self, date: str, data: dict) -> None:
        """存入 FX 資料（預設 save 介面）。"""
        self.save_fx(date, data["currency_pair"], data["rate"], "close_16")

    def save_fx(self, date: str, currency_pair: str, rate: float,
                time_slot: str) -> bool:
        """存入 raw_fx，只更新指定的 time_slot 欄位。slot 非法回 False、不寫入。"""
        now = datetime.now().isoformat()
        if time_slot not in ("close_16", "quote_0845", "ny_close"):
            logger.error("Invalid time_slot: %s", time_slot)
            return False

        with get_connection(self.db_path) as conn:
            conn.execute(
                f"""INSERT INTO raw_fx (date, currency_pair, {time_slot}, collected_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(date, currency_pair) DO UPDATE SET
                     {time_slot} = excluded.{time_slot},
                     collected_at = excluded.collected_at""",
                (date, currency_pair, rate, now),
            )
        return True

    # ── run ──────────────────────────────────────────────────────

    def run(self, date: str) -> dict:
        """執行所有 FX 資料收集。"""
        logger.info("FXCollector: starting all tasks for %s", date)
        results = {}

        # USD/TWD
        results["usd_twd"] = self._try_collect_and_save(
            lambda: self.collect_twd(date, "close_16"),
            lambda data: self.save_fx(date, data["currency_pair"], data["rate"], "close_16"),
        )

        # USD/CNY
        results["usd_cny"] = self._try_collect_and_save(
            lambda: self.collect_foreign_fx("USD/CNY"),
            lambda data: self.save_fx(date, data["currency_pair"], data["rate"], "close_16"),
        )

        # USD/KRW
        results["usd_krw"] = self._try_collect_and_save(
            lambda: self.collect_foreign_fx("USD/KRW"),
            lambda data: self.save_fx(date, data["currency_pair"], data["rate"], "close_16"),
        )

        logger.info("FXCollector results for %s: %s", date, results)
        return results
