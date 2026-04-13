"""籌碼衍生指標計算：買超金額、連續天數、MA20、價格區間。"""

import logging
import sqlite3
from datetime import datetime

from config import settings

logger = logging.getLogger(__name__)

# 排除這些特殊 broker_name（由 TWSE collector 產生的輔助 rows）
_EXCLUDED_BROKERS = ("__PRICE_ONLY__", "__FOREIGN__")


def _get_close_price(conn: sqlite3.Connection, date: str,
                     stock_id: str, row_close: float | None) -> float | None:
    """取得收盤價：優先用 row 自身的，否則從 __PRICE_ONLY__ row 查。"""
    if row_close is not None:
        return row_close
    r = conn.execute(
        "SELECT close_price FROM raw_chip "
        "WHERE date = ? AND stock_id = ? AND close_price IS NOT NULL "
        "LIMIT 1",
        (date, stock_id),
    ).fetchone()
    return r[0] if r else None


def _calc_consecutive_days(conn: sqlite3.Connection, date: str,
                           stock_id: str, broker_name: str,
                           current_net: int) -> int:
    """計算包含今天的連續同方向天數。正=連買，負=連賣，0=中斷。"""
    if current_net == 0:
        return 0

    direction = 1 if current_net > 0 else -1
    count = 1  # 今天算一天

    history = conn.execute(
        "SELECT date, net_volume FROM raw_chip "
        "WHERE stock_id = ? AND broker_name = ? AND date < ? "
        "ORDER BY date DESC LIMIT 30",
        (stock_id, broker_name, date),
    ).fetchall()

    for h_date, h_net in history:
        if h_net is None or h_net == 0:
            break
        h_dir = 1 if h_net > 0 else -1
        if h_dir == direction:
            count += 1
        else:
            break

    return count * direction


def _calc_price_vs_ma20(conn: sqlite3.Connection, date: str,
                        stock_id: str, close_price: float) -> float | None:
    """(close_price - MA) / MA * 100。歷史不足 CHIP_MA_MIN_DAYS 回傳 None。"""
    rows = conn.execute(
        "SELECT DISTINCT date, close_price FROM raw_chip "
        "WHERE stock_id = ? AND date <= ? AND close_price IS NOT NULL "
        "ORDER BY date DESC LIMIT ?",
        (stock_id, date, settings.CHIP_MA_PERIOD),
    ).fetchall()

    if len(rows) < settings.CHIP_MA_MIN_DAYS:
        return None

    ma = sum(r[1] for r in rows) / len(rows)
    if ma == 0:
        return None

    return round((close_price - ma) / ma * 100, 2)


def _classify_price_zone(pct: float | None) -> str | None:
    """根據 price_vs_ma20 分類價格區間。"""
    if pct is None:
        return None
    if pct < settings.PRICE_ZONE_LOW:
        return "low"
    elif -settings.PRICE_ZONE_CONSOLIDATION <= pct <= settings.PRICE_ZONE_CONSOLIDATION:
        return "consolidation"
    elif pct > settings.PRICE_ZONE_HIGH:
        return "high"
    else:
        return "other"


def compute_chip_metrics(date: str, conn: sqlite3.Connection) -> list[dict]:
    """計算當日所有 raw_chip 紀錄的衍生指標，寫入 daily_stock_metrics。"""
    rows = conn.execute(
        "SELECT date, stock_id, stock_name, broker_name, "
        "       buy_volume, sell_volume, net_volume, close_price "
        "FROM raw_chip WHERE date = ? AND broker_name NOT IN (?, ?)",
        (date, *_EXCLUDED_BROKERS),
    ).fetchall()

    if not rows:
        logger.info("compute_chip_metrics: no chip data for %s", date)
        return []

    results = []
    now = datetime.now().isoformat()

    for row in rows:
        r_date, stock_id, stock_name, broker_name, \
            buy_vol, sell_vol, net_vol, row_close = row

        # Resolve close_price
        close_price = _get_close_price(conn, date, stock_id, row_close)

        # 1. net_amount
        if net_vol is not None and close_price is not None:
            net_amount = net_vol * close_price * 1000
        else:
            net_amount = None

        # 2. consecutive_days
        if net_vol is not None:
            consecutive_days = _calc_consecutive_days(
                conn, date, stock_id, broker_name, net_vol
            )
        else:
            consecutive_days = 0

        # 3. price_vs_ma20
        if close_price is not None:
            price_vs_ma20 = _calc_price_vs_ma20(
                conn, date, stock_id, close_price
            )
        else:
            price_vs_ma20 = None

        # 4. price_zone
        price_zone = _classify_price_zone(price_vs_ma20)

        # 5. both_sides_flag
        if buy_vol and sell_vol and buy_vol > 0 and sell_vol > 0:
            both_sides_flag = 1
        else:
            both_sides_flag = 0

        # 6. broker_type
        tag = conn.execute(
            "SELECT broker_type FROM broker_tags WHERE broker_name = ?",
            (broker_name,),
        ).fetchone()
        broker_type = tag[0] if tag else None

        metric = {
            "date": date,
            "stock_id": stock_id,
            "stock_name": stock_name,
            "broker_name": broker_name,
            "net_amount": net_amount,
            "consecutive_days": consecutive_days,
            "price_vs_ma20": price_vs_ma20,
            "price_zone": price_zone,
            "both_sides_flag": both_sides_flag,
            "broker_type": broker_type,
        }
        results.append(metric)

        # Write to daily_stock_metrics
        conn.execute(
            """INSERT INTO daily_stock_metrics
                   (date, stock_id, broker_name, net_amount,
                    consecutive_days, price_vs_ma20, price_zone,
                    both_sides_flag, broker_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date, stock_id, broker_name) DO UPDATE SET
                   net_amount = excluded.net_amount,
                   consecutive_days = excluded.consecutive_days,
                   price_vs_ma20 = excluded.price_vs_ma20,
                   price_zone = excluded.price_zone,
                   both_sides_flag = excluded.both_sides_flag,
                   broker_type = excluded.broker_type""",
            (date, stock_id, broker_name, net_amount,
             consecutive_days, price_vs_ma20, price_zone,
             both_sides_flag, broker_type),
        )

    conn.commit()
    logger.info("Chip metrics computed for %s: %d records", date, len(results))
    return results
