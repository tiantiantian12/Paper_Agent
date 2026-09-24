"""智能体产出的文件卡片：单击用系统默认程序打开，可预览、另存为。"""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from paper_agent.core.signals import signals
from paper_agent.core.theme import theme_manager
from paper_agent.resources.icons import build_icon
from paper_agent.ui.chat.document_preview import DocumentPreview
from paper_agent.ui.widgets.elide_label import ElideLabel
from paper_agent.ui.widgets.icon_button import IconToolButton
from paper_agent.utils.clipboard import copy_files, copy_text
from paper_agent.utils.files import file_kind, file_size, human_readable_size
from paper_agent.utils.text import format_datetime

SOURCE_LABELS = {"user": "我上传", "model": "模型生成"}
SOURCE_ICONS = {"user": "paperclip", "model": "sparkles"}


class ArtifactCard(QWidget):
    """单个文件卡片（消息内产物 / 论文空间共用，复用附件 chip 的样式）。

    Args:
        name: 显示的文件名
        path: 文件路径
        parent: 父控件
        source: ``"user"`` 用户上传 / ``"model"`` 模型生成；留空则不显示来源徽标
        created_at: 上传或生成的时间戳；为 0 时不显示时间
        removable: 是否显示删除按钮（删除由上层确认与执行）
        previewable: 是否允许在卡片下方展开预览
        copyable: 是否显示「复制绝对路径」「复制文件」按钮
    """

    delete_requested = Signal(str)   # 文件路径

    def __init__(
        self,
        name: str,
        path: str,
        parent: QWidget | None = None,
        *,
        source: str = "",
        created_at: float = 0.0,
        removable: bool = False,
        previewable: bool = False,
        copyable: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("attachmentChip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._path = path
        self._source = source
        self._created_at = created_at
        self._preview: DocumentPreview | None = None
        self._preview_open = False      # 自己记状态：Qt 的 isVisible 受父级可见性影响

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)

        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(24, 24)
        layout.addWidget(self.icon_label)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)

        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(6)
        # ElideLabel：窄栏下自动省略，不再用文件名长度撑住卡片（进而撑爆消息列）
        self.name_label = ElideLabel(
            name or "文件", self,
            mode=Qt.TextElideMode.ElideMiddle, max_width=320,
        )
        self.name_label.setObjectName("chipName")
        name_row.addWidget(self.name_label)

        self.source_label: QLabel | None = None
        if source in SOURCE_LABELS:
            self.source_label = QLabel(SOURCE_LABELS[source], self)
            self.source_label.setObjectName(
                "sourceBadgeUser" if source == "user" else "sourceBadgeModel"
            )
            name_row.addWidget(self.source_label)
        name_row.addStretch(1)
        text_layout.addLayout(name_row)

        self.meta_label = ElideLabel(self._meta_text(), self)
        self.meta_label.setObjectName("chipSize")
        text_layout.addWidget(self.meta_label)
        layout.addLayout(text_layout, 1)

        self.preview_button = IconToolButton(
            "eye", "在对话里预览", object_name="messageAction", icon_size=15, parent=self
        )
        self.preview_button.clicked.connect(self.toggle_preview)
        self.preview_button.setVisible(previewable)
        layout.addWidget(self.preview_button)

        # 复制绝对路径 / 复制文件（文件可在资源管理器里直接粘贴）
        self.copy_path_button = IconToolButton(
            "link", "复制绝对路径", object_name="messageAction", icon_size=15, parent=self
        )
        self.copy_path_button.clicked.connect(self._copy_path)
        self.copy_path_button.setVisible(copyable)
        layout.addWidget(self.copy_path_button)

        self.copy_file_button = IconToolButton(
            "copy", "复制文件（可在资源管理器粘贴）", object_name="messageAction", icon_size=15, parent=self
        )
        self.copy_file_button.clicked.connect(self._copy_file)
        self.copy_file_button.setVisible(copyable)
        layout.addWidget(self.copy_file_button)

        self.save_button = IconToolButton(
            "download", "另存为", object_name="messageAction", icon_size=15, parent=self
        )
        self.save_button.clicked.connect(self._save_as)
        layout.addWidget(self.save_button)

        self.delete_button = IconToolButton(
            "trash", "删除文件", object_name="messageAction", icon_size=15, parent=self
        )
        self.delete_button.clicked.connect(lambda: self.delete_requested.emit(self._path))
        self.delete_button.setVisible(removable)
        layout.addWidget(self.delete_button)

        root.addLayout(layout)
        self._root_layout = root

        self._refresh_assets()
        signals.theme_changed.connect(self._refresh_assets)

    # ------------------------------------------------------------------ 信息
    @property
    def path(self) -> str:
        return self._path

    @property
    def preview_visible(self) -> bool:
        return self._preview_open and self._preview is not None

    @property
    def preview(self) -> DocumentPreview | None:
        return self._preview

    def _meta_text(self) -> str:
        size = file_size(self._path)
        suffix = Path(self._path).suffix.lstrip(".").upper() if self._path else ""
        parts = [suffix or "文件"]
        if size:
            parts.append(human_readable_size(size))
        stamp = format_datetime(self._created_at)
        if stamp:
            parts.append(stamp)
        parts.append("点击打开")
        return " · ".join(parts)

    # ------------------------------------------------------------------ 预览
    def toggle_preview(self) -> None:
        self.set_preview_visible(not self.preview_visible)

    def set_preview_visible(self, visible: bool) -> None:
        """展开 / 收起行内预览（首次展开时才创建预览控件）。"""
        if visible and not Path(self._path).exists():
            signals.toast_requested.emit("文件不存在，无法预览")
            return
        if visible and self._preview is None:
            self._preview = DocumentPreview(self._path, self)
            self._root_layout.addWidget(self._preview)
        self._preview_open = visible and self._preview is not None
        if self._preview is not None:
            self._preview.setVisible(self._preview_open)
            if self._preview_open:
                self._preview.set_path(self._path)
        self.preview_button.setToolTip("收起预览" if visible else "在对话里预览")
        self.preview_button.set_icon_name("close" if visible else "eye")

    # ------------------------------------------------------------------ 行为
    def _open(self) -> None:
        if not self._path or not Path(self._path).exists():
            signals.toast_requested.emit("文件不存在，可能已被清理")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._path))

    def _copy_path(self) -> None:
        """复制文件的绝对路径（粘到终端、编辑器、聊天框都能用）。"""
        if not self._path:
            return
        copy_text(self._path)
        signals.toast_requested.emit(f"已复制路径：{self._path}")

    def _copy_file(self) -> None:
        """把文件本身放进剪贴板：在资源管理器里 Ctrl+V 即可粘贴一份。"""
        if copy_files([self._path]) == 0:
            signals.toast_requested.emit("文件不存在，无法复制")
            return
        signals.toast_requested.emit(f"已复制文件：{Path(self._path).name}")

    def _save_as(self) -> None:
        source = Path(self._path)
        if not source.exists():
            signals.toast_requested.emit("文件不存在")
            return
        path, _selected = QFileDialog.getSaveFileName(self, "另存为", source.name)
        if not path:
            return
        try:
            shutil.copy2(source, path)
            signals.toast_requested.emit(f"已保存到 {path}")
        except OSError as exc:
            signals.toast_requested.emit(f"保存失败：{exc}")

    # ------------------------------------------------------------------ 事件
    def mousePressEvent(self, event) -> None:      # noqa: N802
        # 点预览区域内（选文本、看图）不该触发「用系统程序打开」
        if self._preview_open and self._preview is not None:
            if self._preview.geometry().contains(event.position().toPoint()):
                super().mousePressEvent(event)
                return
        self._open()
        super().mousePressEvent(event)

    def _refresh_assets(self, *_args) -> None:
        color = theme_manager.color("text_secondary")
        if file_kind(self._path) == "image":
            icon_name = "image"
        else:
            icon_name = SOURCE_ICONS.get(self._source, "file-text")
        self.icon_label.setPixmap(build_icon(icon_name, color, 36).pixmap(20, 20))
