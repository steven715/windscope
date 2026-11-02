"""strategylib 的純度守則：只 import 標準庫與 strategylib 自己；門檻單一真相（settings 從它取值）。"""

import ast
import sys
from pathlib import Path

from config import settings
from strategylib import __version__
from strategylib.adjust import DEFAULT_GAP_PCT
from strategylib.trend import DEFAULTS, RULE_VERSION

LIB = Path(__file__).resolve().parents[2] / "strategylib"
_STDLIB = set(sys.stdlib_module_names)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_only_stdlib_and_self():
    offenders = []
    for path in LIB.rglob("*.py"):
        bad = {n for n in _imports(path) if n not in _STDLIB and n != "strategylib"}
        if bad:
            offenders.append(f"{path.relative_to(LIB)}: {sorted(bad)}")
    assert offenders == [], "strategylib 只能 import 標準庫與自己：\n" + "\n".join(offenders)


def test_no_forbidden_modules():
    """連標準庫裡會帶來副作用／非決定性的也不准：sqlite3、網路、時鐘、隨機。"""
    forbidden = {"sqlite3", "requests", "urllib", "http", "socket", "datetime", "time", "random", "os", "subprocess"}
    offenders = [f"{p.relative_to(LIB)}: {sorted(_imports(p) & forbidden)}"
                 for p in LIB.rglob("*.py") if _imports(p) & forbidden]
    assert offenders == [], "\n".join(offenders)


def test_settings_derive_trend_thresholds_from_library():
    assert settings.TREND_RULE_VERSION == RULE_VERSION
    assert settings.TREND_ADJUST_GAP_PCT == DEFAULT_GAP_PCT
    for key, val in DEFAULTS.items():
        assert getattr(settings, f"TREND_{key.upper()}") == val, key


def test_version_matches_pyproject():
    text = (LIB / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in text
