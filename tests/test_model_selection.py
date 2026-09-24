"""当前选中的模型失效时要自愈，不能在界面上显示成一串 id。

回归的坑：``sensenova-6.8-flash-lite`` 从「自定义模型」改成服务端代理的内置模型后，
``settings.ini`` 里的 ``custom`` 清空了，但 ``chat/model`` 与历史会话里还留着那个
自定义模型的本地 id（``261995c298af``）—— 模型按钮找不到对应条目，直接把原始 id
显示了出来，发请求时也解析不到模型配置，静默落到离线示例引擎。
"""

from __future__ import annotations

import json

import pytest

from paper_agent.core import config as config_module
from paper_agent.core.config import DEFAULT_MODELS, AppConfig
from paper_agent.core.models import ChatSession, CustomModel

BUILTIN_ID = DEFAULT_MODELS[0]["id"]
GHOST_ID = "261995c298af"          # 已被删掉的自定义模型
CUSTOM_ID = "c0ffee123456"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """把配置文件指到临时目录，避免动到用户真实设置。"""
    monkeypatch.setattr(config_module, "SETTINGS_FILE", tmp_path / "settings.ini")
    config = AppConfig()
    yield config
    config.sync()


def _custom_model() -> CustomModel:
    return CustomModel(
        name="商汤大模型", model_id="sensenova-6.8-flash-lite",
        base_url="https://token.sensenova.cn/v1", api_key="sk-x", id=CUSTOM_ID,
    )


# ---------------------------------------------------------------- 配置层自愈
def test_config_keeps_selected_custom_model(settings):
    settings.custom_models = [_custom_model()]
    settings.model = CUSTOM_ID
    assert settings.model == CUSTOM_ID


def test_config_falls_back_when_model_is_gone(settings):
    """选中的自定义模型被删掉：读出来的当前模型要落回内置默认，不能是空 id。"""
    settings.custom_models = [_custom_model()]
    settings.model = CUSTOM_ID
    settings.custom_models = []                     # 用户把模型删了

    assert settings.model == BUILTIN_ID
    assert settings.find_model(settings.model) is not None


def test_config_falls_back_for_garbage_id(settings):
    settings.model = GHOST_ID
    assert settings.model == BUILTIN_ID


def test_config_falls_back_for_empty_value(settings):
    settings.setValue("chat/model", "")
    assert settings.model == BUILTIN_ID


# ---------------------------------------------------------------- 下拉框
def _models() -> list[dict]:
    return [
        {"id": BUILTIN_ID, "name": "SenseNova 6.8 Flash Lite", "desc": "服务端代理",
         "custom": False, "builtin": True},
        {"id": "gpt-5", "name": "GPT-5", "desc": "示例", "custom": False, "builtin": False},
    ]


def test_button_shows_name_not_raw_id_for_missing_model(qapp):
    """核心回归：传进来的 current 已经不存在，按钮上不能显示那个 id。"""
    from paper_agent.ui.widgets.model_combo import ModelMenuButton

    button = ModelMenuButton(models=_models(), current=GHOST_ID)

    assert button.current_model == BUILTIN_ID
    assert GHOST_ID not in button.text()
    # 按钮上按宽度截断（这里是省略号），开头必须是模型名；完整名称在 tooltip 里
    assert button.text().startswith("SenseNova")
    assert "SenseNova 6.8 Flash Lite" in button.toolTip()


def test_button_keeps_valid_current(qapp):
    from paper_agent.ui.widgets.model_combo import ModelMenuButton

    button = ModelMenuButton(models=_models(), current="gpt-5")

    assert button.current_model == "gpt-5"
    assert button.text() == "GPT-5"


def test_button_reload_heals_stale_current(qapp):
    from paper_agent.ui.widgets.model_combo import ModelMenuButton

    button = ModelMenuButton(models=_models(), current=GHOST_ID)
    button.reload(_models(), current=GHOST_ID)

    assert button.current_model == BUILTIN_ID


def test_button_set_current_resolves_and_notifies(qapp):
    """切到一个不存在的模型：收敛到可用模型，并把结果广播出去让上层纠正。"""
    from paper_agent.ui.widgets.model_combo import ModelMenuButton

    button = ModelMenuButton(models=_models(), current="gpt-5")
    received: list[str] = []
    button.model_changed.connect(received.append)

    button.set_current(GHOST_ID)

    assert button.current_model == BUILTIN_ID
    assert received == [BUILTIN_ID]


def test_button_handles_empty_model_list(qapp):
    """模型列表为空时落回内置默认列表，按钮上不能是空字符串或裸 id。"""
    from paper_agent.ui.widgets.model_combo import ModelMenuButton

    button = ModelMenuButton(models=[], current=GHOST_ID)

    assert button.current_model == BUILTIN_ID
    assert button.text()


# ---------------------------------------------------------------- 会话
def test_switching_session_repairs_removed_model(qapp, tmp_path, settings):
    """老会话里记着已删掉的模型：切过去要改成可用模型并落盘。"""
    from paper_agent.core.session_store import SessionStore
    from paper_agent.ui.main_window import MainWindow

    store = SessionStore(tmp_path / "sessions")
    session = ChatSession(title="老工程会话", model=GHOST_ID)
    store.save(session)

    window = MainWindow(AppConfig(), store)
    window.sessions = {session.id: session}
    window.activate_session(session.id)

    assert window.config.model == BUILTIN_ID
    assert window.composer.model_button.current_model == BUILTIN_ID
    assert GHOST_ID not in window.composer.model_button.text()
    # 落盘后下次启动就不会再出现这个 id
    saved = json.loads((tmp_path / "sessions" / f"{session.id}.json").read_text("utf-8"))
    assert saved["model"] == BUILTIN_ID


def test_switching_session_keeps_valid_model(qapp, tmp_path, settings):
    from paper_agent.core.session_store import SessionStore
    from paper_agent.ui.main_window import MainWindow

    store = SessionStore(tmp_path / "sessions")
    session = ChatSession(title="会话", model="gpt-5")
    store.save(session)

    window = MainWindow(AppConfig(), store)
    window.sessions = {session.id: session}
    window.activate_session(session.id)

    assert window.config.model == "gpt-5"
    assert window.composer.model_button.current_model == "gpt-5"
