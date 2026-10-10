"""盤前輸入：某交易日盤前判斷與解讀所依據的原始值集合（含基準日與出處）。

唯一入口 load_premarket_inputs：「這天的基準是哪個數字」只在這裡決定（含
fallback），票（fx/futures_metrics）、解讀（explain）、摘要（summary）都讀同一份，
避免各自 SQL 造成顯示與立場分岔。讀不到的值一律 None，不拋例外。純讀，不寫 DB。
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from config import settings
from utils.trading_calendar import get_previous_trading_day

FX_PAIRS = ("USD/TWD", "USD/CNY", "USD/KRW", "USD/JPY")

# 前日現貨出處
SPOT_FROM_FUTURES = "spot_close"   # raw_futures.spot_close（FMTQIK）
SPOT_FROM_INDEX = "index_close"    # raw_index.close（MI_5MINS_HIST，fallback）


@dataclass(frozen=True)
class PremarketInputs:
    """某日盤前輸入快照；欄位缺料為 None。dict 欄位為唯讀 MappingProxy。"""

    date: str
    prev_day: str | None
    # 匯率：只含當日有 raw_fx row 的幣別（值可為 None）；前日 16:00 收盤基準
    fx_quote_0845: Mapping[str, float | None] = field(default_factory=dict)
    fx_prev_close_16: Mapping[str, float | None] = field(default_factory=dict)
    # 台幣離岸晨間（intraday_fx 最後一根 5 分 K）：今晨／前一交易日晨
    twd_offshore_today: float | None = None
    twd_offshore_prev: float | None = None
    # 期貨：當日 raw_futures row 是否存在＋夜盤值
    has_futures_row: bool = False
    night_close: float | None = None
    night_volume: int | None = None
    ex_dividend_points: float | None = None
    # 前一交易日現貨收盤（期現價差基準）與出處
    prev_spot: float | None = None
    prev_spot_source: str | None = None
    # 近 N 日（FUTURES_VOLUME_LOOKBACK）夜盤量，新到舊
    recent_night_volumes: tuple[int, ...] = ()
    # 外資未平倉：前一交易日／再前一交易日
    oi_prev: int | None = None
    oi_prev2: int | None = None
    # S&P 500 收盤：當日 row／前一交易日 row
    sp500_today: float | None = None
    sp500_prev: float | None = None

    @property
    def night_volume_avg(self) -> float | None:
        """近 N 日夜盤均量；無資料為 None。"""
        v = self.recent_night_volumes
        return sum(v) / len(v) if v else None


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple) -> object | None:
    """單值查詢；無 row 回 None。"""
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def _offshore_twd_morning(conn: sqlite3.Connection, date: str) -> float | None:
    """某日離岸 USD/TWD 晨間最後一根 5 分 K 收盤（≈08:50）。

    在岸台銀 08:45 牌價開盤前未更新＝前一日收盤，隔夜 delta 失真，故 TWD 用離岸值。
    """
    return _scalar(
        conn,
        "SELECT close FROM intraday_fx WHERE date = ? AND currency_pair = 'USD/TWD' "
        "ORDER BY ts DESC LIMIT 1",
        (date,),
    )


def _prev_spot(conn: sqlite3.Connection, prev_day: str) -> tuple[float | None, str | None]:
    """前一交易日現貨收盤與出處。

    FMTQIK 偶爾當晚回「無資料」使 spot_close 留 NULL → fallback 到同為加權指數收盤、
    更早（14:30）收到的 raw_index.close。
    """
    spot = _scalar(conn, "SELECT spot_close FROM raw_futures WHERE date = ?", (prev_day,))
    if spot is not None:
        return spot, SPOT_FROM_FUTURES
    idx = _scalar(conn, "SELECT close FROM raw_index WHERE date = ?", (prev_day,))
    if idx is not None:
        return idx, SPOT_FROM_INDEX
    return None, None


def load_premarket_inputs(date: str, conn: sqlite3.Connection) -> PremarketInputs:
    """讀取 date 的盤前輸入快照（基準日＝前一交易日，含 fallback 與出處）。"""
    prev = get_previous_trading_day(date)

    fx_quote = {}
    fx_prev = {}
    for pair in FX_PAIRS:
        row = conn.execute(
            "SELECT quote_0845 FROM raw_fx WHERE date = ? AND currency_pair = ?",
            (date, pair),
        ).fetchone()
        if row:
            fx_quote[pair] = row[0]
        fx_prev[pair] = _scalar(
            conn,
            "SELECT close_16 FROM raw_fx WHERE date = ? AND currency_pair = ?",
            (prev, pair),
        ) if prev else None

    fut = conn.execute(
        "SELECT night_close, night_volume, ex_dividend_points, sp500_close "
        "FROM raw_futures WHERE date = ?",
        (date,),
    ).fetchone()
    night_close, night_volume, ex_div, sp500_today = fut if fut else (None,) * 4

    recent = conn.execute(
        "SELECT night_volume FROM raw_futures WHERE date < ? AND night_volume IS NOT NULL "
        "ORDER BY date DESC LIMIT ?",
        (date, settings.FUTURES_VOLUME_LOOKBACK),
    ).fetchall()

    prev_spot, prev_spot_source = _prev_spot(conn, prev) if prev else (None, None)

    oi_prev = oi_prev2 = sp500_prev = None
    if prev:
        oi_prev = _scalar(conn, "SELECT oi_net_foreign FROM raw_futures WHERE date = ?", (prev,))
        sp500_prev = _scalar(conn, "SELECT sp500_close FROM raw_futures WHERE date = ?", (prev,))
        prev2 = get_previous_trading_day(prev)
        if prev2:
            oi_prev2 = _scalar(
                conn, "SELECT oi_net_foreign FROM raw_futures WHERE date = ?", (prev2,))

    return PremarketInputs(
        date=date,
        prev_day=prev,
        fx_quote_0845=MappingProxyType(fx_quote),
        fx_prev_close_16=MappingProxyType(fx_prev),
        twd_offshore_today=_offshore_twd_morning(conn, date),
        twd_offshore_prev=_offshore_twd_morning(conn, prev) if prev else None,
        has_futures_row=fut is not None,
        night_close=night_close,
        night_volume=night_volume,
        ex_dividend_points=ex_div,
        prev_spot=prev_spot,
        prev_spot_source=prev_spot_source,
        recent_night_volumes=tuple(r[0] for r in recent),
        oi_prev=oi_prev,
        oi_prev2=oi_prev2,
        sp500_today=sp500_today,
        sp500_prev=sp500_prev,
    )
