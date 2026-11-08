# strategylib

windscope 與 tradewind **共用的純函式策略庫**。回測（windscope）與交易引擎（tradewind）import 同一份程式，
「回測跑 production 邏輯」才成立（windscope ADR-0005 §3、ADR-0007；tradewind ADR-0008）。

## 內容

| 模組 | 內容 | 版本 |
|---|---|---|
| `strategylib.trend` | 趨勢三態（UP／DOWN／FLAT／UNKNOWN）、小勢、行動／持倉提示：`evaluate_series(bars, params, gate)` | `RULE_VERSION`（t2） |
| `strategylib.adjust` | 還原股價純函式：`back_adjust_with_events(bars, div_events, gap_pct)` | — |
| `strategylib.roll` | 期貨連續代號的換月：`front_contract(continuous, instruments, session_date, holidays)`（`roll_rule=days_before_expiry:N`，換月日＝到期日往前第 N 個交易日；0.3.0 起，windscope 串流用它決定連續代號該接哪一口，語意與 tradewind `market/contracts` 相同） | `ROLL_VERSION`（roll1） |
| `strategylib.playbooks` | Playbook 程式（契約 `trade_plan.playbook_id`）：`get_playbook(id, ver).step(state, bars_1m, plan, direction, prev_close)`。`pullback_hold p1`（微台日內）、`trend_pullback p1`（個股波段：窗口內第一根 1 分 K 收完進場、停損跨日、分批；趨勢進出由上游新 target 表達） | 各 playbook 自帶 `VERSION` |

## 規則

- 只 import 標準庫與 `strategylib` 自己；不碰 settings、DB、網路、時鐘（`tests/test_strategylib/test_purity.py` 擋）。
- 所有門檻的**單一真相在這裡**（`trend.DEFAULTS`、各 playbook 的 `DEFAULT_PARAMS`）；windscope 的 `config/settings.py` 從這裡取值，不另存一份。
- 改任何會改變輸出的規則 → bump 該模組的 `RULE_VERSION`／`VERSION`，並 bump 套件 `__version__`（不同版本的績效不混算）。
- 決定性：同樣的輸入永遠得到同樣的輸出；回放一致性靠它。

## Playbook 的呼叫契約

- `step` 每收完一根 1 分 K 呼叫一次；`bars_1m` 是**本交易日日盤**的 1 分 K（舊→新），只支援 `session=regular`，夜盤回 `no_trade_hit`。
- 5 分 K 依**時刻**分桶（`minute // 5`），桶的最後一分鐘收完才評估；缺一根 1 分 K 不會錯位。
- `Action.kind` 字彙＝契約 `events.schema.json` 的 `plan/*` action（`armed`／`entered`／`scaled_out`／`stop_tightened`／
  `exited`／`force_flat`／`no_trade_hit`／`chase_abandoned`／`expired`），tradewind 回流 events 不需對照表。
- **成交價模型**：`Action.price` 是觸發價位（進場＝觸發那根 K 的 close：pullback_hold 是 5 分桶、trend_pullback 是窗口內第一根 1 分 K；停損／分批若被該根 K 跳空穿越＝該根 open，取對自己不利的一邊）。同一根 K 先停損再分批；分批移停後不用同一根的 low 回頭檢查。
  滑價、手續費、口數由呼叫端的撮合／風控層處理，Playbook 不管。
- `max_entries_per_day > 1`：出場後（非強制平倉）回 `armed` 再找下一次觸發，到達上限才 `exited`（pullback_hold）。
- 跨日 Playbook（trend_pullback）：`bars_1m` 仍是「本交易日」的 1 分 K，`PlanState` 由呼叫端跨日保存；`session_date` 變了 Playbook 自己重設當日欄位，部位與停損不動；非 regular 時段的 K 棒一律略過。趨勢轉向／失效由新 target `weight=0` 表達，呼叫端平倉並丟掉狀態。`move_stop_to` 用契約 enum：`prev_bar_low` 在這支＝前一交易日最低（進場日退回 breakeven）。

## tradewind 怎麼用

```
pip install "git+https://github.com/steven715/windscope@strategylib-v0.3.0#subdirectory=strategylib"
```

tag 格式 `strategylib-vX.Y.Z`，對應 `__version__`。tradewind 的 `plan/` 只呼叫 `get_playbook(...).step(...)`，
不自行重寫任何規則。
