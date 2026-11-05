"""目录倍率的实测校准（`Executor._model_rates` / `_observed_paid_rate`）。

校准只纠正**目录说 x0、但上游明确扣过费**的那一格；其余情况一律保持目录
原值——目录给的正数已经是上游自报倍率，未知（None）也不该被观测值改写。
"""

from __future__ import annotations

from src.engine.executor import Executor, ExecutorDeps
from src.engine.model_resolver import ModelTarget
from src.engine.scheduler import Scheduler
from src.provider.base import Model


class _Repo:
    """`_model_rates` 只用到 credentials 之外的注入，仓储不参与。"""


def _executor(*, paid, declared: float | None,
              model_id: str = "deepseek-v4.1-flash") -> Executor:
    return Executor(ExecutorDeps(
        providers={}, credentials=_Repo(), scheduler=Scheduler(),
        model_aliases={"codebuddy_intl": {model_id: model_id}},
        model_list_cache={"codebuddy_intl": {
            model_id: Model(id=model_id, credit_rate=declared)}},
        paid_models=paid))


def _target(model: str = "deepseek-v4.1-flash") -> ModelTarget:
    return ModelTarget(model=model, providers=("codebuddy_intl",))


def test_zero_rate_replaced_by_observed_average():
    """x0 + 有实扣 → 用实测均值（非 0，免费档失效）。"""
    executor = _executor(paid=lambda: {
        ("codebuddy_intl", "deepseek-v4.1-flash"): 0.1372}, declared=0.0)
    assert executor._model_rates(_target()) == {"codebuddy_intl": 0.1372}


def test_zero_rate_kept_when_no_observation():
    """x0 + 窗口内没观测到扣费 → 仍信目录（夜里没人用不是免费的证据）。"""
    executor = _executor(paid=lambda: {}, declared=0.0)
    assert executor._model_rates(_target()) == {"codebuddy_intl": 0.0}


def test_positive_rate_not_overridden():
    """目录给了正数倍率 → 不被实测均值覆盖（上游自报更贴渠道语义）。"""
    calls: list[int] = []

    def paid():
        calls.append(1)
        return {("codebuddy_intl", "deepseek-v4.1-flash"): 0.9}

    executor = _executor(paid=paid, declared=0.11)
    assert executor._model_rates(_target()) == {"codebuddy_intl": 0.11}
    # 未调用观测源：非 x0 走不到校准
    assert calls == []


def test_unknown_rate_stays_unknown():
    """目录没给倍率（None）→ 保持 None，不因实扣记录变成「付费倍率」。"""
    executor = _executor(paid=lambda: {
        ("codebuddy_intl", "deepseek-v4.1-flash"): 0.5}, declared=None)
    assert executor._model_rates(_target()) == {"codebuddy_intl": None}


def test_calibration_absent_keeps_catalog_rate():
    """未装配观测源（旧装配 / 测试）→ 与加校准前逐字节一致。"""
    executor = _executor(paid=None, declared=0.0)
    assert executor._model_rates(_target()) == {"codebuddy_intl": 0.0}


def test_model_missing_from_cache_is_skipped():
    """目录里没有这条（该渠道未登记该模型）→ 不产生键，调度按未知处理。"""
    executor = Executor(ExecutorDeps(
        providers={}, credentials=_Repo(), scheduler=Scheduler(),
        model_aliases={"codebuddy_intl": {"other": "other"}},
        model_list_cache={"codebuddy_intl": {}},
        paid_models=lambda: {}))
    assert executor._model_rates(_target()) == {}
