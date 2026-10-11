"""Layer 3 籌碼觀察：個股分點與外資動向的分類標籤，寫入 stock_signals。

盤前判斷（大盤）在 integration/premarket_call。分類規則在 integration/stock_chip。
"""

import logging
import sqlite3
from datetime import datetime

from config import settings
from integration.stock_chip import classify_broker, classify_foreign, foreign_streak

logger = logging.getLogger(__name__)


def compute_foreign_stock_signals(date: str, conn: sqlite3.Connection) -> list[dict]:
    """用 T86 每檔外資買賣超（raw_chip __FOREIGN__）產生個股觀察訊號，寫入 stock_signals。

    訊號於 08:50 產出時今日 T86 尚未收（18:30 才收），故用「今日之前」最新（前一交易日）
    的外資資料。對 watchlist 每檔算外資連買/連賣天數與累計張數。
    """
    # 先清掉當日舊的外資訊號（只 INSERT 會殘留已不成立的訊號，如外資由連買轉賣）
    conn.execute(
        "DELETE FROM stock_signals WHERE date = ? AND broker_name = '外資'", (date,)
    )

    watch = conn.execute(
        "SELECT stock_id FROM watchlist ORDER BY stock_id"
    ).fetchall()
    results = []
    now = datetime.now().isoformat()

    for (stock_id,) in watch:
        s = foreign_streak(conn, stock_id, date)
        if s is None:
            continue
        latest_net, streak, cum = s
        direction = 1 if latest_net > 0 else -1 if latest_net < 0 else 0
        if direction == 0:
            continue

        classified = classify_foreign(direction, streak, cum, latest_net)
        if classified is None:
            continue
        category, reason = classified

        conn.execute(
            """INSERT INTO stock_signals
                   (date, stock_id, broker_name, category, reasons,
                    rule_version, created_at)
               VALUES (?, ?, '外資', ?, ?, ?, ?)
               ON CONFLICT(date, stock_id, broker_name) DO UPDATE SET
                   category = excluded.category,
                   reasons = excluded.reasons,
                   rule_version = excluded.rule_version,
                   created_at = excluded.created_at""",
            (date, stock_id, category, reason, settings.SIGNAL_RULE_VERSION, now),
        )
        results.append({"stock_id": stock_id, "category": category, "reason": reason})

    conn.commit()
    logger.info("Foreign stock signals for %s: %d records", date, len(results))
    return results


def compute_stock_signals(date: str, conn: sqlite3.Connection) -> list[dict]:
    """從 daily_stock_metrics 計算 date 的分點籌碼觀察，寫入 stock_signals。

    與外資一致只看 date 之前：每檔取 date 之前最近一個有分點指標的日期
    （08:50 時 date 當日分點未收／未匯入；重算歷史日也才與當時一致）。
    """
    # 先清掉當日舊的分點籌碼觀察（只 INSERT 會殘留已不成立的分類）；外資那列由
    # compute_foreign_stock_signals 負責
    conn.execute(
        "DELETE FROM stock_signals WHERE date = ? AND broker_name != '外資'", (date,)
    )
    rows = conn.execute(
        "SELECT m.stock_id, m.broker_name, m.net_amount, m.consecutive_days, "
        "       m.price_zone, m.both_sides_flag, m.broker_type "
        "FROM daily_stock_metrics m "
        "JOIN (SELECT stock_id, MAX(date) AS d FROM daily_stock_metrics "
        "      WHERE date < ? GROUP BY stock_id) latest "
        "  ON m.stock_id = latest.stock_id AND m.date = latest.d "
        "ORDER BY m.stock_id, m.broker_name",
        (date,),
    ).fetchall()

    if not rows:
        conn.commit()
        logger.info("compute_stock_signals: no stock metrics before %s", date)
        return []

    results = []
    now = datetime.now().isoformat()

    for (stock_id, broker_name, net_amount, consecutive,
         price_zone, both_sides, broker_type) in rows:

        # 對敲 → 隔日沖 → 連買連賣×價位（規則在 integration/stock_chip）
        classified = classify_broker(net_amount, consecutive, price_zone,
                                     both_sides, broker_type)
        if classified is None:
            continue
        category, reason = classified

        signal = {
            "date": date,
            "stock_id": stock_id,
            "broker_name": broker_name,
            "category": category,
            "reasons": reason,
            "rule_version": settings.SIGNAL_RULE_VERSION,
        }
        results.append(signal)

        conn.execute(
            """INSERT INTO stock_signals
                   (date, stock_id, broker_name, category, reasons,
                    rule_version, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date, stock_id, broker_name) DO UPDATE SET
                   category = excluded.category,
                   reasons = excluded.reasons,
                   rule_version = excluded.rule_version,
                   created_at = excluded.created_at""",
            (date, stock_id, broker_name, category, reason,
             settings.SIGNAL_RULE_VERSION, now),
        )

    conn.commit()
    logger.info("Stock signals for %s: %d records", date, len(results))
    return results
