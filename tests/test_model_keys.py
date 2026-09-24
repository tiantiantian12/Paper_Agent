"""多 Key 的模型配置与设置界面：存储、校验、打码展示与收集。"""

from __future__ import annotations

import pytest

from paper_agent.core import config as config_module
from paper_agent.core.config import AppConfig
from paper_agent.core.models import CustomModel
from paper_agent.services.key_pool import reset_shared_pools


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """把配置文件指到临时目录，避免动到用户真实设置。"""
    monkeypatch.setattr(config_module, "SETTINGS_FILE", tmp_path / "settings.ini")
    config = AppConfig()
    yield config
    config.sync()


# ---------------------------------------------------------------- 数据模型
def test_key_pool_puts_primary_first_and_dedupes():
    model = CustomModel(api_key="sk-main", api_keys=["sk-b", "sk-main", "  ", "sk-c"])
    assert model.key_pool == ["sk-main", "sk-b", "sk-c"]


def test_model_valid_with_only_extra_keys():
    model = CustomModel(name="x", model_id="m", base_url="https://a.com/v1", api_keys=["sk-x"])
    assert model.key_pool == ["sk-x"]
    assert model.is_valid()[0] is True

    empty = CustomModel(name="x", model_id="m", base_url="https://a.com/v1")
    assert empty.is_valid() == (False, "请填写 API Key")


def test_serialisation_round_trip():
    model = CustomModel(name="多Key模型", model_id="m", base_url="https://a.com/v1",
                        api_key="sk-1", api_keys=["sk-2", "sk-3"])
    restored = CustomModel.from_dict(model.to_dict())
    assert restored.key_pool == ["sk-1", "sk-2", "sk-3"]


def test_reasoning_defaults_to_auto_and_survives_round_trip():
    model = CustomModel(name="x", model_id="m", base_url="https://a.com/v1", api_key="sk")
    assert model.reasoning == "auto"
    model.reasoning = "enable_thinking"
    assert CustomModel.from_dict(model.to_dict()).reasoning == "enable_thinking"


def test_legacy_model_without_reasoning_field(settings):
    settings.custom_models = [
        CustomModel.from_dict(
            {"name": "旧", "model_id": "m", "base_url": "https://a.com/v1", "api_key": "sk"}
        )
    ]
    assert settings.custom_models[0].reasoning == "auto"
    assert settings.all_models()[-1]["reasoning"] == "auto"


def test_dialog_collects_and_restores_reasoning(qapp, settings):
    """设置弹窗要能选、能存「推理强度参数」，并把影响写在提示里。"""
    from paper_agent.ui.dialogs.model_settings_dialog import ModelSettingsDialog

    settings.custom_models = [
        CustomModel(
            name="o3", model_id="o3-mini", base_url="https://a.com/v1",
            api_key="sk", reasoning="thinking_budget",
        )
    ]
    dialog = ModelSettingsDialog(settings)
    dialog.model_list.setCurrentRow(0)
    assert dialog.reasoning_combo.currentData() == "thinking_budget"
    assert dialog._collect_form().reasoning == "thinking_budget"
    assert "budget_tokens" in dialog.reasoning_hint.text()

    dialog.reasoning_combo.setCurrentIndex(dialog.reasoning_combo.findData("off"))
    assert dialog._collect_form().reasoning == "off"
    assert "不发送" in dialog.reasoning_hint.text(), dialog.reasoning_hint.text()

    # 选 reasoning_effort 时提示里要能看出当前强度会怎么发
    dialog.reasoning_combo.setCurrentIndex(
        dialog.reasoning_combo.findData("reasoning_effort")
    )
    assert "reasoning_effort=" in dialog.reasoning_hint.text()
    dialog.deleteLater()


def test_from_dict_tolerates_legacy_and_broken_values():
    legacy = CustomModel.from_dict({"name": "旧", "api_key": "sk-old"})
    assert legacy.key_pool == ["sk-old"]

    broken = CustomModel.from_dict({"name": "坏", "api_key": "sk-a", "api_keys": "sk-b"})
    assert broken.key_pool == ["sk-a", "sk-b"], "非列表值要归一化，否则会被当成字符遍历"


# ---------------------------------------------------------------- 配置持久化
def test_config_keeps_multiple_keys(settings):
    settings.custom_models = [
        CustomModel(name="A", model_id="m", base_url="https://a.com/v1",
                    api_key="sk-1", api_keys=["sk-2", "sk-3"])
    ]
    settings.sync()

    reloaded = AppConfig().custom_model("m")
    assert reloaded is not None
    assert reloaded.key_pool == ["sk-1", "sk-2", "sk-3"]


def test_all_models_exposes_pool(settings):
    settings.custom_models = [
        CustomModel(name="A", model_id="m", base_url="https://a.com/v1",
                    api_key="sk-1", api_keys=["sk-2"])
    ]
    entry = settings.find_model(settings.custom_models[0].id)
    assert entry is not None
    assert entry["api_keys"] == ["sk-1", "sk-2"]


# ---------------------------------------------------------------- 设置界面
@pytest.fixture
def dialog(qapp, settings):
    from paper_agent.ui.dialogs.model_settings_dialog import ModelSettingsDialog

    settings.custom_models = [
        CustomModel(name="多Key", model_id="m1", base_url="https://a.com/v1",
                    api_key="sk-abcdefghijklmn", api_keys=["sk-opqrstuvwxyz"])
    ]
    return ModelSettingsDialog(settings)


def test_dialog_masks_keys_by_default(dialog):
    text = dialog.keys_edit.toPlainText()
    assert "abcdefghijklmn" not in text and "opqrstuvwxyz" not in text
    assert text.count("\n") == 1, "两个 Key 应各占一行"
    assert dialog.keys_edit.isReadOnly(), "打码状态不允许编辑"
    assert "2 个 API Key" in dialog.key_hint.text()


def test_dialog_reveal_and_collect(dialog):
    dialog.reveal_button.setChecked(True)
    assert not dialog.keys_edit.isReadOnly()
    assert dialog.keys_edit.toPlainText() == "sk-abcdefghijklmn\nsk-opqrstuvwxyz"

    dialog.keys_edit.setPlainText("sk-1, sk-2\nsk-3\nsk-1")     # 模拟粘贴多个 Key
    model = dialog._collect_form()
    assert model.api_key == "sk-1"
    assert model.api_keys == ["sk-2", "sk-3"]
    assert "3 个 API Key" in dialog.key_hint.text() and "轮换" in dialog.key_hint.text()


def test_dialog_saves_keys(dialog, settings):
    dialog.reveal_button.setChecked(True)
    dialog.keys_edit.setPlainText("sk-new1\nsk-new2")
    dialog._on_save()

    saved = AppConfig().custom_model("m1")
    assert saved is not None and saved.key_pool == ["sk-new1", "sk-new2"]


def test_dialog_switching_model_resets_reveal(qapp, settings):
    from paper_agent.ui.dialogs.model_settings_dialog import ModelSettingsDialog

    settings.custom_models = [
        CustomModel(name="A", model_id="m1", base_url="https://a.com/v1", api_key="sk-aaaa"),
        CustomModel(name="B", model_id="m2", base_url="https://a.com/v1", api_key="sk-bbbb"),
    ]
    dialog = ModelSettingsDialog(settings)
    dialog.reveal_button.setChecked(True)
    dialog.model_list.setCurrentRow(1)
    assert dialog.keys_edit.toPlainText() == "sk-bbbb", "切换模型应填入该模型的 Key"


def test_dialog_clears_form_when_no_models(qapp, settings):
    from paper_agent.ui.dialogs.model_settings_dialog import ModelSettingsDialog

    settings.custom_models = []
    dialog = ModelSettingsDialog(settings)
    assert dialog.keys_edit.toPlainText() == ""
    assert dialog.key_hint.text() == "未填写 API Key"
    assert not dialog.keys_edit.isEnabled()


def test_test_thread_uses_whole_pool(qapp, monkeypatch):
    """连接测试也要用整套 Key（否则第一把被限流就误判失败）。"""
    from paper_agent.ui.dialogs import model_settings_dialog as dialog_module
    from paper_agent.ui.dialogs.model_settings_dialog import _ConnectionTestThread

    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def test_connection(self):
            return True, "ok"

    monkeypatch.setattr(dialog_module, "OpenAICompatClient", FakeClient)
    model = CustomModel(
        name="A", model_id="m1", base_url="https://a.com/v1",
        api_key="sk-1", api_keys=["sk-2", "sk-3"],
    )
    thread = _ConnectionTestThread(model)
    thread.run()

    assert captured["api_keys"] == ["sk-1", "sk-2", "sk-3"], captured
    assert captured["base_url"] == "https://a.com/v1"


def teardown_module(module):
    reset_shared_pools()
