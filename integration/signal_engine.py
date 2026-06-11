"""Layer 3 訊號判斷引擎：市場訊號（偏多/偏空/中性 + 信心度 + 理由）與個股觀察訊號。

門檻全部來自 config/settings.py。調整門檻時記得 bump SIGNAL_RULE_VERSION。
"""

import json
import logging
import sqlite3
from datetime import datetime

from config import settings

logger = logging.getLogger(__name__)


def _futures_vote(spread_adjusted: float | None) -> str:
    """期貨票：調整後價差超過 ±FUTURES_SPREAD_THRESHOLD 才有方向。資料缺失視為中性。"""
    if spread_adjusted is None:
        return "neutral"
    if spread_adjusted >= settings.FUTURES_SPREAD_THRESHOLD:
        return "bullish"
    elif spread_adjusted <= -settings.FUTURES_SPREAD_THRESHOLD:
        return "bearish"
    else:
        return "neutral"


def _synthesize(fx_vote: str, futures_vote: str) -> tuple[str, int, str]:
    """合成兩票為（方向, 基礎信心, 理由）。兩票反向時強制中性。"""
    directional = {"bullish", "bearish"}
    if fx_vote in directional and futures_vote in directional:
        if fx_vote == futures_vote:
            return fx_vote, 3, "匯率與期貨同向"
        return "neutral", 1, "匯率與期貨多空分歧，強制中性"
    if fx_vote in directional:
        return fx_vote, 2, "僅匯率有方向，期貨中性"
    if futures_vote in directional:
        return futures_vote, 2, "僅期貨有方向，匯率中性"
    return "neutral", 2, "匯率與期貨皆中性"


def _krw_divergence(asia_detail: dict) -> bool:
    """台幣貶、人民幣貶、但韓元升 → 外資賣台買韓警示。"""
    return (
        asia_detail.get("TWD") == "bearish"
        and asia_detail.get("CNY") == "bearish"
        and asia_detail.get("KRW") == "bullish"
    )


def compute_market_signal(date: str, conn: sqlite3.Connection) -> dict | None:
    """從 daily_metrics 計算市場訊號，寫入 signals。回傳結果 dict 或 None（無指標資料）。"""
    row = conn.execute(
        "SELECT fx_direction, fx_asia_sync, fx_asia_detail, "
        "       futures_spread_adjusted, futures_volume_ratio, oi_net_foreign "
        "FROM daily_metrics WHERE date = ?",
        (date,),
    ).fetchone()

    if row is None:
        logger.warning("compute_market_signal: no daily_metrics for %s", date)
        return None

    (fx_direction, fx_asia_sync, fx_asia_detail_json,
     spread_adjusted, volume_ratio, oi_net) = row

    reasons = []

    # 兩票
    fx_vote = fx_direction if fx_direction is not None else "neutral"
    if fx_direction is None:
        reasons.append("匯率資料不可用，匯率票視為中性")
    futures_vote = _futures_vote(spread_adjusted)
    if spread_adjusted is None:
        reasons.append("期貨價差不可用，期貨票視為中性")

    direction, confidence, base_reason = _synthesize(fx_vote, futures_vote)
    reasons.append(base_reason)

    directional = direction in ("bullish", "bearish")

    # 加減分 1：亞幣同步
    if fx_asia_sync == 1 and directional and fx_vote == direction:
        confidence += 1
        reasons.append("亞幣同步且與訊號同向 +1")
    elif fx_asia_sync == 0 and fx_vote in ("bullish", "bearish"):
        confidence -= 1
        reasons.append("只有台幣在動（亞幣不同步），買盤恐不持續 -1")

    # 加減分 2：夜盤量比
    if volume_ratio is not None:
        if volume_ratio >= settings.VOLUME_RATIO_HIGH and futures_vote in ("bullish", "bearish"):
            confidence += 1
            reasons.append(f"夜盤爆量（{volume_ratio:.2f}x）且價差有方向，大戶佈局 +1")
        elif volume_ratio <= settings.VOLUME_RATIO_LOW:
            confidence -= 1
            reasons.append(f"夜盤量縮（{volume_ratio:.2f}x），市場觀望 -1")

    # 加減分 3：外資未平倉與訊號方向相逆
    if oi_net is not None:
        if direction == "bullish" and oi_net <= settings.OI_BEARISH_THRESHOLD:
            confidence -= 1
            reasons.append(f"外資淨空單 {oi_net:,} 口仍偏空 -1")
        elif direction == "bearish" and oi_net >= settings.OI_BULLISH_THRESHOLD:
            confidence -= 1
            reasons.append(f"外資淨多單 {oi_net:,} 口仍偏多 -1")

    # 加減分 4：KRW 背離警示
    asia_detail = json.loads(fx_asia_detail_json) if fx_asia_detail_json else {}
    if _krw_divergence(asia_detail):
        confidence -= 1
        reasons.append("警示：台幣/人民幣貶但韓元升，外資可能賣台買韓 -1")

    confidence = max(settings.CONFIDENCE_MIN,
                     min(settings.CONFIDENCE_MAX, confidence))

    result = {
        "date": date,
        "direction": direction,
        "confidence": confidence,
        "fx_vote": fx_vote,
        "futures_vote": futures_vote,
        "reasons": reasons,
        "rule_version": settings.SIGNAL_RULE_VERSION,
    }

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
        (date, direction, confidence, fx_vote, futures_vote,
         json.dumps(reasons, ensure_ascii=False),
         settings.SIGNAL_RULE_VERSION, datetime.now().isoformat()),
    )
    conn.commit()

    logger.info("Market signal for %s: %s (confidence=%d)",
                date, direction, confidence)
    return result


def _classify_stock(net_amount: float | None, consecutive: int | None,
                    price_zone: str | None) -> tuple[str, str] | None:
    """個股訊號分類，回傳 (category, reason) 或 None（不產生訊號）。"""
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


def compute_stock_signals(date: str, conn: sqlite3.Connection) -> list[dict]:
    """從 daily_stock_metrics 計算個股觀察訊號，寫入 stock_signals。"""
    rows = conn.execute(
        "SELECT stock_id, broker_name, net_amount, consecutive_days, "
        "       price_zone, both_sides_flag, broker_type "
        "FROM daily_stock_metrics WHERE date = ?",
        (date,),
    ).fetchall()

    if not rows:
        logger.info("compute_stock_signals: no stock metrics for %s", date)
        return []

    results = []
    now = datetime.now().isoformat()

    for (stock_id, broker_name, net_amount, consecutive,
         price_zone, both_sides, broker_type) in rows:

        # 過濾 1：對敲假量，直接標不碰
        if both_sides == 1:
            category, reason = "fake_volume", "同分點買賣兩邊都有量，疑似對敲假量，不碰"
        # 過濾 2：隔日沖分點買超，標不追
        elif broker_type == "day_trade" and net_amount is not None and net_amount > 0:
            category, reason = "day_trade_no_chase", "隔日沖分點買超，明天九成會賣，不追"
        else:
            classified = _classify_stock(net_amount, consecutive, price_zone)
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


_DIRECTION_LABELS = {"bullish": "↑ 偏多", "bearish": "↓ 偏空", "neutral": "— 中性"}


def format_signal_text(signal: dict) -> str:
    """把市場訊號格式化為可附加在 daily summary 後的文字區塊。"""
    label = _DIRECTION_LABELS.get(signal["direction"], signal["direction"])
    lines = [
        "【訊號判斷】",
        f"{label}  信心 {signal['confidence']}/5  (規則 {signal['rule_version']})",
    ]
    for r in signal["reasons"]:
        lines.append(f"  · {r}")
    return "\n".join(lines)
