"""strategylib.roll：與 tradewind market/contracts（Q5 v0）同一組案例——roll_rule 解讀、交易日推移、連續代號 → 月份合約、
換月日當天還是舊月、次月不在主檔回 None；windscope 串流用它決定連續代號接哪一口。"""

import json
from pathlib import Path

import pytest

from strategylib.roll import (ROLL_VERSION, dated_contracts, front_contract, is_continuous, is_trading_day, parse_roll_rule,
                              roll_day, shift_trading_days)

SEED = Path(__file__).resolve().parents[2] / "config" / "instruments.json"


def _rows(rule: str | None = "days_before_expiry:3") -> list[dict]:
    rows = json.loads(SEED.read_text(encoding="utf-8"))
    for r in rows:
        if r["asset_class"] == "future" and r["expiry"] is None:
            r["roll_rule"] = rule
    return rows


def _get(rows, iid):
    return next(r for r in rows if r["instrument_id"] == iid)


def test_parse_roll_rule_accepts_only_days_before_expiry():
    assert parse_roll_rule(None) is None and parse_roll_rule("days_before_expiry:3") == 3 and parse_roll_rule("days_before_expiry:1") == 1
    for bad in ("volume_cross", "days_before_expiry:-1", "days_before_expiry:x", "", "days_before_expiry:0"):
        with pytest.raises(ValueError, match="unknown roll_rule"):
            parse_roll_rule(bad)


def test_shift_trading_days_skips_weekends_and_given_holidays_across_month_and_year_ends():
    assert shift_trading_days("2026-06-17", -3) == "2026-06-12"
    assert shift_trading_days("2026-06-17", -3, {"2026-06-15"}) == "2026-06-11"
    assert shift_trading_days("2026-06-12", 1) == "2026-06-15" and shift_trading_days("2026-06-17", 0) == "2026-06-17"
    assert shift_trading_days("2026-12-31", 1) == "2027-01-01" and shift_trading_days("2027-01-01", -1, {"2026-12-31"}) == "2026-12-30"
    assert shift_trading_days("2028-03-01", -1) == "2028-02-29" and not is_trading_day("2026-06-13")
    with pytest.raises(ValueError):
        shift_trading_days("2026-02-30", 1)


def test_dated_contracts_match_symbol_and_multiplier():
    rows = _rows()
    assert [c["instrument_id"] for c in dated_contracts(_get(rows, "TAIFEX:TMF"), rows)] == ["TAIFEX:TMF202606"]
    assert [c["instrument_id"] for c in dated_contracts(_get(rows, "TAIFEX:TX"), rows)] == ["TAIFEX:TXF202606"]
    assert is_continuous(_get(rows, "TAIFEX:TMF")) and not is_continuous(_get(rows, "TAIFEX:TMF202606"))


def test_front_contract_and_roll_day():
    rows = _rows()
    cont, june = _get(rows, "TAIFEX:TMF"), _get(rows, "TAIFEX:TMF202606")
    assert roll_day(june, 3) == "2026-06-12" and roll_day(june, 0) == "2026-06-17"
    assert front_contract(cont, rows, "2026-06-11")["instrument_id"] == "TAIFEX:TMF202606"
    assert front_contract(cont, rows, "2026-06-12")["instrument_id"] == "TAIFEX:TMF202606"   # 換月日當天還是舊的那口
    assert front_contract(cont, rows, "2026-06-15") is None                                    # 次月不在主檔：不猜
    jul = {**june, "instrument_id": "TAIFEX:TMF202607", "symbol": "TMF202607", "expiry": "2026-07-15"}
    assert front_contract(cont, rows + [jul], "2026-06-15")["instrument_id"] == "TAIFEX:TMF202607"
    one = _rows("days_before_expiry:1")
    assert front_contract(_get(one, "TAIFEX:TMF"), one + [jul], "2026-06-16")["instrument_id"] == "TAIFEX:TMF202606"
    assert front_contract(_get(one, "TAIFEX:TMF"), one + [jul], "2026-06-17")["instrument_id"] == "TAIFEX:TMF202607"
    assert front_contract(_get(_rows(None), "TAIFEX:TMF"), _rows(None), "2026-06-12") is None    # 沒有規則：不換月
    with pytest.raises(ValueError):
        front_contract(_get(_rows("days_before_expiry:0"), "TAIFEX:TMF"), _rows(), "2026-06-12")
    assert ROLL_VERSION == "roll1"
