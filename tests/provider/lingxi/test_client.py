"""灵犀上游客户端：三步对话、run 缓存/单飞、账号面接口与请求体压平。"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from src.provider import base
from src.provider.base import ErrKind, EventKind, UpstreamProtocolViolation
from src.provider.lingxi import client as lingxi_client
from src.provider.lingxi.client import (
    EP_BALANCE,
    EP_CHAT,
    EP_PLANS,
    EP_PROFILE,
    EP_RUNS,
    EP_SESSIONS,
    EP_TASKS,
    LingxiClient,
    RunExpired,
    UpstreamHTTPError,
    build_body,
    normalize_messages,
    sanitize_schema,
)
from tests.provider.lingxi._support import CREDENTIAL, client, frame, lines


def _handler(mapping, *, silent=None):
    """按 (method, path) 返回 JSON / 文本；未命中抛断言（测试即契约）。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        key = (request.method, request.url.path)
        if silent is not None:
            silent(request)
        value = mapping.get(key)
        if value is None:
            raise AssertionError(f"unexpected request {key}")
        if isinstance(value, httpx.Response):
            return value
        if isinstance(value, str):
            return httpx.Response(200, text=value)
        return httpx.Response(200, json=value)

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


def _ok(payload):
    return {"code": "Success", "data": payload}


def test_bases_dedupe_when_same_host():
    instance = LingxiClient(host="https://a.test/", fallback_host="https://a.test")
    assert instance.bases() == ("https://a.test",)
    other = LingxiClient(host="https://a.test", fallback_host="https://b.test")
    assert other.bases() == ("https://a.test", "https://b.test")


def test_headers_carry_cookie_and_desktop_markers():
    instance = LingxiClient(host="https://a.test")
    headers = instance.headers(CREDENTIAL)
    assert headers["Cookie"] == "wps_sid=sid-abc; uid=42"
    assert headers["X-Client-Type"] == "lingxi_claw"
    assert headers["Accept"] == "application/json"
    assert headers["x-kso-app-version"] == "1.3.31"
    assert headers["x-kso-device-id"]


async def test_fetch_profile_and_balance_tolerate_non_dict_data():
    handler = _handler({
        ("GET", EP_PROFILE): {"code": "Success", "data": "x"},
        ("GET", EP_BALANCE): _ok({"total_balance": 10}),
    })
    instance = client(handler)
    assert await instance.fetch_profile(CREDENTIAL) == {}
    assert await instance.fetch_balance(CREDENTIAL) == {"total_balance": 10}


async def test_account_json_falls_back_on_5xx():
    def silent(request):
        if request.url.host == "lingxi.test":
            raise httpx.ConnectError("boom")

    handler = _handler({("GET", EP_PROFILE): _ok({"id": "u1", "name": "nick"})},
                       silent=silent)
    instance = client(handler)
    assert (await instance.fetch_profile(CREDENTIAL))["id"] == "u1"
    assert instance.bases() == ("https://lingxi.test", "https://kdocs.test")


async def test_account_json_falls_back_on_5xx_status():
    def handler(request):
        if request.url.host == "lingxi.test":
            return httpx.Response(500, text="down")
        return httpx.Response(200, json=_ok({"id": "u2"}))

    instance = client(handler)
    assert (await instance.fetch_profile(CREDENTIAL))["id"] == "u2"


async def test_account_json_falls_back_on_401_too():
    """kdocs 账号只在 kdocs 集群认 cookie：主域 401 不直接判死。"""
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "kdocs.test":
            return httpx.Response(200, json=_ok({"id": "u1"}))
        return httpx.Response(401, text="nope")

    instance = client(handler)
    assert (await instance.fetch_profile(CREDENTIAL))["id"] == "u1"
    assert calls == ["lingxi.test", "kdocs.test"]

    def always_401(request):
        return httpx.Response(401, text="nope")

    with pytest.raises(UpstreamHTTPError) as error:
        await client(always_401).fetch_profile(CREDENTIAL)
    assert error.value.status == 401


async def test_account_json_raises_last_error_when_all_bases_fail():
    def handler(request):
        return httpx.Response(500, text="down")

    instance = client(handler)
    with pytest.raises(UpstreamHTTPError):
        await instance.fetch_profile(CREDENTIAL)


async def test_json_rejects_non_json_and_non_object():
    def non_json(request):
        return httpx.Response(200, text="not json")

    instance = client(non_json)
    with pytest.raises(UpstreamProtocolViolation):
        await instance.fetch_balance(CREDENTIAL)

    def array_json(request):
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(UpstreamProtocolViolation):
        await client(array_json).fetch_balance(CREDENTIAL)


async def test_json_transport_error_is_translated():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(base.UpstreamTransportError):
        await client(handler).fetch_balance(CREDENTIAL)


async def test_probe_quota_maps_balance_and_batches():
    now = int(time.time())
    soon = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + 86400))
    later = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + 60 * 86400))
    handler = _handler({("GET", EP_BALANCE): _ok({
        "total_balance": "120",
        "bonus_summary": {"total_value": 300},
        "bonus_credits": [
            {"expire_time": soon, "balance": "50"},
            {"expire_time": later, "balance": 70},
            {"expire_time": "", "balance": 10},
            {"expire_time": soon, "balance": 0},
            "junk",
        ],
    })})
    quota = await client(handler).probe_quota(CREDENTIAL)
    assert quota.remaining == 120
    assert quota.total == 300
    assert quota.probe_failed is False
    # 只有 15 天内的批次进到期阶梯；全部批次进 packages（余额 > 0）
    assert quota.expiry_ladder is not None and len(quota.expiry_ladder) == 1
    assert quota.packages is not None and len(quota.packages) == 2
    assert quota.probed_at is not None


async def test_probe_quota_falls_back_to_credits_balance_and_failed():
    handler = _handler({("GET", EP_BALANCE): _ok({"credits_balance": 5})})
    quota = await client(handler).probe_quota(CREDENTIAL)
    assert quota.remaining == 5

    handler = _handler({("GET", EP_BALANCE): _ok({})})
    quota = await client(handler).probe_quota(CREDENTIAL)
    assert quota.probe_failed is True


async def test_fetch_models_merges_plans_and_models():
    handler = _handler({("GET", EP_PLANS): _ok({
        "plans": [{"key": "plan_pro", "name": "Pro", "reasoning_efforts": ["high"]},
                  {"key": "", "name": "skip"},
                  "junk"],
        "models": [{"key": "GLM-5.3", "name": "GLM-5.3", "multiplier": "0.75",
                    "max_context_tokens": 1000, "max_new_tokens": 200,
                    "support_multimodal": True},
                   {"key": "GLM-5.3", "name": "dup"},
                   {"name": "no key"}],
    })})
    models = await client(handler).fetch_models(CREDENTIAL)
    ids = [model.id for model in models]
    assert ids == ["plan_pro", "GLM-5.3"]
    assert models[0].credit_rate is None
    assert models[1].credit_rate == 0.75
    assert models[1].max_input_tokens == 1000
    assert models[1].supports_images is True
    assert models[1].supports_reasoning is False


async def test_fetch_models_snapshot_on_failure_and_empty():
    def boom(request):
        return httpx.Response(500, text="down")

    models = await client(boom).fetch_models(CREDENTIAL)
    assert len(models) == 14
    assert models[1].id == "deepseek-v4.1-flash"

    empty = _handler({("GET", EP_PLANS): _ok({})})
    assert len(await client(empty).fetch_models(CREDENTIAL)) == 14

    broken = _handler({("GET", EP_PLANS): httpx.Response(200, text="oops")})
    assert len(await client(broken).fetch_models(CREDENTIAL)) == 14


async def test_fetch_models_401_propagates():
    handler = _handler({("GET", EP_PLANS): httpx.Response(401, text="no")})
    with pytest.raises(UpstreamHTTPError):
        await client(handler).fetch_models(CREDENTIAL)


async def test_fetch_tasks_shapes():
    handler = _handler({("GET", EP_TASKS): _ok({"tasks": [{"task_key": "a"}, 1]})})
    tasks = await client(handler).fetch_tasks(CREDENTIAL)
    assert tasks == [{"task_key": "a"}]

    handler = _handler({("GET", EP_TASKS): _ok({"tasks": "bad"})})
    assert await client(handler).fetch_tasks(CREDENTIAL) == []


async def test_claim_task_with_and_without_date():
    seen: list[str] = []

    def handler(request):
        seen.append(request.url.path + ("?" + request.url.query.decode()
                                         if request.url.query else ""))
        return httpx.Response(200, json=_ok({"ok": 1}))

    instance = client(handler)
    await instance.claim_task(CREDENTIAL, "daily_check_in", "2026-11-09")
    await instance.claim_task(CREDENTIAL, "daily_check_in")
    assert seen == ["/api/public/v1/tasks/daily_check_in/claim?date=2026-11-09",
                    "/api/public/v1/tasks/daily_check_in/claim"]


# ---------------------------------------------------------------- 会话/run


def _run_mapping(*, run_id="run-1", session_id="ses-1"):
    return {
        ("POST", EP_SESSIONS): _ok({"session_id": session_id}),
        ("POST", EP_RUNS): _ok({"run_id": run_id, "group_id": "group-1"}),
    }


async def test_ensure_run_caches_for_ttl():
    handler = _handler({**_run_mapping(), ("GET", EP_CHAT): "x"})
    instance = client(handler)
    first = await instance.ensure_run(CREDENTIAL, "m", [])
    second = await instance.ensure_run(CREDENTIAL, "m", [])
    assert first["run_id"] == "run-1" and first["group_id"] == "group-1"
    assert second is first
    # 只建一次会话 / run
    creates = [call for call in handler.calls if call.url.path == EP_SESSIONS]
    assert len(creates) == 1


async def test_ensure_run_rebuilds_after_ttl(monkeypatch):
    handler = _handler(_run_mapping())
    instance = client(handler)
    await instance.ensure_run(CREDENTIAL, "m")
    monkeypatch.setattr(lingxi_client, "RUN_TTL_SECONDS", 0)
    await instance.ensure_run(CREDENTIAL, "m")
    assert len([c for c in handler.calls if c.url.path == EP_RUNS]) == 2


async def test_ensure_run_single_flight():
    started = asyncio.Event()
    release = asyncio.Event()
    count = {"runs": 0}

    def handler(request):
        if request.url.path == EP_SESSIONS:
            return httpx.Response(200, json=_ok({"session_id": "s"}))
        count["runs"] += 1
        return httpx.Response(200, json=_ok({"run_id": "r"}))

    instance = client(handler)
    first = asyncio.create_task(instance.ensure_run(CREDENTIAL, "m"))
    await asyncio.sleep(0)
    second = asyncio.create_task(instance.ensure_run(CREDENTIAL, "m"))
    started.set()
    release.set()
    runs = await asyncio.gather(first, second)
    assert runs[0]["run_id"] == "r"
    assert count["runs"] == 1


async def test_ensure_run_falls_back_to_second_base():
    def handler(request):
        if request.url.host == "lingxi.test":
            return httpx.Response(500, text="down")
        if request.url.path == EP_SESSIONS:
            return httpx.Response(200, json=_ok({"session_id": "s2"}))
        return httpx.Response(200, json=_ok({"run_id": "r2"}))

    instance = client(handler)
    run = await instance.ensure_run(CREDENTIAL, "m")
    assert run["base"] == "https://kdocs.test"
    assert run["run_id"] == "r2"


async def test_ensure_run_falls_back_on_401():
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "kdocs.test":
            if request.url.path == EP_SESSIONS:
                return httpx.Response(200, json=_ok({"session_id": "s"}))
            return httpx.Response(200, json=_ok({"run_id": "r"}))
        return httpx.Response(401, text="no")

    run = await client(handler).ensure_run(CREDENTIAL, "m")
    assert run["base"] == "https://kdocs.test"
    assert calls[0] == "lingxi.test"


async def test_ensure_run_all_bases_fail():
    def handler(request):
        return httpx.Response(500, text="down")

    with pytest.raises(UpstreamHTTPError):
        await client(handler).ensure_run(CREDENTIAL, "m")


async def test_ensure_run_missing_ids_are_protocol_violations():
    handler = _handler({("POST", EP_SESSIONS): _ok({})})
    with pytest.raises(UpstreamProtocolViolation):
        await client(handler).ensure_run(CREDENTIAL, "m")

    handler = _handler({("POST", EP_SESSIONS): _ok({"session_id": "s"}),
                        ("POST", EP_RUNS): _ok({})})
    with pytest.raises(UpstreamProtocolViolation):
        await client(handler).ensure_run(CREDENTIAL, "m")


async def test_ensure_run_transport_error_on_both_bases():
    def handler(request):
        raise httpx.ConnectError("no")

    with pytest.raises(base.UpstreamTransportError):
        await client(handler).ensure_run(CREDENTIAL, "m")


def test_forget_run():
    instance = LingxiClient(host="https://a.test")
    instance._runs["k"] = {"at": time.time()}
    instance.forget_run(CREDENTIAL, "m")
    assert instance._runs  # key 由 identity|model 组成，这里只是覆盖调用路径


# ---------------------------------------------------------------- 聊天流


def _chat_mapping(stream: str, *, run=None):
    mapping = dict(run or _run_mapping())
    mapping[("POST", EP_CHAT)] = stream
    return mapping


async def test_stream_chat_yields_events_and_headers():
    stream = lines({"code": "Success", "choices": [{"text": "hi"}]},
                   {"code": "Success",
                    "choices": [{"text": "!", "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2}})
    handler = _handler(_chat_mapping(stream))
    events = [event async for event in client(handler).stream_chat(
        CREDENTIAL, {"messages": [{"role": "user", "content": "hi"}],
                     "max_tokens": 5}, "m")]
    kinds = [event.kind for event in events]
    assert kinds == [EventKind.CONTENT, EventKind.CONTENT,
                     EventKind.FINISH, EventKind.USAGE]
    chat = [c for c in handler.calls if c.url.path == EP_CHAT][0]
    assert chat.headers["x-cc-session-id"] == "ses-1"
    assert chat.headers["x-cc-run-id"] == "run-1"
    assert json.loads(chat.content)["base_llm_arguments"] == {"max_tokens": 5}


async def test_stream_chat_done_marker_ends_stream():
    handler = _handler(_chat_mapping(lines({"code": "Success",
                                            "choices": [{"text": "a"}]})
                                    + "data: [DONE]\n"))
    events = [event async for event in client(handler).stream_chat(
        CREDENTIAL, {"messages": []}, "m")]
    assert [event.kind for event in events] == [EventKind.CONTENT]


async def test_stream_chat_session_expired_before_content():
    stream = lines({"code": "SQLNoRows", "hint": "会话不存在"})
    handler = _handler(_chat_mapping(stream))
    with pytest.raises(RunExpired):
        [event async for event in client(handler).stream_chat(
            CREDENTIAL, {"messages": []}, "m")]


async def test_stream_chat_error_after_content_is_an_event():
    stream = lines({"code": "Success", "choices": [{"text": "a"}]},
                   {"code": "SQLNoRows", "hint": "会话不存在"})
    handler = _handler(_chat_mapping(stream))
    events = [event async for event in client(handler).stream_chat(
        CREDENTIAL, {"messages": []}, "m")]
    assert events[-1].kind == EventKind.ERROR
    assert events[-1].error_kind is ErrKind.SOFT


async def test_stream_chat_skips_junk_lines_and_empty_frames():
    stream = ("\n: keepalive\nid: 1\nevent: x\nnot json\n"
              + frame({"code": "Success", "choices": [{"text": "a"}]})
              + frame({"code": "Success", "choices": [{"finish_reason": "stop"}]}))
    handler = _handler(_chat_mapping(stream))
    events = [event async for event in client(handler).stream_chat(
        CREDENTIAL, {"messages": []}, "m")]
    assert [event.kind for event in events] == [EventKind.CONTENT,
                                                EventKind.FINISH]


async def test_stream_chat_truncated_without_finish():
    handler = _handler(_chat_mapping(lines({"code": "Success",
                                            "choices": [{"text": "a"}]})))
    with pytest.raises(UpstreamProtocolViolation):
        [event async for event in client(handler).stream_chat(
            CREDENTIAL, {"messages": []}, "m")]


async def test_stream_chat_http_error_and_transport_error():
    handler = _handler(_chat_mapping("", run=_run_mapping()))
    handler = _handler({**_run_mapping(),
                        ("POST", EP_CHAT): httpx.Response(429, text="slow")})
    with pytest.raises(UpstreamHTTPError) as error:
        [event async for event in client(handler).stream_chat(
            CREDENTIAL, {"messages": []}, "m")]
    assert error.value.status == 429

    def raising(request):
        raise httpx.ConnectError("down")

    with pytest.raises(base.UpstreamTransportError):
        [event async for event in client(raising).stream_chat(
            CREDENTIAL, {"messages": []}, "m")]


# ---------------------------------------------------------------- 签到


def _task_mapping(daily, balance=None, claim=None):
    mapping = {("GET", EP_TASKS): _ok({"tasks": [daily, {"task_key": "x"}]}),
               ("GET", EP_BALANCE): _ok(balance or {"total_balance": 10})}
    if claim is not None:
        mapping[("POST", "/api/public/v1/tasks/daily_check_in/claim")] = claim
    return mapping


async def test_checkin_claimed_already():
    daily = {"task_key": "daily_check_in", "status": "claimed",
             "extra": {"consecutive_days": 3}}
    result = await client(_handler(_task_mapping(daily))).checkin(CREDENTIAL)
    assert result.ok and result.already_checked_in
    assert result.message == "今日已签到"


async def test_checkin_missing_task_and_auth_error_on_balance():
    result = await client(_handler(_task_mapping({"task_key": "other"}))).checkin(CREDENTIAL)
    assert not result.ok and "每日签到" in result.message

    handler = _handler({("GET", EP_BALANCE): httpx.Response(401, text="no"),
                        ("GET", EP_TASKS): _ok({"tasks": []})})
    result = await client(handler).checkin(CREDENTIAL)
    assert not result.ok and "登录态" in result.message


async def test_checkin_claims_and_uses_balance_delta():
    daily = {"task_key": "daily_check_in", "status": "claimable",
             "extra": {"consecutive_days": 1}}
    mapping = {("GET", EP_BALANCE): httpx.Response(200, json=_ok({"total_balance": 100})),
               ("GET", EP_TASKS): _ok({"tasks": [daily]}),
               ("POST", "/api/public/v1/tasks/daily_check_in/claim"):
                   _ok({"extra": {"today_reward": 5}})}
    sequence = {"balance_calls": 0}

    def handler(request):
        if request.url.path == EP_BALANCE:
            sequence["balance_calls"] += 1
            value = 100 if sequence["balance_calls"] == 1 else 120
            return httpx.Response(200, json=_ok({"total_balance": value}))
        return mapping[(request.method, request.url.path)] \
            if isinstance(mapping.get((request.method, request.url.path)), httpx.Response) \
            else httpx.Response(200, json=mapping[(request.method, request.url.path)])

    result = await client(handler).checkin(CREDENTIAL)
    assert result.ok and result.credit == 20 and not result.already_checked_in


async def test_checkin_claim_failure_and_zero_balance_delta():
    daily = {"task_key": "daily_check_in", "status": "claimable",
             "extra": {"consecutive_days": 1}}
    failure = _handler(_task_mapping(daily, claim={"result": "failed",
                                                   "hint": "领取失败"}))
    result = await client(failure).checkin(CREDENTIAL)
    assert not result.ok and result.message == "领取失败"

    # 余额不变 → 读 claim 里的奖励字段；再退到 already_checked_in
    zero = _handler({("GET", EP_BALANCE): _ok({"total_balance": 10}),
                     ("GET", EP_TASKS): _ok({"tasks": [daily]}),
                     ("POST", "/api/public/v1/tasks/daily_check_in/claim"):
                         {"code": "Success",
                          "data": {"extra": {"today_reward": 3}}}})
    result = await client(zero).checkin(CREDENTIAL)
    assert result.ok and result.credit == 3

    zero = _handler({("GET", EP_BALANCE): _ok({"total_balance": 10}),
                     ("GET", EP_TASKS): _ok({"tasks": [daily]}),
                     ("POST", "/api/public/v1/tasks/daily_check_in/claim"): {}})
    result = await client(zero).checkin(CREDENTIAL)
    assert result.ok and result.already_checked_in


async def test_checkin_balance_after_failure_falls_back_to_reward():
    daily = {"task_key": "daily_check_in", "status": "claimable",
             "reward_amount": 7}
    calls = {"n": 0}

    def handler(request):
        if request.url.path == EP_BALANCE:
            calls["n"] += 1
            if calls["n"] > 1:
                return httpx.Response(500, text="down")
            return httpx.Response(200, json=_ok({"total_balance": 10}))
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json=_ok({"tasks": [daily]}))
        return httpx.Response(200, json=_ok({}))

    result = await client(handler).checkin(CREDENTIAL)
    assert result.ok and result.credit == 7


async def test_checkin_status_variants():
    daily = {"task_key": "daily_check_in", "status": "claimed",
             "extra": {"consecutive_days": 2, "today_reward": 5}}
    status = await client(_handler(_task_mapping(daily))).checkin_status(CREDENTIAL)
    assert status["today_checked_in"] is True
    assert status["streak_days"] == 2

    status = await client(_handler(_task_mapping({"task_key": "other"}))).checkin_status(CREDENTIAL)
    assert status == {"active": False, "today_checked_in": False}


# ---------------------------------------------------------------- 请求体/工具 schema


def test_build_body_maps_openai_fields():
    body = build_body({
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function",
                   "function": {"name": "f", "parameters": {
                       "type": "object", "properties": {"a": {"type": ["string", "null"]}}}}}],
        "tool_choice": "auto",
        "temperature": 0.2,
        "max_tokens": 9,
        "reasoning_effort": "high",
    }, "m")
    assert body["stream"] is True and body["model"] == "m"
    assert body["tool_choice"] == "auto"
    assert body["temperature"] == 0.2
    assert body["base_llm_arguments"] == {"max_tokens": 9}
    assert body["reasoning_effort"] == "high"
    assert body["llm_arguments"] == {"reasoning_effort": "high"}
    assert body["tools"][0]["function"]["parameters"]["properties"]["a"] == {
        "type": "string"}


def test_build_body_drops_bad_values():
    body = build_body({"messages": "bad", "tools": "bad", "temperature": True,
                       "max_tokens": 0, "reasoning_effort": "  "}, "m")
    assert body["messages"] == []
    assert "tools" not in body and "tool_choice" not in body
    assert "temperature" not in body and "base_llm_arguments" not in body
    assert "reasoning_effort" not in body


def test_normalize_messages_drops_orphan_tool_messages():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "c0", "content": "orphan"},
        {"role": "assistant", "tool_calls": [{"id": "c1"}, {"id": "c2"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "tool", "tool_call_id": "c1", "content": "dup"},
        {"role": "system", "content": "s"},
        {"role": "tool", "tool_call_id": "c2", "content": "orphan again"},
        {"role": "assistant"},
        "junk",
    ]
    out = normalize_messages(messages)
    assert [item.get("role") for item in out] == [
        "user", "assistant", "tool", "system", "assistant"]
    assert normalize_messages(None) == []
    # 深拷贝：原列表不被改写
    assert messages[0] == {"role": "user", "content": "hi"}


def test_sanitize_schema_resolves_refs_and_cycles():
    schema = {"type": "object",
              "properties": {"a": {"$ref": "#/$defs/A"}},
              "$defs": {"A": {"type": "object", "properties": {
                  "self": {"$ref": "#/$defs/A"}}},
                  "B": {"type": ["integer", "null"]}}}
    out = sanitize_schema(schema)
    assert out["properties"]["a"]["properties"]["self"] == {"type": "string"}
    assert "$defs" not in out

    assert sanitize_schema({"$ref": "#/$defs/nope"}) == {"type": "string"}
    assert sanitize_schema({"$ref": "#/definitions/B",
                            "definitions": {"B": {"type": "string"}}}) == {
        "type": "string"}


def test_sanitize_schema_picks_branches_and_prunes_unsupported_keys():
    schema = {"anyOf": [{"type": "string"}, {"type": "object",
                                             "properties": {"x": {"type": "string"}}}],
              "description": "d"}
    out = sanitize_schema(schema)
    assert out["type"] == "object"
    assert out["description"] == "d"

    assert sanitize_schema({"oneOf": []}) == {"type": "string"}
    assert sanitize_schema({"oneOf": [1]}) == {"type": "string"}
    assert sanitize_schema({"oneOf": [{"type": "array", "items": {"type": "string"}}]}) == {
        "type": "array", "items": {"type": "string"}}

    pruned = sanitize_schema({"type": "object", "allOf": [{"x": 1}],
                              "not": {}, "if": {}, "then": {}, "else": {},
                              "$id": "x", "$schema": "y"})
    assert pruned == {"type": "object"}


def test_sanitize_schema_primitives_and_depth_cap():
    assert sanitize_schema(None) == {"type": "string"}
    assert sanitize_schema(True) == {"type": "boolean"}
    assert sanitize_schema(3) == {"type": "number"}
    assert sanitize_schema([]) == {"type": "array"}
    nested: dict = {"type": "object", "properties": {}}
    cursor = nested
    for _ in range(30):
        child: dict = {"type": "object", "properties": {}}
        cursor["properties"]["x"] = child
        cursor = child
    out = sanitize_schema(nested)
    walk = out
    for _ in range(25):
        walk = walk["properties"]["x"]
    assert walk == {"type": "string"}


def test_last_user_text_and_helpers():
    assert lingxi_client.last_user_text([
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": [{"text": "second"}, {"x": 1}]},
        {"role": "user", "content": "   "},
    ]) == "second"
    assert lingxi_client.last_user_text([]) == "灵犀会话"
    assert lingxi_client.last_user_text([{"role": "user", "content": None}]) == "灵犀会话"

    assert lingxi_client._num("1.5") == 1.5
    assert lingxi_client._num("bad") is None
    assert lingxi_client._num(True) is None
    assert lingxi_client._num(object()) is None
    assert lingxi_client._int("7") == 7
    assert lingxi_client._dict("x") == {}

    assert lingxi_client._parse_expire("") == 0
    assert lingxi_client._parse_expire("2026-11-09T10:00:00Z") > 0
    assert lingxi_client._parse_expire("2026-11-09 10:00:00+08:00") > 0
    assert lingxi_client._parse_expire("2026-11-09 10:00") > 0
    assert lingxi_client._parse_expire("2026-11-09") > 0
    assert lingxi_client._parse_expire("bad") == 0

    assert lingxi_client._claim_date({"extra": {"claim_date": "2026-11-01"}}) == \
        "2026-11-01"
    assert lingxi_client._claim_date({})  # 本地日期


def test_frame_of_variants():
    assert lingxi_client._frame_of(None) is None
    assert lingxi_client._frame_of("") is None
    assert lingxi_client._frame_of(": hi") is None
    assert lingxi_client._frame_of("data:") is None
    assert lingxi_client._frame_of("data: [DONE]") == {}
    assert lingxi_client._frame_of("data: 12") is None
    assert lingxi_client._frame_of('data: {"a": 1}') == {"a": 1}

async def test_checkin_balance_http_error_other_than_401_is_swallowed():
    daily = {"task_key": "daily_check_in", "status": "claimed"}
    handler = _handler({("GET", EP_BALANCE): httpx.Response(500, text="down"),
                        ("GET", EP_TASKS): _ok({"tasks": [daily]})})
    result = await client(handler).checkin(CREDENTIAL)
    assert result.ok and result.already_checked_in


async def test_ensure_run_recheck_under_lock_returns_cached():
    instance = client(_handler(_run_mapping()))
    key = instance._run_key(CREDENTIAL, "m")
    lock = instance._run_locks.setdefault(key, asyncio.Lock())
    async with lock:
        task = asyncio.create_task(instance.ensure_run(CREDENTIAL, "m"))
        await asyncio.sleep(0)
        instance._runs[key] = {"session_id": "s", "run_id": "r", "at": time.time()}
    run = await task
    assert run["run_id"] == "r"


async def test_stream_chat_empty_frame_continues_then_finish():
    stream = lines({"code": "Success", "choices": [{"text": ""}]},
                   {"code": "Success", "choices": [{"finish_reason": "stop"}]})
    handler = _handler(_chat_mapping(stream))
    events = [event async for event in client(handler).stream_chat(
        CREDENTIAL, {"messages": []}, "m")]
    assert [event.kind for event in events] == [EventKind.FINISH]


class _BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"data: {\"code\": \"Success\", \"choices\": [{\"text\": \"a\"}]}\n"
        raise httpx.ReadTimeout("mid-stream")


async def test_stream_chat_transport_error_during_stream():
    handler = _handler(_run_mapping() | {
        ("POST", EP_CHAT): httpx.Response(200, stream=_BrokenStream())})
    with pytest.raises(base.UpstreamTransportError):
        [event async for event in client(handler).stream_chat(
            CREDENTIAL, {"messages": []}, "m")]


async def test_client_aclose_releases_pools():
    instance = LingxiClient(host="https://a.test")
    await instance.aclose()


def test_last_user_text_skips_non_user_items():
    assert lingxi_client.last_user_text([
        {"role": "user", "content": "first"}, {"role": "assistant", "content": "a"},
    ]) == "first"
    assert lingxi_client.last_user_text(["junk"]) == "灵犀会话"


def test_normalize_messages_tool_calls_with_junk_entries():
    out = normalize_messages([
        {"role": "assistant", "tool_calls": ["junk", {"id": 1}, {"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ])
    assert [item["role"] for item in out] == ["assistant", "tool"]


def test_build_body_drops_non_function_tools_and_bad_choice():
    body = build_body({"messages": [], "tools": [
        {"type": "function"},
        {"type": "web_search", "web_search": {}},
        {"type": "function", "function": {"name": "f", "parameters": None}},
    ]}, "m")
    assert "tool_choice" not in body
    assert len(body["tools"]) == 1
    # parameters 缺失时按标量兜底（压平函数对 None 的既定行为）
    assert body["tools"][0]["function"]["parameters"] == {"type": "string"}
    # 全部工具都被剔除时不带 tools 键
    assert "tools" not in build_body({"messages": [], "tools": [
        {"type": "web_search"}]}, "m")


def test_tool_choice_normalization():
    """实测：字符串三种值可用，对象形态上游 400，故降级为 required。"""
    assert lingxi_client._tool_choice_of("auto") == "auto"
    assert lingxi_client._tool_choice_of(" none ") == "none"
    assert lingxi_client._tool_choice_of({"type": "function",
                                          "function": {"name": "f"}}) == "required"
    assert lingxi_client._tool_choice_of("") is None
    assert lingxi_client._tool_choice_of(1) is None
    assert lingxi_client._tool_choice_of(None) is None
    body = build_body({"messages": [], "tools": [
        {"type": "function", "function": {"name": "f"}}],
        "tool_choice": {"type": "function", "function": {"name": "f"}}}, "m")
    assert body["tool_choice"] == "required"


async def test_fetch_models_parses_multiplier_text():
    """上游倍率是带标记的字符串（`0.12x` / `0.75倍`），必须解析成数值。"""
    handler = _handler({("GET", EP_PLANS): _ok({"models": [
        {"key": "a", "multiplier": "0.12x"},
        {"key": "b", "multiplier": "0.75倍"},
        {"key": "c", "multiplier": "×1.6"},
        {"key": "d", "multiplier": "～0.08x"},
        {"key": "e", "multiplier": 0.5},
        {"key": "f", "multiplier": "模式"},
        {"key": "g", "credit_rate": 0.2},
    ]})})
    models = {model.id: model.credit_rate
              for model in await client(handler).fetch_models(CREDENTIAL)}
    assert models == {"a": 0.12, "b": 0.75, "c": 1.6, "d": 0.08,
                      "e": 0.5, "f": None, "g": 0.2}


def test_rate_helper_variants():
    assert lingxi_client._rate("0.12x") == 0.12
    assert lingxi_client._rate("0.75倍") == 0.75
    assert lingxi_client._rate("×1.6") == 1.6
    assert lingxi_client._rate("约1.6x") == 1.6
    # 上游实测形态：全角波浪线（U+FF5E）
    assert lingxi_client._rate("～0.12x") == 0.12
    assert lingxi_client._rate(" 2 ") == 2
    assert lingxi_client._rate(1.5) == 1.5
    assert lingxi_client._rate("模式") is None
    assert lingxi_client._rate(True) is None
    assert lingxi_client._rate(None) is None


def test_multiplier_of_nested_and_fallbacks():
    assert lingxi_client._multiplier_of({"credit": {"multiplier": "0.2x"}}) == 0.2
    assert lingxi_client._multiplier_of({"rate": 0.3}) == 0.3
    assert lingxi_client._multiplier_of({"credit": "bad"}) is None
    assert lingxi_client._multiplier_of({}) is None
