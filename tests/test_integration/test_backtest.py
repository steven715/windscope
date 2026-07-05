"""回測引擎測試（Layer 5）。

不打真實 HTTP、不用 pandas；用合成K線與 in-memory SQLite。引擎邏輯（進場/加碼/減碼/
出場/成本）用「腳本化訊號」（monkeypatch evaluate_series）做確定性驗證；另有一條用真實
evaluate_series 的端到端整合測試。
"""

import sqlite3

import pytest

import integration.backtest as backtest_mod
from integration.backtest import (
    BacktestParams,
    _tranche_weights,
    run_and_save,
    run_backtest,
)
from integration.trend_signal import TrendParams


def _bars(prices: list[float]) -> list[dict]:
    """把價格序列包成 OHLC bar（open=close=price，供確定性成交/MTM）。"""
    return [{"date": f"2020-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
             "open": p, "high": p * 1.01, "low": p * 0.99,
             "close": p, "volume": 1000} for i, p in enumerate(prices)]


def _sig(big: str, action: str = "觀望", position: str = "持有") -> dict:
    """腳本化訊號（引擎只讀 big_trend/action_hint/position_hint 三欄）。"""
    return {"big_trend": big, "action_hint": action, "position_hint": position}


def _patch_signals(monkeypatch, signals: list[dict]) -> None:
    monkeypatch.setattr(backtest_mod, "evaluate_series", lambda *a, **k: signals)


# ── 純函式 ──────────────────────────────────────────────────────


class TestPrimitives:
    def test_tranche_weights_equal(self):
        assert _tranche_weights(3, "equal") == pytest.approx([1 / 3, 1 / 3, 1 / 3])

    def test_tranche_weights_decreasing(self):
        # (3,2,1)/6 ＝ ½⅓⅙
        assert _tranche_weights(3, "decreasing") == pytest.approx([0.5, 1 / 3, 1 / 6])

    def test_tranche_weights_sum_to_one(self):
        for sizing in ("equal", "decreasing"):
            assert sum(_tranche_weights(4, sizing)) == pytest.approx(1.0)

    def test_empty_bars_raises(self):
        with pytest.raises(ValueError):
            run_backtest([])


# ── 引擎邏輯（腳本化訊號）──────────────────────────────────────


class TestEngineScripted:
    def test_single_entry_then_trend_exit(self, monkeypatch):
        prices = [100, 101, 102, 103, 104, 90]
        # 決策 signals[i-1] → 於 bar i 開盤成交
        _patch_signals(monkeypatch, [
            _sig("UP", action="關注建倉區"),   # [0] → buy @ open[1]=101
            _sig("UP"), _sig("UP"), _sig("UP"),
            _sig("DOWN"),                       # [4] → exit @ open[5]=90
            _sig("DOWN"),
        ])
        p = BacktestParams(entry_mode="single")
        r = run_backtest(_bars(prices), p, stock_id="T")

        assert len(r["trades"]) == 1
        t = r["trades"][0]
        assert t["entry_price"] == 101 and t["exit_price"] == 90
        assert t["exit_reason"] == "trend_exit"
        assert t["net_pnl"] < 0                 # 買高賣低 + 成本
        assert t["hold_days"] > 0
        assert r["equity_curve"][-1]["units"] == 0   # 已平倉

    def test_single_ignores_overbought_trim(self, monkeypatch):
        # single 模式應抱到趨勢轉向，不因「減倉」出場
        prices = [100] * 6
        _patch_signals(monkeypatch, [
            _sig("UP", action="關注建倉區"),
            _sig("UP", position="減倉"),
            _sig("UP", position="減倉"),
            _sig("UP"), _sig("UP"), _sig("UP"),
        ])
        r = run_backtest(_bars(prices), BacktestParams(entry_mode="single"), stock_id="T")
        # 從未出場 → 收尾強制平倉一筆
        assert len(r["trades"]) == 1
        assert r["trades"][0]["exit_reason"] == "end_of_data"

    def test_scaled_add_trim_exit(self, monkeypatch):
        prices = [100] * 8
        _patch_signals(monkeypatch, [
            _sig("UP", action="關注建倉區"),   # [0] i=1 buy unit0
            _sig("UP", position="可加倉"),      # [1] i=2 add unit1
            _sig("UP", position="可加倉"),      # [2] i=3 add unit2 (滿 3 批)
            _sig("UP", position="可加倉"),      # [3] i=4 已滿，不加
            _sig("UP", position="減倉"),        # [4] i=5 trim 一批 → 2 批
            _sig("DOWN"),                        # [5] i=6 全數出場 2 批
            _sig("DOWN"), _sig("DOWN"),
        ])
        p = BacktestParams(entry_mode="scaled", max_units=3, add_cooldown_days=0)
        r = run_backtest(_bars(prices), p, stock_id="T")

        reasons = [t["exit_reason"] for t in r["trades"]]
        assert reasons.count("trim_overbought") == 1
        assert reasons.count("trend_exit") == 2
        assert len(r["trades"]) == 3
        assert r["equity_curve"][-1]["units"] == 0
        # 曾同時持有 3 批
        assert max(c["units"] for c in r["equity_curve"]) == 3

    def test_add_respects_cooldown(self, monkeypatch):
        prices = [100] * 6
        _patch_signals(monkeypatch, [
            _sig("UP", action="關注建倉區"),   # i=1 buy unit0 (last_add=1)
            _sig("UP", position="可加倉"),      # i=2: 2-1=1 < cooldown 3 → 不加
            _sig("UP", position="可加倉"),      # i=3: 3-1=2 < 3 → 不加
            _sig("UP", position="可加倉"),      # i=4: 4-1=3 >= 3 → 加 unit1
            _sig("UP"), _sig("UP"),
        ])
        p = BacktestParams(entry_mode="scaled", max_units=3, add_cooldown_days=3)
        r = run_backtest(_bars(prices), p, stock_id="T")
        assert max(c["units"] for c in r["equity_curve"]) == 2   # 只加到 2 批

    def test_no_signal_no_trade_flat_equity(self, monkeypatch):
        prices = [100, 105, 110, 108]
        _patch_signals(monkeypatch, [_sig("UNKNOWN")] * 4)
        r = run_backtest(_bars(prices), BacktestParams(), stock_id="T")
        assert r["trades"] == []
        assert r["metrics"]["n_trades"] == 0
        cap = BacktestParams().initial_capital
        assert all(c["equity"] == cap for c in r["equity_curve"])
        assert r["metrics"]["total_return"] == 0.0

    def test_costs_charged_on_flat_roundtrip(self, monkeypatch):
        # 平盤進出，唯一虧損來自手續費 + 稅 → net_pnl 必為負且量級合理
        prices = [100] * 5
        _patch_signals(monkeypatch, [
            _sig("UP", action="關注建倉區"),   # buy @100
            _sig("UP"), _sig("UP"),
            _sig("DOWN"),                        # sell @100
            _sig("DOWN"),
        ])
        p = BacktestParams(entry_mode="single", fee_bps=14.25, tax_bps=30.0)
        r = run_backtest(_bars(prices), p, stock_id="T")
        t = r["trades"][0]
        assert t["gross_pnl"] == pytest.approx(0.0, abs=1e-6)
        # 成本 ≈ 買0.1425% + 賣(0.1425%+0.3%) 的名目 ≈ 0.585% * 1,000,000
        assert t["net_pnl"] < 0
        assert t["net_pnl"] == pytest.approx(-5850, rel=0.05)


# ── 端到端（真實 evaluate_series）───────────────────────────────


class TestEndToEnd:
    _SMALL = TrendParams(ma_big=20, ma_center=5, ma_fast=3, slope_lookback=5)

    def test_uptrend_produces_profitable_trade(self):
        prices = [100 + i for i in range(120)]   # 穩定強升 → 進場抱到底
        p = BacktestParams(entry_mode="single", trend_params=self._SMALL)
        r = run_backtest(_bars(prices), p, stock_id="RISE")

        m = r["metrics"]
        assert m["n_trades"] >= 1
        assert m["total_return"] > 0
        assert m["benchmark_return"] > 0
        assert len(r["equity_curve"]) == 120
        for key in ("total_return", "cagr", "max_drawdown", "sharpe",
                    "win_rate", "profit_factor", "expectancy", "exposure_pct"):
            assert key in m

    def test_run_and_save_persists(self, memory_db):
        prices = [100 + i for i in range(120)]
        now = "2020-01-01T00:00:00"
        for b in _bars(prices):
            memory_db.execute(
                "INSERT INTO raw_stock_daily (date, stock_id, open, high, low, close, "
                "volume, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (b["date"], "RISE", b["open"], b["high"], b["low"], b["close"],
                 b["volume"], now),
            )
        memory_db.commit()

        p = BacktestParams(entry_mode="scaled", trend_params=self._SMALL)
        r = run_and_save("RISE", memory_db, p)

        assert r is not None and r["run_id"] is not None
        assert memory_db.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 1
        assert memory_db.execute("SELECT COUNT(*) FROM backtest_equity WHERE run_id = ?",
                                 (r["run_id"],)).fetchone()[0] == 120
        assert memory_db.execute("SELECT COUNT(*) FROM backtest_trades WHERE run_id = ?",
                                 (r["run_id"],)).fetchone()[0] >= 1

    def test_final_equity_reconciles_with_trade_ledger(self):
        # final_equity 必須 ＝ 起始資金 + Σ交易淨損益（含收尾平倉的出場成本，否則報酬高估）
        prices = [100 + i for i in range(140)]
        p = BacktestParams(entry_mode="scaled", trend_params=self._SMALL)
        r = run_backtest(_bars(prices), p, stock_id="X")
        recon = p.initial_capital + sum(t["net_pnl"] for t in r["trades"])
        assert r["metrics"]["final_equity"] == pytest.approx(recon, abs=1.0)

    def test_benchmark_anchored_at_first_entry(self, monkeypatch):
        # 基準報酬用「首次進場」當起點：進場後平盤 → 基準 ≈ 0（不是從序列頭起算）
        prices = list(range(1, 51)) + [100] * 10   # 前段大漲、進場後平
        _patch_signals(monkeypatch, [_sig("UNKNOWN")] * 50 +
                       [_sig("UP", action="關注建倉區")] + [_sig("UP")] * 9)
        r = run_backtest(_bars(prices), BacktestParams(entry_mode="single"), stock_id="X")
        assert r["metrics"]["benchmark_return"] == pytest.approx(0.0, abs=1e-9)

    def test_run_and_save_no_data_returns_none(self, memory_db):
        assert run_and_save("NOPE", memory_db, BacktestParams()) is None
