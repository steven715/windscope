"""免費除權息還原因子（FinMind TaiwanStockDividendResult）。

該 dataset 免 token 可用（付費的是 TaiwanStockPriceAdj）。取每次除權息的 after/before
參考價比例，供 price_adjust 的「配息細調」。分割/大除權缺口由價格自動偵測，不靠此表。
"""

import logging
from datetime import datetime

from config import settings
from db.connection import get_connection
from utils.http_client import http_get

logger = logging.getLogger(__name__)

_DATASET = "TaiwanStockDividendResult"


def collect_dividend_factors(stock_id: str) -> list[dict]:
    """FinMind 除權息結果 → [{"ex_date", "factor"}]（factor = after/before）。

    請求失敗、非 200、或無資料一律回 []（best-effort，不讓回補流程掛掉）。
    """
    try:
        resp = http_get(
            settings.FINMIND_API_URL,
            params={
                "dataset": _DATASET,
                "data_id": stock_id,
                "start_date": "2000-01-01",
                "token": settings.FINMIND_TOKEN,
            },
        )
        payload = resp.json()
    except Exception as e:
        logger.warning("collect_dividend_factors %s failed: %s", stock_id, e)
        return []

    if payload.get("status") != 200:
        logger.info("dividend: FinMind non-200 for %s: %s",
                    stock_id, str(payload.get("msg", ""))[:120])
        return []

    events = []
    for row in payload.get("data", []):
        try:
            before = float(row["before_price"])
            after = float(row["after_price"])
            ex_date = row["date"]
        except (KeyError, TypeError, ValueError):
            continue
        if before > 0 and after > 0 and ex_date:
            events.append({"ex_date": ex_date, "factor": after / before})

    logger.info("dividend: %d factors for %s", len(events), stock_id)
    return events


def save_dividend_factors(stock_id: str, events: list[dict],
                          db_path: str | None = None) -> int:
    """存入 stock_dividends，ON CONFLICT DO UPDATE。回傳筆數。"""
    now = datetime.now().isoformat()
    with get_connection(db_path) as conn:
        for e in events:
            conn.execute(
                "INSERT INTO stock_dividends (stock_id, ex_date, factor, collected_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(stock_id, ex_date) DO UPDATE SET "
                "factor = excluded.factor, collected_at = excluded.collected_at",
                (stock_id, e["ex_date"], e["factor"], now),
            )
    return len(events)


def load_dividend_factors(conn, stock_id: str) -> list[dict]:
    """從 stock_dividends 讀出 [{"ex_date", "factor"}]（供 price_adjust 用）。"""
    rows = conn.execute(
        "SELECT ex_date, factor FROM stock_dividends WHERE stock_id = ? ORDER BY ex_date",
        (stock_id,),
    ).fetchall()
    return [{"ex_date": r[0], "factor": r[1]} for r in rows]
