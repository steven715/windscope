"""Layer 3 盤前判斷：兩票合成方向、信心調整、理由（立場，計入命中率）。

一個交易日的 daily_metrics 進、一份盤前判斷出（純函式 evaluate_premarket_call）；
每個維度除了對方向／信心的「效果」，也帶「級別」代碼，供解讀（explain）排版判讀——
門檻只在這裡比一次，判讀與信心調整不會分岔。判讀措辭屬詮釋，由 explain 負責。

門檻全部來自 config/settings.py。調整門檻時記得 bump SIGNAL_RULE_VERSION。
"""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from config import settings

logger = logging.getLogger(__name__)

DIRECTION_LABELS = {"bullish": "↑ 偏多", "bearish": "↓ 偏空", "neutral": "— 中性"}

_DIRECTIONAL = ("bullish", "bearish")

# 效果為 0 時「為什麼沒計入」的原因代碼
SKIP_FX_VOTE_NEUTRAL = "fx_vote_neutral"          # 匯率票中性
SKIP_FUTURES_VOTE_NEUTRAL = "futures_vote_neutral"  # 期貨票中性
SKIP_DIRECTION_NEUTRAL = "direction_neutral"      # 盤前判斷方向為中性
SKIP_ALIGNED = "aligned"                          # 與盤前判斷方向同向，不調整


@dataclass(frozen=True)
class DailyMetrics:
    """某交易日的盤前衍生指標（daily_metrics 一列）；fx_asia_detail 已解析成 dict。"""

    date: str
    fx_delta_twd: float | None = None
    fx_delta_cny: float | None = None
    fx_delta_krw: float | None = None
    fx_direction: str | None = None
    fx_asia_sync: int | None = None
    fx_asia_detail: dict | None = None
    futures_spread: float | None = None
    futures_spread_adjusted: float | None = None
    futures_volume_ratio: float | None = None
    oi_net_foreign: int | None = None
    oi_delta: int | None = None


_METRIC_COLUMNS = (
    "fx_delta_twd", "fx_delta_cny", "fx_delta_krw", "fx_direction",
    "fx_asia_sync", "fx_asia_detail", "futures_spread", "futures_spread_adjusted",
    "futures_volume_ratio", "oi_net_foreign", "oi_delta",
)


def load_daily_metrics(date: str, conn: sqlite3.Connection) -> DailyMetrics | None:
    """讀取 date 的 daily_metrics（唯一讀取入口）。無該日資料回 None。"""
    row = conn.execute(
        f"SELECT {', '.join(_METRIC_COLUMNS)} FROM daily_metrics WHERE date = ?",
        (date,),
    ).fetchone()
    if row is None:
        return None
    values = dict(zip(_METRIC_COLUMNS, row))
    detail = values["fx_asia_detail"]
    values["fx_asia_detail"] = json.loads(detail) if detail else None
    return DailyMetrics(date=date, **values)


@dataclass(frozen=True)
class Adjustment:
    """單一維度的信心調整。level 為級別代碼（None＝資料不足）；effect 為 +1／-1／0；
    effect 為 0 且級別本可調整時，skip 給未計入原因代碼。"""

    level: str | None
    effect: int = 0
    skip: str | None = None


@dataclass(frozen=True)
class PremarketCall:
    """一個交易日的盤前判斷：方向、信心、兩票、理由，加上各信心調整維度的級別與效果。"""

    date: str
    direction: str
    confidence: int
    fx_vote: str
    futures_vote: str
    reasons: tuple[str, ...]
    rule_version: str
    asia_sync: Adjustment       # level: sync／diverge
    night_volume: Adjustment    # level: high／normal／low
    foreign_oi: Adjustment      # level: net_short／neutral／net_long
    krw_divergence: Adjustment  # level: divergence／none
    metrics: DailyMetrics

    def as_signal_dict(self) -> dict:
        """轉成 signals 列／TG 摘要用的 dict（reasons 為 list）。"""
        return {
            "date": self.date,
            "direction": self.direction,
            "confidence": self.confidence,
            "fx_vote": self.fx_vote,
            "futures_vote": self.futures_vote,
            "reasons": list(self.reasons),
            "rule_version": self.rule_version,
        }


def _futures_vote(spread_adjusted: float | None) -> str:
    """期貨票：調整後價差超過 ±FUTURES_SPREAD_THRESHOLD 才有方向。資料缺失視為中性。"""
    if spread_adjusted is None:
        return "neutral"
    if spread_adjusted >= settings.FUTURES_SPREAD_THRESHOLD:
        return "bullish"
    if spread_adjusted <= -settings.FUTURES_SPREAD_THRESHOLD:
        return "bearish"
    return "neutral"


def _synthesize(fx_vote: str, futures_vote: str) -> tuple[str, int, str]:
    """合成兩票為（方向, 基礎信心, 理由）。兩票反向時強制中性。"""
    if fx_vote in _DIRECTIONAL and futures_vote in _DIRECTIONAL:
        if fx_vote == futures_vote:
            return fx_vote, 3, "匯率與期貨同向"
        return "neutral", 1, "匯率與期貨多空分歧，強制中性"
    if fx_vote in _DIRECTIONAL:
        return fx_vote, 2, "僅匯率有方向，期貨中性"
    if futures_vote in _DIRECTIONAL:
        return futures_vote, 2, "僅期貨有方向，匯率中性"
    return "neutral", 2, "匯率與期貨皆中性"


def _asia_sync(sync: int | None, fx_vote: str, direction: str) -> Adjustment:
    """亞幣同步且匯率票與方向同向 +1；不同步而匯率票有方向 -1。"""
    if sync == 1:
        if direction in _DIRECTIONAL and fx_vote == direction:
            return Adjustment("sync", +1)
        if fx_vote not in _DIRECTIONAL:
            return Adjustment("sync", 0, SKIP_FX_VOTE_NEUTRAL)
        return Adjustment("sync", 0, SKIP_DIRECTION_NEUTRAL)  # 兩票分歧被強制中性
    if sync == 0:
        if fx_vote in _DIRECTIONAL:
            return Adjustment("diverge", -1)
        return Adjustment("diverge", 0, SKIP_FX_VOTE_NEUTRAL)
    return Adjustment(None)


def _night_volume(ratio: float | None, futures_vote: str) -> Adjustment:
    """夜盤爆量且期貨票有方向 +1；量縮 -1。"""
    if ratio is None:
        return Adjustment(None)
    if ratio >= settings.VOLUME_RATIO_HIGH:
        if futures_vote in _DIRECTIONAL:
            return Adjustment("high", +1)
        return Adjustment("high", 0, SKIP_FUTURES_VOTE_NEUTRAL)
    if ratio <= settings.VOLUME_RATIO_LOW:
        return Adjustment("low", -1)
    return Adjustment("normal")


def _foreign_oi(oi_net: int | None, direction: str) -> Adjustment:
    """外資未平倉與方向相逆 -1；同向或方向中性不調整（籌碼沒有票）。"""
    if oi_net is None:
        return Adjustment(None)
    if oi_net <= settings.OI_BEARISH_THRESHOLD:
        level, against = "net_short", "bullish"
    elif oi_net >= settings.OI_BULLISH_THRESHOLD:
        level, against = "net_long", "bearish"
    else:
        return Adjustment("neutral")
    if direction == against:
        return Adjustment(level, -1)
    if direction in _DIRECTIONAL:
        return Adjustment(level, 0, SKIP_ALIGNED)
    return Adjustment(level, 0, SKIP_DIRECTION_NEUTRAL)


def _krw_divergence(asia_detail: dict | None) -> Adjustment:
    """台幣貶、人民幣貶、但韓元升 → 外資賣台買韓警示 -1。"""
    if not asia_detail:
        return Adjustment(None)
    if (asia_detail.get("TWD") == "bearish"
            and asia_detail.get("CNY") == "bearish"
            and asia_detail.get("KRW") == "bullish"):
        return Adjustment("divergence", -1)
    return Adjustment("none")


def evaluate_premarket_call(m: DailyMetrics) -> PremarketCall:
    """純函式：由一日指標算出盤前判斷（不碰 DB）。"""
    reasons: list[str] = []

    fx_vote = m.fx_direction if m.fx_direction is not None else "neutral"
    if m.fx_direction is None:
        reasons.append("匯率資料不可用，匯率票視為中性")
    futures_vote = _futures_vote(m.futures_spread_adjusted)
    if m.futures_spread_adjusted is None:
        reasons.append("期貨價差不可用，期貨票視為中性")

    direction, confidence, base_reason = _synthesize(fx_vote, futures_vote)
    reasons.append(base_reason)

    asia = _asia_sync(m.fx_asia_sync, fx_vote, direction)
    if asia.effect > 0:
        reasons.append("亞幣同步且與訊號同向 +1")
    elif asia.effect < 0:
        reasons.append("只有台幣在動（亞幣不同步），買盤恐不持續 -1")

    volume = _night_volume(m.futures_volume_ratio, futures_vote)
    if volume.effect > 0:
        reasons.append(f"夜盤爆量（{m.futures_volume_ratio:.2f}x）且價差有方向，大戶佈局 +1")
    elif volume.effect < 0:
        reasons.append(f"夜盤量縮（{m.futures_volume_ratio:.2f}x），市場觀望 -1")

    oi = _foreign_oi(m.oi_net_foreign, direction)
    if oi.effect < 0:
        side = "淨空單" if oi.level == "net_short" else "淨多單"
        lean = "偏空" if oi.level == "net_short" else "偏多"
        reasons.append(f"外資{side} {m.oi_net_foreign:,} 口仍{lean} -1")

    krw = _krw_divergence(m.fx_asia_detail)
    if krw.effect < 0:
        reasons.append("警示：台幣/人民幣貶但韓元升，外資可能賣台買韓 -1")

    confidence += asia.effect + volume.effect + oi.effect + krw.effect
    confidence = max(settings.CONFIDENCE_MIN, min(settings.CONFIDENCE_MAX, confidence))

    return PremarketCall(
        date=m.date, direction=direction, confidence=confidence,
        fx_vote=fx_vote, futures_vote=futures_vote, reasons=tuple(reasons),
        rule_version=settings.SIGNAL_RULE_VERSION,
        asia_sync=asia, night_volume=volume, foreign_oi=oi, krw_divergence=krw,
        metrics=m,
    )


def compute_premarket_call(date: str, conn: sqlite3.Connection) -> dict | None:
    """算 date 的盤前判斷並寫入 signals。回傳 signals 列 dict，無指標資料回 None。"""
    metrics = load_daily_metrics(date, conn)
    if metrics is None:
        logger.warning("compute_premarket_call: no daily_metrics for %s", date)
        return None

    call = evaluate_premarket_call(metrics)
    conn.execute(
        """INSERT INTO signals
               (date, direction, confidence, fx_vote, futures_vote,
                reasons, rule_version, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(date) DO UPDATE SET
               direction = excluded.direction,
               confidence = excluded.confidence,
               fx_vote = excluded.fx_vote,
               futures_vote = excluded.futures_vote,
               reasons = excluded.reasons,
               rule_version = excluded.rule_version,
               created_at = excluded.created_at""",
        (date, call.direction, call.confidence, call.fx_vote, call.futures_vote,
         json.dumps(list(call.reasons), ensure_ascii=False),
         call.rule_version, datetime.now().isoformat()),
    )
    conn.commit()

    logger.info("Pre-market call for %s: %s (confidence=%d)",
                date, call.direction, call.confidence)
    return call.as_signal_dict()
