"""還原股價純函式：偵測公司行為缺口（分割/大除權）+ 配息，比例回推歷史 OHLC。

1. 分割/大除權：單日收盤變動 > DEFAULT_GAP_PCT（台股 ±10% 漲跌幅限制，>11% 必為公司行為）
   → factor = close[i]/close[i-1]，把該日之前的價格按比例縮到同一尺度。
2. 配息（<10%，價格看不出）：用公司行為事件的 after/before 比例（契約 corporate_actions.factor）。
還原價[j] = 原價[j] × （j 之後所有 factor 的累積乘積）。純函式、不寫 DB。

時點正確性：還原是**尺度變換**——t 之後才發生的事件只會把 ≤t 的所有價格乘同一個常數，
趨勢引擎用的斜率／乖離／箱型都是比例，輸出不變（windscope tests/test_strategylib/test_pit.py 證明）。
"""

# 單日收盤變動 > 此比例＝公司行為缺口（極少數無漲跌幅限制的槓桿/反向/外國 ETF 可能誤判）
DEFAULT_GAP_PCT = 0.11


def back_adjust_with_events(
        bars: list[dict], div_events: list[dict] | None = None,
        gap_pct: float = DEFAULT_GAP_PCT) -> tuple[list[dict], list[dict]]:
    """回推還原 OHLC（bars 需舊→新），並回報套用的還原事件。

    回傳 (adjusted_bars, events)；events = [{"date", "factor", "kind"}]，
    kind ∈ {"gap"(分割/大除權，價格偵測), "div"(配息事件)}。空輸入回 ([], [])。
    """
    if not bars:
        return [], []
    n = len(bars)

    # factor[i] = 第 i 天相對前一天的缺口比例（1.0 = 無缺口）；影響第 i 天之前的日子
    factor = [1.0] * n
    events: list[dict] = []

    # 1) 分割/大除權：從價格自動偵測（單日變動 > gap_pct）。cur/prev 皆需為正。
    for i in range(1, n):
        prev, cur = bars[i - 1]["close"], bars[i]["close"]
        if prev and prev > 0 and cur and cur > 0 and abs(cur / prev - 1) > gap_pct:
            factor[i] = cur / prev
            events.append({"date": bars[i]["date"], "factor": factor[i], "kind": "gap"})

    # 2) 配息 ex-date（未被價格缺口涵蓋者才套，避免與分割重複）
    if div_events:
        idx = {b["date"]: k for k, b in enumerate(bars)}
        for ev in div_events:
            k = idx.get(ev.get("ex_date"))
            f = ev.get("factor")
            if k is not None and k >= 1 and f and factor[k] == 1.0:
                factor[k] = f
                events.append({"date": bars[k]["date"], "factor": f, "kind": "div"})

    # 3) 累積回推：coef[j] = product(factor[i] for i > j)
    cum = 1.0
    coef = [1.0] * n
    for j in range(n - 1, -1, -1):
        coef[j] = cum
        cum *= factor[j]

    out = []
    for j, b in enumerate(bars):
        c = coef[j]
        if c == 1.0:
            out.append(dict(b))
        else:
            out.append({**b,
                        "open": b["open"] * c, "high": b["high"] * c,
                        "low": b["low"] * c, "close": b["close"] * c})
    return out, events


def back_adjust(bars: list[dict], div_events: list[dict] | None = None,
                gap_pct: float = DEFAULT_GAP_PCT) -> list[dict]:
    """回推還原 OHLC（bars 需舊→新）。回傳新 list（不改動原物件）；空輸入回 []。"""
    return back_adjust_with_events(bars, div_events, gap_pct)[0]


def adjust_note(events: list[dict]) -> str | None:
    """把價格偵測的公司行為缺口(gap)整理成一句可顯示的還原提示；無則回 None。

    只提醒 gap（±10% 漲跌幅限制外，可能誤判需人工核對）；配息(div)例行、低風險，不列入。
    """
    gaps = [e for e in events if e.get("kind") == "gap"]
    if not gaps:
        return None
    last = gaps[-1]
    return (f"已還原公司行為缺口 {len(gaps)} 次"
            f"（最近 {last['date']} ×{last['factor']:.2f}，請核對是否分割/大除權）")
