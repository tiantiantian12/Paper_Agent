"""底部输入区：工具条、附件、模型选择与发送。"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from paper_agent.core.config import AppConfig
from paper_agent.core.constants import FILE_OPEN_DIALOG_FILTER, MAX_ATTACHMENT_SIZE_MB
from paper_agent.core.models import Attachment
from paper_agent.core.signals import signals
from paper_agent.ui.composer.attachment_bar import AttachmentBar
from paper_agent.ui.composer.text_editor import ComposerEditor
from paper_agent.ui.widgets.icon_button import IconToolButton
from paper_agent.ui.widgets.model_combo import (
    EffortMenuButton,
    MenuButton,
    ModeMenuButton,
    ModelMenuButton,
)
from paper_agent.ui.widgets.video_options import VideoOptionsButton
from paper_agent.utils.files import file_kind, file_size, import_file, save_pasted_image
from paper_agent.utils.text import count_words


class Composer(QWidget):
    """用户输入区。"""

    submit_requested = Signal(str, list)  # 文本, list[Attachment]
    stop_requested = Signal()
    model_changed = Signal(str)
    effort_changed = Signal(str)
    mode_changed = Signal(str)          # 工作模式：doc / code
    manage_models_requested = Signal()

    # 窄于这个宽度就切紧凑模式：模型名 / 推理强度只留很短一截文字。
    # 输入区的最小宽度会直接变成聊天栏的地板（拖动结构面板时压不下去），
    # 所以这里必须让它能随窗口收窄。
    COMPACT_WIDTH = 620
    COMPACT_MODEL_TEXT_WIDTH = 96
    COMPACT_EFFORT_TEXT_WIDTH = 56

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("composer")
        self._config = config
        self._streaming = False
        self._compact = False
        self._hint_full = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 6, 24, 16)
        root.setSpacing(6)

        # ---------------------------------------------------------- 输入卡片
        self.card = QWidget(self)
        self.card.setObjectName("composerCard")
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(10, 8, 10, 8)
        card_layout.setSpacing(6)

        self.attachment_bar = AttachmentBar(self.card)
        self.attachment_bar.attachment_removed.connect(self._on_attachment_removed)
        card_layout.addWidget(self.attachment_bar)

        self.editor = ComposerEditor(self.card)
        self.editor.set_send_shortcut(config.send_shortcut)
        self.editor.submitted.connect(self._on_submit)
        self.editor.image_pasted.connect(self._on_image_pasted)
        self.editor.files_dropped.connect(self.add_files)
        self.editor.textChanged.connect(self._on_text_changed)
        card_layout.addWidget(self.editor)

        card_layout.addLayout(self._build_toolbar())
        root.addWidget(self.card)

        # ---------------------------------------------------------- 提示行
        hint_row = QHBoxLayout()
        hint_row.setContentsMargins(6, 0, 6, 0)
        self.hint_label = QLabel(self._hint_text())
        self.hint_label.setObjectName("composerHint")
        # 提示文案最长的一条有 480px 宽，QLabel 的最小宽度跟着文字走会让整个
        # 输入区被顶到 558px（→ 聊天栏再也压不下去）。放开压缩 + 主动省略。
        self.hint_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        hint_row.addWidget(self.hint_label)
        hint_row.addStretch(1)
        self.model_hint = QLabel("")
        self.model_hint.setObjectName("composerHint")
        hint_row.addWidget(self.model_hint)
        root.addLayout(hint_row)

        self._sync_send_state()
        self._on_text_changed()
        self._apply_mode_hint()          # 首屏提示语按当前模式来（文档/编程）
        self.editor.installEventFilter(self)
        self._hint_full = self._hint_text()
        self._elide_hint()

    # ------------------------------------------------------------------ 工具条
    def _build_toolbar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 2, 0, 0)
        row.setSpacing(2)

        self.attach_button = IconToolButton("paperclip", "添加附件", parent=self)
        self.attach_button.clicked.connect(self._on_pick_file)
        row.addWidget(self.attach_button)

        self.image_button = IconToolButton("image", "添加图片", parent=self)
        self.image_button.clicked.connect(self._on_pick_image)
        row.addWidget(self.image_button)

        self.mode_button = ModeMenuButton(self._config.mode, self)
        self.mode_button.mode_changed.connect(self._on_mode_changed)
        row.addWidget(self.mode_button)

        self.deep_button = IconToolButton(
            "sparkles", "深度写作：本轮把内容写足（正文 ≥3000 字，逐段展开）", parent=self
        )
        self.deep_button.setCheckable(True)
        row.addWidget(self.deep_button)

        self.effort_button = EffortMenuButton(self._config.reasoning_effort, self)
        self.effort_button.effort_changed.connect(self._on_effort_changed)
        row.addWidget(self.effort_button)

        # 视频参数：默认收起，选中「文生视频」模型时才显示（见 set_video_mode）
        self.video_button = VideoOptionsButton(parent=self)
        self.video_button.set_video_mode(False)
        row.addWidget(self.video_button)

        row.addStretch(1)

        self.count_label = QLabel("")
        self.count_label.setObjectName("composerHint")
        row.addWidget(self.count_label)

        self.model_button = ModelMenuButton(
            models=self._config.all_models(), current=self._config.model, parent=self
        )
        self.model_button.model_changed.connect(self._on_model_changed)
        self.model_button.manage_requested.connect(self.manage_models_requested)
        row.addWidget(self.model_button)

        self.send_button = IconToolButton(
            "send", "发送 (Enter)", color_token="accent_text", parent=self, object_name="sendButton"
        )
        self.send_button.setEnabled(False)
        self.send_button.clicked.connect(self._on_send_clicked)
        row.addWidget(self.send_button)
        return row

    # ------------------------------------------------------------------ 附件
    def add_files(self, paths: list[str]) -> None:
        for path in paths:
            if file_size(path) > MAX_ATTACHMENT_SIZE_MB * 1024 * 1024:
                signals.toast_requested.emit(f"文件过大（超过 {MAX_ATTACHMENT_SIZE_MB} MB）：{path}")
                continue
            stored = import_file(path)
            self.attachment_bar.add(
                Attachment(
                    name=Path(stored).name,
                    path=stored,
                    kind="image" if file_kind(stored) == "image" else "file",
                    size=file_size(stored),
                    created_at=time.time(),
                )
            )
        self._sync_send_state()

    def _on_image_pasted(self, image: QImage) -> None:
        path = save_pasted_image(image)
        if not path:
            signals.toast_requested.emit("图片读取失败")
            return
        self.attachment_bar.add(
            Attachment(
                name=Path(path).name,
                path=path,
                kind="image",
                size=file_size(path),
                created_at=time.time(),
            )
        )
        self._sync_send_state()

    def _on_pick_file(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "选择附件", "", FILE_OPEN_DIALOG_FILTER)
        if paths:
            self.add_files(paths)

    def _on_pick_image(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择图片", "", "图片 (*.png *.jpg *.jpeg *.gif *.bmp *.webp);;所有文件 (*.*)"
        )
        if paths:
            self.add_files(paths)

    def _on_attachment_removed(self, _attachment_id: str) -> None:
        self._sync_send_state()

    # ------------------------------------------------------------------ 发送
    def _on_send_clicked(self) -> None:
        if self._streaming:
            self.stop_requested.emit()
            return
        self._on_submit()

    def _on_submit(self) -> None:
        if self._streaming:
            return
        text = self.editor.toPlainText().strip()
        attachments = self.attachment_bar.items()
        if not text and not attachments:
            return
        self.submit_requested.emit(text, attachments)
        self.editor.clear()
        self.editor.setFixedHeight(26)
        self.attachment_bar.clear()
        self._sync_send_state()

    def _on_text_changed(self) -> None:
        text = self.editor.toPlainText()
        words = count_words(text)
        self.count_label.setText(f"{words} 字" if words else "")
        self._sync_send_state()

    # ------------------------------------------------------------------ 状态
    @property
    def is_streaming(self) -> bool:
        """输入区是否处于「正在生成」状态（发送键显示为停止）。"""
        return self._streaming

    def set_streaming(self, streaming: bool) -> None:
        self._streaming = streaming
        self.send_button.set_icon_name("stop" if streaming else "send")
        self.send_button.setProperty("state", "stop" if streaming else "send")
        self.send_button.setToolTip("停止生成" if streaming else "发送 (Enter)")
        self.send_button.set_color_token("accent_text")
        self.send_button.setEnabled(True if streaming else self._has_input())
        if streaming:
            self.editor.setPlaceholderText("正在生成中，可继续输入…")
        else:
            self._apply_mode_hint()

    def _sync_send_state(self) -> None:
        if self._streaming:
            return
        self.send_button.setEnabled(self._has_input())

    def _has_input(self) -> bool:
        return bool(self.editor.toPlainText().strip()) or self.attachment_bar.count() > 0

    def _hint_text(self) -> str:
        if self._config.send_shortcut == "ctrl+enter":
            return "Ctrl + Enter 发送 · Enter 换行 · 可粘贴图片或拖入文件"
        return "Enter 发送 · Shift + Enter 换行 · 可粘贴图片或拖入文件"

    # ------------------------------------------------------------------ 选项
    def _on_model_changed(self, model_id: str) -> None:
        self._config.model = model_id
        self.model_hint.setText("")
        self.sync_video_mode()        # 选中视频模型时露出视频参数
        self.model_changed.emit(model_id)

    def _on_effort_changed(self, effort: str) -> None:
        self._config.reasoning_effort = effort
        self.effort_changed.emit(effort)

    def _on_mode_changed(self, mode: str) -> None:
        """切换工作模式：文档（论文/PPT）或编程（配环境/写代码/跑代码）。"""
        # 配置里这份只是「上次用过什么」的兜底；真正生效的是当前会话的模式，
        # 由主窗口写回会话（切换会话时模式跟着会话走）
        self._config.mode = mode
        self._apply_mode_hint()
        self.mode_changed.emit(mode)

    @property
    def mode(self) -> str:
        return self.mode_button.current_mode

    def set_mode(self, mode: str) -> None:
        """切会话时同步模式控件（不发 mode_changed，避免和主窗口来回触发）。"""
        self.mode_button.set_current(mode)
        self._apply_mode_hint()

    def _apply_mode_hint(self) -> None:
        """按模式改输入区提示语，让用户一眼知道当前在哪个模式。"""
        if self._streaming:
            return
        if getattr(self, "_video_mode", False):
            # 选中视频模型：提示语改成出视频的写法（附图片就是图生视频）
            self.editor.setPlaceholderText(
                "描述想生成的画面（主体 / 场景 / 镜头运动）；带上图片就是图生视频…"
            )
            return
        if self.mode_button.current_mode == "code":
            self.editor.setPlaceholderText(
                "描述编程需求，例如：用 FastAPI 写一个待办事项接口并跑通测试…"
            )
        else:
            self.editor.setPlaceholderText(
                "描述你的论文需求，可粘贴图片或拖入参考文献…"
            )

    def refresh_models(self, current: str = "") -> None:
        """自定义模型变更后重建下拉菜单。"""
        self.model_button.reload(self._config.all_models(), current or self._config.model)
        self.set_video_mode(self._config.is_video_model(self.model_button.current_model))

    def set_video_mode(self, enabled: bool) -> None:
        """当前选中的是文生视频模型时，露出视频参数选择。"""
        self._video_mode = enabled
        self.video_button.set_video_mode(enabled)
        self._apply_mode_hint()

    def sync_video_mode(self) -> None:
        """按当前选中的模型刷新视频参数按钮的显隐。"""
        self.set_video_mode(self._config.is_video_model(self.model_button.current_model))

    def set_focus(self) -> None:
        self.editor.setFocus()

    def set_send_shortcut(self, mode: str) -> None:
        self._config.send_shortcut = mode
        self.editor.set_send_shortcut(mode)
        self._hint_full = self._hint_text()
        self.hint_label.setToolTip(self._hint_full)
        self._elide_hint()

    # ------------------------------------------------------------------ 宽度自适应
    def _elide_hint(self) -> None:
        """提示文案按可用宽度省略，避免它成为输入区（进而聊天栏）的最小宽度。"""
        if not self._hint_full:
            return
        available = max(
            0, self.width() - 48 - self.model_hint.sizeHint().width() - 12
        )
        elided = self.hint_label.fontMetrics().elidedText(
            self._hint_full, Qt.TextElideMode.ElideRight, available
        )
        if elided != self.hint_label.text():
            self.hint_label.setText(elided)
        self.hint_label.setToolTip(self._hint_full)

    def _apply_compact(self, compact: bool) -> None:
        """窄栏下缩短模型名 / 推理强度 / 模式的显示文字，给聊天栏让出宽度。"""
        if compact == self._compact:
            return
        self._compact = compact
        self.mode_button.set_text_visible(not compact)   # 模式按钮窄栏下只留图标
        self.model_button.set_text_width(
            self.COMPACT_MODEL_TEXT_WIDTH if compact else MenuButton.MAX_TEXT_WIDTH
        )
        self.effort_button.set_text_width(
            self.COMPACT_EFFORT_TEXT_WIDTH if compact else MenuButton.MAX_TEXT_WIDTH
        )

    # ------------------------------------------------------------------ 事件
    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._apply_compact(self.width() < self.COMPACT_WIDTH)
        self._elide_hint()

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.editor:
            if event.type() == QEvent.Type.FocusIn:
                self.card.setProperty("focused", True)
                self._polish_card()
            elif event.type() == QEvent.Type.FocusOut:
                self.card.setProperty("focused", False)
                self._polish_card()
        return super().eventFilter(obj, event)

    def _polish_card(self) -> None:
        self.card.style().unpolish(self.card)
        self.card.style().polish(self.card)
