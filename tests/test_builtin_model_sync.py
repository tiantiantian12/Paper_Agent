"""内置模型的名称 / 备注由服务端看板维护，客户端登录后同步。

回归的坑：内置模型的显示名原先写死在桌面端的 ``DEFAULT_MODELS`` 里，服务端改了名字
（比如「SenseNova 6.8 Flash Lite」→「商汤日日新 6.8」）客户端完全不知道，只能发新版。
"""

from __future__ import annotations

import pytest

from paper_agent.core import config as config_module
from paper_agent.core.config import AppConfig
from paper_agent.services.llm_models import LlmModelsSyncer, normalize

BUILTIN_ID = "sensenova-6.8-flash-lite"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """把配置文件指到临时目录，避免动到用户真实设置。"""
    monkeypatch.setattr(config_module, "SETTINGS_FILE", tmp_path / "settings.ini")
    config = AppConfig()
    yield config
    config.sync()


def _entry(config: AppConfig, model_id: str = BUILTIN_ID) -> dict:
    return next(item for item in config.all_models() if item["id"] == model_id)


# ---------------------------------------------------------------- 归一化
def test_normalize_keeps_display_fields():
    models = normalize([{"model_id": "m", "name": "名字", "desc": "备注", "base_url": "x"}])
    assert models == [{"model_id": "m", "name": "名字", "desc": "备注", "kind": "chat"}]


@pytest.mark.parametrize(
    "raw,expected",
    [("image", "image"), ("IMAGE", "image"), ("chat", "chat"), ("未知", "chat"), ("", "chat")],
)
def test_normalize_kind(raw, expected):
    models = normalize([{"model_id": "m", "kind": raw}])
    assert models[0]["kind"] == expected


@pytest.mark.parametrize(
    "payload",
    [None, "文字", 3, [None], [{"name": "没有 id"}], [{"model_id": "  "}]],
)
def test_normalize_drops_junk(payload):
    assert normalize(payload) == []


# ---------------------------------------------------------------- 配置合并
def test_local_copy_used_before_sync(settings):
    entry = _entry(settings)
    assert entry["name"] == "SenseNova 6.8 Flash Lite"
    assert "服务端" in entry["desc"]


def test_server_copy_overrides_name_and_desc(settings):
    settings.builtin_models = [
        {"model_id": BUILTIN_ID, "name": "商汤日日新 6.8", "desc": "国内直连 · 高性价比"}
    ]

    entry = _entry(settings)
    assert entry["name"] == "商汤日日新 6.8"
    assert entry["desc"] == "国内直连 · 高性价比"


def test_server_name_does_not_touch_other_models(settings):
    settings.builtin_models = [{"model_id": BUILTIN_ID, "name": "改名了", "desc": ""}]

    assert _entry(settings, "gpt-5")["name"] == "GPT-5"


def test_blank_server_copy_falls_back_to_local(settings):
    """服务端没填备注（老库）时沿用本地默认文案，别显示成空白。"""
    settings.builtin_models = [{"model_id": BUILTIN_ID, "name": "", "desc": ""}]

    entry = _entry(settings)
    assert entry["name"] == "SenseNova 6.8 Flash Lite"
    assert "服务端" in entry["desc"]


def test_server_added_model_shows_up_in_menu(settings):
    """看板里新加的模型：本地清单没有也要出现在下拉框里，并走服务端代理。"""
    settings.builtin_models = [
        {"model_id": "gpt-4o", "name": "GPT-4o", "desc": "服务端新加的多模态模型"}
    ]

    entry = _entry(settings, "gpt-4o")
    assert entry["name"] == "GPT-4o"
    assert entry["desc"] == "服务端新加的多模态模型"
    assert entry["builtin"] is True and entry["custom"] is False
    assert settings.is_builtin_model("gpt-4o") is True
    assert settings.is_custom_model("gpt-4o") is False


def test_server_added_model_falls_back_to_id_and_hint(settings):
    settings.builtin_models = [{"model_id": "gpt-4o", "name": "", "desc": ""}]

    entry = _entry(settings, "gpt-4o")
    assert entry["name"] == "gpt-4o", "没填显示名就用模型 id"
    assert entry["desc"], "没填备注时给一句提示，别显示成空白"


def test_server_added_model_sits_beside_local_builtin(settings):
    """新加的模型要排在内置模型区里（服务端代理那一条后面），不掉到文生图下面。"""
    settings.builtin_models = [{"model_id": "gpt-4o", "name": "GPT-4o", "desc": ""}]

    ids = [item["id"] for item in settings.all_models()]
    assert ids.index("gpt-4o") == ids.index(BUILTIN_ID) + 1


def test_server_model_matching_local_id_is_not_duplicated(settings):
    """服务端下发本地已有的 id：只覆盖文案，不会再追加一条。"""
    settings.builtin_models = [{"model_id": "gpt-5", "name": "GPT-5 改名版", "desc": "覆盖"}]

    entries = [item for item in settings.all_models() if item["id"] == "gpt-5"]
    assert len(entries) == 1
    assert entries[0]["name"] == "GPT-5 改名版"
    assert entries[0].get("server") is None, "本地已有的条目不算服务端新增"


def test_broken_cache_value_is_ignored(settings):
    settings.setValue("llm/builtinModels", "{不是 JSON")
    assert settings.builtin_models == []
    assert _entry(settings)["name"] == "SenseNova 6.8 Flash Lite"


def test_server_kind_marks_local_entry_as_image(settings):
    """服务端把某条标成文生图，桌面端就按文生图处理（选中后把输入当提示词）。"""
    settings.builtin_models = [
        {"model_id": BUILTIN_ID, "name": "", "desc": "", "kind": "image"}
    ]

    assert _entry(settings)["kind"] == "image"
    assert settings.is_image_model(BUILTIN_ID) is True


def test_server_kind_never_downgrades_local_image_model(settings):
    """回归：旧版服务端还不认识 kind 字段（一律当 chat 回），

    不能因此把本地内置的生图模型改成对话模型 —— 那样选中它就变成聊天了，
    出图能力直接消失。
    """
    settings.builtin_models = [
        {"model_id": IMAGE_ID, "name": "U1.5", "desc": "", "kind": "chat"}
    ]

    assert _entry(settings, IMAGE_ID)["kind"] == "image"
    assert settings.is_image_model(IMAGE_ID) is True


def test_server_added_image_model_is_recognised(settings):
    settings.builtin_models = [
        {"model_id": "doubao-seedream", "name": "即梦", "desc": "文生图", "kind": "image"}
    ]

    entry = _entry(settings, "doubao-seedream")
    assert entry["kind"] == "image" and entry["builtin"] is True
    assert settings.is_image_model("doubao-seedream") is True
    assert settings.is_image_model("gpt-5") is False


def test_logout_clears_server_copy(settings):
    settings.set_account("token", {"id": 1, "email": "a@qq.com", "nickname": "甲"})
    settings.builtin_models = [{"model_id": BUILTIN_ID, "name": "服务端名字", "desc": ""}]
    assert settings.builtin_models

    settings.clear_account()

    assert settings.builtin_models == []
    assert _entry(settings)["name"] == "SenseNova 6.8 Flash Lite"


# ---------------------------------------------------------------- 文生图
IMAGE_ID = "sensenova-u1.5-lite"


def _logged_in(settings, key: str = "pk-user", expires: str = "") -> None:
    settings.set_account("token", {"id": 1, "email": "a@qq.com", "nickname": "甲"})
    settings.llm_user_key = key
    settings.llm_key_expires = expires


def _image_models(*extra: dict) -> list[dict]:
    models = [{"model_id": IMAGE_ID, "name": "U1.5", "desc": "文生图", "kind": "image"}]
    models.extend(extra)
    return models


def test_image_config_proxies_through_server(settings):
    """服务端配了生图模型：Base URL 指向服务端、Key 用代理密钥，供应商 Key 留在服务端。"""
    _logged_in(settings)
    settings.builtin_models = _image_models()

    config = settings.image_config(IMAGE_ID)

    assert config["proxy"] is True
    assert config["base_url"] == settings.llm_proxy_url
    assert config["api_key"] == "pk-user"
    assert config["model_id"] == IMAGE_ID


def test_image_config_uses_the_selected_server_model(settings):
    """服务端新加的另一个生图模型：选中它就按它出图。"""
    _logged_in(settings)
    settings.builtin_models = _image_models(
        {"model_id": "doubao-seedream", "name": "即梦", "desc": "", "kind": "image"}
    )

    assert settings.image_config("doubao-seedream")["model_id"] == "doubao-seedream"


def test_image_config_uses_server_model_for_a_chat_model(settings):
    """选的是对话模型、让智能体顺手画一张图：照样走服务端那个生图模型。"""
    _logged_in(settings)
    settings.builtin_models = _image_models()

    config = settings.image_config(BUILTIN_ID)

    assert config["proxy"] is True
    assert config["model_id"] == IMAGE_ID
    assert config["base_url"] == settings.llm_proxy_url


def test_image_config_uses_server_model_when_nothing_selected(settings):
    _logged_in(settings)
    settings.builtin_models = _image_models(
        {"model_id": "doubao-seedream", "name": "即梦", "desc": "", "kind": "image"}
    )

    assert settings.image_config()["model_id"] == IMAGE_ID, "默认用服务端配的第一个生图模型"


def test_image_config_needs_a_server_image_model(settings):
    """服务端只配了对话模型：回落到本地那套（内置的那把 SenseNova Key）。"""
    _logged_in(settings)
    settings.builtin_models = [
        {"model_id": BUILTIN_ID, "name": "", "desc": "", "kind": "chat"}
    ]

    config = settings.image_config(IMAGE_ID)

    assert "proxy" not in config
    assert config["base_url"] == settings.image_base_url
    assert config["api_key"] == settings.image_api_key
    assert config["model_id"] == settings.image_model


@pytest.mark.parametrize("key,expires", [("", ""), ("pk-user", "2020-01-01T00:00:00+08:00")])
def test_image_config_falls_back_without_usable_key(settings, key, expires):
    """没登录 / 密钥过期：别拿着空 Key 去撞服务端，直接用本地配置出图。"""
    _logged_in(settings, key=key, expires=expires)
    settings.builtin_models = _image_models()

    assert "proxy" not in settings.image_config(IMAGE_ID)


def test_image_config_local_fallback_keeps_local_model(settings):
    """没走代理时不能把服务端的模型名发给本地那把 Key 的服务商。"""
    settings.builtin_models = _image_models(
        {"model_id": "doubao-seedream", "name": "即梦", "desc": "", "kind": "image"}
    )

    config = settings.image_config("doubao-seedream")

    assert config["model_id"] == settings.image_model == IMAGE_ID


# ---------------------------------------------------------------- 同步器
def test_syncer_clears_when_not_logged_in(settings):
    received: list[dict] = []
    from paper_agent.core.signals import signals

    signals.llm_models_updated.connect(received.append)
    try:
        LlmModelsSyncer(settings).sync()
    finally:
        signals.llm_models_updated.disconnect(received.append)

    assert received == [{"models": []}]


# ---------------------------------------------------------------- 界面接线
def _window(qapp, tmp_path, settings):
    from paper_agent.core.session_store import SessionStore
    from paper_agent.ui.main_window import MainWindow

    return MainWindow(settings, SessionStore(tmp_path / "sessions"))


def test_window_caches_server_copy_and_refreshes_menu(qapp, tmp_path, settings):
    window = _window(qapp, tmp_path, settings)
    window.config.model = BUILTIN_ID
    window.composer.model_button.set_current(BUILTIN_ID)

    window._on_llm_models_updated(
        {"models": [{"model_id": BUILTIN_ID, "name": "商汤日日新 6.8", "desc": "国内直连"}]}
    )

    assert settings.builtin_models[0]["name"] == "商汤日日新 6.8"
    assert "商汤日日新 6.8" in window.composer.model_button.toolTip()
    assert window.composer.model_button.current_model == BUILTIN_ID, "选中项不能跟着变"


def test_window_ignores_sync_error(qapp, tmp_path, settings):
    window = _window(qapp, tmp_path, settings)

    window._on_llm_models_updated({"error": "无法连接账号服务"})

    assert settings.builtin_models == []
    assert "SenseNova" in window.composer.model_button.toolTip()


def test_server_added_model_is_usable_through_proxy(qapp, tmp_path, settings, monkeypatch):
    """看板新加的模型要能直接选中并用：请求走服务端代理，不需要任何本地配置。"""
    monkeypatch.setattr(
        type(settings), "llm_user_key", property(lambda self: "pk-user")
    )
    window = _window(qapp, tmp_path, settings)
    window._on_llm_models_updated(
        {"models": [{"model_id": "gpt-4o", "name": "GPT-4o", "desc": "服务端新加的"}]}
    )

    # 下拉框里能选到它，选中后请求走服务端代理
    assert "gpt-4o" in window.composer.model_button._items                # noqa: SLF001
    window.composer.model_button.set_current("gpt-4o")
    endpoint = window._current_endpoint()

    assert endpoint is not None, "应走服务端代理，而不是回落离线示例引擎"
    assert endpoint["proxy"] is True
    assert endpoint["model_id"] == "gpt-4o"
    assert endpoint["base_url"] == settings.llm_proxy_url
    assert window._endpoint_problem(endpoint) == ""
    assert settings.model == "gpt-4o"


def test_removed_server_model_falls_back_to_local_builtin(qapp, tmp_path, settings):
    """看板把模型删了：下拉框里跟着消失，选中项自动回到可用的内置模型。"""
    window = _window(qapp, tmp_path, settings)
    window._on_llm_models_updated(
        {"models": [{"model_id": "gpt-4o", "name": "GPT-4o", "desc": ""}]}
    )
    window.config.model = "gpt-4o"
    window.composer.model_button.set_current("gpt-4o")
    assert window.composer.model_button.current_model == "gpt-4o"

    window._on_llm_models_updated({"models": []})

    assert window.config.model == BUILTIN_ID
    assert window.composer.model_button.current_model == BUILTIN_ID
    assert "gpt-4o" not in window.composer.model_button._items            # noqa: SLF001
