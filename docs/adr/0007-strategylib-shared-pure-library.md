# ADR-0007：共用純函式策略庫 `strategylib/` 放在 windscope 子目錄，以 git tag 發布

- **狀態**：Accepted（2026-11-01，依 ADR-0005 §3／§5 與 tradewind ADR-0008 落地）
- **日期**：2026-11-01
- **脈絡背景**：ADR-0005 定了「策略與特徵是共用純函式庫，不是資料交換」；tradewind ADR-0008 定了
  「Playbook 是程式、放共用庫」。但庫放哪、怎麼版本、門檻的真相在誰那裡，一直沒落地。
  windscope 的 `integration/trend_signal.py` 混著規則與 DB 存取、門檻在 `config/settings.py`，tradewind 無法 import。

---

## 決策（Decision）

1. **位置**：`strategylib/` 是 windscope repo 的子目錄，自帶 `pyproject.toml`（套件名 `windscope-strategylib`），
   不另開 repo（ADR-0005 §5 的回頭條件仍在：共用庫一週要發 ≥2 次 tag 就改 monorepo）。
2. **內容**：`strategylib.trend`（趨勢三態）、`strategylib.adjust`（還原股價）、`strategylib.playbooks`
   （契約 `trade_plan.playbook_id/version` 對應的程式，`get_playbook(id, version).step(state, bars_1m, plan, direction, prev_close)`）。
3. **純度**：只 import 標準庫與自己；不碰 settings、DB、網路、時鐘、隨機（`tests/test_strategylib/test_purity.py` 擋，
   `sqlite3`／`datetime`／`time`／`random`／`os` 也在禁用清單）。同樣輸入永遠同樣輸出，回放一致性靠它。
4. **門檻的單一真相移到庫裡**：`strategylib.trend.DEFAULTS`／`RULE_VERSION`、`strategylib.adjust.DEFAULT_GAP_PCT`、
   各 playbook 的 `DEFAULT_PARAMS`／`VERSION`。`config/settings.py` 的 `TREND_*` 改為**從庫取值**（不再是第二份），
   硬規則 6「門檻集中 settings」對趨勢層改讀為「settings 轉述 strategylib」；改門檻去庫裡改並 bump 該模組版本。
5. **windscope 端薄層**：`integration/trend_signal.py`／`price_adjust.py` 只重新匯出純函式＋DB 存取，所有既有 import 不變。
6. **版本與安裝**：套件 `__version__` 與 `pyproject.toml` 同步（測試擋）；tag 格式 `strategylib-vX.Y.Z`；tradewind 用
   `pip install "git+https://github.com/steven715/windscope@strategylib-v0.1.0#subdirectory=strategylib"`，
   並在自己的 `contracts/COMMIT` 旁記下用的 tag。
7. **golden 與時點正確性**：`tests/fixtures/strategylib/trend_golden.json` 由重構前的程式產生，重構後逐根相同；
   `test_pit.py` 證明還原是尺度變換——t 之後才知道的配息不改變 ≤t 的趨勢輸出，所以回測用整段還原序列不構成前視偏差。

## 理由（Rationale）

- 回測跑 production 邏輯只有一種可靠做法：兩邊 import 同一份 code。
- 放子目錄而非新 repo：現在只有 windscope 一個生產者、一個消費者，第三個 repo 的發布流程是純負擔。
- 門檻真相移到庫裡是必要的：tradewind 沒有 windscope 的 settings，若真相留在 settings，庫裡的預設值就會與它分岔。
- 禁用 `datetime`／`time`：Playbook 的「現在」必須來自 K 棒時刻，不能來自牆鐘，否則回放與實時不同。

## 承擔的代價（Consequences）

- 改趨勢門檻的流程多一步：改 `strategylib`、bump 版本、打 tag、tradewind 更新 tag。
- golden fixture 要在**有意**改規則時重產（先 bump `RULE_VERSION`，再重產，commit 一起進）。
- `strategylib/` 進了 windscope 的 Docker image，但它不依賴任何東西，無害。
- Playbook 只看自己商品的 K 棒；跨商品的失效條件（`invalidation`）仍由呼叫端（tradewind Trade Management）判定。

## 替代方案（Alternatives considered）

- **第三個 repo**：發布與版本管理成本高，等第二個生產者再搬（ADR-0005 §5）。
- **tradewind vendored 複製一份**：兩份必分岔；否決。
- **門檻留在 settings、庫裡讀環境變數**：庫不再純、tradewind 還是要帶一份值；否決。

## 何時重新考慮（Revisit triggers）

- 一週內要發 ≥2 次 strategylib tag → monorepo（uv workspace）。
- tradewind 需要庫裡沒有的特徵且要在每根 bar 上算 → 先加進庫，不在 tradewind 寫。
- Playbook 數量 >5 或需要跨商品狀態 → 重新評估 `step` 介面（可能改成拿 MarketView）。
