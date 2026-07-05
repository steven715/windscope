# ADR-0001：不使用 pandas，採 stdlib-first 資料處理

- **狀態**：Accepted
- **日期**：2026-07-05
- **脈絡背景**：原始決策在早期 AI 協作時做成，當時未留下書面理由；本 ADR 為回溯補記，讓「為什麼不用 pandas」有明確依據。

---

## 脈絡（Context）

專案是台股開盤前情報系統，**資料量極小**：每日新增數十列、追蹤少數個股、每檔日K歷史數百到數千根 bar，整個資料庫是 MB 級。

技術棧刻意走「**標準庫優先、最小依賴**」：SQLite 只用 stdlib `sqlite3`、不用 ORM、Web 用 Jinja server-render 不用前端框架（見 `CLAUDE.md` 硬約束 #1）。引入 pandas 的可能性當時被否決，但未留書面理由，導致日後無法回顧判斷是否仍成立。

## 決策（Decision）

資料處理**不使用 pandas**（亦不預設引入 numpy）。以 stdlib 的 `list`/`dict` + 明確迴圈 + 型別註記的純函式處理；需要的計算（移動平均、還原股價、趨勢指標、回測績效）自行以標準庫實作。

## 理由（Rationale）

1. **在這個資料量下 pandas 零效益。** pandas 的價值在於對大表做向量化運算；在數百列的規模，原生 Python 迴圈本來就是瞬間完成，DataFrame 只增加一層抽象、不帶來任何加速。
2. **依賴體積與維護面。** pandas（＋numpy）是數十 MB 的 C 擴充相依，對單機 Docker 部署平白脹大 image，並帶來版本/ABI 相容與 API 汰換（deprecation）的長期維護成本——換不到任何好處。
3. **明確性與可審查性。** 對一個由單人維護、正確性關鍵的財務管線，顯式的 stdlib 迴圈比 pandas 一行式更好推理、測試、審查。pandas 會隱藏 index 對齊、NaN 傳播、dtype 自動轉換、`SettingWithCopyWarning` 等語意陷阱。
4. **資料模型一致性。** 全專案已以 `dict`/`list` + `sqlite3.Row` 為統一資料表示；引入 DataFrame 會產生第二套平行資料模型，測試（`:memory:` sqlite + 純 dict/list fixture）也會跟著分裂。
5. **未來函數（look-ahead bias）風險——與即將開發的回測層特別相關。** pandas 讓 `df.shift(-1)`、整欄運算、`.rolling()` 這類「不小心用到未來資料」變得太容易；顯式逐 bar 迴圈讓「決策只用 ≤t 的資料」在程式碼上一目了然，是回測正確性的護欄。

## 承擔的代價（Consequences）

- 部分操作（rolling MA、join、groupby）在 stdlib 較囉嗦。可接受；專案已自行實作 `_sma` 等輔助函式。
- 失去 pandas 生態的現成工具（讀 CSV、繪圖橋接等），改以標準庫或既有純函式處理。

## 替代方案（Alternatives considered）

- **pandas**：如上——規模不匹配、依賴過重。
- **polars**（2025–2026 新專案取代 pandas 的趨勢選項）：比 pandas 輕快、API 更嚴謹（無隱式 index），但仍是重量級外部相依，在本專案資料規模同樣無效益。**若日後真要向量化，polars 優先於 pandas 評估。**
- **單獨引入 numpy**：比 pandas 輕，但同樣在此規模無明顯效益；暫不引入，維持零數值相依。

## 何時重新考慮（Revisit triggers）

- 單表資料量成長到十萬～百萬列級，原生迴圈出現體感延遲。
- 導入需要大量數值運算的功能（大規模參數掃描最佳化、統計/ML 模型）。
- 屆時**優先評估 polars**，並僅在真正的效能熱點局部引入，而非全面改寫。
