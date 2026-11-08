"""Paper／券商模擬的交易成績（依成交打分）：讀 tradewind 回流的 events 與 account_snapshots，純讀取、不寫 DB、不動任何門檻。

- **一筆交易**＝(run_mode, account_id, target_id, 同日重跑序號 `-r{n}`) 的成交：第一筆成交的方向是進場方向，反方向的成交是出場。
  **已實現 R**＝Σ(出場口數 × 該筆成交的 r_multiple) ÷ 進場口數（tradewind 的 fill r_multiple＝(成交價 − 持倉成本) × 方向 ÷ stop_pts，
  是每口的 R；進場成交＝0）。口數還沒出完＝未平倉，不進統計。手續費與稅另以金額列（R 不扣成本，跟 tradewind 同口徑）。
- **出場原因**：該 target 最後一筆終局事件——override/early_exit → override；plan/invalidated → invalidated（Insight 的主張不成立）；
  plan/force_flat → force_flat（時間到）；plan/exited 的 reason：stop → stop、scaled_out_all／runner → target、superseded by … →
  superseded；其餘 → other（原文附上）。停損與失效分開算（tradewind review 2026-11-01）。
- **分組**：(run_mode, strategy_id, target 的 rule_version, playbook_id/version)；Paper 與實盤永不混算。同一組裡 engine／execution／
  risk／cost_model 版本不只一種 → mixed_versions（不同版本的結果不該混在一起看，tradewind 規則 4）。
- **Insight 對錯 × 結果**：盤前判斷的 target（inputs_ref.source_table＝signals）對到當天 verifications.hit_day，分開看 R。
- **帳戶**：account_snapshots 每天取最晚那筆（同日重跑有 -r{n}），算天數、期初期末權益、報酬、最大回撤。
樣本門檻沿用 settings.STATS_MIN_SAMPLE（純統計，不影響任何判斷，不必 bump 版本）。
"""

import logging
import re
import sqlite3
import statistics

from config import settings
from db.events import events_range, snapshots_range
from db.targets import target_get
from integration.call_stats import _rate_block

logger = logging.getLogger(__name__)

_RUN = re.compile(r"-r(\d+)$")
TERMINAL = {("plan", "exited"), ("plan", "force_flat"), ("plan", "invalidated"), ("override", "early_exit")}
TARGET_REASONS = {"scaled_out_all", "runner"}
_VERSION_KEYS = ("engine_version", "execution_version", "risk_version", "cost_model")


def _run_of(event_id: str) -> int:
    m = _RUN.search(event_id or "")
    return int(m.group(1)) if m else 1


def exit_reason(kind: str | None, action: str | None, reason: str | None) -> str:
    """終局事件 → 出場原因分類（見模組說明）。"""
    if kind == "override":
        return "override"
    if action in ("invalidated", "force_flat"):
        return action
    r = (reason or "").strip()
    if r == "stop":
        return "stop"
    if r in TARGET_REASONS:
        return "target"
    if r.startswith("superseded by"):
        return "superseded"
    return "other"


def build_trades(events: list[dict]) -> list[dict]:
    """events（含 fill、plan、override）→ 交易列表（依出場時刻；未平倉排最後）。"""
    fills: dict[tuple, list[dict]] = {}
    lifecycle: dict[tuple, list[dict]] = {}
    for e in events:
        if not e.get("target_id"):
            continue
        key = (e.get("run_mode"), e.get("account_id"), e["target_id"], _run_of(e.get("event_id", "")))
        if e.get("kind") == "fill":
            fills.setdefault(key, []).append(e)
        elif e.get("kind") in ("plan", "override"):
            lifecycle.setdefault(key, []).append(e)
    trades = []
    for key, fs in fills.items():
        fs = sorted(fs, key=lambda e: (e.get("ts") or "", e.get("event_id") or ""))
        side = fs[0].get("side")
        entry_qty = sum(int(f.get("qty") or 0) for f in fs if f.get("side") == side)
        exits = [f for f in fs if f.get("side") != side]
        exit_qty = sum(int(f.get("qty") or 0) for f in exits)
        bad = [f.get("event_id") for f in exits if not isinstance(f.get("r_multiple"), (int, float))]
        closed = entry_qty > 0 and exit_qty == entry_qty and not bad
        r = round(sum(int(f.get("qty") or 0) * float(f["r_multiple"]) for f in exits) / entry_qty, 4) if closed else None
        costs = sum(float((f.get("detail") or {}).get(k) or 0) for f in fs for k in ("commission", "tax"))
        life = sorted(lifecycle.get(key, []), key=lambda e: (e.get("ts") or "", e.get("event_id") or ""))
        term = [e for e in life if (e.get("kind"), e.get("action")) in TERMINAL]
        last = term[-1] if term else None
        versions = sorted({tuple((f.get("versions") or {}).get(k) for k in _VERSION_KEYS) for f in fs})
        trades.append({
            "run_mode": key[0], "account_id": key[1], "target_id": key[2], "run": key[3],
            "strategy_id": fs[0].get("strategy_id"), "instrument_id": fs[0].get("instrument_id"),
            "side": side, "entry_qty": entry_qty, "exit_qty": exit_qty,
            "status": "closed" if closed else ("inconsistent" if exit_qty > entry_qty or bad else "open"),
            "entry_ts": fs[0].get("ts"), "exit_ts": exits[-1].get("ts") if exits else None,
            "r": r, "costs": round(costs, 2),
            "exit_reason": exit_reason(last.get("kind"), last.get("action"), last.get("reason")) if (last and closed) else None,
            "exit_detail": last.get("reason") if last else None,
            "overrides": sum(1 for e in life if e.get("kind") == "override"),
            "versions": [dict(zip(_VERSION_KEYS, v)) for v in versions],
        })
        if exit_qty > entry_qty or bad:
            logger.warning("trade %s: exits %d > entries %d or exit fills without r_multiple %s; not scored",
                           key, exit_qty, entry_qty, bad[:3])
    return sorted(trades, key=lambda t: (t["status"] != "closed", t["exit_ts"] or t["entry_ts"] or "", t["target_id"]))


def _r_block(rs: list[float]) -> dict:
    """一串 R 的統計（空串回 n=0 與 None）。"""
    n = len(rs)
    wins, losses = [r for r in rs if r > 0], [r for r in rs if r < 0]
    cum, peak, mdd = 0.0, 0.0, 0.0
    streak = worst = 0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
        streak = streak + 1 if r < 0 else 0
        worst = max(worst, streak)
    gross_loss = -sum(losses)
    return {"n": n, "wins": len(wins), "losses": len(losses), "win_rate": _rate_block(len(wins), n),
            "avg_r": round(statistics.fmean(rs), 4) if rs else None,
            "avg_win_r": round(statistics.fmean(wins), 4) if wins else None,
            "avg_loss_r": round(statistics.fmean(losses), 4) if losses else None,
            "profit_factor": round(sum(wins) / gross_loss, 4) if gross_loss > 0 else None,
            "total_r": round(sum(rs), 4), "max_drawdown_r": round(mdd, 4), "max_losing_streak": worst}


def _insight(conn: sqlite3.Connection, target: dict | None) -> bool | None:
    """盤前判斷的 target：當天驗證的 hit_day（1→True、0→False）；不是盤前判斷或還沒驗證 → None。"""
    ref = (target or {}).get("inputs_ref") or {}
    day = (ref.get("key") or {}).get("date") if ref.get("source_table") == "signals" else None
    if not day:
        return None
    row = conn.execute("SELECT hit_day FROM verifications WHERE date = ?", (day,)).fetchone()
    return None if row is None or row[0] is None else bool(row[0])


def account_curves(snapshots: list[dict]) -> list[dict]:
    """每個 (run_mode, account_id)：每天最晚一筆快照的權益 → 天數、期初期末、報酬、最大回撤（比例）。"""
    by: dict[tuple, dict[str, dict]] = {}
    for s in snapshots:
        if not isinstance(s.get("equity"), (int, float)):
            continue
        day = (s.get("as_of") or "")[:10]
        cur = by.setdefault((s.get("run_mode"), s.get("account_id")), {})
        if day not in cur or (s.get("as_of") or "") >= (cur[day].get("as_of") or ""):
            cur[day] = s
    out = []
    for (mode, acct), days in sorted(by.items()):
        eq = [float(days[d]["equity"]) for d in sorted(days)]
        peak, mdd = eq[0], 0.0
        for v in eq:
            peak = max(peak, v)
            mdd = max(mdd, (peak - v) / peak if peak > 0 else 0.0)
        out.append({"run_mode": mode, "account_id": acct, "days": len(eq), "date_from": min(days), "date_to": max(days),
                    "start_equity": eq[0], "end_equity": eq[-1],
                    "return_pct": round((eq[-1] / eq[0] - 1) * 100, 4) if eq[0] > 0 else None,
                    "max_drawdown_pct": round(mdd * 100, 4)})
    return out


def build_trade_stats(conn: sqlite3.Connection, run_mode: str | None = None, strategy_id: str | None = None,
                      account_id: str | None = None, date_from: str | None = None, date_to: str | None = None,
                      trades_limit: int = 500) -> dict:
    """整份成績（/api/trade-stats）：分組統計、帳戶曲線、交易明細（新→舊，最多 trades_limit 筆）。"""
    events = events_range(conn, run_mode, date_from, date_to, kinds=("fill", "plan", "override"))
    trades = [t for t in build_trades(events)
              if (strategy_id is None or t["strategy_id"] == strategy_id) and (account_id is None or t["account_id"] == account_id)]
    targets: dict[str, dict | None] = {}
    for t in trades:
        if t["target_id"] not in targets:
            targets[t["target_id"]] = target_get(conn, t["target_id"])
        tg = targets[t["target_id"]] or {}
        plan = tg.get("trade_plan") or {}
        t["rule_version"] = tg.get("rule_version")
        t["playbook"] = f"{plan['playbook_id']}/{plan.get('playbook_version')}" if plan.get("playbook_id") else None
        t["insight_hit"] = _insight(conn, targets[t["target_id"]])
    groups: dict[tuple, list[dict]] = {}
    for t in trades:
        groups.setdefault((t["run_mode"], t["strategy_id"], t["rule_version"], t["playbook"]), []).append(t)
    out_groups = []
    for (mode, strat, rule, pb), ts in sorted(groups.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        closed = [t for t in ts if t["status"] == "closed"]
        rs = [t["r"] for t in closed]
        by_reason: dict[str, list[float]] = {}
        for t in closed:
            by_reason.setdefault(t["exit_reason"], []).append(t["r"])
        versions = sorted({tuple(v.get(k) for k in _VERSION_KEYS) for t in ts for v in t["versions"]}, key=str)
        out_groups.append({
            "run_mode": mode, "strategy_id": strat, "rule_version": rule, "playbook": pb,
            "accounts": sorted({t["account_id"] for t in ts}), "open_trades": sum(t["status"] == "open" for t in ts),
            "inconsistent_trades": sum(t["status"] == "inconsistent" for t in ts),
            "sufficient": len(closed) >= settings.STATS_MIN_SAMPLE, "costs": round(sum(t["costs"] for t in closed), 2),
            "r": _r_block(rs),
            "by_exit_reason": {k: _r_block(v) for k, v in sorted(by_reason.items())},
            "override": {"with": _r_block([t["r"] for t in closed if t["overrides"]]),
                         "without": _r_block([t["r"] for t in closed if not t["overrides"]])},
            "insight": {"hit": _r_block([t["r"] for t in closed if t["insight_hit"] is True]),
                        "miss": _r_block([t["r"] for t in closed if t["insight_hit"] is False])},
            "versions": [dict(zip(_VERSION_KEYS, v)) for v in versions], "mixed_versions": len(versions) > 1,
        })
    snaps = [s for s in snapshots_range(conn, run_mode, date_from, date_to) if account_id is None or s.get("account_id") == account_id]
    return {"filters": {"run_mode": run_mode, "strategy_id": strategy_id, "account_id": account_id,
                        "date_from": date_from, "date_to": date_to},
            "min_sample": settings.STATS_MIN_SAMPLE, "n_trades": len(trades), "groups": out_groups,
            "accounts": account_curves(snaps),
            "trades": sorted(trades, key=lambda t: (t["exit_ts"] or t["entry_ts"] or "", t["target_id"]), reverse=True)[:max(0, trades_limit)]}
