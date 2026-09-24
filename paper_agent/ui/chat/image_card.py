"""生成图片的内联卡片：直接展示图片，点击用系统默认程序打开。"""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from paper_agent.core.signals import signals
from paper_agent.core.theme import theme_manager
from paper_agent.resources.icons import build_icon
from paper_agent.ui.widgets.elide_label import ElideLabel
from paper_agent.ui.widgets.icon_button import IconToolButton
from paper_agent.utils.files import file_size, human_readable_size
from paper_agent.utils.text import format_datetime

MAX_PREVIEW_WIDTH = 420
MAX_PREVIEW_HEIGHT = 360
MIN_PREVIEW_WIDTH = 160
NAME_MAX_WIDTH = 260
META_MAX_WIDTH = 150


class ImageCard(QWidget):
    """消息区里的生成图片（点击打开，可另存为）。"""

    def __init__(
        self,
        name: str,
        path: str,
        parent: QWidget | None = None,
        *,
        created_at: float = 0.0,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("imageCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._path = path
        self._created_at = created_at

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 6)
        layout.setSpacing(6)

        self.preview = QLabel(self)
        self.preview.setObjectName("imagePreview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.preview)

        footer = QHBoxLayout()
        footer.setContentsMargins(2, 0, 2, 0)
        footer.setSpacing(6)

        # ElideLabel：聊天栏收窄时文字自动省略，而不是把卡片顶宽、把消息列撑出聊天栏
        self.name_label = ElideLabel(
            name or "配图", self,
            mode=Qt.TextElideMode.ElideMiddle, max_width=NAME_MAX_WIDTH,
        )
        self.name_label.setObjectName("chipName")
        footer.addWidget(self.name_label)

        # 元信息上限比文件名短：卡片最大就 444 宽，两个都不设限会互相挤压，
        # 结果文件名被省略到只剩「…」。这里让文件名优先拿到宽度。
        self.meta_label = ElideLabel(self._meta_text(), self, max_width=META_MAX_WIDTH)
        self.meta_label.setObjectName("chipSize")
        footer.addWidget(self.meta_label)
        footer.addStretch(1)

        self.save_button = IconToolButton(
            "download", "另存为", object_name="messageAction", icon_size=15, parent=self
        )
        self.save_button.clicked.connect(self._save_as)
        footer.addWidget(self.save_button)

        layout.addLayout(footer)
        self.setMaximumWidth(MAX_PREVIEW_WIDTH + 24)

        self._source_pixmap: QPixmap | None = None
        self._refresh_preview()
        signals.theme_changed.connect(self._refresh_preview)

    # ------------------------------------------------------------------ 信息
    @property
    def path(self) -> str:
        return self._path

    def _meta_text(self) -> str:
        suffix = Path(self._path).suffix.lstrip(".").upper() if self._path else "图片"
        parts = [suffix or "图片"]
        size = file_size(self._path)
        if size:
            parts.append(human_readable_size(size))
        stamp = format_datetime(self._created_at)
        if stamp:
            parts.append(stamp)
        parts.append("点击查看")
        return " · ".join(parts)

    # ------------------------------------------------------------------ 行为
    def _open(self) -> None:
        if not self._path or not Path(self._path).exists():
            signals.toast_requested.emit("图片不存在，可能已被清理")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._path))

    def _save_as(self) -> None:
        source = Path(self._path)
        if not source.exists():
            signals.toast_requested.emit("图片不存在")
            return
        path, _selected = QFileDialog.getSaveFileName(self, "另存为", source.name)
        if not path:
            return
        try:
            shutil.copy2(source, path)
            signals.toast_requested.emit(f"已保存到 {path}")
        except OSError as exc:
            signals.toast_requested.emit(f"保存失败：{exc}")

    def mousePressEvent(self, event) -> None:      # noqa: N802
        self._open()
        super().mousePressEvent(event)

    # ------------------------------------------------------------------ 渲染
    def resizeEvent(self, event) -> None:      # noqa: N802
        super().resizeEvent(event)
        # 预览图宽度跟着卡片走：固定成 420 时，窄栏下的卡片最小宽度就是 420+，
        # 整条消息列会被它撑到聊天栏外面（右侧被裁）。
        self._refresh_preview()

    def _refresh_preview(self, *_args) -> None:
        if self._source_pixmap is None:
            self._source_pixmap = QPixmap(self._path) if self._path else QPixmap()
        pixmap = self._source_pixmap
        if pixmap.isNull():
            self.preview.setText("图片无法预览")
            self.preview.setPixmap(
                build_icon("image", theme_manager.color("text_muted"), 48).pixmap(48, 48)
            )
            return
        width = max(MIN_PREVIEW_WIDTH, min(MAX_PREVIEW_WIDTH, self.width() - 16))
        scaled = pixmap.scaled(
            width,
            MAX_PREVIEW_HEIGHT,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview.setText("")
        if scaled.size() != self.preview.size():
            self.preview.setFixedSize(scaled.size())
            self.preview.setPixmap(scaled)
