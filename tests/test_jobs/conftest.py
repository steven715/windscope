"""job 測試共用：JobRun 的唯一 seam（jobs.helpers.run_step_value）。"""

import pytest


@pytest.fixture
def step_seam(monkeypatch):
    """把 jobs.helpers.run_step_value 換成 fake(name, fn) -> (value, err)。

    value_steps（預設 signal／summary：要拿回傳值的步驟）改走 value_fake，預設照真的執行。
    """
    from jobs import helpers

    real = helpers.run_step_value

    def install(fake, value_fake=None, value_steps=("signal", "summary")):
        vf = value_fake or real
        monkeypatch.setattr(
            helpers, "run_step_value",
            lambda name, fn: vf(name, fn) if name in value_steps else fake(name, fn))
    return install
