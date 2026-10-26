"""模型目录出口 /v1/models：跨渠道同名合并与对外 id 归一/消歧。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.config import Settings
from src.main import build_app
from tests.helpers.constants import SECRET


def test_models_name_passthrough_backfill_and_omit(tmp_path):
    """`name`（上游人类可读名）透传给前端；空名不占位、不产出空字段。

    Qoder 的 id 是 `qmodel_38max` 这类内部代号，仅显示 id 用户无法识别；
    上游 `display_name` 经 `Model.name` 透传后前端才有的可读名。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):
            return raw

    from src.provider.base import Model

    # 上游没给可读名时（name=""）展示名从 id 现派生，两条渠道同 id 自然并成一条。
    # id 本身是纯 free 噪声（`free`）时派生不出名字，才不产出空的 "name" 字段。
    app = build_app(settings, providers={
        "codebuddy": Stub("codebuddy", [Model(id="shared-model", name="")]),
        "trae": Stub("trae", [Model(id="shared-model", name="Shared Model"),
                              Model(id="free")]),
    })
    app.state.credentials.add(provider="codebuddy", credential_data={"token": "a"})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "b"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = {m["id"]: m for m in client.get("/v1/models", headers={
            "Authorization": f"Bearer {key}"}).json()["data"]}
    assert set(data) == {"shared-model", "free"}       # 同 id 并成一条
    assert data["shared-model"]["name"] == "Shared Model"  # 取上游名并清洗
    assert "name" not in data["free"]                  # 派生不出名字则留空

def test_models_merge_by_readable_name_across_channels(tmp_path):
    """CB/TRAE/Qoder 同名不同 id 的模型并成一条，对外 id 用展示名的归一键。

    各渠道同一模型内部代号互不相同（`kimi-k3-1` / `kimi-k3` /
    `kmodel_latest`），只有可读名能对齐；请求转发到某渠道时再经别名表
    换回该渠道自己的原 id（Qoder 仍发 `kmodel_latest`）。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        "codebuddy": Stub("codebuddy",
                          [Model(id="kimi-k3-1", name="Kimi-K3", credit_rate=0.3)]),
        "trae": Stub("trae", [Model(id="kimi-k3", name="Kimi-K3", credit_rate=0.2)]),
        "qoder": Stub("qoder",
                      [Model(id="kmodel_latest", name="Kimi-K3", credit_rate=1.4)]),
    })
    for provider_id in ("codebuddy", "trae", "qoder"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = client.get("/v1/models", headers={
            "Authorization": f"Bearer {key}"}).json()["data"]

    assert [m["id"] for m in data] == ["kimi-k3"]          # 三渠道并成一条
    entry = data[0]
    assert entry["providers"] == ["codebuddy", "qoder", "trae"]
    assert entry["name"] == "Kimi K3"      # 展示名清洗后与对外 id 同源
    # by_provider 带各渠道 raw_id（渠道请求 key，永不改动）与倍率
    assert entry["by_provider"] == {
        "codebuddy": {"raw_id": "kimi-k3-1", "credit_rate": 0.3},
        "qoder": {"raw_id": "kmodel_latest", "credit_rate": 1.4},
        "trae": {"raw_id": "kimi-k3", "credit_rate": 0.2}}
    # 别名表：对外 id 与原 id 都能映射回各渠道自己的上游 id
    aliases = app.state.services.model_aliases
    assert aliases["qoder"]["kimi-k3"] == "kmodel_latest"
    assert aliases["qoder"]["kmodel_latest"] == "kmodel_latest"
    assert aliases["codebuddy"]["kimi-k3"] == "kimi-k3-1"
    assert aliases["trae"]["kimi-k3"] == "kimi-k3"

def test_models_synonym_merges_qoder_deepseek_flash(tmp_path):
    """Qoder 的 `DeepSeek-Flash` 与别家的 `DeepSeek-V4.1-Flash` 并成一条。

    Qoder 上游名不带版本号（`dfmodel` 只叫 `DeepSeek-Flash`），归一键是
    `deepseek-flash`，单靠清洗与 `DeepSeek-V4.1-Flash` 对不上；`MODEL_SYNONYMS`
    把两者收敛到同一个规范键与规范展示名，请求 `deepseek-v4.1-flash` 也能
    路由到 Qoder（经别名表换回原代号 `dfmodel`）。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        "codebuddy": Stub("codebuddy", [Model(id="deepseek-v4.1-flash",
                                              name="Deepseek-V4.1-Flash",
                                              credit_rate=0.11)]),
        "trae": Stub("trae", [Model(id="deepseek-v4.1-flash",
                                    name="DeepSeek-V4.1-Flash", credit_rate=0.08)]),
        "qoder": Stub("qoder", [Model(id="dfmodel", name="DeepSeek-Flash",
                                      credit_rate=0.1)]),
    })
    for provider_id in ("codebuddy", "trae", "qoder"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = client.get("/v1/models", headers={
            "Authorization": f"Bearer {key}"}).json()["data"]

    assert [m["id"] for m in data] == ["deepseek-v4.1-flash"]    # 三渠道并成一条
    entry = data[0]
    assert entry["providers"] == ["codebuddy", "qoder", "trae"]
    assert entry["name"] == "DeepSeek V4.1 Flash"
    assert entry["by_provider"]["qoder"] == {"raw_id": "dfmodel", "credit_rate": 0.1}
    # 别名表：对外 id 与 Qoder 原代号都能映射到 dfmodel
    aliases = app.state.services.model_aliases
    assert aliases["qoder"]["deepseek-v4.1-flash"] == "dfmodel"
    assert aliases["qoder"]["dfmodel"] == "dfmodel"

def test_models_synonym_merges_cb_xiaomi_mimo_pro(tmp_path):
    """CB 的 `Xiaomi MiMo-V2.6-Pro` 与 TRAE 的 `MiMo-V2.6-Pro` 并成一条。

    CB 上游名带厂商前缀（空格式），清洗规则削不到，归一键会多出 `xiaomi-`，
    同一模型拆成两条、对外多出 `xiaomi-mimo-v2.6-pro`。`MODEL_SYNONYMS` 把
    两者收敛到同一个规范键与展示名，请求 `mimo-v2.6-pro` 也能路由到 CB。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        "codebuddy": Stub("codebuddy", [Model(id="mimo-v2.6-pro",
                                              name="Xiaomi MiMo-V2.6-Pro",
                                              credit_rate=0.13)]),
        "trae": Stub("trae", [Model(id="mimo-v2.6-pro", name="MiMo-V2.6-Pro",
                                    credit_rate=0.15)]),
    })
    for provider_id in ("codebuddy", "trae"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = client.get("/v1/models", headers={
            "Authorization": f"Bearer {key}"}).json()["data"]

    assert [m["id"] for m in data] == ["mimo-v2.6-pro"]      # 两渠道并成一条
    assert data[0]["providers"] == ["codebuddy", "trae"]
    assert data[0]["name"] == "MiMo V2.6 Pro"
    aliases = app.state.services.model_aliases
    assert aliases["codebuddy"]["mimo-v2.6-pro"] == "mimo-v2.6-pro"
    assert aliases["trae"]["mimo-v2.6-pro"] == "mimo-v2.6-pro"

def test_models_name_merge_includes_zen_and_guards_colliding_names(tmp_path):
    """六条渠道统一按归一键合并；同渠道内归一键重复的退回原 id。

    清洗规则统一后，zen 由 id 派生的名字（`LongCat 2.5 Preview` → `longcat-2.5-
    preview`）也能与其它渠道对齐，故不再排除在名合并之外——原先排除是因为
    派生名与别家不同源，现在同源了。

    仍要挡住同渠道重名：CodeBuddy 的 `hy4-preview` / `hy4-preview-x`（都叫
    「Hy4 Preview」）若都按名入键，其中一个会被同键覆盖而消失。

    黑名单置空：本用例只验合并口径，`hy4-preview-x` 已被默认黑名单滤掉
    （2026-10-10 实测 CB 400 11102 下架）。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        MODEL_BLOCKLIST="")

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        "zen": Stub("zen", [Model(id="longcat-2.5-preview-free",
                                  name="LongCat 2.5 Preview"),
                            Model(id="mimo-v2.5-free", name="MiMo V2.5")]),
        "codebuddy": Stub("codebuddy", [Model(id="hy4-preview", name="Hy4 preview"),
                                        Model(id="hy4-preview-x",
                                              name="Hy4 preview")]),
    })
    for provider_id in ("zen", "codebuddy"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        ids = {m["id"] for m in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]}
    # 对外 id 一律是归一键：zen 的 free 尾缀被去掉；CB 重名条目因退回原代号
    # （展示名不唯一）也保持原 id。
    assert ids == {"longcat-2.5-preview", "mimo-v2.5",
                   "hy4-preview", "hy4-preview-x"}
    # 重名未按名入键：不得留下指向其中一个的歧义名字别名。两个原 id 各自
    # 映射回自己是正确的（用户按原 id 直连仍要能定位到本渠道）。
    assert app.state.services.model_aliases["codebuddy"] == {
        "hy4-preview": "hy4-preview", "hy4-preview-x": "hy4-preview-x"}

def test_models_merges_across_channels_when_duplicate_name_has_self_named_id(tmp_path):
    """渠道内展示名重名时，若 id 自洽的那个仍要**跨渠道合并**（2026-10-12）。

    上游把多个模型共用一个展示名是常态：CB 的 `hy4-preview` / `hy4-preview-x`
    都叫「Hy4 preview」，`hy3` / `hy3-x` 都叫「Hy3」。原来一律退回渠道限定键，
    代价是这些模型在**本渠道内**就断了跨渠道合并——国际站（无 `-x` 变体）的
    `hy4-preview` 本该与国内站合成一条，却因撞上被挤出去，对外多出
    `hy4-preview-codebuddy_intl` / `deepseek-v4.1-flash-codebuddy_intl` 这类后缀。

    改后：重名时用 id 归一值当合并键（重名者 id 不同故自然分开），其中 id 自洽
    的那个重新获得跨渠道合并能力；`-x` 变体保持独立条目，不会被合并掉。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        MODEL_BLOCKLIST="")

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        "codebuddy": Stub("codebuddy", [Model(id="hy4-preview", name="Hy4 preview"),
                                        Model(id="hy4-preview-x", name="Hy4 preview")]),
        "codebuddy_intl": Stub("codebuddy_intl", [Model(id="hy4-preview",
                                                        name="Hy4 preview")]),
    })
    for provider_id in ("codebuddy", "codebuddy_intl"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        entries = {m["id"]: m for m in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]}

    # 裸名一条，同时挂两站——不再出现 -codebuddy_intl 后缀
    assert set(entries) == {"hy4-preview", "hy4-preview-x"}
    assert entries["hy4-preview"]["providers"] == ["codebuddy", "codebuddy_intl"]
    # -x 变体仍是国内站独有，不被合并掉
    assert entries["hy4-preview-x"]["providers"] == ["codebuddy"]
    # 两站都能按裸名定位到各自的原代号（国际站的原 id 恰同名）
    aliases = app.state.services.model_aliases
    assert aliases["codebuddy"]["hy4-preview"] == "hy4-preview"
    assert aliases["codebuddy_intl"]["hy4-preview"] == "hy4-preview"

def test_models_keeps_scoped_key_when_duplicate_names_have_no_self_named_id(tmp_path):
    """上一条的边界：重名组里**没有任何** id 自洽的模型时，仍退回渠道限定键。

    上游把两个不同模型都叫「Aquila」但 id 都与展示名无关（`orion` / `vega`），
    此时没有可用的归一键可当合并键——退回限定键才不会互相覆盖，行为与
    2026-10-12 改动前一致。
    """
    from src.api.model_merge import (
        _merge_key,
        _model_label,
        _scoped_key,
        normalize_model_key,
    )
    from src.provider.base import Model

    models = [Model(id="orion", name="Aquila"), Model(id="vega", name="Aquila")]
    label_counts: dict[str, int] = {}
    for model in models:
        key = normalize_model_key(_model_label(model))
        label_counts[key] = label_counts.get(key, 0) + 1
    duplicates = {k for k, c in label_counts.items() if c > 1}
    assert duplicates == {"aquila"}
    id_keys = {normalize_model_key(m.id) for m in models}
    # 两个 id 都与展示名归一键无关，故 id 自洽的候选不存在
    assert "aquila" not in id_keys
    for model in models:
        key, name_keyed = _merge_key(model, "trae", duplicates, id_keys)
        assert key == _scoped_key("trae", model.id)
        assert name_keyed is False

def test_models_id_named_model_in_duplicate_group_uses_id_key(tmp_path):
    """重名组里 id 自洽的那个走 id 键（不是限定键），因此仍可跨渠道合并。

    TRAE 实测口径：`sagitta` 与 `aquila` 都叫「Aquila」，其中 `aquila` 的 id
    归一后等于展示名归一键，故它拿到合并键 `aquila`，而 `sagitta` 单独成键。
    """
    from src.api.model_merge import _merge_key, _model_label, normalize_model_key
    from src.provider.base import Model

    models = [Model(id="sagitta", name="Aquila"), Model(id="aquila", name="Aquila")]
    label_counts: dict[str, int] = {}
    for model in models:
        key = normalize_model_key(_model_label(model))
        label_counts[key] = label_counts.get(key, 0) + 1
    duplicates = {k for k, c in label_counts.items() if c > 1}
    id_keys = {normalize_model_key(m.id) for m in models}
    by_id = {m.id: _merge_key(m, "trae", duplicates, id_keys) for m in models}
    assert by_id["aquila"] == ("aquila", False)      # id 自洽 → 用 id 键，不登记别名
    assert by_id["sagitta"] == ("sagitta", False)

def test_models_single_channel_outward_id_is_normalized(tmp_path):
    """单渠道条目的对外 id 也是归一键（Qoder `kmodel_latest` → `kimi-k3`）。

    原代号只留在别名表与 `by_provider.raw_id`，转发时换回；用户按原代号直连
    仍能命中。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        "qoder": Stub("qoder", [Model(id="kmodel_latest", name="Kimi-K3")]),
    })
    app.state.credentials.add(provider="qoder", credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = client.get("/v1/models", headers={
            "Authorization": f"Bearer {key}"}).json()["data"]
    assert [m["id"] for m in data] == ["kimi-k3"]      # 对外 id 用归一键
    assert data[0]["name"] == "Kimi K3"                # 展示名统一清洗
    # 单渠道也下发 by_provider（原代号对外可见，PROPOSAL §3.4）
    assert data[0]["by_provider"] == {"qoder": {"raw_id": "kmodel_latest"}}
    aliases = app.state.services.model_aliases
    assert aliases["qoder"]["kimi-k3"] == "kmodel_latest"    # 对外 id → 原代号
    assert aliases["qoder"]["kmodel_latest"] == "kmodel_latest"  # 原代号也能直连
    assert aliases["qoder"]["kimi k3"] == "kmodel_latest"    # 原样的展示名也能直连

def test_models_name_merge_cross_channel_via_normalized_key(tmp_path):
    """清洗规则统一后 zen/kilo 也能与其它渠道并成一条。

    zen 的展示名原本由 id 本地派生、kilo 的是短标签，两条渠道都被排除在按名
    合并之外。现在归一规则对六条渠道一致：zen 的免费档尾缀与 TRAE 同名模型的
    id 归一到同一个键，kilo 的命名空间路径段也一样。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        # zen 的免费档 `longcat-2.5-preview-free` 与 TRAE 的 `longcat-2.5-preview`
        # 归一到同一键（`longcat-2.5-preview`）→ 并成一条，对外 id 用该键
        "zen": Stub("zen", [Model(id="longcat-2.5-preview-free", name="")]),
        "trae": Stub("trae", [Model(id="longcat-2.5-preview", name="")]),
        # kilo 的 `kilo-auto/free` 归一到 `kilo-auto`，单渠道对外 id 用归一键
        "kilo": Stub("kilo", [Model(id="kilo-auto/free", name="")]),
    })
    for provider_id in ("zen", "trae", "kilo"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = {m["id"]: m for m in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]}

    assert set(data) == {"longcat-2.5-preview", "kilo-auto"}
    merged = data["longcat-2.5-preview"]
    assert merged["providers"] == ["trae", "zen"]
    assert merged["name"] == "LongCat 2.5 Preview"
    # 请求转发时仍发各渠道自己的原 key（raw key 永不改动）
    assert merged["by_provider"] == {
        "trae": {"raw_id": "longcat-2.5-preview"},
        "zen": {"raw_id": "longcat-2.5-preview-free"}}
    aliases = app.state.services.model_aliases
    assert aliases["zen"]["longcat-2.5-preview"] == "longcat-2.5-preview-free"
    assert aliases["zen"]["longcat-2.5-preview-free"] == "longcat-2.5-preview-free"
    # kilo 单渠道：对外 id 是归一键，原代号仍可直连
    assert aliases["kilo"]["kilo-auto"] == "kilo-auto/free"
    assert aliases["kilo"]["kilo-auto/free"] == "kilo-auto/free"

def test_models_sentinel_names_not_merged_across_channels(tmp_path):
    """哨兵名（`auto` / `default`）不跨渠道合并，各自单列。

    kilo 的 `kilo-auto/free`（上游名 `Auto Free`）与 Qoder 的 `auto` 都归一成
    `auto`，但一个是 kilo 的自动路由、一个是 Qoder 的默认模型，语义只在本渠道
    内成立。若合并，调度会把请求错误地路由到另一个上游。

    这两个哨兵名在默认黑名单里（列表不展示占位模型），本用例只验证合并规则，
    故清空黑名单。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        MODEL_BLOCKLIST="")

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        "kilo": Stub("kilo", [Model(id="kilo-auto/free", name="Auto Free")]),
        "qoder": Stub("qoder", [Model(id="auto", name="Auto")]),
    })
    for provider_id in ("kilo", "qoder"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = [m for m in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]]
    assert {m["id"] for m in data} == {"kilo-auto", "auto"}
    assert all(len(m["providers"]) == 1 for m in data)   # 各渠道单列

def test_models_outward_id_disambiguated_on_collision(tmp_path):
    """对外 id 撞车时后来者退回原 id，保证对外 id 全局唯一。

    多渠道条目的对外 id 取展示名归一键，可能与另一条单渠道条目的原 id 撞上
    （这里 TRAE 的 `glm-5` 归一键 `glm-5`，而 kilo 有个原 id 就叫 `glm-5` 的
    模型）。撞车时两条 entry 会共用一个 id，前端选择器的 key 与别名表都会
    互相覆盖，故后来的那条退回自己的原 id。保留谁按渠道优先级：TRAE 优先于
    未排名的 kilo。`glm-5` 是默认黑名单里的老模型，本用例只验证撞车消歧，
    故清空黑名单。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        MODEL_BLOCKLIST="")

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        # trae（rank 1）多渠道条目归一键为 `glm-5`
        "trae": Stub("trae", [Model(id="glm", name="GLM 5")]),
        "zen": Stub("zen", [Model(id="glm-5", name="GLM 5")]),
        # kilo（未排名，靠后）单渠道条目的原 id 恰好也是 `glm-5` → 撞车
        "kilo": Stub("kilo", [Model(id="glm-5", name="Orphan GLM")]),
    })
    for provider_id in ("trae", "zen", "kilo"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = {m["id"]: m for m in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]}

    assert len(data) == 2
    # trae(rank 1) + zen 并成一条、保留归一键 glm-5；kilo 靠后，原 id 也撞上
    # → 换成归一后的展示名
    merged = data["glm-5"]
    assert merged["providers"] == ["trae", "zen"]
    disambiguated = data["orphan-glm"]
    assert disambiguated["providers"] == ["kilo"]
    # 别名表按渠道隔离：kilo 的模型只能发 kilo 的原 id
    aliases = app.state.services.model_aliases
    assert aliases["kilo"]["orphan-glm"] == "glm-5"
    assert aliases["trae"]["glm-5"] == "glm"
    assert aliases["zen"]["glm-5"] == "glm-5"

def test_models_outward_id_collision_falls_back_to_channel_suffix():
    """对外 id 撞车且候选被占时，依次退回「原代号归一键 → 加渠道名后缀」。

    entry 的对外 id 与主渠道原代号归一键都被先定稿的条目占掉时，退回加渠道名
    后缀的候选，保证对外 id 唯一。
    """
    from src.api.model_merge import _disambiguate

    entries = [
        {"id": "x", "key": "kx", "providers": ["p1"], "raw_ids": {"p1": "x"}},
        {"id": "y", "key": "ky", "providers": ["p2"], "raw_ids": {"p2": "y"}},
        {"id": "k4", "key": "k4", "providers": ["p3"], "raw_ids": {"p3": "k4"}},
        # id（x）与主渠道原代号归一（y）都被占：退回 `y-p1`
        {"id": "x", "key": "k9", "providers": ["p1"], "raw_ids": {"p1": "y"}},
    ]
    _disambiguate(entries)
    assert [e["id"] for e in entries] == ["x", "y", "k4", "y-p1"]

def test_models_outward_id_collision_falls_back_to_numeric_suffix():
    """候选全被占时追加数字后缀（`_unique_suffix` 兜底），保证对外 id 唯一。"""
    from src.api.model_merge import _disambiguate

    entries = [
        {"id": "x", "key": "kx", "providers": ["p1"], "raw_ids": {"p1": "x"}},
        {"id": "y", "key": "ky", "providers": ["p2"], "raw_ids": {"p2": "y"}},
        {"id": "y-p1", "key": "kz", "providers": ["p3"],
         "raw_ids": {"p3": "y-p1"}},
        {"id": "x-2", "key": "kw", "providers": ["p4"],
         "raw_ids": {"p4": "x-2"}},
        # 候选 x / y / y-p1 与后缀 x-2 全被占 → 用 x-3 兜底
        {"id": "x", "key": "k9", "providers": ["p1"], "raw_ids": {"p1": "y"}},
    ]
    _disambiguate(entries)
    assert [e["id"] for e in entries] == ["x", "y", "y-p1", "x-2", "x-3"]


def test_entry_response_omits_by_provider_when_empty():
    """`provider_meta` 为空 → 不下发 `by_provider` 键（而不是给空对象）。

    空对象会被前端读成「这个模型没有任何渠道」，与「渠道信息缺失」混淆。
    """
    from src.api.model_merge import _entry_response

    entry = {
        "id": "m", "providers": {"codebuddy"},
        "meta": {"name": "M", "credit_rate": None, "max_input_tokens": None,
                 "max_output_tokens": None, "supports_images": None,
                 "supports_tool_call": None, "supports_reasoning": None,
                 "default_effort": None},
        "provider_meta": {}, "raw_ids": {}, "labels": {"M"},
    }
    result = _entry_response(entry)
    assert "by_provider" not in result
    assert result["id"] == "m"
