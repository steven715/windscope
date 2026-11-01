# strategylib

windscope 與 tradewind **共用的純函式策略庫**。回測（windscope）與交易引擎（tradewind）import 同一份程式，
「回測跑 production 邏輯」才成立（windscope ADR-0005 §3、ADR-0007；tradewind ADR-0008）。

## 內容

| 模組 | 內容 | 版本 |
|---|---|---|
| `strategylib.trend` | 趨勢三態（UP／DOWN／FLAT／UNKNOWN）、小勢、行動／持倉提示：`evaluate_series(bars, params, gate)` | `RULE_VERSION`（t2） |
| `strategylib.adjust` | 還原股價純函式：`back_adjust_with_events(bars, div_events, gap_pct)` | — |
| `strategylib.playbooks` | Playbook 程式（契約 `trade_plan.playbook_id`）：`get_playbook("pullback_hold", "p1").step(state, bars_1m, plan, direction, prev_close)` | 各 playbook 自帶 `VERSION` |

## 規則

- 只 import 標準庫與 `strategylib` 自己；不碰 settings、DB、網路、時鐘（`tests/test_strategylib/test_purity.py` 擋）。
- 所有門檻的**單一真相在這裡**（`trend.DEFAULTS`、各 playbook 的 `DEFAULT_PARAMS`）；windscope 的 `config/settings.py` 從這裡取值，不另存一份。
- 改任何會改變輸出的規則 → bump 該模組的 `RULE_VERSION`／`VERSION`，並 bump 套件 `__version__`（不同版本的績效不混算）。
- 決定性：同樣的輸入永遠得到同樣的輸出；回放一致性靠它。

## tradewind 怎麼用

```
pip install "git+https://github.com/steven715/windscope@strategylib-v0.1.0#subdirectory=strategylib"
```

tag 格式 `strategylib-vX.Y.Z`，對應 `__version__`。tradewind 的 `plan/` 只呼叫 `get_playbook(...).step(...)`，
不自行重寫任何規則。
