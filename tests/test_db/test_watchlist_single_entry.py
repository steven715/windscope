"""防回歸：watchlist 表的讀寫只走 db/watchlist.py（CLAUDE.md 硬約束 #10）。

掃描所有非測試的 .py，db/watchlist.py（唯一入口）以外不得出現對 watchlist 表的
讀寫 SQL（建表的 CREATE TABLE IF NOT EXISTS 不在比對範圍）。新增繞過點時這裡會直接紅。
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_ALLOWED = {Path("db/watchlist.py")}
# 整檔比對：字串可能跨行拼接（"... FROM "\n"watchlist ..."），中間允許引號與空白
_SQL = re.compile(r"""\b(FROM|INTO|UPDATE|JOIN|TABLE)[\s"']+watchlist\b""", re.IGNORECASE)


def _offenders(text: str, rel: Path) -> list[str]:
    return [f"{rel}:{text.count(chr(10), 0, m.start()) + 1}: {m.group(0)!r}"
            for m in _SQL.finditer(text)]


def test_watchlist_sql_only_in_db_watchlist():
    offenders = []
    for path in _ROOT.rglob("*.py"):
        rel = path.relative_to(_ROOT)
        if rel.parts[0] in {"tests", ".venv", "venv"} or rel in _ALLOWED:
            continue
        offenders += _offenders(path.read_text(encoding="utf-8"), rel)
    assert offenders == [], "watchlist 表只能經 db/watchlist.py 存取：\n" + "\n".join(offenders)


def test_guard_catches_split_and_ddl_sql():
    """比對規則本身：跨行拼接、ALTER 抓得到；建表不誤判。"""
    rel = Path("x.py")
    assert _offenders('q = ("SELECT 1 FROM "\n     "watchlist WHERE x")', rel)
    assert _offenders('conn.execute("ALTER TABLE watchlist ADD c")', rel)
    assert not _offenders("CREATE TABLE IF NOT EXISTS watchlist (", rel)
