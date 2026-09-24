"""推理强度参数：字段映射、自动识别、以及「服务端不认就撤掉」的降级保护。"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from paper_agent.services.openai_client import OpenAICompatClient, OpenAIError
from paper_agent.services.reasoning import (
    REASONING_MODES,
    is_unsupported_error,
    normalise_effort,
    reasoning_payload,
    resolve_mode,
)

BASE = "https://api.example.com/v1"


# ---------------------------------------------------------------- 字段映射
def test_reasoning_effort_payload():
    assert reasoning_payload("reasoning_effort", "low") == {"reasoning_effort": "low"}
    assert reasoning_payload("reasoning_effort", "high") == {"reasoning_effort": "high"}


def test_enable_thinking_payload():
    """只有「关闭」档才关思考；低/中/高/最深都要发 true（思考长度由服务端决定）。"""
    assert reasoning_payload("enable_thinking", "none") == {"enable_thinking": False}
    assert reasoning_payload("enable_thinking", "low") == {"enable_thinking": True}
    assert reasoning_payload("enable_thinking", "medium") == {"enable_thinking": True}
    assert reasoning_payload("enable_thinking", "high") == {"enable_thinking": True}
    assert reasoning_payload("enable_thinking", "max") == {"enable_thinking": True}


def test_thinking_budget_payload():
    payload = reasoning_payload("thinking_budget", "high")
    assert payload["thinking"]["budget_tokens"] > reasoning_payload(
        "thinking_budget", "low"
    )["thinking"]["budget_tokens"]


def test_off_sends_nothing():
    assert reasoning_payload("off", "high") == {}


def test_all_modes_are_known():
    for mode in REASONING_MODES:
        assert resolve_mode(mode) in (*REASONING_MODES, "off")


@pytest.mark.parametrize(
    "model_id,name",
    [
        ("o3-mini", ""),
        ("gpt-5", ""),
        ("deepseek-reasoner", ""),
        ("", "DeepSeek-R1 满血版"),
        ("qwq-32b", ""),
    ],
)
def test_auto_detects_reasoning_models(model_id, name):
    assert resolve_mode("auto", model_id, name) == "reasoning_effort"


@pytest.mark.parametrize("model_id", ["261995c298af", "deepseek-chat", "qwen-max"])
def test_auto_is_silent_for_unknown_models(model_id):
    """认不出来就不发参数 —— 保持和以前完全一致的兼容性。"""
    assert resolve_mode("auto", model_id) == "off"
    assert reasoning_payload("auto", "high", model_id) == {}


def test_unknown_mode_and_effort_fall_back():
    assert resolve_mode("不存在的模式", "o3") == "reasoning_effort"   # 按 auto 处理
    assert normalise_effort("HUGE") == "medium"
    assert reasoning_payload("reasoning_effort", "HUGE") == {"reasoning_effort": "medium"}


# ---------------------------------------------------------------- 商汤口径
@pytest.mark.parametrize(
    "model_id,name",
    [("sensenova-6.8-flash-lite", ""), ("", "商汤日日新"), ("sensenova", "")],
)
def test_sensenova_is_detected_from_name(model_id, name):
    assert resolve_mode("auto", model_id, name) == "sensenova_effort"


@pytest.mark.parametrize(
    "effort,expected",
    [("none", "none"), ("low", "low"), ("medium", "medium"), ("high", "high"), ("max", "xhigh")],
)
def test_sensenova_five_levels(effort, expected):
    """实测商汤接受 low / medium / high / xhigh / none。"""
    payload = reasoning_payload("sensenova_effort", effort, "sensenova-6.8-flash-lite")
    assert payload == {"reasoning_effort": expected}


def test_never_send_value_max_to_sensenova():
    """商汤实测会 400：field ReasoningEffort invalid, should be one of … xhigh …"""
    allowed = {"none", "low", "medium", "high", "xhigh"}
    for effort in ("none", "low", "medium", "high", "max"):
        value = reasoning_payload("sensenova_effort", effort).get("reasoning_effort")
        assert value in allowed, f"{effort} 映射成了非法值 {value}"


def test_openai_effort_max_clamps_to_high():
    """o 系列 / GPT-5 只认 low/medium/high，最深档要夹到 high。"""
    assert reasoning_payload("reasoning_effort", "max") == {"reasoning_effort": "high"}


def test_none_level_for_providers_without_it():
    assert reasoning_payload("reasoning_effort", "none") == {}
    assert reasoning_payload("thinking_budget", "none") == {}
    assert reasoning_payload("enable_thinking", "none") == {"enable_thinking": False}


def test_temperature_is_kept_alongside_reasoning(monkeypatch):
    """商汤实测 reasoning_effort 与 temperature 可同时传，不该被省略。"""
    bodies = _payloads(monkeypatch)
    client = OpenAICompatClient(
        BASE, model_id="sensenova-6.8-flash-lite", api_key="sk-a",
        reasoning="auto", effort="max",
    )
    client.chat([{"role": "user", "content": "hi"}])

    assert bodies[0]["reasoning_effort"] == "xhigh"
    assert "temperature" in bodies[0], "商汤接受 temperature，不需要省略"


def test_effort_levels_are_consistent_across_ui_and_engine():
    from paper_agent.core.config import REASONING_EFFORTS
    from paper_agent.services.agents.engine import AgentOrchestrator
    from paper_agent.services.reasoning import VALID_EFFORTS

    ids = [item["id"] for item in REASONING_EFFORTS]
    assert ids == ["none", "low", "medium", "high", "max"], "界面档位"
    assert set(ids) <= set(VALID_EFFORTS)
    for level in ids:
        assert level in AgentOrchestrator.EFFORT_TEMPERATURES
        assert level in AgentOrchestrator.STEP_LIMITS
    assert AgentOrchestrator.STEP_LIMITS["max"] > AgentOrchestrator.STEP_LIMITS["high"]
    assert (
        AgentOrchestrator.EFFORT_TEMPERATURES["none"]
        < AgentOrchestrator.EFFORT_TEMPERATURES["high"]
    )


def test_default_payload_is_empty_for_sensenova_none_level():
    """「关闭」档在商汤上要真的发 none（服务端有这个概念）。"""
    assert reasoning_payload("sensenova_effort", "none") == {"reasoning_effort": "none"}


@pytest.mark.parametrize(
    "text",
    [
        "HTTP 400 · Unrecognized request argument supplied: reasoning_effort",
        "unexpected keyword argument 'enable_thinking'",
        "unknown parameter: thinking",
        "不支持该参数：reasoning_effort",     # 必须提到推理参数才算
    ],
)
def test_unsupported_error_detection(text):
    assert is_unsupported_error(text) is True


def test_normal_errors_are_not_unsupported():
    assert is_unsupported_error("HTTP 429 · rate limit") is False
    assert is_unsupported_error("HTTP 500 · server error") is False


# ---------------------------------------------------------------- 请求体
class _OkResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __iter__(self):        # 供流式循环按行读取
        return iter([self._body])

    def close(self) -> None:
        pass


def _install(monkeypatch, responses: list[object]) -> list[str]:
    """替换 urlopen，记录每次请求的 Authorization 头并依次返回给定响应。"""
    captured: list[str] = []
    queue = list(responses)

    def fake_urlopen(request, timeout=None, context=None):   # noqa: ARG001
        captured.append(request.get_header("Authorization"))
        item = queue.pop(0) if queue else responses[-1]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return captured


def _payloads(monkeypatch) -> list[dict]:
    """记录每次真正发出去的请求体。"""
    bodies: list[dict] = []

    def fake_urlopen(request, timeout=None, context=None):   # noqa: ARG001
        bodies.append(json.loads(request.data.decode("utf-8")))
        return _OkResponse(json.dumps({"choices": [{"message": {"content": "ok"}}]}))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return bodies


def test_payload_carries_reasoning_effort(monkeypatch):
    bodies = _payloads(monkeypatch)
    client = OpenAICompatClient(
        BASE, model_id="o3-mini", api_key="sk-a",
        reasoning="reasoning_effort", effort="high",
    )
    client.chat([{"role": "user", "content": "hi"}])

    assert bodies[0]["reasoning_effort"] == "high"
    assert bodies[0]["model"] == "o3-mini"


def test_off_mode_keeps_payload_clean(monkeypatch):
    bodies = _payloads(monkeypatch)
    client = OpenAICompatClient(
        BASE, model_id="deepseek-chat", api_key="sk-a", reasoning="off", effort="high"
    )
    client.chat([{"role": "user", "content": "hi"}])

    assert "reasoning_effort" not in bodies[0]
    assert "thinking" not in bodies[0]


def test_stream_payload_carries_reasoning_fields(monkeypatch):
    bodies = _payloads(monkeypatch)
    client = OpenAICompatClient(
        BASE, model_id="qwen3-max", api_key="sk-a",
        reasoning="enable_thinking", effort="medium",
    )
    list(client.stream_chat([{"role": "user", "content": "hi"}]))

    assert bodies[0]["enable_thinking"] is True


# ---------------------------------------------------------------- 降级保护
def _http_400(message: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        f"{BASE}/chat/completions",
        400,
        "Bad Request",
        {},
        io.BytesIO(json.dumps({"error": {"message": message}}).encode("utf-8")),
    )


def test_rejected_reasoning_param_is_dropped_and_retried(monkeypatch):
    """服务端不认参数时应撤掉重试，而不是把整个请求判死。"""
    bodies = _payloads(monkeypatch)
    calls = {"n": 0}
    good = _OkResponse(json.dumps({"choices": [{"message": {"content": "ok"}}]}))

    def fake_urlopen(request, timeout=None, context=None):   # noqa: ARG001
        bodies.append(json.loads(request.data.decode("utf-8")))
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_400("Unrecognized request argument supplied: reasoning_effort")
        return good

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatClient(
        BASE, model_id="o3-mini", api_key="sk-a",
        reasoning="reasoning_effort", effort="high",
    )
    assert client.chat([{"role": "user", "content": "hi"}]) == "ok"

    assert len(bodies) == 2, "应重试一次"
    assert bodies[0]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in bodies[1], "重试时应撤掉参数"
    assert client.reasoning_dropped is True


def test_drop_does_not_consume_key_rotation(monkeypatch):
    """撤参数重试与 Key 轮换无关，不该因此少试一把 Key。"""
    calls: list[str] = []
    good = _OkResponse(json.dumps({"choices": [{"message": {"content": "ok"}}]}))

    def fake_urlopen(request, timeout=None, context=None):   # noqa: ARG001
        key = request.get_header("Authorization")
        calls.append(key)
        if len(calls) == 1:
            raise _http_400("unknown parameter reasoning_effort")
        return good

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatClient(
        BASE, model_id="o3-mini", api_keys=["sk-a", "sk-b"],
        reasoning="reasoning_effort", effort="low",
    )
    client.chat([{"role": "user", "content": "hi"}])

    assert calls == ["Bearer sk-a", "Bearer sk-a"], "撤参数重试应仍用同一把 Key"
    assert client.pool.available_count() == 2, "该 Key 不该被记为限流"


def test_unrelated_400_is_raised(monkeypatch):
    """其它 400（如参数非法、模型不存在）不能被误吞。"""
    _payloads(monkeypatch)

    def fake_urlopen(request, timeout=None, context=None):   # noqa: ARG001
        raise _http_400("Invalid value for 'temperature'")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatClient(
        BASE, model_id="o3-mini", api_key="sk-a",
        reasoning="reasoning_effort", effort="high",
    )
    with pytest.raises(OpenAIError) as info:
        client.chat([{"role": "user", "content": "hi"}])
    assert "temperature" in str(info.value)


def test_drop_happens_only_once_per_client(monkeypatch):
    """撤过一次之后，后续请求不再携带该参数（不再白撞一次 400）。"""
    bodies = _payloads(monkeypatch)
    calls = {"n": 0}
    good = _OkResponse(json.dumps({"choices": [{"message": {"content": "ok"}}]}))

    def fake_urlopen(request, timeout=None, context=None):   # noqa: ARG001
        bodies.append(json.loads(request.data.decode("utf-8")))
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_400("unexpected keyword argument 'reasoning_effort'")
        return good

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatClient(
        BASE, model_id="o3-mini", api_key="sk-a",
        reasoning="reasoning_effort", effort="high",
    )
    client.chat([{"role": "user", "content": "one"}])
    client.chat([{"role": "user", "content": "two"}])

    assert all("reasoning_effort" not in body for body in bodies[1:]), bodies
    assert len(bodies) == 3, "第 1 次(拒) + 第 2 次(撤) + 第 3 次(后续不再带)"
