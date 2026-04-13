"""開盤前情報文字摘要：從 daily_metrics + daily_stock_metrics 產出 human-readable 文字。"""

import json
import logging
import sqlite3

logger = logging.getLogger(__name__)


def _format_amount(amount: float | None) -> str:
    """格式化金額：超過 1 億用「億」，超過 1000 萬用「萬」。"""
    if amount is None:
        return "資料不可用"
    if abs(amount) >= 1e8:
        return f"{amount / 1e8:.2f}億"
    elif abs(amount) >= 1e7:
        return f"{amount / 1e4:.0f}萬"
    elif abs(amount) >= 1e4:
        return f"{amount / 1e4:.0f}萬"
    else:
        return f"{amount:,.0f}"


def _fx_arrow(delta: float | None) -> str:
    """匯率升值用 ▼（USD/TWD 下降 = 台幣升值），貶值用 ▲，平盤用 —。"""
    if delta is None:
        return "—"
    if delta < -0.001:
        return "▼升值"
    elif delta > 0.001:
        return "▲貶值"
    else:
        return "—平盤"


def _fx_short_arrow(direction: str | None) -> str:
    """短格式方向符號。"""
    if direction is None:
        return "?"
    return {"bullish": "↑", "bearish": "↓", "neutral": "—"}.get(direction, "?")


def generate_daily_summary(date: str, conn: sqlite3.Connection) -> str | None:
    """從 daily_metrics + daily_stock_metrics 讀取，格式化成文字摘要。"""
    # 讀取 daily_metrics
    row = conn.execute(
        "SELECT fx_delta_twd, fx_delta_cny, fx_delta_krw, "
        "       fx_direction, fx_asia_sync, fx_asia_detail, "
        "       futures_spread, futures_spread_adjusted, "
        "       futures_volume_ratio, oi_net_foreign, oi_delta "
        "FROM daily_metrics WHERE date = ?",
        (date,),
    ).fetchone()

    if row is None:
        logger.warning("generate_daily_summary: no daily_metrics for %s", date)
        return None

    (fx_twd, fx_cny, fx_krw, fx_dir, fx_sync, fx_detail_json,
     fut_spread, fut_adj, fut_ratio, oi_net, oi_delta) = row

    # 讀取 raw_fx 取得實際匯率值
    fx_rates = {}
    for pair in ["USD/TWD", "USD/CNY", "USD/KRW"]:
        r = conn.execute(
            "SELECT close_16, quote_0845 FROM raw_fx "
            "WHERE date = ? AND currency_pair = ?",
            (date, pair),
        ).fetchone()
        if r:
            fx_rates[pair] = {"close_16": r[0], "quote_0845": r[1]}

    # 讀取 raw_futures 取得實際值
    fut_row = conn.execute(
        "SELECT night_close, spot_close, ex_dividend_points "
        "FROM raw_futures WHERE date = ?",
        (date,),
    ).fetchone()
    night_close = fut_row[0] if fut_row else None
    spot_close = fut_row[1] if fut_row else None
    ex_div = fut_row[2] if fut_row else None

    lines = []
    lines.append("══════════════════════════════════")
    lines.append(f"  {date} 開盤前情報")
    lines.append("══════════════════════════════════")
    lines.append("")

    # === 匯率 ===
    lines.append("【匯率】")
    for pair, label, delta in [
        ("USD/TWD", "USD/TWD", fx_twd),
        ("USD/CNY", "USD/CNY", fx_cny),
        ("USD/KRW", "USD/KRW", fx_krw),
    ]:
        rates = fx_rates.get(pair, {})
        quote = rates.get("quote_0845")
        close = rates.get("close_16")
        if quote is not None:
            quote_str = f"{quote:.4f}" if pair != "USD/KRW" else f"{quote:.0f}"
        else:
            quote_str = "N/A"
        if close is not None:
            close_str = f"{close:.4f}" if pair != "USD/KRW" else f"{close:.0f}"
        else:
            close_str = "N/A"
        if delta is not None:
            delta_str = f"△{delta:+.4f}" if pair != "USD/KRW" else f"△{delta:+.1f}"
        else:
            delta_str = "△N/A"
        arrow = _fx_arrow(delta)
        lines.append(f"{label:8s} {quote_str} (前日 {close_str}) {delta_str} {arrow}")

    # 亞幣同步
    if fx_sync is not None:
        sync_label = "是" if fx_sync == 1 else "否"
        detail_str = ""
        if fx_detail_json:
            detail = json.loads(fx_detail_json)
            parts = []
            for k, v in detail.items():
                parts.append(f"{k}{_fx_short_arrow(v)}")
            detail_str = f" ({' '.join(parts)})"
        lines.append(f"亞幣同步：{sync_label}{detail_str}")
    else:
        lines.append("亞幣同步：資料不可用")
    lines.append("")

    # === 期貨 ===
    lines.append("【期貨】")
    if night_close is not None:
        lines.append(f"夜盤收盤  {night_close:.0f}  現貨收盤  "
                     f"{spot_close:.0f}" if spot_close else f"夜盤收盤  {night_close:.0f}  現貨收盤  資料不可用")
    else:
        lines.append("夜盤收盤  資料不可用")

    if fut_spread is not None:
        spread_line = f"價差 {fut_spread:+.1f}"
        if fut_adj is not None and ex_div and ex_div > 0:
            spread_line += f"  調整後 {fut_adj:+.1f} (除息 {ex_div:.1f})"
        elif fut_adj is not None:
            spread_line += f"  調整後 {fut_adj:+.1f}"
        lines.append(spread_line)
    else:
        lines.append("價差  資料不可用")

    if fut_ratio is not None:
        lines.append(f"夜盤量比 {fut_ratio:.2f}x")
    else:
        lines.append("夜盤量比  資料不可用")

    if oi_net is not None:
        oi_line = f"外資未平倉：{oi_net:,}"
        if oi_delta is not None:
            oi_line += f" (△{oi_delta:+,})"
        lines.append(oi_line)
    else:
        lines.append("外資未平倉：資料不可用")
    lines.append("")

    # === 籌碼觀察 ===
    lines.append("【籌碼觀察】")

    # 從 watchlist 取得觀察名單
    watchlist = conn.execute(
        "SELECT stock_id, stock_name FROM watchlist ORDER BY stock_id"
    ).fetchall()

    if not watchlist:
        lines.append("  （觀察名單為空）")
    else:
        for stock_id, stock_name in watchlist:
            lines.append(f"{stock_name}({stock_id})")

            metrics = conn.execute(
                "SELECT broker_name, net_amount, consecutive_days, "
                "       price_zone, broker_type "
                "FROM daily_stock_metrics "
                "WHERE date = ? AND stock_id = ? "
                "ORDER BY ABS(net_amount) DESC",
                (date, stock_id),
            ).fetchall()

            if not metrics:
                lines.append("  無今日資料")
            else:
                for broker, net_amt, consec, zone, btype in metrics:
                    type_label = f" [{btype}]" if btype else ""
                    amt_str = _format_amount(net_amt)
                    if net_amt is not None and net_amt >= 0:
                        action = "買超"
                    else:
                        action = "賣超"

                    consec_str = ""
                    if consec is not None and consec != 0:
                        direction = "買" if consec > 0 else "賣"
                        consec_str = f" 連{direction}{abs(consec)}天"

                    zone_str = f" 股價位置:{zone}" if zone else ""

                    lines.append(
                        f"  {broker}{type_label} {action} {amt_str}"
                        f"{consec_str}{zone_str}"
                    )

    lines.append("")
    lines.append("══════════════════════════════════")

    return "\n".join(lines)
