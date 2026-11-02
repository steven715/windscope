"""Playbook 介面：`step(state, bars_1m, plan, direction, prev_close) -> (state, actions)`。

- 純函式、決定性：同樣的 (state, bars) 永遠得到同樣的 actions（回放一致性靠它）。
- 呼叫時機：每收完一根 1 分 K 呼叫一次（tradewind 的 Trade Management），或對整個交易日逐根重放
  （windscope 回測）。Playbook 只看自己商品的 K 棒；跨商品的失效條件（invalidation）由呼叫端判定。
- actions 是「建議的動作」，呼叫端負責成交、記 events；Playbook 不知道口數與帳戶（口數由 trade_plan.risk 反推）。
"""

from dataclasses import asdict, dataclass, field
from typing import Protocol

# 階段
IDLE, ARMED, ENTERED, SCALED_OUT, EXITED, NO_TRADE = "idle", "armed", "entered", "scaled_out", "exited", "no_trade"
# Action.kind 字彙＝契約 events.schema.json 的 plan/* action（tradewind 回流時直接用，不需對照）
KINDS = ("armed", "entered", "scaled_out", "stop_tightened", "exited", "force_flat",
         "no_trade_hit", "chase_abandoned", "expired")


@dataclass
class Action:
    """一個建議動作。price 為建議成交價（以該根 K 的價位計）；fraction 為部位比例（0–1）。"""

    kind: str                    # 見 KINDS（＝契約 events 的 plan/* action）
    ts: str                      # 觸發的 K 棒結束時刻（契約格式 +08:00；只支援日盤，不跨日）
    price: float | None = None
    fraction: float | None = None
    stop_price: float | None = None
    reason: str | None = None
    r_multiple: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown action kind {self.kind!r}; expected one of {KINDS}")

    def as_dict(self) -> dict:
        """轉 dict（給 events.detail／測試）。"""
        return asdict(self)


@dataclass
class PlanState:
    """Trade Management 狀態（可序列化；呼叫端保存、下一次 step 傳回）。"""

    phase: str = IDLE
    direction: int = 0               # +1 多 / -1 空 / 0 無
    range_high: float | None = None  # 開盤區間
    range_low: float | None = None
    session_open: float | None = None
    session_start: str | None = None   # HH:MM，開盤區間與桶對齊的基準
    session_date: str | None = None    # 目前處理中的交易日（跨日 Playbook 用來偵測換日）
    days_held: int = 0                 # 跨日 Playbook：持倉經過的交易日數（進場日＝0）
    day_low: float | None = None       # 當日至今最低／最高（跨日 Playbook 的移停基準）
    day_high: float | None = None
    prev_day_low: float | None = None  # 前一交易日的最低／最高
    prev_day_high: float | None = None
    bars_seen: int = 0
    confirm_count: int = 0           # 連續符合觸發條件的桶數
    entries: int = 0
    entry_price: float | None = None
    stop_price: float | None = None
    open_fraction: float = 0.0       # 仍持有的部位比例
    scaled: list[int] = field(default_factory=list)   # 已觸發的 scaling 索引
    prev_5m_low: float | None = None
    prev_5m_high: float | None = None
    high_water_r: float = 0.0
    note: str | None = None

    def as_dict(self) -> dict:
        """轉 dict（給呼叫端持久化）。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "PlanState":
        """從 dict 還原；None → 新狀態。"""
        return cls(**d) if d else cls()


class Playbook(Protocol):
    """Playbook 程式介面。"""

    playbook_id: str
    version: str

    def step(self, state: PlanState, bars_1m: list[dict], plan: dict, direction: int,
             prev_close: float | None) -> tuple[PlanState, list[Action]]:
        """處理最新一根已收完的 1 分 K（bars_1m[-1]），回傳新狀態與建議動作。"""
        ...


def hhmm(ts: str) -> str:
    """契約時刻 → HH:MM。"""
    return ts[11:16]


def bar_end(ts: str, minutes: int = 1) -> str:
    """K 棒開始時刻 → 結束時刻（同一天內；跨日的夜盤 K 棒呼叫端自行處理）。"""
    h, m = int(ts[11:13]), int(ts[14:16])
    m += minutes
    h, m = h + m // 60, m % 60
    return f"{ts[:11]}{h % 24:02d}:{m:02d}{ts[16:]}"
