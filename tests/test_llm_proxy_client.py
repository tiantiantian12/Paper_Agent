"""内置模型的代理链路：模型清单、端点组装、代理密钥的存取与过期判断。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from paper_agent.core.config import DEFAULT_MODELS, AppConfig
from paper_agent.core.session_store import SessionStore
from paper_agent.core.signals import signals
from paper_agent.services.llm_key import LlmKeySyncer

BUILTIN_ID = "sensenova-6.8-flash-lite"


def test_sensenova_is_a_builtin_model():
    entry = next((item for item in DEFAULT_MODELS if item["id"] == BUILTIN_ID), None)
    assert entry is not None, "sensenova-6.8-flash-lite 应作为内置模型"
    assert entry["builtin"] is True


def test_all_models_marks_builtin_flag():
    config = AppConfig()
    models = config.all_models()
    builtin = [m for m in models if m["builtin"]]
    assert [m["id"] for m in builtin] == [BUILTIN_ID]
    assert config.is_builtin_model(BUILTIN_ID) is True
    assert config.is_builtin_model("gpt-5") is False


def test_proxy_url_follows_server_address():
    config = AppConfig()
    original = config.auth_server_url
    try:
        config.auth_server_url = "http://10.0.0.5:9000/"
        assert config.llm_proxy_url == "http://10.0.0.5:9000/api/llm/v1"
    finally:
        config.auth_server_url = original


# ---------------------------------------------------------------- 密钥存取
def test_llm_key_round_trip_and_expiry():
    config = AppConfig()
    original = (config.llm_user_key, config.llm_key_expires)
    try:
        config.llm_user_key = "pk-abc"
        config.llm_key_expires = ""
        assert config.llm_user_key == "pk-abc"
        assert config.llm_key_expired is False, "没有到期时间表示永不过期"

        future = datetime.now().astimezone() + timedelta(days=1)
        config.llm_key_expires = future.isoformat()
        assert config.llm_key_expired is False

        past = datetime.now().astimezone() - timedelta(seconds=5)
        config.llm_key_expires = past.isoformat()
        assert config.llm_key_expired is True

        config.llm_key_expires = "看不懂的时间"
        assert config.llm_key_expired is False, "格式异常时交给服务端判定，不误杀"

        config.clear_llm_key()
        assert config.llm_user_key == "" and config.llm_key_expires == ""
    finally:
        config.llm_user_key, config.llm_key_expires = original


# ---------------------------------------------------------------- 同步器
def test_syncer_clears_key_when_logged_out(monkeypatch):
    config = AppConfig()
    original = (config.llm_user_key, config.llm_key_expires)
    monkeypatch.setattr(type(config), "logged_in", property(lambda self: False))
    received: list[dict] = []
    signals.llm_key_updated.connect(received.append)
    try:
        config.llm_user_key = "pk-旧密钥"
        LlmKeySyncer(config).sync()
        assert received and received[-1]["key"] == "", "未登录要把旧密钥清掉"
    finally:
        signals.llm_key_updated.disconnect(received.append)
        config.llm_user_key, config.llm_key_expires = original


# ---------------------------------------------------------------- 端点
def _window(tmp_path):
    from paper_agent.ui.main_window import MainWindow

    return MainWindow(AppConfig(), SessionStore(tmp_path / "sessions"))


def test_builtin_model_uses_server_proxy(qapp, tmp_path, monkeypatch):
    """内置模型必须指向服务端代理，并且带上服务端签发的用户密钥。"""
    config = AppConfig()
    monkeypatch.setattr(type(config), "llm_user_key", property(lambda self: "pk-user"))
    window = _window(tmp_path)
    window.config.model = BUILTIN_ID

    endpoint = window._current_endpoint()
    assert endpoint is not None
    assert endpoint["proxy"] is True
    assert endpoint["base_url"].endswith("/api/llm/v1")
    assert endpoint["api_key"] == "pk-user"
    assert endpoint["model_id"] == BUILTIN_ID


def test_builtin_model_without_key_falls_back(qapp, tmp_path, monkeypatch):
    config = AppConfig()
    monkeypatch.setattr(type(config), "llm_user_key", property(lambda self: ""))
    window = _window(tmp_path)
    window.config.model = BUILTIN_ID

    assert window._current_endpoint() is None
    monkeypatch.setattr(type(config), "logged_in", property(lambda self: False))
    assert window._endpoint_problem(None) == "", "没登录时保持离线示例引擎，不报错"


def test_builtin_model_without_key_reports_problem(qapp, tmp_path, monkeypatch):
    config = AppConfig()
    monkeypatch.setattr(type(config), "logged_in", property(lambda self: True))
    monkeypatch.setattr(type(config), "llm_key_expired", property(lambda self: True))
    window = _window(tmp_path)
    window.config.model = BUILTIN_ID

    problem = window._endpoint_problem(None)
    assert "过期" in problem

    monkeypatch.setattr(type(config), "llm_key_expired", property(lambda self: False))
    assert "服务端" in window._endpoint_problem(None)


def test_custom_model_missing_config_reports_problem(qapp, tmp_path, monkeypatch):
    window = _window(tmp_path)
    window.config.model = "gpt-5"
    assert window._endpoint_problem(None) == "", "普通示例模型不该被拦"
    assert "自定义模型" not in window._endpoint_problem(None)

    monkeypatch.setattr(type(window.config), "is_custom_model", lambda self, _id: True)
    assert "自定义模型" in window._endpoint_problem(None)


def test_logout_clears_llm_key(qapp, tmp_path):
    window = _window(tmp_path)
    window.config.llm_user_key = "pk-abc"
    window.config.llm_key_expires = "2099-01-01T00:00:00+08:00"
    window.config.set_account("token-1", {"id": 1, "email": "a@qq.com", "nickname": "张三"})

    window.config.clear_account()
    assert window.config.llm_user_key == ""
    assert window.config.logged_in is False


def test_main_window_wires_key_signal(qapp, tmp_path):
    """主窗口要接收全局密钥信号并写回配置。"""
    window = _window(tmp_path)
    future = (datetime.now().astimezone() + timedelta(hours=1)).isoformat()
    signals.llm_key_updated.emit({"key": "pk-from-server", "expires_at": future})

    assert window.config.llm_user_key == "pk-from-server"
    assert window.config.llm_key_expired is False
    assert time.time() > 0      # 触发过一遍同步逻辑即可


# ---------------------------------------------------------------- 连不上的提示
def _dead_port() -> int:
    """占一个端口再放掉，拿到一个确定没人监听的端口号。"""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_unreachable_error_names_the_address():
    """回归：服务端地址填错（端口对不上）时，错误里必须带地址。

    以前只说「无法连接服务：连接被拒绝」，用户完全看不出是自己登录页里
    留着一个旧端口 —— 聊天发不出去就是这么被卡住的。
    """
    import pytest

    from paper_agent.services.openai_client import OpenAICompatClient, OpenAIError

    port = _dead_port()
    client = OpenAICompatClient(
        f"http://127.0.0.1:{port}/api/llm/v1", api_key="pk-x", model_id=BUILTIN_ID
    )

    with pytest.raises(OpenAIError) as err:
        client.chat([{"role": "user", "content": "hi"}])

    message = str(err.value)
    assert f":{port}" in message
    assert "/api/llm/v1/chat/completions" in message
    assert "服务地址" in message, "内置模型走服务端代理，要提示去哪儿改地址"


def test_unreachable_direct_provider_has_no_server_hint():
    """直连供应商（自定义模型）时不该提「登录页的服务地址」。"""
    from paper_agent.services.openai_client import OpenAICompatClient

    client = OpenAICompatClient("https://api.example.invalid/v1", api_key="sk-x")
    message = client.unreachable(OSError("连接被拒绝"))

    assert "api.example.invalid" in message
    assert "服务地址" not in message
