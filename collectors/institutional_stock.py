"""個股外資買賣超歷史回補（FinMind TaiwanStockInstitutionalInvestorsBuySell）。

該 dataset 免 token 可用，一次回整段歷史。取「Foreign_Investor」（外資，不含外資自營，
對齊 TWSE T86「外資及陸資(不含外資自營商)」語意），淨額(股)/1000=張，存進 raw_chip
的 __FOREIGN__ 標記列——與每日 T86 收集同格式，於除權息前後仍相容。
"""

import logging
from datetime import datetime

from config import settings
from db.connection import get_connection
from utils.http_client import http_get

logger = logging.getLogger(__name__)

_DATASET = "TaiwanStockInstitutionalInvestorsBuySell"
_FOREIGN = "Foreign_Investor"


def collect_foreign_history(stock_id: str,
                            start_date: str = "2000-01-01") -> list[dict]:
    """回傳 [{"date", "net_volume"(張)}]（外資每日買賣超）。失敗/非200/無資料回 []。"""
    try:
        resp = http_get(
            settings.FINMIND_API_URL,
            params={
                "dataset": _DATASET,
                "data_id": stock_id,
                "start_date": start_date,
                "token": settings.FINMIND_TOKEN,
            },
        )
        payload = resp.json()
    except Exception as e:
        logger.warning("collect_foreign_history %s failed: %s", stock_id, e)
        return []

    if payload.get("status") != 200:
        logger.info("foreign_history: FinMind non-200 for %s: %s",
                    stock_id, str(payload.get("msg", ""))[:120])
        return []

    rows = []
    for row in payload.get("data", []):
        if row.get("name") != _FOREIGN:
            continue
        try:
            net_shares = int(row["buy"]) - int(row["sell"])
            date = row["date"]
        except (KeyError, TypeError, ValueError):
            continue
        if date:
            rows.append({"date": date, "net_volume": round(net_shares / 1000)})

    logger.info("foreign_history: %d days for %s", len(rows), stock_id)
    return rows


def save_foreign_history(stock_id: str, rows: list[dict],
                         db_path: str | None = None) -> int:
    """存進 raw_chip 的 __FOREIGN__ 標記列（與每日 T86 同格式）。回傳筆數。"""
    now = datetime.now().isoformat()
    with get_connection(db_path) as conn:
        for r in rows:
            conn.execute(
                """INSERT INTO raw_chip
                   (date, stock_id, broker_name, net_volume, collected_at)
                   VALUES (?, ?, '__FOREIGN__', ?, ?)
                   ON CONFLICT(date, stock_id, broker_name) DO UPDATE SET
                    net_volume = excluded.net_volume,
                    collected_at = excluded.collected_at""",
                (r["date"], stock_id, r["net_volume"], now),
            )
    return len(rows)
