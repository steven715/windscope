# ADR-0002：排程設定維持兩張表，以單一 module 為唯一入口

- **狀態**：Accepted
- **日期**：2026-10-11
- **脈絡背景**：2026-10-09 架構審查（improve-codebase-architecture）候選 #7 提議把 `job_config` 與 `schedule_config` 合併成單一「job settings」。評估後決定不合併；本 ADR 記錄理由，避免日後審查重複提出。

---

## 脈絡（Context）

排程器的使用者覆寫設定存在兩張表：

| 表 | 欄位 | 用途 |
|---|---|---|
| `schedule_config` | `job_id`, `time_hhmm` | 每日 job 的執行時間 |
| `job_config` | `job_id`, `display_name`, `display_desc`, `notify_enabled`, `enabled` | 顯示名稱、說明、通知開關、啟用 |

`CLAUDE.md` 硬約束 #10 要求每份設定只有「單一真相來源＋單一存取入口」，看起來兩張表像是違反。

## 決策（Decision）

**維持兩張表，不合併。** 兩張表的所有讀寫只經 `server/scheduler.py`（`get_schedule_times`／`set_schedule_time`、`get_job_overrides`／`_job_override`／`set_job_*`），其他 module 不得直接讀寫這兩張表。

## 理由（Rationale）

1. **沒有違反 #10。** 兩張表存的是**不同欄位**，沒有同一份設定存兩處；入口也只有一個 module。#10 防的是「同一份配置在不同地方各自讀／寫造成分岔」（如當年 watchlist 的 json＋DB 雙源），此處不成立。
2. **合併的收益低、風險高。** 合併需要對線上 `data/premarket.db` 做 schema 遷移與資料搬移（硬約束 #9 要求不可逆動作先確認），換來的只是少一張表；介面（`server/scheduler.py` 的函式）對呼叫端不會變得更簡單。
3. **兩者生命週期不同。** 時間只對 `trigger.kind == "daily"` 的 job 有意義並需要立即 reschedule；其餘覆寫對所有 job 適用，只影響顯示與 runner 行為。分開存放反映這個差異。

## 承擔的代價（Consequences）

- 讀一個 job 的完整設定要查兩張表（目前由 `get_jobs_info` 組合，資料量極小，無效能問題）。
- `get_job_overrides`（全部）與 `_job_override`（單筆）各自解析同一列形狀，屬小重複，可接受。
- **守則**：若日後新增排程相關覆寫欄位，加進 `job_config` 並只經 `server/scheduler.py` 存取；若發現其他 module 直接讀寫這兩張表，視為違反 #10 的 bug。
