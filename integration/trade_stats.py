"""Paper／券商模擬的交易成績（依成交打分）：讀 tradewind 回流的 events 與 account_snapshots，純讀取、不寫 DB、不動任何門檻。

**一筆交易＝一段部位**（不是一個 target）：同一個 (run_mode, account_id, instrument_id) 的成交依時間重放淨部位，從空手到空手是一筆。
tradewind 的成交不一定掛在開倉的 target 上——風控／kill switch 的全部平倉、換月、殘留部位的平倉單沒有 target（target_id 空、
r_multiple 0）；趨勢訊號加碼、減碼時，execution 把舊 target 歸零與新 target 進場淨額成一張單、掛在新的 target 上——所以只能看部位。
- **同日重跑**：每個 (run_mode, account_id, 交易日) 只留最後一次執行（`-r{n}` 最大）的成交；重跑從前一天的狀態重來，前一次的紀錄已被取代
  （與 tradewind research/experiments 同口徑）。
- **R**＝Σ出場（成交價 − 當下平均成本）× 方向 × 口數 ÷（開倉 target 的 trade_plan.risk.stop_pts × 進場總口數）。單一 target、沒加碼時
  ＝tradewind 每筆出場成交 r_multiple 的口數加權（tradewind 用不被結算重設的平均成本）；加碼過的部位以開倉 target 的 1R 計。
  **損益**另以金額列：毛損益（× 主檔乘數）與手續費＋稅、淨損益。R 不扣成本，跟 tradewind 同口徑。
- **出場原因**：最後一筆出場成交 → 有 target：那個 target 在這筆成交之前最近一筆出場類事件（plan exited／force_flat／invalidated／
  scaled_out、override early_exit／reduce）；沒有 target：成交的 order_reason（flatten、roll、orphan…）。停損與失效分開算。
- **沒有成交就消失的部位**（到期結算、券商端處理）：之後的帳戶快照已經沒有這個部位 → `closed_without_fill`，不打分。
- **日期區間**：只取「結束日」在區間內的交易，但重放用區間結束日以前的所有成交（跨日交易不會被切半）。
- **分組**：(run_mode, 策略, 規則版本, Playbook, 版本〔engine／execution／risk／cost_model〕)——Paper 與實盤永不混算，不同版本也不混
  （tradewind 規則 4）；一筆交易跨了版本 → 那筆標 mixed_versions，歸到結束那筆成交的版本。
- **Insight 對錯**：盤前判斷的 target（inputs_ref.source_table＝signals）——開倉方向（target_weight 正負）對當天 verifications 的
  day_change_class：同向＝hit、反向＝miss、平盤＝flat（不用 hit_day：目標鎖定後判斷重算過時，hit_day 對的是新的判斷）。
- **帳戶**：account_snapshots 每天最晚一筆；基準＝區間前最後一筆（沒有就區間第一天收盤，標 first_day_pnl_excluded）。
  windscope 沒有入出金帳（cash_events 不回流），所以報酬與回撤**含入出金**（includes_cash_flows）。
樣本門檻沿用 settings.STATS_MIN_SAMPLE（純統計，不影響任何判斷，不必 bump 版本）。
"""

import logging
import re
import sqlite3
import statistics

from config import settings
from db.events import events_range, snapshots_range
from db.instruments import instrument_get
from db.targets import target_get
from integration.call_stats import _rate_block

logger = logging.getLogger(__name__)

_RUN = re.compile(r"-r(\d+)$")
EXIT_EVENTS = {("plan", "exited"), ("plan", "force_flat"), ("plan", "invalidated"), ("plan", "scaled_out"),
               ("override", "early_exit"), ("override", "reduce")}
TARGET_REASONS = {"scaled_out_all", "runner"}
_VERSION_KEYS = ("engine_version", "execution_version", "risk_version", "cost_model")


def _run_of(event_id: str) -> int:
    m = _RUN.search(event_id or "")
    return int(m.group(1)) if m else 1


def exit_reason(kind: str | None, action: str | None, reason: str | None) -> str:
    """有 target 的出場事件 → 出場原因分類。"""
    if kind == "override":
        return "override"
    if action in ("invalidated", "force_flat"):
        return action
    if action == "scaled_out":
        return "target"
    r = (reason or "").strip()
    if r == "stop":
        return "stop"
    if r in TARGET_REASONS:
        return "target"
    if r.startswith("superseded by"):
        return "superseded"
    if r == "roll":
        return "roll"
    return "other"


def system_exit_reason(order_reason: str | None) -> str:
    """沒有 target 的出場成交（engine 自己送的平倉單）→ 原因。"""
    r = (order_reason or "").strip()
    return r if r in ("flatten", "roll", "orphan") else "system"


def latest_runs(events: list[dict]) -> list[dict]:
    """每個 (run_mode, account_id, 交易日) 只留最後一次執行的事件（同日重跑）。"""
    last: dict[tuple, int] = {}
    for e in events:
        k = (e.get("run_mode"), e.get("account_id"), e.get("_session_date") or (e.get("ts") or "")[:10])
        last[k] = max(last.get(k, 0), _run_of(e.get("event_id", "")))
    return [e for e in events
            if _run_of(e.get("event_id", "")) == last[(e.get("run_mode"), e.get("account_id"),
                                                       e.get("_session_date") or (e.get("ts") or "")[:10])]]


def _versions(f: dict) -> tuple:
    return tuple((f.get("versions") or {}).get(k) for k in _VERSION_KEYS)


def build_episodes(events: list[dict]) -> list[dict]:
    """事件（已去掉被取代的重跑）→ 部位段落（依 (run_mode, account, instrument) 重放淨部位；翻向＝先平再開）。"""
    fills: dict[tuple, list[dict]] = {}
    for e in events:
        if e.get("kind") != "fill":
            continue
        if e.get("side") not in ("buy", "sell") or not isinstance(e.get("qty"), (int, float)) or not isinstance(e.get("price"), (int, float)):
            logger.warning("fill %s without side/qty/price; skipped", e.get("event_id"))
            continue
        fills.setdefault((e.get("run_mode"), e.get("account_id"), e.get("instrument_id")), []).append(e)
    out = []
    for (mode, acct, iid), fs in fills.items():
        fs.sort(key=lambda f: (f.get("ts") or "", f.get("event_id") or ""))
        pos, cur = 0, None
        for f in fs:
            signed = int(f["qty"]) * (1 if f["side"] == "buy" else -1)
            while signed:
                if pos == 0:                                          # 開新的一段
                    cur = {"run_mode": mode, "account_id": acct, "instrument_id": iid, "direction": 1 if signed > 0 else -1,
                           "open_fill": f, "entries": [], "exits": [], "targets": [], "avg_cost": 0.0, "entry_qty": 0,
                           "max_qty": 0, "gross_pts": 0.0, "costs": 0.0, "versions": set(), "closed": False}
                    out.append(cur)
                same = (signed > 0) == (cur["direction"] > 0)
                q = abs(signed) if same else min(abs(signed), abs(pos))
                if same:                                              # 進場／加碼：平均成本
                    cur["avg_cost"] = (cur["avg_cost"] * abs(pos) + float(f["price"]) * q) / (abs(pos) + q)
                    cur["entry_qty"] += q
                    pos += q * cur["direction"]
                    cur["entries"].append(f)
                else:                                                 # 出場（翻向時只算平掉的那部分）
                    cur["gross_pts"] += (float(f["price"]) - cur["avg_cost"]) * cur["direction"] * q
                    pos -= q * cur["direction"]
                    cur["exits"].append(f)
                cur["max_qty"] = max(cur["max_qty"], abs(pos))
                if f.get("target_id") and f["target_id"] not in cur["targets"]:
                    cur["targets"].append(f["target_id"])
                share = q / int(f["qty"])
                det = f.get("detail") or {}
                cur["costs"] += share * sum(float(det.get(k) or 0) for k in ("commission", "tax"))
                cur["versions"].add(_versions(f))
                signed -= q * (1 if signed > 0 else -1)
                if pos == 0:
                    cur["closed"] = True
    return out


def _last_exit_reason(ep: dict, life: dict[str, list[dict]]) -> tuple[str | None, str | None]:
    """（分類, 原文）：最後一筆出場成交是誰造成的。"""
    f = ep["exits"][-1]
    tid = f.get("target_id")
    if not tid:
        why = (f.get("detail") or {}).get("order_reason")
        return system_exit_reason(why), why
    cands = [e for e in life.get(tid, []) if (e.get("kind"), e.get("action")) in EXIT_EVENTS and (e.get("ts") or "") <= (f.get("ts") or "")]
    if not cands:
        return "other", None
    e = max(cands, key=lambda x: (x.get("ts") or "", x.get("event_id") or ""))
    return exit_reason(e.get("kind"), e.get("action"), e.get("reason")), e.get("reason")


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


def _insight(conn: sqlite3.Connection, target: dict | None, direction: int) -> str | None:
    """盤前判斷的 target：開倉方向 vs 當天實際漲跌分類 → hit／miss／flat；不是盤前判斷或還沒驗證 → None。"""
    ref = (target or {}).get("inputs_ref") or {}
    day = (ref.get("key") or {}).get("date") if ref.get("source_table") == "signals" else None
    if not day:
        return None
    row = conn.execute("SELECT day_change_class FROM verifications WHERE date = ?", (day,)).fetchone()
    if row is None or row[0] not in ("up", "down", "flat"):
        return None
    if row[0] == "flat":
        return "flat"
    return "hit" if (row[0] == "up") == (direction > 0) else "miss"


def account_curves(snapshots: list[dict], date_from: str | None = None) -> list[dict]:
    """每個 (run_mode, account_id)：每天最晚一筆快照的權益 → 基準、期末、報酬、最大回撤（比例）。snapshots 要含區間前的（找基準）。"""
    by: dict[tuple, dict[str, dict]] = {}
    for s in snapshots:
        if not isinstance(s.get("equity"), (int, float)):
            continue
        day = (s.get("as_of") or "")[:10]
        cur = by.setdefault((s.get("run_mode"), s.get("account_id")), {})
        if day not in cur or (s.get("as_of") or "") >= (cur[day].get("as_of") or ""):
            cur[day] = s
    out = []
    for (mode, acct), days in sorted(by.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        before = [d for d in sorted(days) if date_from and d < date_from]
        inside = [d for d in sorted(days) if not date_from or d >= date_from]
        if not inside:
            continue
        base_day = before[-1] if before else inside[0]
        series = [float(days[base_day]["equity"])] + [float(days[d]["equity"]) for d in inside if d != base_day]
        peak, mdd = series[0], 0.0
        for v in series:
            peak = max(peak, v)
            mdd = max(mdd, (peak - v) / peak if peak > 0 else 0.0)
        out.append({"run_mode": mode, "account_id": acct, "days": len(inside), "date_from": inside[0], "date_to": inside[-1],
                    "base_date": base_day, "start_equity": series[0], "end_equity": series[-1],
                    "return_pct": round((series[-1] / series[0] - 1) * 100, 4) if series[0] > 0 else None,
                    "max_drawdown_pct": round(mdd * 100, 4), "first_day_pnl_excluded": not before, "includes_cash_flows": True})
    return out


def build_trade_stats(conn: sqlite3.Connection, run_mode: str | None = None, strategy_id: str | None = None,
                      account_id: str | None = None, date_from: str | None = None, date_to: str | None = None,
                      trades_limit: int = 500) -> dict:
    """整份成績（/api/trade-stats）：分組統計、帳戶曲線、交易明細（新→舊，最多 trades_limit 筆）。"""
    events = latest_runs(events_range(conn, run_mode, None, date_to, kinds=("fill", "plan", "override")))   # 重放要從頭
    if account_id:
        events = [e for e in events if e.get("account_id") == account_id]
    life: dict[str, list[dict]] = {}
    for e in events:
        if e.get("kind") in ("plan", "override") and e.get("target_id"):
            life.setdefault(e["target_id"], []).append(e)
    snaps_all = snapshots_range(conn, run_mode, None, date_to)
    if account_id:
        snaps_all = [s for s in snaps_all if s.get("account_id") == account_id]
    targets: dict[str, dict | None] = {}
    mults: dict[str, float] = {}
    trades = []
    for ep in build_episodes(events):
        f0 = ep["open_fill"]
        tid = f0.get("target_id") or (ep["targets"][0] if ep["targets"] else None)
        if tid and tid not in targets:
            targets[tid] = target_get(conn, tid)
        tg = targets.get(tid) or {}
        plan = tg.get("trade_plan") or {}
        strategy = tg.get("strategy_id") or f0.get("strategy_id")
        if strategy_id is not None and strategy != strategy_id:
            continue
        iid = ep["instrument_id"]
        if iid not in mults:
            inst = instrument_get(iid, conn=conn) or {}
            mults[iid] = float(inst.get("multiplier") or 1)
        last = ep["exits"][-1] if ep["exits"] else ep["entries"][-1]
        end_ts = last.get("ts") if ep["closed"] else None
        status = "closed" if ep["closed"] else "open"
        if status == "open":
            later = [s for s in snaps_all if s.get("account_id") == ep["account_id"] and s.get("run_mode") == ep["run_mode"]
                     and (s.get("as_of") or "") > (last.get("ts") or "")]
            if later and not any(p.get("instrument_id") == iid and p.get("qty") for p in (later[-1].get("positions") or [])):
                status = "closed_without_fill"                       # 到期結算等：部位不見了但沒有出場成交，不打分
        end_day = (end_ts or "")[:10]
        if status == "closed" and ((date_from and end_day < date_from) or (date_to and end_day > date_to)):
            continue
        stop_pts = ((plan.get("risk") or {}).get("stop_pts")) or None
        r = (round(ep["gross_pts"] / (float(stop_pts) * ep["entry_qty"]), 4)
             if status == "closed" and stop_pts and ep["entry_qty"] else None)
        reason, reason_text = _last_exit_reason(ep, life) if status == "closed" else (None, None)
        gross = round(ep["gross_pts"] * mults[iid], 2)
        trades.append({
            "run_mode": ep["run_mode"], "account_id": ep["account_id"], "instrument_id": iid, "status": status,
            "direction": ep["direction"], "entry_qty": ep["entry_qty"], "max_qty": ep["max_qty"],
            "adds": max(0, len(ep["entries"]) - 1), "entry_ts": f0.get("ts"), "exit_ts": end_ts,
            "target_id": tid, "targets": ep["targets"], "strategy_id": strategy, "rule_version": tg.get("rule_version"),
            "playbook": f"{plan['playbook_id']}/{plan.get('playbook_version')}" if plan.get("playbook_id") else None,
            "r": r, "gross_pnl": gross if status == "closed" else None, "costs": round(ep["costs"], 2),
            "net_pnl": round(gross - ep["costs"], 2) if status == "closed" else None,
            "exit_reason": reason, "exit_detail": reason_text,
            "overrides": sum(1 for t in ep["targets"] for e in life.get(t, []) if e.get("kind") == "override"
                             and (e.get("ts") or "") >= (f0.get("ts") or "") and (not end_ts or (e.get("ts") or "") <= end_ts)),
            "insight": _insight(conn, targets.get(tid), ep["direction"]),
            "versions": dict(zip(_VERSION_KEYS, _versions(last))), "mixed_versions": len(ep["versions"]) > 1,
            "unscored": "no stop_pts on the opening target" if status == "closed" and r is None else None,
        })
    groups: dict[tuple, list[dict]] = {}
    for t in trades:
        groups.setdefault((t["run_mode"], t["strategy_id"], t["rule_version"], t["playbook"],
                           tuple(t["versions"].get(k) for k in _VERSION_KEYS)), []).append(t)
    out_groups = []
    for key, ts in sorted(groups.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        mode, strat, rule, pb, vers = key
        scored = sorted((t for t in ts if t["status"] == "closed" and t["r"] is not None), key=lambda t: t["exit_ts"] or "")
        rs = [t["r"] for t in scored]
        by_reason: dict[str, list[float]] = {}
        for t in scored:
            by_reason.setdefault(str(t["exit_reason"]), []).append(t["r"])
        out_groups.append({
            "run_mode": mode, "strategy_id": strat, "rule_version": rule, "playbook": pb,
            "versions": dict(zip(_VERSION_KEYS, vers)), "accounts": sorted({str(t["account_id"]) for t in ts}),
            "open_trades": sum(t["status"] == "open" for t in ts),
            "closed_without_fill": sum(t["status"] == "closed_without_fill" for t in ts),
            "unscored": sum(1 for t in ts if t["unscored"]), "mixed_versions": any(t["mixed_versions"] for t in ts),
            "sufficient": len(scored) >= settings.STATS_MIN_SAMPLE,
            "net_pnl": round(sum(t["net_pnl"] for t in scored), 2), "costs": round(sum(t["costs"] for t in scored), 2),
            "r": _r_block(rs),
            "by_exit_reason": {k: _r_block(v) for k, v in sorted(by_reason.items())},
            "override": {"with": _r_block([t["r"] for t in scored if t["overrides"]]),
                         "without": _r_block([t["r"] for t in scored if not t["overrides"]])},
            "insight": {k: _r_block([t["r"] for t in scored if t["insight"] == k]) for k in ("hit", "miss", "flat")},
        })
    return {"filters": {"run_mode": run_mode, "strategy_id": strategy_id, "account_id": account_id,
                        "date_from": date_from, "date_to": date_to},
            "min_sample": settings.STATS_MIN_SAMPLE, "n_trades": len(trades), "groups": out_groups,
            "accounts": account_curves(snaps_all, date_from),
            "trades": sorted(trades, key=lambda t: (t["exit_ts"] or t["entry_ts"] or "", str(t["target_id"])),
                             reverse=True)[:max(0, trades_limit)]}
