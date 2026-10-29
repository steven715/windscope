"""盤前判斷有效性統計（Layer 4b）：基準線、信心校準、單因子表、判斷回測。

回答一個問題：盤前判斷有沒有 edge？結論只有四種——有／可能有／看不出／樣本不足。
純函式、唯讀：讀 signals ⋈ verifications ⋈ daily_metrics ⋈ raw_index，不寫 DB、不改門檻。
與 verification.get_verification_stats（近 N 筆小卡）不同，本模組吃全歷史並可依
規則版本與日期區間篩選（不同規則版本不混算，見 GLOSSARY「命中率」）。

四張表各自回答：
  基準線   — 命中率有沒有贏過「永遠喊同一邊」「跟昨天」「與結果無關」這些傻策略。
  信心校準 — 信心 1–5 的命中率是否單調上升；喊中性落在 ±band 的命中與喊方向的命中分開看。
  單因子   — 每個輸入依現行門檻分桶後，隔日漲跌分布與該因子單獨暗示方向的命中率。
  判斷回測 — 把判斷當部位（偏多=多、偏空=空、中性=空手）算勝率/期望值/回撤，
             把「預測準確率」換成「交易勝率」。
"""

import logging
import math
import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass

from config import settings
from integration.premarket_call import _futures_vote
from integration.verification import _classify_change
from utils.trading_calendar import get_previous_trading_day

logger = logging.getLogger(__name__)

_CLASSES = ("up", "down", "flat")
_DIRECTIONS = ("bullish", "bearish", "neutral")
_DIRECTION_TO_CLASS = {"bullish": "up", "bearish": "down", "neutral": "flat"}
_DIRECTION_SIGN = {"bullish": 1.0, "bearish": -1.0}
_Z95 = 1.959964


@dataclass
class CallRow:
    """一筆已驗證的盤前判斷（signals ⋈ verifications 同日），日期升冪供回測串接。

    方向/信心取自 verifications（驗證當下的立場），hit 由本模組依三分類重算，
    避免 signals 事後被 recompute 覆寫而與 hit_day 不一致。
    """

    date: str
    direction: str
    confidence: int
    rule_version: str | None
    prev_close: float | None
    open: float | None
    close: float | None
    open_gap_pct: float | None
    day_change_pct: float | None
    open_gap_class: str | None
    day_change_class: str | None
    hit_day: int
    hit_open: int

    @property
    def predicted_class(self) -> str | None:
        """判斷方向對應的三分類（偏多→up、偏空→down、中性→flat）。"""
        return _DIRECTION_TO_CLASS.get(self.direction)


# ── 統計工具 ─────────────────────────────────────────────────────


def wilson_interval(hits: int, n: int, z: float = _Z95) -> tuple[float, float] | None:
    """Wilson score 區間（比例的 95% 信賴區間，小樣本不會跑出 [0,1]）。n=0 回 None。"""
    if n <= 0:
        return None
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _rate_block(hits: int, n: int) -> dict:
    """{n, hits, rate, ci_low, ci_high}；rate 以 0–1 表示，n=0 時三者為 None。"""
    ci = wilson_interval(hits, n)
    return {
        "n": n, "hits": hits,
        "rate": hits / n if n else None,
        "ci_low": ci[0] if ci else None,
        "ci_high": ci[1] if ci else None,
    }


def _mean(xs: list[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


# ── 資料載入 ─────────────────────────────────────────────────────


def _date_where(date_from: str | None, date_to: str | None, col: str) -> tuple[list[str], list]:
    conds, params = [], []
    if date_from:
        conds.append(f"{col} >= ?")
        params.append(date_from)
    if date_to:
        conds.append(f"{col} <= ?")
        params.append(date_to)
    return conds, params


def load_verified_calls(conn: sqlite3.Connection, rule_version: str | None = None,
                        date_from: str | None = None,
                        date_to: str | None = None) -> list[CallRow]:
    """讀已驗證的盤前判斷（signals ⋈ verifications），日期升冪；可篩規則版本與區間。"""
    conds, params = _date_where(date_from, date_to, "s.date")
    if rule_version:
        conds.append("s.rule_version = ?")
        params.append(rule_version)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    rows = conn.execute(
        "SELECT s.date, v.predicted_direction, v.confidence, s.rule_version, "
        "       v.prev_close, v.open, v.close, v.open_gap_pct, v.day_change_pct, "
        "       v.open_gap_class, v.day_change_class "
        "FROM signals s JOIN verifications v ON s.date = v.date"
        f"{where} ORDER BY s.date ASC", params,
    ).fetchall()
    out: list[CallRow] = []
    skipped = 0
    for r in rows:
        direction, conf, day_cls, open_cls = r[1], r[2], r[10], r[9]
        if (direction not in _DIRECTIONS or day_cls not in _CLASSES or conf is None
                or not settings.CONFIDENCE_MIN <= conf <= settings.CONFIDENCE_MAX):
            skipped += 1
            continue
        pred_cls = _DIRECTION_TO_CLASS[direction]
        out.append(CallRow(
            date=r[0], direction=direction, confidence=int(conf), rule_version=r[3],
            prev_close=r[4], open=r[5], close=r[6], open_gap_pct=r[7],
            day_change_pct=r[8], open_gap_class=open_cls, day_change_class=day_cls,
            hit_day=int(pred_cls == day_cls), hit_open=int(pred_cls == open_cls)))
    if skipped:
        logger.warning("call_stats 略過 %d 筆方向/三分類/信心不合法的驗證列", skipped)
    return out


def list_rule_versions(conn: sqlite3.Connection) -> list[dict]:
    """各規則版本的已驗證筆數（供篩選下拉），最近使用的版本在前（不用字串排序，v10 才不會排在 v9 前）。"""
    rows = conn.execute(
        "SELECT s.rule_version, COUNT(*) FROM signals s "
        "JOIN verifications v ON s.date = v.date "
        "GROUP BY s.rule_version ORDER BY MAX(s.date) DESC"
    ).fetchall()
    return [{"rule_version": r[0] or "", "n": r[1]} for r in rows]


def _load_index_changes(conn: sqlite3.Connection) -> dict[str, float]:
    """每個 raw_index 日期的收盤漲跌幅 %（對前一交易日收盤，與驗證同口徑）。缺前收的日期不列。"""
    closes = {d: c for d, c in conn.execute(
        "SELECT date, close FROM raw_index WHERE close IS NOT NULL").fetchall()}
    out: dict[str, float] = {}
    for d, c in closes.items():
        prev = get_previous_trading_day(d)
        pc = closes.get(prev) if prev else None
        if pc:
            out[d] = round((c - pc) / pc * 100, 4)
    return out


# ── ① 基準線 ─────────────────────────────────────────────────────


def compute_baselines(rows: list[CallRow], index_changes: dict[str, float] | None = None) -> dict:
    """系統命中率 vs 傻策略：永遠喊漲/跌/平、跟昨天、與結果無關（independence）。

    index_changes 供「跟昨天」查前一交易日漲跌；缺則該基準 n=0。
    回傳 {system, actual_dist, baselines:[...]}；每條基準含 n/hits/rate 與 edge_pp（系統−基準）。
    """
    n = len(rows)
    actual = Counter(r.day_change_class for r in rows)
    pred = Counter(r.predicted_class for r in rows)
    system = _rate_block(sum(r.hit_day for r in rows), n)
    sys_rate = system["rate"]

    def _bl(key: str, label: str, hits: int, m: int) -> dict:
        blk = _rate_block(hits, m)
        blk.update({"key": key, "label": label,
                    "edge_pp": (sys_rate - blk["rate"]) * 100
                    if sys_rate is not None and blk["rate"] is not None else None})
        return blk

    baselines = [
        _bl("always_up", "永遠喊偏多", actual["up"], n),
        _bl("always_down", "永遠喊偏空", actual["down"], n),
        _bl("always_flat", "永遠喊中性", actual["flat"], n),
    ]

    # 跟昨天：前一交易日漲跌三分類當今天的預測。raw_index 有洞時只比有前日漲跌的子集，
    # 系統命中率也在同一子集上算，edge_pp 才是同一批樣本相減。
    fy_hits = fy_n = fy_sys_hits = 0
    if index_changes:
        for r in rows:
            prev = get_previous_trading_day(r.date)
            pct = index_changes.get(prev) if prev else None
            if pct is None:
                continue
            fy_n += 1
            fy_hits += int(_classify_change(pct) == r.day_change_class)
            fy_sys_hits += r.hit_day
    fy = _bl("follow_yesterday", "跟昨天（昨漲喊漲）", fy_hits, fy_n)
    fy["edge_pp"] = ((fy_sys_hits / fy_n - fy["rate"]) * 100) if fy_n else None
    fy["system_rate_same_sample"] = fy_sys_hits / fy_n if fy_n else None
    baselines.append(fy)

    # 與結果無關：保留系統自己的預測分布，但假設與實際獨立 → 期望命中 Σ P(預測=c)·P(實際=c)
    indep = {"key": "independence", "label": "與結果無關（同預測分布）",
             "n": n, "hits": None, "rate": None, "ci_low": None, "ci_high": None,
             "edge_pp": None}
    if n:
        indep["rate"] = sum(pred[c] / n * actual[c] / n for c in _CLASSES)
        indep["edge_pp"] = (sys_rate - indep["rate"]) * 100
    baselines.append(indep)

    return {
        "system": system,
        "actual_dist": {c: {"n": actual[c], "share": actual[c] / n if n else None}
                        for c in _CLASSES},
        "predicted_dist": {d: {"n": pred[_DIRECTION_TO_CLASS[d]],
                               "share": pred[_DIRECTION_TO_CLASS[d]] / n if n else None}
                           for d in _DIRECTIONS},
        "baselines": baselines,
    }


# ── ② 信心校準 ───────────────────────────────────────────────────


def _directional_return(r: CallRow) -> float | None:
    """順判斷方向的收盤報酬 %（偏多=+漲跌幅、偏空=−漲跌幅）；中性無方向回 None。"""
    sign = _DIRECTION_SIGN.get(r.direction)
    if sign is None or r.day_change_pct is None:
        return None
    return sign * r.day_change_pct


def confidence_calibration(rows: list[CallRow], min_bucket: int | None = None) -> dict:
    """依信心 1–5 與方向分組的命中率（含 CI）、順向平均報酬；並判定信心是否單調。

    monotonic：樣本 ≥ min_bucket 的信心桶，收盤命中率是否隨信心嚴格上升（全平＝無資訊，判 False）；
    桶數 <2 回 None。
    """
    min_bucket = settings.STATS_MIN_BUCKET if min_bucket is None else min_bucket

    def _bucket(group: list[CallRow]) -> dict:
        n = len(group)
        blk = _rate_block(sum(r.hit_day for r in group), n)
        open_blk = _rate_block(sum(r.hit_open for r in group), n)
        dir_rets = [x for x in (_directional_return(r) for r in group) if x is not None]
        pcts = [r.day_change_pct for r in group if r.day_change_pct is not None]
        blk.update({
            "open_rate": open_blk["rate"],
            "n_directional": len(dir_rets),
            "avg_directional_return": _mean(dir_rets),
            "avg_day_change_pct": _mean(pcts),
            "insufficient": n < min_bucket,
        })
        return blk

    by_conf: dict[int, dict] = {}
    for c in range(settings.CONFIDENCE_MIN, settings.CONFIDENCE_MAX + 1):
        group = [r for r in rows if r.confidence == c]
        if group:
            by_conf[c] = _bucket(group)

    usable = [(c, b["rate"]) for c, b in sorted(by_conf.items()) if not b["insufficient"]]
    monotonic: bool | None = None
    if len(usable) >= 2:
        monotonic = all(usable[i][1] < usable[i + 1][1] for i in range(len(usable) - 1))

    by_dir = {d: _bucket([r for r in rows if r.direction == d]) for d in _DIRECTIONS
              if any(r.direction == d for r in rows)}
    return {"by_confidence": by_conf, "monotonic": monotonic,
            "monotonic_buckets": [c for c, _ in usable], "by_direction": by_dir}


# ── ③ 單因子表 ───────────────────────────────────────────────────


def _b_fx(v) -> tuple[str, str, str | None]:
    lab = {"bullish": "台幣升（偏多）", "bearish": "台幣貶（偏空）", "neutral": "平（中性）"}
    return (v, lab[v], v) if v in lab else ("na", "無資料", None)


def _b_sync(v) -> tuple[str, str, str | None]:
    if v is None:
        return ("na", "無資料", None)
    return ("sync", "亞幣同步", None) if int(v) == 1 else ("diverge", "不同步", None)


def _b_spread(v) -> tuple[str, str, str | None]:
    """分桶直接用 premarket_call._futures_vote，與真正的期貨票同一條邊界（≥ / ≤）。"""
    if v is None:
        return ("na", "無資料", None)
    t = settings.FUTURES_SPREAD_THRESHOLD
    vote = _futures_vote(v)
    if vote == "bullish":
        return ("pos", f"正價差 ≥ +{t:g}", "bullish")
    if vote == "bearish":
        return ("neg", f"逆價差 ≤ −{t:g}", "bearish")
    return ("mid", f"±{t:g} 內", "neutral")


def _b_volume(v) -> tuple[str, str, str | None]:
    if v is None:
        return ("na", "無資料", None)
    if v >= settings.VOLUME_RATIO_HIGH:
        return ("high", f"爆量 ≥ {settings.VOLUME_RATIO_HIGH:g}", None)
    if v <= settings.VOLUME_RATIO_LOW:
        return ("low", f"量縮 ≤ {settings.VOLUME_RATIO_LOW:g}", None)
    return ("normal", "正常", None)


def _b_oi(v) -> tuple[str, str, str | None]:
    if v is None:
        return ("na", "無資料", None)
    if v <= settings.OI_BEARISH_THRESHOLD:
        return ("net_short", f"淨空 ≤ {settings.OI_BEARISH_THRESHOLD:,}", "bearish")
    if v >= settings.OI_BULLISH_THRESHOLD:
        return ("net_long", f"淨多 ≥ {settings.OI_BULLISH_THRESHOLD:,}", "bullish")
    return ("mid", "其間", None)


def _b_oi_delta(v) -> tuple[str, str, str | None]:
    if v is None:
        return ("na", "無資料", None)
    if v > 0:
        return ("add", "淨多增加", None)
    if v < 0:
        return ("cut", "淨多減少", None)
    return ("zero", "不變", None)


# (欄位, 標籤, 分桶函式, 桶顯示順序)
_FACTORS = [
    ("fx_direction", "匯率票方向", _b_fx, ["bullish", "bearish", "neutral", "na"]),
    ("fx_asia_sync", "亞幣同步", _b_sync, ["sync", "diverge", "na"]),
    ("futures_spread_adjusted", "調整後價差", _b_spread, ["pos", "mid", "neg", "na"]),
    ("futures_volume_ratio", "夜盤量比", _b_volume, ["high", "normal", "low", "na"]),
    ("oi_net_foreign", "外資未平倉", _b_oi, ["net_long", "mid", "net_short", "na"]),
    ("oi_delta", "外資未平倉增減", _b_oi_delta, ["add", "zero", "cut", "na"]),
]


def factor_table(conn: sqlite3.Connection, date_from: str | None = None,
                 date_to: str | None = None, min_bucket: int | None = None,
                 index_changes: dict[str, float] | None = None) -> list[dict]:
    """每個盤前輸入依現行門檻分桶 → 當日漲跌三分類分布、平均/中位漲跌、因子暗示方向命中率。

    只讀 daily_metrics ⋈ raw_index（不需有判斷），回補過的歷史日也算得進去；不依規則版本篩。
    """
    min_bucket = settings.STATS_MIN_BUCKET if min_bucket is None else min_bucket
    conds, params = _date_where(date_from, date_to, "date")
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    cols = [f[0] for f in _FACTORS]
    metrics = conn.execute(
        f"SELECT date, {', '.join(cols)} FROM daily_metrics{where}", params).fetchall()
    changes = _load_index_changes(conn) if index_changes is None else index_changes

    out = []
    for idx, (col, label, bucketer, order) in enumerate(_FACTORS, start=1):
        groups: dict[str, dict] = {}
        for m in metrics:
            pct = changes.get(m[0])
            if pct is None:
                continue
            key, blabel, implied = bucketer(m[idx])
            g = groups.setdefault(key, {"key": key, "label": blabel, "implied": implied,
                                        "pcts": [], "classes": Counter()})
            g["pcts"].append(pct)
            g["classes"][_classify_change(pct)] += 1

        buckets = []
        for key in order:
            g = groups.get(key)
            if not g:
                continue
            n = len(g["pcts"])
            implied_cls = _DIRECTION_TO_CLASS.get(g["implied"]) if g["implied"] else None
            hit = _rate_block(g["classes"][implied_cls], n) if implied_cls else None
            buckets.append({
                "key": key, "label": g["label"], "n": n,
                "dist": {c: {"n": g["classes"][c], "share": g["classes"][c] / n}
                         for c in _CLASSES},
                "avg_pct": _mean(g["pcts"]), "median_pct": _median(g["pcts"]),
                "implied": g["implied"], "implied_hit": hit,
                "insufficient": n < min_bucket,
            })
        out.append({"key": col, "label": label, "n": sum(b["n"] for b in buckets),
                    "buckets": buckets})
    return out


# ── ④ 判斷回測 ───────────────────────────────────────────────────


def _trade_metrics(rets: list[float]) -> dict:
    """逐筆報酬（小數）→ 勝率/盈虧比/期望值/PF/累積報酬/最大回撤/Sharpe/最大連敗。空則 n=0。"""
    n = len(rets)
    if n == 0:
        return {"n": 0, "wins": 0, "win_rate": None, "ci_low": None, "ci_high": None,
                "avg_win": None, "avg_loss": None, "payoff": None, "expectancy": None,
                "profit_factor": None, "total_return": None, "max_drawdown": None,
                "sharpe": None, "max_losing_streak": 0, "max_winning_streak": 0}
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    blk = _rate_block(len(wins), n)
    avg_win = _mean(wins)
    avg_loss = _mean(losses)
    gross_loss = abs(sum(losses))
    payoff = (avg_win / abs(avg_loss)) if avg_win is not None and avg_loss else None

    equity = peak = 1.0
    max_dd = 0.0
    for r in rets:
        equity *= 1 + r
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1)

    # Sharpe：單筆或零波動無定義 → None（n<2）/ 0.0（sd=0）
    sd = statistics.pstdev(rets) if n >= 2 else None
    sharpe = (None if sd is None else
              (statistics.fmean(rets) / sd * math.sqrt(settings.BACKTEST_ANNUALIZATION_DAYS)
               if sd else 0.0))

    streak_l = streak_w = max_l = max_w = 0
    for r in rets:
        if r > 0:
            streak_w, streak_l = streak_w + 1, 0
        else:
            streak_l, streak_w = streak_l + 1, 0
        max_l, max_w = max(max_l, streak_l), max(max_w, streak_w)

    return {
        "n": n, "wins": len(wins), "win_rate": blk["rate"],
        "ci_low": blk["ci_low"], "ci_high": blk["ci_high"],
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff": payoff,
        "expectancy": statistics.fmean(rets),
        "profit_factor": (sum(wins) / gross_loss) if gross_loss else None,  # 無虧損→None(∞)
        "total_return": equity - 1, "max_drawdown": max_dd, "sharpe": sharpe,
        "max_losing_streak": max_l, "max_winning_streak": max_w,
    }


def backtest_calls(rows: list[CallRow], cost_bps: float | None = None) -> dict:
    """把盤前判斷當部位重放：偏多=做多、偏空=做空、中性=空手；中性日不計入勝率。

    兩種成交口徑並列：open_to_close（08:50 看判斷、開盤進收盤出＝真正可執行）與
    close_to_close（前收→收，與 hit_day 同口徑，對照「方向對」和「有賺」差多少）。
    每種再分等權與信心加權（部位∝信心，並把權重正規化成平均 1，兩者總曝險相同、
    才能公平比較；成本等比）。對照列：每天做多／每天做空（含成本）、同口徑逐日複利的指數
    漲跌（不含成本，= 每天做多零成本）。成本為每趟來回 bps，預設 settings.STATS_BT_COST_BPS。
    """
    cost = (settings.STATS_BT_COST_BPS if cost_bps is None else cost_bps) / 10000.0

    def _gross(r: CallRow, mode: str) -> float | None:
        if r.close is None:
            return None
        base = r.open if mode == "open_to_close" else r.prev_close
        return (r.close / base - 1) if base else None

    result: dict = {"cost_bps": cost * 10000, "modes": {}}
    for mode in ("open_to_close", "close_to_close"):
        usable = [(r, g) for r in rows for g in [_gross(r, mode)] if g is not None]
        directional = [(r, g) for r, g in usable if r.direction in _DIRECTION_SIGN]
        mean_conf = (statistics.fmean(r.confidence for r, _ in directional)
                     if directional else 1.0)
        trades, equal, weighted = [], [], []
        for r, g in directional:
            ret = _DIRECTION_SIGN[r.direction] * g - cost
            w = r.confidence / mean_conf          # 平均權重 = 1
            trades.append({"date": r.date, "direction": r.direction,
                           "confidence": r.confidence, "weight": round(w, 4), "ret": ret})
            equal.append(ret)
            weighted.append(w * ret)
        always_long = [g - cost for _, g in usable]
        always_short = [-g - cost for _, g in usable]
        index_ret = None
        if usable:
            index_ret = math.prod(1 + g for _, g in usable) - 1
        result["modes"][mode] = {
            "n_days": len(usable),
            "n_neutral": sum(1 for r, _ in usable if r.direction == "neutral"),
            "equal": _trade_metrics(equal),
            "weighted": _trade_metrics(weighted),
            "always_long": _trade_metrics(always_long),
            "always_short": _trade_metrics(always_short),
            "index_return": index_ret,
            "trades": trades,
        }
    return result


# ── 總表與結論 ───────────────────────────────────────────────────


def _assess(n: int, baselines: dict, min_sample: int) -> dict:
    """四選一結論：insufficient / edge / unclear / no_edge（系統 CI 下界 vs 最強基準）。"""
    if n < min_sample:
        return {"verdict": "insufficient",
                "text": f"已驗證 {n} 筆，低於 {min_sample} 筆門檻；信賴區間太寬，分不出有沒有 edge。"}
    sys_blk = baselines["system"]
    best = max((b for b in baselines["baselines"] if b["rate"] is not None),
               key=lambda b: b["rate"], default=None)
    if best is None or sys_blk["rate"] is None:
        return {"verdict": "insufficient", "text": "無可對照的基準。"}
    best_pct = best["rate"] * 100
    if sys_blk["ci_low"] > best["rate"]:
        return {"verdict": "edge",
                "text": f"命中率 {sys_blk['rate'] * 100:.1f}% 的 95% 區間下界 "
                        f"{sys_blk['ci_low'] * 100:.1f}% 仍高於最強基準「{best['label']}」"
                        f"{best_pct:.1f}%——目前資料支持有 edge。"}
    if sys_blk["rate"] <= best["rate"]:
        return {"verdict": "no_edge",
                "text": f"命中率 {sys_blk['rate'] * 100:.1f}% 沒有贏過最強基準"
                        f"「{best['label']}」{best_pct:.1f}%——目前資料看不出 edge。"}
    return {"verdict": "unclear",
            "text": f"命中率 {sys_blk['rate'] * 100:.1f}% 高於最強基準「{best['label']}」"
                    f"{best_pct:.1f}%，但 95% 區間（{sys_blk['ci_low'] * 100:.1f}–"
                    f"{sys_blk['ci_high'] * 100:.1f}%）仍蓋住它——可能有 edge，樣本還不夠證明。"}


def build_call_stats(conn: sqlite3.Connection, rule_version: str | None = None,
                     date_from: str | None = None, date_to: str | None = None,
                     cost_bps: float | None = None) -> dict:
    """一次算齊四張表＋結論，供 /stats 頁與 /api/call-stats（亦是餵給 LLM 的結構化戰績）。"""
    rows = load_verified_calls(conn, rule_version, date_from, date_to)
    index_changes = _load_index_changes(conn)
    baselines = compute_baselines(rows, index_changes)
    n = len(rows)
    return {
        "filters": {"rule_version": rule_version or "", "date_from": date_from or "",
                    "date_to": date_to or ""},
        "n": n,
        "date_range": [rows[0].date, rows[-1].date] if rows else None,
        "min_sample": settings.STATS_MIN_SAMPLE,
        "min_bucket": settings.STATS_MIN_BUCKET,
        "versions": list_rule_versions(conn),
        "assessment": _assess(n, baselines, settings.STATS_MIN_SAMPLE),
        "baselines": baselines,
        "calibration": confidence_calibration(rows),
        "factors": factor_table(conn, date_from, date_to, index_changes=index_changes),
        "backtest": backtest_calls(rows, cost_bps),
    }
