"""防回歸：契約表只能經各自的 db/ 入口存取（CLAUDE.md 硬約束 #10，與 watchlist 同一套規則）。

instruments → db/instruments.py；bars／bars_v → db/bars.py；targets → db/targets.py；
events／account_snapshots → db/events.py。建表／建 view（schema.py）不在比對範圍。
"""

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_RULES = {
    "instruments": Path("db/instruments.py"),
    "bars": Path("db/bars.py"),
    "bars_v": Path("db/bars.py"),
    "targets": Path("db/targets.py"),
    "events": Path("db/events.py"),
    "account_snapshots": Path("db/events.py"),
}


def _sql_re(table: str) -> re.Pattern:
    return re.compile(rf"""\b(FROM|INTO|UPDATE|JOIN|TABLE)[\s"']+{table}\b""", re.IGNORECASE)


def _offenders(table: str, text: str, rel: Path) -> list[str]:
    return [f"{rel}:{text.count(chr(10), 0, m.start()) + 1}: {m.group(0)!r}"
            for m in _sql_re(table).finditer(text)]


@pytest.mark.parametrize("table,allowed", sorted(_RULES.items()))
def test_contract_table_sql_only_in_entry_module(table, allowed):
    offenders = []
    for path in _ROOT.rglob("*.py"):
        rel = path.relative_to(_ROOT)
        if rel.parts[0] in {"tests", ".venv", "venv"} or rel in {allowed, Path("db/schema.py")}:
            continue
        offenders += _offenders(table, path.read_text(encoding="utf-8"), rel)
    assert offenders == [], f"{table} 表只能經 {allowed} 存取：\n" + "\n".join(offenders)


def test_guard_regex_matches_real_sql():
    rel = Path("x.py")
    assert _offenders("targets", 'conn.execute("SELECT payload FROM targets WHERE 1")', rel)
    assert _offenders("bars_v", 'sql = ("SELECT 1 FROM "\n     "bars_v WHERE x")', rel)
    assert not _offenders("events", "# events 回流（契約 events）", rel)
