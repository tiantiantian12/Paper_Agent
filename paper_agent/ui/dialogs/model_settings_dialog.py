"""自定义模型管理：新增 / 编辑 / 删除 OpenAI 兼容模型，并支持连接测试。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from paper_agent.core.config import AppConfig
from paper_agent.core.models import CustomModel
from paper_agent.services.key_pool import mask_key, parse_api_keys
from paper_agent.services.reasoning import (
    MODE_LABELS,
    REASONING_MODES,
    reasoning_payload,
    resolve_mode,
)
from paper_agent.services.openai_client import OpenAICompatClient
from paper_agent.ui.widgets.icon_button import IconToolButton


class _ConnectionTestThread(QThread):
    """在后台测试模型连通性，避免界面卡顿。"""

    result_ready = Signal(bool, str)

    def __init__(self, model: CustomModel, effort: str = "medium", parent=None) -> None:
        super().__init__(parent)
        self._model = model
        self._effort = effort

    def run(self) -> None:  # noqa: D102
        client = OpenAICompatClient(
            base_url=self._model.base_url,
            model_id=self._model.model_id,
            api_keys=self._model.key_pool,
            reasoning=self._model.reasoning,
            effort=self._effort,
            model_name=self._model.name,
        )
        ok, message = client.test_connection()
        self.result_ready.emit(ok, message)


class ModelSettingsDialog(QDialog):
    """自定义模型管理对话框。"""

    models_changed = Signal()

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("modelSettingsDialog")
        self.setWindowTitle("自定义模型")
        self.resize(760, 480)
        self.setMinimumSize(680, 440)

        self.config = config
        self._models: list[CustomModel] = list(config.custom_models)
        self._current: CustomModel | None = None
        self._dirty = False
        self._keys: list[str] = []          # 当前模型的全部 Key（第 1 个为主 Key）
        self._keys_revealed = False
        self._test_thread: _ConnectionTestThread | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(12)

        title = QLabel("自定义模型（OpenAI 兼容接口）")
        title.setObjectName("welcomeTitle")
        root.addWidget(title)

        hint = QLabel(
            "只需填写显示名称、模型 ID、Base URL 与 API Key，即可接入任意 OpenAI 兼容服务；"
            "API Key 支持填多个（每行一个），请求会在它们之间轮换，降低触发限流的概率。"
        )
        hint.setObjectName("welcomeSubtitle")
        hint.setWordWrap(True)
        root.addWidget(hint)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_list_panel())
        splitter.addWidget(self._build_form_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([230, 500])
        root.addWidget(splitter, 1)

        root.addLayout(self._build_footer())

        self._reload_list()
        if self._models:
            self.model_list.setCurrentRow(0)
        else:
            self._set_form_enabled(False)

    # ------------------------------------------------------------------ 左：列表
    def _build_list_panel(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.setSpacing(8)

        header = QHBoxLayout()
        label = QLabel("模型列表")
        label.setObjectName("sectionLabel")
        header.addWidget(label)
        header.addStretch(1)
        layout.addLayout(header)

        self.model_list = QListWidget(panel)
        self.model_list.setObjectName("modelList")
        self.model_list.setFrameShape(QListWidget.Shape.NoFrame)
        self.model_list.setSpacing(2)
        self.model_list.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self.model_list, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.add_button = QPushButton("新增")
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_button.clicked.connect(self._on_add)
        row.addWidget(self.add_button)

        self.remove_button = QPushButton("删除")
        self.remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_button.clicked.connect(self._on_remove)
        row.addWidget(self.remove_button)
        layout.addLayout(row)
        return panel

    # ------------------------------------------------------------------ 右：表单
    def _build_form_panel(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 0, 0, 0)
        layout.setSpacing(8)

        form_card = QWidget(panel)
        form_card.setObjectName("formCard")
        form = QFormLayout(form_card)
        form.setContentsMargins(16, 14, 16, 14)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.name_edit = QLineEdit(form_card)
        self.name_edit.setPlaceholderText("例如：我的 DeepSeek")
        form.addRow("显示名称", self.name_edit)

        self.model_id_edit = QLineEdit(form_card)
        self.model_id_edit.setPlaceholderText("例如：deepseek-chat")
        form.addRow("模型 ID", self.model_id_edit)

        self.base_url_edit = QLineEdit(form_card)
        self.base_url_edit.setPlaceholderText("例如：https://api.deepseek.com/v1")
        form.addRow("Base URL", self.base_url_edit)

        key_row = QHBoxLayout()
        key_row.setSpacing(4)
        self.keys_edit = QPlainTextEdit(form_card)
        self.keys_edit.setObjectName("keysEdit")
        self.keys_edit.setPlaceholderText("sk-...\n每行一个 Key；填多个会组成请求池轮换使用")
        self.keys_edit.setFixedHeight(76)
        self.keys_edit.setTabChangesFocus(True)
        self.keys_edit.textChanged.connect(self._on_keys_edited)
        key_row.addWidget(self.keys_edit, 1)

        self.reveal_button = IconToolButton(
            "search", "显示 / 隐藏密钥", object_name="composerButton", parent=form_card
        )
        self.reveal_button.setCheckable(True)
        self.reveal_button.toggled.connect(self._on_toggle_reveal)
        key_row.addWidget(self.reveal_button, 0, Qt.AlignmentFlag.AlignTop)
        form.addRow("API Key", key_row)

        self.key_hint = QLabel("")
        self.key_hint.setObjectName("composerHint")
        form.addRow("", self.key_hint)

        self.desc_edit = QLineEdit(form_card)
        self.desc_edit.setPlaceholderText("可选，例如：用于文献综述的备用模型")
        form.addRow("备注", self.desc_edit)

        self.reasoning_combo = QComboBox(form_card)
        self.reasoning_combo.setObjectName("reasoningCombo")
        for mode in REASONING_MODES:
            self.reasoning_combo.addItem(MODE_LABELS[mode], mode)
        self.reasoning_combo.currentIndexChanged.connect(self._update_reasoning_hint)
        form.addRow("推理强度参数", self.reasoning_combo)

        self.reasoning_hint = QLabel("")
        self.reasoning_hint.setObjectName("composerHint")
        self.reasoning_hint.setWordWrap(True)
        form.addRow("", self.reasoning_hint)

        self.model_id_edit.textChanged.connect(self._update_reasoning_hint)
        self.name_edit.textChanged.connect(self._update_reasoning_hint)

        layout.addWidget(form_card)

        self.status_label = QLabel("")
        self.status_label.setObjectName("composerHint")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addStretch(1)
        return panel

    # ------------------------------------------------------------------ 底部
    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self.test_button = QPushButton("测试连接")
        self.test_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.test_button.clicked.connect(self._on_test)
        row.addWidget(self.test_button)

        self.result_label = QLabel("")
        self.result_label.setObjectName("composerHint")
        row.addWidget(self.result_label, 1)

        self.save_button = QPushButton("保存")
        self.save_button.setObjectName("primaryButton")
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.clicked.connect(self._on_save)
        row.addWidget(self.save_button)

        close_button = QPushButton("关闭")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.clicked.connect(self._on_close)
        row.addWidget(close_button)
        return row

    # ------------------------------------------------------------------ 数据
    def _reload_list(self) -> None:
        self.model_list.blockSignals(True)
        self.model_list.clear()
        for model in self._models:
            pool = len(model.key_pool)
            suffix = f" · {pool} Key 轮换" if pool > 1 else ""
            item = QListWidgetItem(f"{model.name}\n{model.model_id}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, model.id)
            self.model_list.addItem(item)
        self.model_list.blockSignals(False)

    def _on_selection_changed(self, current: QListWidgetItem | None, _previous) -> None:
        if current is None:
            self._current = None
            self._set_form_enabled(False)
            return
        model_id = current.data(Qt.ItemDataRole.UserRole)
        model = next((m for m in self._models if m.id == model_id), None)
        self._current = model
        self._set_form_enabled(model is not None)
        if model is not None:
            self._fill_form(model)
        self.result_label.setText("")

    def _fill_form(self, model: CustomModel) -> None:
        self.name_edit.setText(model.name)
        self.model_id_edit.setText(model.model_id)
        self.base_url_edit.setText(model.base_url)
        self.desc_edit.setText(model.desc)
        index = self.reasoning_combo.findData(model.reasoning or "auto")
        self.reasoning_combo.setCurrentIndex(index if index >= 0 else 0)
        self._update_reasoning_hint()
        self._keys = model.key_pool
        self._render_keys()
        self._update_key_hint()
        self._dirty = False

    def _update_reasoning_hint(self) -> None:
        """说明当前选择会往请求里加什么参数 —— 避免「低中高其实没生效」。"""
        mode = self.reasoning_combo.currentData() or "auto"
        effort = self.config.reasoning_effort
        model_id = self.model_id_edit.text().strip()
        name = self.name_edit.text().strip()
        payload = reasoning_payload(mode, effort, model_id, name)
        if not payload:
            self.reasoning_hint.setText(
                "不发送推理参数：底部「低 / 中 / 高」只影响 temperature 与工具轮次上限"
            )
            return
        preview = "、".join(f"{key}={value}" for key, value in payload.items())
        source = "自动识别" if mode == "auto" else "手动指定"
        dropped = resolve_mode(mode, model_id, name)
        self.reasoning_hint.setText(
            f"{source}为 {dropped} → 请求会带上 {preview}（当前强度：{effort}）；"
            "若服务端不认这个参数，会自动撤掉并重试"
        )

    # ------------------------------------------------------------------ 密钥
    def _render_keys(self) -> None:
        """没点「显示」时只展示打码结果，并禁止编辑。"""
        if self._keys_revealed:
            text = "\n".join(self._keys)
        else:
            text = "\n".join(mask_key(key) for key in self._keys)
        self.keys_edit.blockSignals(True)
        self.keys_edit.setPlainText(text)
        self.keys_edit.blockSignals(False)
        self.keys_edit.setReadOnly(not self._keys_revealed)

    def _on_keys_edited(self) -> None:
        if not self._keys_revealed:
            return
        self._keys = parse_api_keys(self.keys_edit.toPlainText())
        self._update_key_hint()

    def _update_key_hint(self) -> None:
        count = len(self._keys)
        if not count:
            self.key_hint.setText("未填写 API Key")
        elif count == 1:
            self.key_hint.setText("1 个 API Key")
        else:
            self.key_hint.setText(f"{count} 个 API Key · 请求会轮换使用，降低 429 概率")

    def _set_form_enabled(self, enabled: bool) -> None:
        for widget in (
            self.name_edit,
            self.model_id_edit,
            self.base_url_edit,
            self.keys_edit,
            self.desc_edit,
            self.reasoning_combo,
            self.reveal_button,
            self.test_button,
        ):
            widget.setEnabled(enabled)
        if not enabled:
            self.name_edit.clear()
            self.model_id_edit.clear()
            self.base_url_edit.clear()
            self.desc_edit.clear()
            self._keys = []
            self._render_keys()
            self._update_key_hint()

    def _collect_form(self) -> CustomModel:
        current = self._current or CustomModel()
        return CustomModel(
            id=current.id,
            name=self.name_edit.text().strip(),
            model_id=self.model_id_edit.text().strip(),
            base_url=self.base_url_edit.text().strip(),
            api_key=self._keys[0] if self._keys else "",
            api_keys=list(self._keys[1:]),
            desc=self.desc_edit.text().strip(),
            reasoning=self.reasoning_combo.currentData() or "auto",
        )

    # ------------------------------------------------------------------ 操作
    def _on_add(self) -> None:
        model = CustomModel(name="自定义模型")
        self._models.append(model)
        self._reload_list()
        self.model_list.setCurrentRow(self.model_list.count() - 1)
        self.name_edit.setFocus()
        self.name_edit.selectAll()
        self.result_label.setText("请填写右侧信息后保存")

    def _on_remove(self) -> None:
        if self._current is None:
            return
        answer = QMessageBox.question(
            self,
            "删除模型",
            f"确定删除“{self._current.name or self._current.model_id}”吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._models = [m for m in self._models if m.id != self._current.id]
        self._current = None
        self._reload_list()
        if self._models:
            self.model_list.setCurrentRow(0)
        else:
            self._set_form_enabled(False)

    def _on_toggle_reveal(self, checked: bool) -> None:
        self._keys_revealed = checked
        self._render_keys()
        if checked:
            self.keys_edit.setFocus()

    def _on_test(self) -> None:
        model = self._collect_form()
        valid, message = model.is_valid()
        if not valid:
            self.result_label.setText(message)
            return

        self.test_button.setEnabled(False)
        self.result_label.setText("正在测试连接…")

        # 连测试也带上推理参数：服务端认不认这个字段，测一次就知道
        self._test_thread = _ConnectionTestThread(
            model, self.config.reasoning_effort, self
        )
        self._test_thread.result_ready.connect(self._on_test_result)
        self._test_thread.finished.connect(self._test_thread.deleteLater)
        self._test_thread.start()

    def _on_test_result(self, ok: bool, message: str) -> None:
        self.test_button.setEnabled(True)
        self.result_label.setText(message)

    def _on_save(self) -> None:
        # 表单内容先回写到当前模型
        if self._current is not None:
            updated = self._collect_form()
            valid, message = updated.is_valid()
            if not valid:
                self.result_label.setText(message)
                return
            self._models = [
                updated if m.id == updated.id else m for m in self._models
            ]

        # 校验全部条目（未填写完整的给出提示，允许保留为空壳的排除掉）
        keep: list[CustomModel] = []
        invalid_names: list[str] = []
        for model in self._models:
            valid, _ = model.is_valid()
            if valid:
                keep.append(model)
            elif model.name.strip() or model.model_id.strip() or model.base_url.strip():
                invalid_names.append(model.name or model.model_id or "未命名")

        if invalid_names:
            self.result_label.setText(
                "以下模型信息不完整，未保存：" + "、".join(invalid_names)
            )

        self._models = keep
        self.config.custom_models = keep
        self.config.sync()
        self._reload_list()
        if self._models:
            self.model_list.setCurrentRow(0)
        else:
            self._set_form_enabled(False)
        if not invalid_names:
            self.result_label.setText("已保存")
        self.models_changed.emit()

    def _on_close(self) -> None:
        self.accept()

    # ------------------------------------------------------------------ 事件
    def closeEvent(self, event):  # noqa: N802
        if self._test_thread is not None and self._test_thread.isRunning():
            self._test_thread.quit()
            self._test_thread.wait(1000)
        super().closeEvent(event)
