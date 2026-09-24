"""聊天客户端的多 Key 行为：命中 429 自动换 Key 重试。"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from paper_agent.services.key_pool import reset_shared_pools
from paper_agent.services.openai_client import OpenAICompatClient, OpenAIError

BASE = "https://api.example.com/v1"


@pytest.fixture(autouse=True)
def _clean_pools():
    reset_shared_pools()
    yield
    reset_shared_pools()


def _http_error(code: int, message: str = "rate limit exceeded") -> urllib.error.HTTPError:
    body = json.dumps({"error": {"message": message}}).encode("utf-8")
    return urllib.error.HTTPError(BASE, code, "err", {}, io.BytesIO(body))


class _Response:
    """最小可用的响应对象：支持 chat() 的 read() 与流式的迭代。"""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        self.closed = True


def _install(monkeypatch, outcomes: list):
    """把 urlopen 换成按顺序返回结果的假实现，并记录每次用的 Key。"""
    used: list[str] = []

    def fake_urlopen(request, timeout=None, context=None):
        used.append(request.headers.get("Authorization", ""))
        outcome = outcomes[len(used) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return used


def test_retries_with_next_key_on_429(monkeypatch):
    response = _Response({"choices": [{"message": {"content": "pong"}}]})
    used = _install(monkeypatch, [_http_error(429), response])

    client = OpenAICompatClient(BASE, model_id="m1", api_keys=["sk-a", "sk-b"])
    assert client.chat([{"role": "user", "content": "ping"}]) == "pong"
    assert used == ["Bearer sk-a", "Bearer sk-b"], "第一个 Key 被限流后应换第二个重试"
    assert client.pool.available_count() == 1, "被限流的 sk-a 应留在冷却期，sk-b 保持可用"


def test_reports_all_keys_limited(monkeypatch):
    _install(monkeypatch, [_http_error(429), _http_error(429)])
    client = OpenAICompatClient(BASE, model_id="m1", api_keys=["sk-a", "sk-b"])

    with pytest.raises(OpenAIError) as info:
        client.chat([{"role": "user", "content": "ping"}])
    assert "2 个 API Key 都被限流" in str(info.value)
    assert client.pool.available_count() == 0


def test_single_key_does_not_retry_forever(monkeypatch):
    used = _install(monkeypatch, [_http_error(429)])
    client = OpenAICompatClient(BASE, api_key="sk-only", model_id="m1")

    with pytest.raises(OpenAIError):
        client.chat([{"role": "user", "content": "ping"}])
    assert len(used) == 1


def test_non_429_error_raises_immediately(monkeypatch):
    used = _install(monkeypatch, [_http_error(401, "invalid api key"), _Response({})])
    client = OpenAICompatClient(BASE, model_id="m1", api_keys=["sk-a", "sk-b"])

    with pytest.raises(OpenAIError) as info:
        client.chat([{"role": "user", "content": "ping"}])
    assert "401" in str(info.value)
    assert len(used) == 1, "鉴权失败换 Key 也没用，不该盲目重试"


def test_limited_key_is_skipped_on_next_request(monkeypatch):
    """冷却状态跨请求保留：下一轮直接跳过被限流的 Key。"""
    reply = {"choices": [{"message": {"content": "pong"}}]}
    used = _install(monkeypatch, [_http_error(429), _Response(reply), _Response(reply)])

    client = OpenAICompatClient(BASE, model_id="m1", api_keys=["sk-a", "sk-b"])
    client.chat([{"role": "user", "content": "ping"}])
    client.chat([{"role": "user", "content": "ping again"}])       # 第二次

    assert used[:3] == ["Bearer sk-a", "Bearer sk-b", "Bearer sk-b"], (
        "被限流的 sk-a 在下一轮应被跳过"
    )


def test_pool_is_shared_between_clients(monkeypatch):
    _install(monkeypatch, [_http_error(429), _Response({"choices": [{"message": {"content": "ok"}}]})])

    first = OpenAICompatClient(BASE, model_id="m1", api_keys=["sk-a", "sk-b"])
    second = OpenAICompatClient(BASE, model_id="m1", api_keys=["sk-a", "sk-b"])
    assert first.pool is second.pool, "同一模型应共用请求池"

    first.chat([{"role": "user", "content": "ping"}])
    assert second.pool.available_count() == 1, "别的客户端发起的限流对本池也生效"


def test_api_key_attribute_keeps_first_key():
    client = OpenAICompatClient(BASE, api_key="sk-main", model_id="m1", api_keys=["sk-extra"])
    assert client.api_key == "sk-main"
    assert client.pool.keys == ("sk-main", "sk-extra")


# ---------------------------------------------------------------- 流式中途限流
class _SseResponse:
    """可迭代的假响应：模拟 SSE 流（含中途返回 error 的情况）。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = [f"{line}\n".encode("utf-8") for line in lines]
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def close(self) -> None:
        self.closed = True


def test_midstream_rate_limit_cools_down_active_key(monkeypatch):
    """连接建立后才收到的 429 也要记账，否则重试还会撞同一把 Key。"""
    stream = _SseResponse(
        [
            'data: {"choices":[{"delta":{"content":"好的"}}]}',
            'data: {"error":{"message":"inference exceeds tpm/rpm limit"}}',
        ]
    )
    used = _install(monkeypatch, [stream])
    client = OpenAICompatClient(BASE, model_id="m1", api_keys=["sk-a", "sk-b"])

    with pytest.raises(OpenAIError) as info:
        list(client.stream_events([{"role": "user", "content": "hi"}]))
    assert "tpm/rpm" in str(info.value)

    assert used == ["Bearer sk-a"]
    assert client.pool.available_count() == 1, "被限流的 sk-a 应进入冷却"
    assert client.pool.acquire() == "sk-b", "下一次请求应换到 sk-b"


def test_midstream_normal_stream_keeps_pool_healthy(monkeypatch):
    stream = _SseResponse(
        [
            'data: {"choices":[{"delta":{"content":"你好"}}]}',
            "data: [DONE]",
        ]
    )
    _install(monkeypatch, [stream])
    client = OpenAICompatClient(BASE, model_id="m1", api_keys=["sk-a", "sk-b"])

    events = list(client.stream_events([{"role": "user", "content": "hi"}]))
    # 解析器会为了处理跨 chunk 的 <think> 标签先吐一个空片段，过滤掉再比对
    texts = [item["text"] for item in events if item["type"] == "content" and item["text"]]
    assert texts == ["你好"]
    assert client.pool.available_count() == 2
