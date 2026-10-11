"""個股籌碼判讀：外資動向／主力分點／股價位置三維度，與彙總出的籌碼傾向。

唯一入口 load_stock_chip：分類規則（綁 settings 門檻）只在這裡；chip_observation 用
classify_foreign／classify_broker 產籌碼觀察（寫 stock_signals），explain 只把
StockChip 排成解讀表，pages 只顯示。純讀，不寫 DB。籌碼傾向不經驗證，不是立場。
"""

import sqlite3
from dataclasses import dataclass

from config import settings

# 分類 category → 方向（up=偏買、down=偏賣、flat=無方向；對敲/隔日沖等警示類走 flat）
CATEGORY_DIRECTION = {
    "外資連買": "up", "外資大買": "up", "外資連賣": "down", "外資大賣": "down",
    "bottom_watch": "up", "accumulation": "up",
    "distribution_warning": "down", "avoid": "down",
    "fake_volume": "flat", "day_trade_no_chase": "flat",
}


def classify_foreign(direction: int, streak: int, cum: float,
                     latest_net: float) -> tuple[str, str] | None:
    """外資流向分類。回傳 (category, reason) 或 None。"""
    buy = direction > 0
    if streak >= settings.FOREIGN_CONSECUTIVE_MIN and abs(cum) >= settings.FOREIGN_CUM_NET_MIN:
        if buy:
            return "外資連買", f"外資連買 {streak} 天（累計 {cum:,.0f} 張），值得觀察，站回月線才進"
        return "外資連賣", f"外資連賣 {streak} 天（累計 {abs(cum):,.0f} 張），籌碼轉弱"
    if abs(latest_net) >= settings.FOREIGN_BIG_NET:
        if buy:
            return "外資大買", f"外資單日大買 {latest_net:,.0f} 張（反手買，僅觀察，站回月線才進）"
        return "外資大賣", f"外資單日大賣 {abs(latest_net):,.0f} 張"
    return None


def _classify_broker_net(net_amount: float | None, consecutive: int | None,
                         price_zone: str | None) -> tuple[str, str] | None:
    """分點連買連賣 × 價位分類，回傳 (category, reason) 或 None。"""
    consecutive = consecutive or 0

    # 連賣
    if consecutive <= -settings.STOCK_CONSECUTIVE_MIN:
        return "avoid", f"分點連賣 {abs(consecutive)} 天"

    # 連買但金額不足或天數不足
    if consecutive < settings.STOCK_CONSECUTIVE_MIN:
        return None
    if net_amount is None or net_amount < settings.STOCK_NET_AMOUNT_MIN:
        return None

    amount_str = f"{net_amount / 1e8:.2f}億" if net_amount >= 1e8 else f"{net_amount / 1e4:.0f}萬"

    if price_zone == "low":
        return "bottom_watch", f"低檔連買 {consecutive} 天（{amount_str}），主力摸底"
    if price_zone == "high":
        return "distribution_warning", f"高檔連買 {consecutive} 天（{amount_str}），慎防出貨"
    if price_zone == "consolidation" and consecutive >= settings.STOCK_ACCUMULATION_MIN:
        return "accumulation", f"盤整區連買 {consecutive} 天（{amount_str}），主力吸籌"
    return None


def classify_broker(net_amount: float | None, consecutive: int | None,
                    price_zone: str | None, both_sides: int | None,
                    broker_type: str | None) -> tuple[str, str] | None:
    """單一分點分類：對敲 → 隔日沖 → 連買連賣×價位。回傳 (category, reason) 或 None。"""
    if both_sides == 1:
        return "fake_volume", "同分點買賣兩邊都有量，疑似對敲假量，不碰"
    if broker_type == "day_trade" and net_amount is not None and net_amount > 0:
        return "day_trade_no_chase", "隔日沖分點買超，明天九成會賣，不追"
    return _classify_broker_net(net_amount, consecutive, price_zone)


def foreign_streak(conn: sqlite3.Connection, stock_id: str,
                   date: str) -> tuple[float, int, float] | None:
    """date 之前外資個股買賣超（raw_chip __FOREIGN__）的 (最新淨張數, 連續天數, 累計張數)。

    08:50 產出時今日 T86 未收，故只看 date 之前；無資料回 None。最新淨額為 0 時天數 0。
    """
    rows = conn.execute(
        "SELECT net_volume FROM raw_chip "
        "WHERE stock_id = ? AND broker_name = '__FOREIGN__' "
        "      AND date < ? AND net_volume IS NOT NULL "
        "ORDER BY date DESC LIMIT 30",
        (stock_id, date),
    ).fetchall()
    if not rows:
        return None
    latest_net = rows[0][0]
    direction = 1 if latest_net > 0 else -1 if latest_net < 0 else 0
    streak, cum = 0, 0.0
    if direction != 0:
        for (net,) in rows:
            if net is None or net == 0 or (1 if net > 0 else -1) != direction:
                break
            streak += 1
            cum += net
    return latest_net, streak, cum


@dataclass(frozen=True)
class ForeignReading:
    """外資動向判讀；available=False 表尚無 T86 資料。"""

    available: bool
    latest_net: float | None = None
    streak: int = 0
    cum: float = 0.0
    category: str | None = None
    reason: str | None = None

    @property
    def direction(self) -> str:
        """up/down/flat。"""
        return CATEGORY_DIRECTION.get(self.category, "flat")


@dataclass(frozen=True)
class BrokerReading:
    """主力分點判讀（最顯著的一個分點）；available=False 表尚未匯入分點。"""

    available: bool
    data_date: str | None = None
    broker: str | None = None
    net: float | None = None
    category: str | None = None
    reason: str | None = None

    @property
    def direction(self) -> str:
        """up/down/flat。"""
        return CATEGORY_DIRECTION.get(self.category, "flat")


@dataclass(frozen=True)
class ZoneReading:
    """股價位置（收盤對 MA20 乖離 %）；from_kline 表無分點時由 K線趨勢乖離補。"""

    available: bool
    pct: float | None = None
    zone: str | None = None
    from_kline: bool = False


@dataclass(frozen=True)
class StockChip:
    """某檔個股某日的籌碼判讀三維度。"""

    stock_id: str
    date: str
    foreign: ForeignReading
    broker: BrokerReading
    zone: ZoneReading

    @property
    def lights(self) -> list[str]:
        """三顆燈（外資/分點/位置）：up/down/flat，缺資料為 na。"""
        return [
            self.foreign.direction if self.foreign.available else "na",
            self.broker.direction if self.broker.available else "na",
            "flat" if self.zone.available else "na",
        ]

    @property
    def lean(self) -> str:
        """籌碼傾向 bull(偏買)/bear(偏賣)/none(無傾向)：外資優先，分點輔助；位置不參與。"""
        f, b = self.foreign.direction, self.broker.direction
        if f == "up" or (b == "up" and f != "down"):
            return "bull"
        if f == "down" or b == "down":
            return "bear"
        return "none"


def _read_foreign(conn: sqlite3.Connection, stock_id: str, date: str) -> ForeignReading:
    s = foreign_streak(conn, stock_id, date)
    if s is None:
        return ForeignReading(available=False)
    latest_net, streak, cum = s
    direction = 1 if latest_net > 0 else -1 if latest_net < 0 else 0
    classified = classify_foreign(direction, streak, cum, latest_net) if direction else None
    category, reason = classified if classified else (None, None)
    return ForeignReading(True, latest_net, streak, cum, category, reason)


def _read_broker(conn: sqlite3.Connection, stock_id: str, date: str) -> BrokerReading:
    # 取該股 <= date 最近一個有分點資料的日期（剛匯入的也讀得到）
    row_date = conn.execute(
        "SELECT MAX(date) FROM daily_stock_metrics WHERE stock_id = ? AND date <= ?",
        (stock_id, date),
    ).fetchone()[0]
    if row_date is None:
        return BrokerReading(available=False)

    rows = conn.execute(
        "SELECT broker_name, net_amount, consecutive_days, price_zone, "
        "       both_sides_flag, broker_type "
        "FROM daily_stock_metrics WHERE stock_id = ? AND date = ?",
        (stock_id, row_date),
    ).fetchall()

    # 挑最顯著的分點：有分類者優先，再比 |淨額|
    best = None  # (key, broker, net, category, reason)
    for broker, net, consec, zone, both_sides, btype in rows:
        classified = classify_broker(net, consec, zone, both_sides, btype)
        cat, reason = classified if classified else (None, None)
        key = (1 if cat else 0, abs(net) if net is not None else 0)
        if best is None or key > best[0]:
            best = (key, broker, net, cat, reason)

    _, broker, net, cat, reason = best
    return BrokerReading(True, row_date, broker, net, cat, reason)


def _read_zone(conn: sqlite3.Connection, stock_id: str, date: str) -> ZoneReading:
    row = conn.execute(
        "SELECT price_vs_ma20, price_zone FROM daily_stock_metrics "
        "WHERE stock_id = ? AND date <= ? AND price_vs_ma20 IS NOT NULL "
        "ORDER BY date DESC LIMIT 1",
        (stock_id, date),
    ).fetchone()
    if row is not None and row[0] is not None:
        return ZoneReading(True, row[0], row[1])

    # 退回 K線趨勢的乖離（bias_pct 是還原後對 MA20 的乖離，比例）
    from integration.chip_metrics import classify_price_zone
    from integration.trend_signal import load_stock_trend

    trend = load_stock_trend(stock_id, conn, asof=date)
    if trend is not None and trend.bias_pct is not None:
        pct = trend.bias_pct * 100
        return ZoneReading(True, pct, classify_price_zone(pct), from_kline=True)

    return ZoneReading(available=False)


def load_stock_chip(date: str, stock_id: str, conn: sqlite3.Connection) -> StockChip:
    """讀取 stock_id 在 date 的籌碼判讀（外資動向／主力分點／股價位置）。"""
    return StockChip(
        stock_id=stock_id,
        date=date,
        foreign=_read_foreign(conn, stock_id, date),
        broker=_read_broker(conn, stock_id, date),
        zone=_read_zone(conn, stock_id, date),
    )
