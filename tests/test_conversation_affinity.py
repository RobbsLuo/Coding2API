"""会话粘性（ConversationAffinity）：指纹匹配 + 执行器选号集成。

对话进行中不换号：同一对话（消息前缀延续）的多轮请求固定用同一凭证，
出错才轮换，成功后重新粘定实际服务的凭证。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from src.config import load_settings
from src.engine.affinity import ConversationAffinity
from src.engine.executor import Executor, ExecutorDeps
from src.engine.scheduler import Candidate, Scheduler
from src.provider.base import ErrKind, Event, EventKind, Usage
from tests.conftest import SECRET

# ------------------------------------------------------ ConversationAffinity

NOW = 1000.0
TURN1 = [{"role": "system", "content": "s"}, {"role": "user", "content": "q1"}]
TURN1_REPLY = [{"role": "assistant", "content": "a1"}]
TURN2 = TURN1 + TURN1_REPLY + [{"role": "user", "content": "q2"}]


def raw(messages: list[dict], **extra) -> dict:
    """最小请求体（affinity 现在收整个 raw）。"""
    return {"messages": messages, **extra}


def test_remember_then_pin_longest_prefix():
    affinity = ConversationAffinity(ttl_seconds=600)
    affinity.remember(raw(TURN1), "alice", "c1", now=NOW)
    assert affinity.pin_for(raw(TURN1), "alice", now=NOW) == "c1"
    # TURN2 以 TURN1 为前缀：最长匹配粘到 c1
    assert affinity.pin_for(raw(TURN2), "alice", now=NOW) == "c1"
    # TURN1 的前缀（只到第一条）不属于任何已记录对话
    assert affinity.pin_for(raw(TURN1[:1]), "alice", now=NOW) is None


def test_pin_unknown_conversation_returns_none():
    affinity = ConversationAffinity(ttl_seconds=600)
    assert affinity.pin_for(raw(TURN1), "alice", now=NOW) is None


def test_username_is_part_of_fingerprint():
    affinity = ConversationAffinity(ttl_seconds=600)
    affinity.remember(raw(TURN1), "alice", "c1", now=NOW)
    assert affinity.pin_for(raw(TURN1), "bob", now=NOW) is None


def test_expired_entry_is_dropped():
    affinity = ConversationAffinity(ttl_seconds=600)
    affinity.remember(raw(TURN1), "alice", "c1", now=NOW)
    assert affinity.pin_for(raw(TURN1), "alice", now=NOW + 601) is None
    assert len(affinity) == 0


def test_hit_refreshes_expiry():
    affinity = ConversationAffinity(ttl_seconds=600)
    affinity.remember(raw(TURN1), "alice", "c1", now=NOW)
    # 500s 后命中刷新，再过 500s 仍然有效（原始 TTL 已过）
    assert affinity.pin_for(raw(TURN1), "alice", now=NOW + 500) == "c1"
    assert affinity.pin_for(raw(TURN1), "alice", now=NOW + 1000) == "c1"


def test_disabled_when_ttl_non_positive():
    affinity = ConversationAffinity(ttl_seconds=0)
    affinity.remember(raw(TURN1), "alice", "c1", now=NOW)
    assert len(affinity) == 0
    assert affinity.pin_for(raw(TURN1), "alice", now=NOW) is None


def test_empty_messages_never_match():
    affinity = ConversationAffinity(ttl_seconds=600)
    affinity.remember(raw([]), "alice", "c1", now=NOW)
    assert affinity.pin_for(raw([]), "alice", now=NOW) is None


def test_eviction_drops_expired_first_then_oldest():
    affinity = ConversationAffinity(ttl_seconds=100, max_entries=2)
    affinity.remember(raw(TURN1), "alice", "c1", now=NOW)          # 到期 NOW+100
    affinity.remember(raw(TURN2), "alice", "c2", now=NOW + 10)     # 到期 NOW+110
    # 写入 c3 时超上限：c1 已过期（NOW+105 > NOW+100）先被清
    affinity.remember(raw(TURN1), "bob", "c3", now=NOW + 105)
    assert affinity.pin_for(raw(TURN1), "alice", now=NOW + 105) is None
    # 写入 c4 时超上限：c2 已过期（NOW+115 > NOW+110）先被清
    affinity.remember(raw(TURN2), "bob", "c4", now=NOW + 115)
    assert affinity.pin_for(raw(TURN2), "alice", now=NOW + 115) is None
    # 写入 c5 时无过期可清：按最旧淘汰 c3
    affinity.remember(raw(TURN1), "alice", "c5", now=NOW + 116)
    assert affinity.pin_for(raw(TURN1), "bob", now=NOW + 116) is None
    assert affinity.pin_for(raw(TURN2), "bob", now=NOW + 116) == "c4"
    assert affinity.pin_for(raw(TURN1), "alice", now=NOW + 116) == "c5"
    assert len(affinity) == 2


# ------------------------------------------------- 显式会话标识（B1.5）

@pytest.mark.parametrize("container,key", [
    ("metadata", "conversation_id"),
    ("metadata", "conversationId"),
    ("metadata", "prompt_cache_key"),
    ("top", "conversation_id"),
    ("top", "conversationId"),
    ("top", "prompt_cache_key"),
])
def test_explicit_key_sources(container, key):
    from src.engine.affinity import explicit_key

    body = {"metadata": {key: " conv-1 "}} if container == "metadata" else {key: "conv-1"}
    assert explicit_key(body) == f"{key}:conv-1"


def test_explicit_key_priority_and_container_order():
    from src.engine.affinity import explicit_key, has_user_id

    # conversation_id 优先于 conversationId / prompt_cache_key
    assert explicit_key({"conversation_id": "a", "conversationId": "b",
                         "prompt_cache_key": "c"}) == "conversation_id:a"
    # metadata 优先于顶层（同键名）
    assert explicit_key({"metadata": {"conversation_id": "m"},
                         "conversation_id": "t"}) == "conversation_id:m"
    # 空串/空白/非字符串视为未提供，继续往后找
    assert explicit_key({"conversation_id": "   ",
                         "prompt_cache_key": "p"}) == "prompt_cache_key:p"
    assert explicit_key({"conversation_id": 123}) is None
    assert explicit_key({}) is None
    assert explicit_key({"metadata": "not-a-dict"}) is None
    # user_id：顶层或 metadata，空白不算
    assert has_user_id({"user_id": "u"}) is True
    assert has_user_id({"metadata": {"user_id": "u"}}) is True
    assert has_user_id({"user_id": "  "}) is False
    assert has_user_id({"metadata": {"user_id": 7}}) is False
    assert has_user_id({}) is False


def test_explicit_conversation_id_pins_even_when_messages_change():
    """显式标识命中时不受消息裁剪影响：消息完全不同也粘同一凭证。"""
    affinity = ConversationAffinity(ttl_seconds=600)
    affinity.remember(raw(TURN1, conversation_id="conv-1"), "alice", "c1", now=NOW)
    other = [{"role": "user", "content": "totally different"}]
    assert affinity.pin_for(raw(other, conversation_id="conv-1"), "alice", now=NOW) == "c1"
    # 换个会话标识 → 无粘性
    assert affinity.pin_for(raw(other, conversation_id="conv-2"), "alice", now=NOW) is None


def test_explicit_key_isolated_per_user_and_expires():
    affinity = ConversationAffinity(ttl_seconds=600)
    affinity.remember(raw(TURN1, prompt_cache_key="k1"), "alice", "c1", now=NOW)
    assert affinity.pin_for(raw(TURN1, prompt_cache_key="k1"), "bob", now=NOW) is None
    assert affinity.pin_for(raw(TURN1, prompt_cache_key="k1"), "alice", now=NOW + 601) is None
    assert len(affinity) == 0


def test_explicit_key_takes_precedence_over_prefix_fingerprint():
    """同一请求体既有显式标识又有可匹配前缀时，显式标识优先。"""
    affinity = ConversationAffinity(ttl_seconds=600)
    affinity.remember(raw(TURN1), "alice", "c_prefix", now=NOW)
    affinity.remember(raw(TURN1, conversation_id="conv-1"), "alice", "c_explicit", now=NOW)
    assert affinity.pin_for(raw(TURN2, conversation_id="conv-1"), "alice", now=NOW) == "c_explicit"
    # 不带显式标识时仍走前缀兜底
    assert affinity.pin_for(raw(TURN2), "alice", now=NOW) == "c_prefix"


def test_user_id_disables_prefix_fallback_on_both_paths():
    """带 user_id 时不派生前缀兜底键：remember 不写、pin_for 不读。"""
    affinity = ConversationAffinity(ttl_seconds=600)
    affinity.remember(raw(TURN1, user_id="u1"), "alice", "c1", now=NOW)
    assert len(affinity) == 0
    assert affinity.pin_for(raw(TURN1, user_id="u1"), "alice", now=NOW) is None
    # user_id 只挡兜底键，不挡显式标识
    affinity.remember(raw(TURN1, user_id="u1", conversation_id="conv-1"),
                      "alice", "c2", now=NOW)
    assert affinity.pin_for(raw(TURN1, user_id="u1", conversation_id="conv-1"),
                            "alice", now=NOW) == "c2"
    # metadata 内 user_id 同样生效
    affinity.remember(raw(TURN1, metadata={"user_id": "u1"}), "alice", "c3", now=NOW)
    assert len(affinity) == 1


def test_non_dict_raw_is_ignored():
    affinity = ConversationAffinity(ttl_seconds=600)
    affinity.remember(None, "alice", "c1", now=NOW)  # type: ignore[arg-type]
    assert len(affinity) == 0
    assert affinity.pin_for(None, "alice", now=NOW) is None  # type: ignore[arg-type]


# ------------------------------------------------------------ 执行器集成


@dataclass
class SoftError(Exception):
    def kind(self) -> ErrKind:
        return ErrKind.SOFT


@dataclass
class FakeAffinity:
    pinned: str | None = None
    remembered: list[str] = field(default_factory=list)
    seen: list[object] = field(default_factory=list)

    def pin_for(self, raw, username):
        self.seen.append(raw)
        return self.pinned

    def remember(self, raw, username, credential_id):
        self.seen.append(raw)
        self.remembered.append(credential_id)


@dataclass
class FakeRepo:
    rows: list[Candidate]
    successes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def candidates(self, providers=None, *, selectable_only=False):
        return [c for c in self.rows if providers is None or c.provider in providers]

    def provider_of(self, credential_id):
        return next(c.provider for c in self.rows
                    if c.credential_id == credential_id)

    def credential_data(self, credential_id):
        return ({"id": credential_id} if any(c.credential_id == credential_id
                                             for c in self.rows) else None)

    def save_success(self, credential_id, *, model=None):
        self.successes.append(credential_id)

    def save_error(self, credential_id, outcome):
        self.errors.append(credential_id)


@dataclass
class FakeProvider:
    fail_on: frozenset[str] = frozenset()
    served: list[str] = field(default_factory=list)

    async def stream_chat(self, credential_data, payload, model):
        if credential_data["id"] in self.fail_on:
            raise SoftError()
        self.served.append(credential_data["id"])
        yield Event(kind=EventKind.CONTENT, content="hi")
        yield Event(kind=EventKind.USAGE, usage=Usage(input_tokens=1, output_tokens=1))
        yield Event(kind=EventKind.FINISH, finish_reason="stop")


def _repo(*rows) -> FakeRepo:
    """行参数："c_a" 或 ("c_z", {"cooling_until": ...})。"""
    return FakeRepo(rows=[
        Candidate(credential_id=cid, provider="trae", **flags)
        for row in rows
        for cid, flags in [(row, {}) if isinstance(row, str) else row]
    ])


def _request(messages: list[dict], **extra):
    from src.compat.openai.request import parse_chat_request

    return parse_chat_request({"model": "glm-5.2", "messages": messages,
                               "stream": True, **extra})


async def _drain(iterator):
    frames = []
    async for frame in iterator:
        frames.append(frame)
    return frames


@pytest.mark.asyncio
async def test_stream_sticky_bypasses_scheduler_ordering():
    """指纹命中 c_z 时直接复用，尽管常规排序会先选 c_a。"""
    provider = FakeProvider()
    executor = Executor(ExecutorDeps(
        providers={"trae": provider}, credentials=_repo("c_a", "c_z"),
        scheduler=Scheduler(), affinity=FakeAffinity(pinned="c_z")))
    await _drain(executor.stream(_request(TURN1), username="alice"))
    assert provider.served == ["c_z"]


@pytest.mark.asyncio
async def test_stream_rotates_off_sticky_credential_and_repins():
    """粘住的凭证报错仍走正常轮换，成功后重新粘到实际服务的凭证。"""
    provider = FakeProvider(fail_on=frozenset({"c_a"}))
    affinity = FakeAffinity(pinned="c_a")
    repo = _repo("c_a", "c_z")
    executor = Executor(ExecutorDeps(
        providers={"trae": provider}, credentials=repo,
        scheduler=Scheduler(), affinity=affinity))
    await _drain(executor.stream(_request(TURN1), username="alice"))
    assert repo.errors == ["c_a"]
    assert provider.served == ["c_z"]
    assert affinity.remembered == ["c_z"]


@pytest.mark.asyncio
async def test_stream_ignores_sticky_when_credential_cooling():
    """粘住的凭证冷却中不强行复用，回退常规调度并重新粘定。"""
    provider = FakeProvider()
    affinity = FakeAffinity(pinned="c_z")
    executor = Executor(ExecutorDeps(
        providers={"trae": provider},
        credentials=_repo("c_a", ("c_z", {"cooling_until": 10**12})),
        scheduler=Scheduler(), affinity=affinity))
    await _drain(executor.stream(_request(TURN1), username="alice"))
    assert provider.served == ["c_a"]
    assert affinity.remembered == ["c_a"]


@pytest.mark.asyncio
async def test_stream_ignores_sticky_when_not_in_candidates():
    """粘住的凭证不在候选池（被删/属其他上游）时回退常规调度。"""
    provider = FakeProvider()
    affinity = FakeAffinity(pinned="c_missing")
    executor = Executor(ExecutorDeps(
        providers={"trae": provider}, credentials=_repo("c_a"),
        scheduler=Scheduler(), affinity=affinity))
    await _drain(executor.stream(_request(TURN1), username="alice"))
    assert provider.served == ["c_a"]
    assert affinity.remembered == ["c_a"]


@pytest.mark.asyncio
async def test_stream_sticky_yields_to_manual_pin():
    """手动 pin 优先于粘性（PROPOSAL §4.3）：管理员显式指定的凭证不被粘性顶掉。"""
    provider = FakeProvider()
    affinity = FakeAffinity(pinned="c_z")
    executor = Executor(ExecutorDeps(
        providers={"trae": provider},
        credentials=_repo(("c_a", {"pinned": True}), "c_z"),
        scheduler=Scheduler(), affinity=affinity))
    await _drain(executor.stream(_request(TURN1), username="alice"))
    assert provider.served == ["c_a"]
    assert affinity.remembered == ["c_a"]


@pytest.mark.asyncio
async def test_stream_sticky_wins_when_pinned_credential_not_selectable():
    """pin 的凭证不可选（冷却中）时粘性照常生效，不会因 pin 就卡死。"""
    provider = FakeProvider()
    affinity = FakeAffinity(pinned="c_z")
    executor = Executor(ExecutorDeps(
        providers={"trae": provider},
        credentials=_repo(("c_a", {"pinned": True, "cooling_until": 10**12}), "c_z"),
        scheduler=Scheduler(), affinity=affinity))
    await _drain(executor.stream(_request(TURN1), username="alice"))
    assert provider.served == ["c_z"]


@pytest.mark.asyncio
async def test_stream_without_affinity_still_works():
    provider = FakeProvider()
    executor = Executor(ExecutorDeps(
        providers={"trae": provider}, credentials=_repo("c_a"),
        scheduler=Scheduler(), affinity=None))
    frames = await _drain(executor.stream(_request(TURN1), username="alice"))
    assert any(b"[DONE]" in frame for frame in frames)
    assert provider.served == ["c_a"]


@pytest.mark.asyncio
async def test_conversation_sticks_across_turns_end_to_end():
    """端到端：第二轮 messages 延续第一轮 → 复用同一凭证；新对话不串。"""
    provider = FakeProvider()
    affinity = ConversationAffinity(ttl_seconds=600)
    executor = Executor(ExecutorDeps(
        providers={"trae": provider}, credentials=_repo("c_a", "c_z"),
        scheduler=Scheduler(), affinity=affinity))
    # 第一轮：常规排序选 c_a（id 升序），成功后粘定
    await _drain(executor.stream(_request(TURN1), username="alice"))
    # 第二轮（消息延续）：仍用 c_a，尽管排序不变也是同一个
    await _drain(executor.stream(_request(TURN2), username="alice"))
    assert provider.served == ["c_a", "c_a"]
    # 同样的消息换一个 API 用户 → 视为新对话，不继承粘性是允许的；
    # 这里只验证指纹不串：bob 的第一轮按常规排序也选 c_a
    await _drain(executor.stream(_request(TURN1), username="bob"))
    assert provider.served == ["c_a", "c_a", "c_a"]


@pytest.mark.asyncio
async def test_complete_sticks_across_turns():
    """非流式同路径：粘性优先 + 成功后粘定。"""
    provider = FakeProvider()
    affinity = FakeAffinity(pinned="c_z")
    executor = Executor(ExecutorDeps(
        providers={"trae": provider}, credentials=_repo("c_a", "c_z"),
        scheduler=Scheduler(), affinity=affinity))
    result = await executor.complete(_request(TURN1), username="alice")
    assert provider.served == ["c_z"]
    assert affinity.remembered == ["c_z"]
    assert result["choices"]


@pytest.mark.asyncio
async def test_executor_passes_whole_raw_to_affinity():
    """B1.5：executor 把整个 request.raw 交给 affinity（而非仅 messages）。"""
    provider = FakeProvider()
    affinity = FakeAffinity(pinned=None)
    executor = Executor(ExecutorDeps(
        providers={"trae": provider}, credentials=_repo("c_a"),
        scheduler=Scheduler(), affinity=affinity))
    await _drain(executor.stream(_request(TURN1, conversation_id="conv-9"),
                                 username="alice"))
    assert affinity.seen, "affinity 未被调用"
    for raw_body in affinity.seen:
        assert raw_body.get("conversation_id") == "conv-9"
        assert raw_body.get("messages") == TURN1


@pytest.mark.asyncio
async def test_explicit_conversation_id_sticks_across_disjoint_messages():
    """端到端：消息数组不延续但 conversation_id 相同 → 仍复用同一凭证。"""
    provider = FakeProvider()
    affinity = ConversationAffinity(ttl_seconds=600)
    executor = Executor(ExecutorDeps(
        providers={"trae": provider}, credentials=_repo("c_a", "c_z"),
        scheduler=Scheduler(), affinity=affinity))
    await _drain(executor.stream(_request(TURN1, conversation_id="conv-1"),
                                 username="alice"))
    # 第二轮消息完全不同（模拟客户端裁剪历史），靠 conversation_id 粘住
    other = [{"role": "user", "content": "brand new"}]
    await _drain(executor.stream(_request(other, conversation_id="conv-1"),
                                 username="alice"))
    assert provider.served == ["c_a", "c_a"]
    # 另一个会话标识则按常规排序（同为 c_a，但走的是新键，不继承）
    await _drain(executor.stream(_request(other, conversation_id="conv-2"),
                                 username="alice"))
    assert provider.served == ["c_a", "c_a", "c_a"]


@pytest.mark.asyncio
async def test_user_id_disables_sticky_fallback_end_to_end():
    """端到端：带 user_id 的并行对话不互相钉号（不派生前缀兜底键）。"""
    provider = FakeProvider()
    affinity = ConversationAffinity(ttl_seconds=600)
    executor = Executor(ExecutorDeps(
        providers={"trae": provider}, credentials=_repo("c_a", "c_z"),
        scheduler=Scheduler(), affinity=affinity))
    await _drain(executor.stream(_request(TURN1, user_id="u1"), username="alice"))
    assert len(affinity) == 0          # 未写任何键
    assert affinity.pin_for({"messages": TURN2, "user_id": "u1"}, "alice") is None


# ---------------------------------------------------------------- 配置装配


def test_settings_conversation_sticky_default_and_override():
    settings = load_settings({"app_secret": SECRET})
    assert settings.conversation_sticky_seconds == 3600
    settings = load_settings({"app_secret": SECRET,
                              "conversation_sticky_seconds": "0"})
    assert settings.conversation_sticky_seconds == 0


def test_build_app_wires_affinity(tmp_path):
    from src.config import Settings
    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    executor = build_app(settings).state.executor
    assert isinstance(executor._deps.affinity, ConversationAffinity)
    assert executor._deps.affinity.ttl_seconds == 3600
