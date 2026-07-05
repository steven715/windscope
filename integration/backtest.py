"""Layer 5 回測引擎：拿個股趨勢訊號當「動作」重放歷史、算績效。

純函式、唯讀：run_backtest(還原日K, 參數) → 逐筆交易 + 權益曲線 + 績效指標，不寫既有
訊號/排程表。訊號直接呼叫 trend_signal.evaluate_series（＝production 邏輯），回測驗的
就是實際規則，非另寫一份會走樣的複製品。

無未來函數：第 t 根的訊號只用 ≤t 的資料（evaluate_series 保證），成交在「次一根開盤」。
方法論假設（見 docs/roadmap.md）：
  - 先拋棄基本面 → fundamental_gate 一律 PASS（否則「關注建倉區」永不觸發＝永不進場）。
  - 出場綁「大勢轉向」（big_trend 離開 UP），無固定 % 止損。
  - 分批：single＝全進全出（1 批、不因超買減碼，抱到趨勢轉向）；
          scaled＝金字塔（回檔可加倉、超買減碼、趨勢轉向全出）。
成本：手續費買賣各收、證交稅只在賣出收（見 settings BACKTEST_FEE_BPS/TAX_BPS）。
還原股價：run_backtest 收「已還原」的 bars；報酬以還原收盤算＝已含配息的總報酬。
"""

import json
import logging
import statistics
from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime

from config import settings
from integration.trend_signal import (
    GATE_PASS,
    GATE_UNKNOWN,
    UP,
    TrendParams,
    evaluate_series,
)

logger = logging.getLogger(__name__)


@dataclass
class BacktestParams:
    """回測可調參數，預設取自 settings（可注入覆寫做參數掃描/逐版本比較）。"""

    entry_mode: str = settings.BACKTEST_ENTRY_MODE
    max_units: int = settings.BACKTEST_MAX_UNITS
    tranche_sizing: str = settings.BACKTEST_TRANCHE_SIZING
    add_cooldown_days: int = settings.BACKTEST_ADD_COOLDOWN_DAYS
    fee_bps: float = settings.BACKTEST_FEE_BPS
    tax_bps: float = settings.BACKTEST_TAX_BPS
    initial_capital: float = settings.BACKTEST_INITIAL_CAPITAL
    annualization_days: int = settings.BACKTEST_ANNUALIZATION_DAYS
    assume_gate_pass: bool = settings.BACKTEST_ASSUME_GATE_PASS
    trend_params: TrendParams | None = None

    @property
    def effective_max_units(self) -> int:
        """single 模式強制 1 批；scaled 用設定的批數（至少 1）。"""
        return 1 if self.entry_mode == "single" else max(1, self.max_units)

    @property
    def trim_on_overbought(self) -> bool:
        """single 模式抱到趨勢轉向、不因超買減碼；scaled 才減碼。"""
        return self.entry_mode != "single"


def _tranche_weights(units: int, sizing: str) -> list[float]:
    """每批佔用資金比例（和為 1）。equal 均分；decreasing 遞減 (n,n-1,…,1)/Σ ＝ ½⅓⅙…。"""
    units = max(1, units)
    raw = [float(units - i) for i in range(units)] if sizing == "decreasing" else [1.0] * units
    total = sum(raw)
    return [r / total for r in raw]


def _days(d1: str, d2: str) -> int:
    """兩個 YYYY-MM-DD 的日曆天數差（持有天數）。解析失敗回 0。"""
    try:
        return (_date.fromisoformat(d2) - _date.fromisoformat(d1)).days
    except ValueError:
        return 0


def run_backtest(bars: list[dict], params: BacktestParams | None = None,
                 stock_id: str = "") -> dict:
    """對單檔還原日K（舊→新）回測趨勢策略，回傳 {metrics, trades, equity_curve, params}。

    純函式：bars 每根需含 date/open/close（high/low 供趨勢判斷）。空序列拋 ValueError。
    """
    if not bars:
        raise ValueError("run_backtest: bars 不可為空")
    p = params or BacktestParams()
    gate = GATE_PASS if p.assume_gate_pass else GATE_UNKNOWN
    max_units = p.effective_max_units
    weights = _tranche_weights(max_units, p.tranche_sizing)
    capital = float(p.initial_capital)
    fee_rate = p.fee_bps / 10000.0
    tax_rate = p.tax_bps / 10000.0

    n = len(bars)
    dates = [b["date"] for b in bars]
    closes = [b["close"] for b in bars]
    opens = [b.get("open", b["close"]) for b in bars]

    signals = evaluate_series(bars, p.trend_params, gate)

    cash = capital
    lots: list[dict] = []          # 未平倉批次（LIFO 減碼/出場）
    trades: list[dict] = []
    equity_curve: list[dict] = []
    peak = capital
    last_add_i = -(10 ** 9)

    def buy_unit(exec_i: int, unit_index: int) -> None:
        nonlocal cash
        price = opens[exec_i]
        if price <= 0:
            return
        notional = weights[unit_index] * capital
        fee = notional * fee_rate
        cash -= notional + fee
        lots.append({"shares": notional / price, "entry_price": price,
                     "entry_date": dates[exec_i], "unit_index": unit_index,
                     "entry_fee": fee})

    def sell_lot(exec_i: int, lot: dict, price: float, reason: str) -> None:
        nonlocal cash
        proceeds = lot["shares"] * price
        exit_fee = proceeds * fee_rate
        tax = proceeds * tax_rate
        cash += proceeds - exit_fee - tax
        gross = (price - lot["entry_price"]) * lot["shares"]
        basis = lot["entry_price"] * lot["shares"]
        net = gross - lot["entry_fee"] - exit_fee - tax
        trades.append({
            "stock_id": stock_id,
            "entry_date": lot["entry_date"], "entry_price": round(lot["entry_price"], 4),
            "exit_date": dates[exec_i], "exit_price": round(price, 4),
            "shares": round(lot["shares"], 4), "unit_index": lot["unit_index"],
            "gross_pnl": round(gross, 2), "fees": round(lot["entry_fee"] + exit_fee, 2),
            "tax": round(tax, 2), "net_pnl": round(net, 2),
            "return_pct": round(net / basis * 100, 4) if basis else 0.0,
            "hold_days": _days(lot["entry_date"], dates[exec_i]),
            "exit_reason": reason,
        })

    for i in range(n):
        # 依「前一根(i-1)收盤」的訊號，於本根(i)開盤成交（次日成交，無未來函數）
        if i >= 1:
            sig = signals[i - 1]
            big, action, position = sig["big_trend"], sig["action_hint"], sig["position_hint"]
            if big != UP:
                while lots:                                   # 大勢轉向 → 全數出場
                    sell_lot(i, lots.pop(), opens[i], "trend_exit")
            elif not lots:
                if action == "關注建倉區":                     # 首批進場
                    buy_unit(i, 0)
                    last_add_i = i
            elif p.trim_on_overbought and position == "減倉":   # 超買遠離 → 減一批
                sell_lot(i, lots.pop(), opens[i], "trim_overbought")
            elif (len(lots) < max_units and position == "可加倉"
                  and action != "不宜追高" and (i - last_add_i) >= p.add_cooldown_days):
                buy_unit(i, len(lots))                         # 回檔企穩 → 加一批
                last_add_i = i

        price = closes[i]                                     # 本根收盤 mark-to-market
        equity = cash + sum(lot["shares"] for lot in lots) * price
        peak = max(peak, equity)
        equity_curve.append({
            "date": dates[i], "equity": round(equity, 2),
            "drawdown": round(equity / peak - 1, 6) if peak else 0.0,
            "units": len(lots), "price": round(price, 4),
        })

    # 收尾：最後一根收盤強制平倉未平倉批次，使所有交易皆為完整回合（reason=end_of_data）
    while lots:
        sell_lot(n - 1, lots.pop(), closes[-1], "end_of_data")
    # 平倉後最後一點權益＝已實現現金（收尾出場的手續費/稅已入帳），
    # 讓 final_equity 對得上 capital + Σnet_pnl（否則收尾成本會被漏算、報酬高估）。
    if equity_curve:
        equity_curve[-1]["equity"] = round(cash, 2)
        equity_curve[-1]["drawdown"] = round(cash / peak - 1, 6) if peak else 0.0
        equity_curve[-1]["units"] = 0

    metrics = _metrics(equity_curve, trades, closes, capital, p)
    return {
        "stock_id": stock_id, "n_bars": n,
        "date_from": dates[0], "date_to": dates[-1],
        "entry_mode": p.entry_mode, "max_units": max_units,
        "trades": trades, "equity_curve": equity_curve,
        "metrics": metrics, "params": _params_snapshot(p),
    }


def _metrics(curve: list[dict], trades: list[dict], closes: list[float],
             capital: float, p: BacktestParams) -> dict:
    """從權益曲線 + 交易明細算標準績效指標（報酬/CAGR/回撤/Sharpe/勝率/盈虧比/期望值）。"""
    eq = [c["equity"] for c in curve]
    final = eq[-1] if eq else capital
    total_return = final / capital - 1.0 if capital else 0.0

    rets = [eq[i] / eq[i - 1] - 1.0 for i in range(1, len(eq)) if eq[i - 1]]
    if len(rets) >= 2:
        sd = statistics.pstdev(rets)
        sharpe = statistics.fmean(rets) / sd * (p.annualization_days ** 0.5) if sd else 0.0
    else:
        sharpe = 0.0

    max_dd = min((c["drawdown"] for c in curve), default=0.0)   # 最負值
    years = len(curve) / p.annualization_days if p.annualization_days else 0.0
    cagr = (final / capital) ** (1 / years) - 1 if years > 0 and final > 0 and capital else 0.0

    n_trades = len(trades)
    wins = [t["net_pnl"] for t in trades if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in trades if t["net_pnl"] <= 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    # profit_factor：無虧損時無定義 → None（畫面顯示 ∞），避免 JSON/DB 存 inf
    profit_factor = round(gross_win / gross_loss, 3) if gross_loss else None

    # 基準：從「策略首次進場」當根還原收盤買進抱到底（與策略同起點才公平；未進場則從頭抱）。
    # 與 pages._equity_chart 的基準線用同一個 first_active，卡片數字才對得上圖。
    first_active = next((i for i, c in enumerate(curve) if c["units"] > 0), 0)
    bh_base = closes[first_active] if closes else 0.0
    benchmark_return = closes[-1] / bh_base - 1.0 if bh_base else 0.0
    exposure = sum(1 for c in curve if c["units"] > 0) / len(curve) if curve else 0.0

    return {
        "final_equity": round(final, 2),
        "total_return": round(total_return, 4),
        "cagr": round(cagr, 4),
        "max_drawdown": round(max_dd, 4),          # 負值
        "sharpe": round(sharpe, 3),
        "n_trades": n_trades,
        "win_rate": round(len(wins) / n_trades, 4) if n_trades else 0.0,
        "profit_factor": profit_factor,
        "expectancy": round(sum(t["net_pnl"] for t in trades) / n_trades, 2) if n_trades else 0.0,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "payoff": round(avg_win / abs(avg_loss), 3) if avg_loss else 0.0,
        "benchmark_return": round(benchmark_return, 4),
        "exposure_pct": round(exposure, 4),
    }


def _params_snapshot(p: BacktestParams) -> dict:
    """回測參數 + rule_version 快照（存 params_json，供逐版本比較與重現）。"""
    tp = p.trend_params or TrendParams()
    return {
        "entry_mode": p.entry_mode, "max_units": p.effective_max_units,
        "tranche_sizing": p.tranche_sizing, "add_cooldown_days": p.add_cooldown_days,
        "fee_bps": p.fee_bps, "tax_bps": p.tax_bps,
        "initial_capital": p.initial_capital, "assume_gate_pass": p.assume_gate_pass,
        "trend_rule_version": settings.TREND_RULE_VERSION,
        "backtest_rule_version": settings.BACKTEST_RULE_VERSION,
        "trend_params": {
            "ma_big": tp.ma_big, "ma_center": tp.ma_center, "ma_fast": tp.ma_fast,
            "slope_lookback": tp.slope_lookback, "slope_deadband": tp.slope_deadband,
            "overbought_bias": tp.overbought_bias, "confirm_bars": tp.confirm_bars,
        },
    }


def run_and_save(stock_id: str, conn, params: BacktestParams | None = None) -> dict | None:
    """讀 raw_stock_daily → 還原 → run_backtest → 存 backtest_runs/trades/equity。

    回傳含 run_id 的結果 dict；無K線回 None。（唯讀來源，只寫 backtest_* 表。）
    """
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume FROM raw_stock_daily "
        "WHERE stock_id = ? AND open IS NOT NULL AND high IS NOT NULL "
        "AND low IS NOT NULL AND close IS NOT NULL ORDER BY date ASC",
        (stock_id,),
    ).fetchall()
    bars = [{"date": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4]}
            for r in rows]
    if not bars:
        logger.info("run_and_save: no OHLC for %s", stock_id)
        return None

    from collectors.dividend import load_dividend_factors
    from integration.price_adjust import back_adjust
    bars = back_adjust(bars, load_dividend_factors(conn, stock_id))

    result = run_backtest(bars, params, stock_id=stock_id)
    result["run_id"] = _save_run(conn, result)
    logger.info("Backtest %s [%s]: %d trades, total_return=%s, run_id=%s",
                stock_id, result["entry_mode"], result["metrics"]["n_trades"],
                result["metrics"]["total_return"], result["run_id"])
    return result


def _save_run(conn, result: dict) -> int:
    """存一次 run 摘要 + 逐筆交易 + 權益曲線，回傳 backtest_runs.id。"""
    m = result["metrics"]
    cur = conn.execute(
        """INSERT INTO backtest_runs
               (created_at, stock_id, entry_mode, max_units, date_from, date_to, n_bars,
                n_trades, total_return, cagr, max_drawdown, sharpe, win_rate, profit_factor,
                expectancy, benchmark_return, exposure_pct, final_equity,
                trend_rule_version, backtest_rule_version, params_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now().isoformat(), result["stock_id"], result["entry_mode"],
         result["max_units"], result["date_from"], result["date_to"], result["n_bars"],
         m["n_trades"], m["total_return"], m["cagr"], m["max_drawdown"], m["sharpe"],
         m["win_rate"], m["profit_factor"], m["expectancy"], m["benchmark_return"],
         m["exposure_pct"], m["final_equity"], settings.TREND_RULE_VERSION,
         settings.BACKTEST_RULE_VERSION, json.dumps(result["params"], ensure_ascii=False)),
    )
    run_id = cur.lastrowid
    for t in result["trades"]:
        conn.execute(
            """INSERT INTO backtest_trades
                   (run_id, stock_id, entry_date, entry_price, exit_date, exit_price,
                    shares, unit_index, gross_pnl, fees, tax, net_pnl, return_pct,
                    hold_days, exit_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, t["stock_id"], t["entry_date"], t["entry_price"], t["exit_date"],
             t["exit_price"], t["shares"], t["unit_index"], t["gross_pnl"], t["fees"],
             t["tax"], t["net_pnl"], t["return_pct"], t["hold_days"], t["exit_reason"]),
        )
    for e in result["equity_curve"]:
        conn.execute(
            """INSERT INTO backtest_equity (run_id, date, equity, drawdown, units, price)
               VALUES (?, ?, ?, ?, ?, ?)""",   # run_id 每次新生，(run_id,date) 不會撞
            (run_id, e["date"], e["equity"], e["drawdown"], e["units"], e["price"]),
        )
    conn.commit()
    return run_id
