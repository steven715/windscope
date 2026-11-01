"""Playbook 程式註冊表：契約 `trade_plan.playbook_id`／`playbook_version` → 可執行的純函式物件。"""

from strategylib.playbooks import pullback_hold
from strategylib.playbooks.base import Action, Playbook, PlanState

_REGISTRY: dict[tuple[str, str], Playbook] = {
    (pullback_hold.PLAYBOOK_ID, pullback_hold.VERSION): pullback_hold.PullbackHold(),
}


def get_playbook(playbook_id: str, version: str) -> Playbook:
    """取 Playbook 程式；未知 id／版本 raise KeyError（tradewind 不得用其他版本執行）。"""
    try:
        return _REGISTRY[(playbook_id, version)]
    except KeyError:
        raise KeyError(f"unknown playbook {playbook_id}@{version}; known: {sorted(_REGISTRY)}") from None


def list_playbooks() -> list[tuple[str, str]]:
    """已註冊的 (playbook_id, version)。"""
    return sorted(_REGISTRY)


__all__ = ["Action", "Playbook", "PlanState", "get_playbook", "list_playbooks"]
