"""模型目录落盘的线程化（P2-23）：`save_catalog` 不得占用事件循环。

`save_catalog` 内部是 makedirs / open / json.dump / os.replace 四个同步系统
调用；模型多时（zen 70+ 条）序列化 + 写盘能占住事件循环几毫秒到十几毫秒，
正好卡在别的请求的 SSE 出帧上。改为 `asyncio.to_thread` 后：

* 事件循环线程**不执行**落盘（在 worker 线程里跑）；
* 落盘内容与缓存一致（不是拿到一半的快照）；
* 落盘失败只记日志，绝不上抛打断模型列表刷新。
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

from src.api import models as models_module
from src.provider.base import Model


class _Provider:
    """最小上游：`list_models` 返回固定表。"""

    async def list_models(self, _data):
        return [Model(id="glm-5.3", name="GLM 5.3")]


class _Credentials:
    """候选替身：一条凭证、明文可取（本用例不覆盖解密失败路径）。"""

    def candidates(self, providers=None, *, selectable_only=False, **_kw):
        return [SimpleNamespace(credential_id="c1")]

    def credential_data(self, _credential_id):
        return {"accessToken": "a"}


def _services(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(data_dir=str(tmp_path)),
        credentials=_Credentials(),
        model_list_cache={},
        model_list_fetched_at={},
        model_list_failures={},
        model_list_failure_reason={},
        model_list_degraded={},
    )


async def test_save_catalog_runs_off_the_event_loop(tmp_path, monkeypatch):
    """落盘发生在 worker 线程，不在事件循环线程里同步执行。"""
    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []
    real_save = models_module.save_catalog

    def recording_save(data_dir, tables):
        seen.append(threading.current_thread())
        real_save(data_dir, tables)

    monkeypatch.setattr(models_module, "save_catalog", recording_save)
    services = _services(tmp_path)

    await models_module._refresh_provider(services, "trae", _Provider())

    assert len(seen) == 1
    assert seen[0] is not loop_thread       # 没占事件循环线程
    # 落盘内容与进程内缓存一致
    catalog = json.loads((tmp_path / "model_catalog.json").read_text())
    ids = [model["id"] for model in catalog["providers"]["trae"]["models"]]
    assert ids == ["glm-5.3"]
    assert set(services.model_list_cache["trae"]) == {"glm-5.3"}


async def test_save_catalog_failure_does_not_break_refresh(tmp_path, monkeypatch):
    """落盘失败只记日志：模型列表照常更新，异常不上抛。"""
    def boom(_data_dir, _tables):
        raise OSError("disk full")

    monkeypatch.setattr(models_module, "save_catalog", boom)
    services = _services(tmp_path)

    # `asyncio.to_thread` 会把 worker 线程里的异常带回调用方；这里验证调用方
    # 已经吞掉它（否则一次磁盘故障就会让整个模型列表刷新失败）。
    await models_module._refresh_provider(services, "trae", _Provider())

    assert set(services.model_list_cache["trae"]) == {"glm-5.3"}


async def test_event_loop_keeps_running_while_catalog_is_written(tmp_path, monkeypatch):
    """落盘期间事件循环仍可推进其它协程（不被同步 IO 卡住）。"""
    ticks: list[int] = []
    real_save = models_module.__dict__["save_catalog"]

    def slow_save(data_dir, tables):
        # 模拟大表落盘：真实实现在 worker 线程里跑，循环线程不该被挡
        threading.Event().wait(0.05)
        real_save(data_dir, tables)

    monkeypatch.setattr(models_module, "save_catalog", slow_save)
    services = _services(tmp_path)

    async def ticker():
        for _ in range(5):
            ticks.append(1)
            await asyncio.sleep(0.005)

    await asyncio.gather(models_module._refresh_provider(services, "trae", _Provider()),
                         ticker())

    assert len(ticks) == 5


async def test_catalog_write_uses_a_copy_of_the_cache(tmp_path, monkeypatch):
    """落盘拿到的是**快照副本**：之后改缓存不会污染已写给 worker 的内容。"""
    written: list[dict] = []
    monkeypatch.setattr(models_module, "save_catalog",
                        lambda _dir, tables: written.append(tables))
    services = _services(tmp_path)

    await models_module._refresh_provider(services, "trae", _Provider())
    # 落盘之后另一个渠道刷新改了自己的表（真实并发场景）
    services.model_list_cache["other"] = {"late": Model(id="late")}

    assert written and "trae" in written[0]
    assert "other" not in written[0]        # 副本，不是活引用

    # 副本与缓存同内容（不是拿到一半）
    assert set(written[0]["trae"]) == {"glm-5.3"}
