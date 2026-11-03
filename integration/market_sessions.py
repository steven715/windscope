"""時段與交易日歸屬（契約 README「時段與歸屬」）：某商品在某時刻屬於哪個 session、歸哪個 session_date。

- 股票／指數：regular 09:00–13:30，session_date＝當天。
- 期貨：regular 08:45–13:45；night 15:00–05:00（跨日）。**夜盤歸次一交易日**：15:00 後的 tick 歸「該日之後的下一個交易日」
  （週五夜盤歸下週一）；凌晨 05:00 前的 tick 歸「該日本身若是交易日，否則下一個交易日」。
- 時段外（例如 13:46、14:30）回 (None, None)：串流不該有這種 tick，有就丟掉並記 WARNING。
時段表來自 instruments 主檔（`sessions`），不寫死。
"""

from utils.trading_calendar import get_next_trading_day, is_trading_day


def session_at(sessions: dict, hhmm: str) -> str | None:
    """HH:MM 落在哪個時段（含兩端）；跨日時段（open > close）兩段都算；都不在回 None。"""
    for name, span in (sessions or {}).items():
        o, c = span["open"], span["close"]
        if (o <= hhmm <= c) if o <= c else (hhmm >= o or hhmm <= c):
            return name
    return None


def attribute(sessions: dict, ts: str) -> tuple[str | None, str | None]:
    """契約時刻 → (session, session_date)。時段外回 (None, None)。"""
    hhmm, date = ts[11:16], ts[:10]
    name = session_at(sessions, hhmm)
    if name is None:
        return None, None
    span = sessions[name]
    if span["open"] <= span["close"]:                              # 日內時段
        return name, date
    if hhmm >= span["open"]:                                       # 跨日時段的前半（當晚）→ 下一個交易日
        return name, get_next_trading_day(date)
    return name, date if is_trading_day(date) else get_next_trading_day(date)   # 凌晨
