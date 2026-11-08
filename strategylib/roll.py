"""期貨連續代號的換月（純函式，只用標準庫）：windscope 串流用它決定連續代號接哪一口。

- **連續代號**：主檔 `expiry` 為空、`roll_rule` 有值的期貨（例 `TAIFEX:TMF`）。對應的**月份合約**＝同市場、期貨、
  `symbol` 為「連續 symbol ＋ 可選 `F` ＋ `YYYYMM`」（TX↔TXF202606、TMF↔TMF202606）、乘數相同、`expiry` 有值。
- **`roll_rule = days_before_expiry:N`**（N ≥ 1）：換月日＝到期日往前第 N 個交易日。某交易日的**近月（front）**＝
  換月日 ≥ 當天的最早那口；換月日當天仍是舊月，下一個交易日起是次月。N=0 不接受（最後交易日的收盤時刻 ⚠ 未查證）。
- **交易日**＝週一到週五扣掉呼叫端給的假日表（夜盤歸屬由呼叫端先算成交易日再傳進來）。
語意與 tradewind `market/contracts`（2026-11-05 Q5 v0）相同（目前兩份程式，審查時對跑約 12,000 組一致）；tradewind 下次同步
strategylib tag 時改呼叫這裡，才算單一真相。改語意 → bump ROLL_VERSION 與套件 __version__，兩邊一起換。
"""

import calendar
import re

ROLL_VERSION = "roll1"
_RULE = re.compile(r"^days_before_expiry:(\d+)$")


def parse_roll_rule(rule: str | None) -> int | None:
    """`days_before_expiry:N`（N ≥ 1）→ N；None → None；其他格式或 N=0 raise ValueError（不猜規則）。"""
    if rule is None:
        return None
    m = _RULE.match(rule.strip())
    if not m:
        raise ValueError(f"unknown roll_rule {rule!r} (expected days_before_expiry:N)")
    n = int(m.group(1))
    if n < 1:
        raise ValueError(f"unknown roll_rule {rule!r}: N=0 would roll on the last trading day, whose session close is unverified")
    return n


def _ymd(d: str) -> tuple[int, int, int]:
    y, m, dd = (int(x) for x in d.split("-"))
    calendar.monthrange(y, m)                                       # 月份不合法 raise
    if not 1 <= dd <= calendar.monthrange(y, m)[1]:
        raise ValueError(f"invalid date {d!r}")
    return y, m, dd


def _step(d: str, step: int) -> str:
    """d 的下一天（step=1）或前一天（step=-1）；不用 datetime（本套件不碰時鐘模組）。"""
    y, m, dd = _ymd(d)
    dd += step
    if dd < 1:
        m, y = (12, y - 1) if m == 1 else (m - 1, y)
        dd = calendar.monthrange(y, m)[1]
    elif dd > calendar.monthrange(y, m)[1]:
        m, y = (1, y + 1) if m == 12 else (m + 1, y)
        dd = 1
    return f"{y:04d}-{m:02d}-{dd:02d}"


def is_trading_day(d: str, holidays: frozenset[str] | set[str] = frozenset()) -> bool:
    """週一到週五且不在假日表。"""
    return calendar.weekday(*_ymd(d)) < 5 and d not in holidays


def shift_trading_days(d: str, n: int, holidays: frozenset[str] | set[str] = frozenset()) -> str:
    """從 d 往後（n>0）或往前（n<0）數 |n| 個交易日；n=0 回 d 本身。"""
    cur, step, left = d, (1 if n > 0 else -1), abs(n)
    _ymd(cur)
    while left:
        cur = _step(cur, step)
        if is_trading_day(cur, holidays):
            left -= 1
    return cur


def _is_future(inst: dict) -> bool:
    return inst.get("asset_class") == "future"


def is_continuous(inst: dict) -> bool:
    """有換月規則的連續代號（主檔 dict）。"""
    return _is_future(inst) and not inst.get("expiry") and inst.get("roll_rule") is not None


def dated_contracts(continuous: dict, instruments: list[dict]) -> list[dict]:
    """連續代號在主檔裡對應的月份合約（依到期日排序）。instruments＝契約 instruments 形狀的 dict。"""
    pat = re.compile(rf"^{re.escape(continuous['symbol'])}F?\d{{6}}$")
    out = [i for i in instruments if _is_future(i) and i.get("expiry") and i.get("market") == continuous.get("market")
           and pat.match(i.get("symbol") or "") and i.get("multiplier") == continuous.get("multiplier")]
    return sorted(out, key=lambda i: (i["expiry"], i["instrument_id"]))


def roll_day(contract: dict, n: int, holidays: frozenset[str] | set[str] = frozenset()) -> str:
    """這口月份合約的換月日：到期日往前第 n 個交易日。"""
    return shift_trading_days(contract["expiry"], -n, holidays)


def front_contract(continuous: dict, instruments: list[dict], session_date: str,
                   holidays: frozenset[str] | set[str] = frozenset()) -> dict | None:
    """session_date（交易日）這天連續代號對應的月份合約：換月日 ≥ 當天的最早那口；沒有回 None。"""
    n = parse_roll_rule(continuous.get("roll_rule"))
    if n is None:
        return None
    for c in dated_contracts(continuous, instruments):
        if roll_day(c, n, holidays) >= session_date:
            return c
    return None
