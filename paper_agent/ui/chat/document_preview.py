"""聊天内的文档预览：Markdown / 纯文本 / 图片，随文件变化自动刷新。

模型正在写文档时，文件会不断被改写；预览按「修改时间 + 大小」判断是否重新读取，
因此边写边看时内容会自动跟上。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from paper_agent.services.document_preview import Preview, build_preview
from paper_agent.ui.chat.rich_text import RichTextLabel
from paper_agent.ui.widgets.icon_button import IconToolButton

PREVIEW_MAX_HEIGHT = 340
IMAGE_MAX_WIDTH = 460
REFRESH_INTERVAL_MS = 1200


class DocumentPreview(QWidget):
    """文件行内预览控件。"""

    def __init__(self, path: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("docPreview")
        self._path = path
        self._stamp: tuple[float, int] | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(6)
        self.header_label = QLabel("预览（只读）", self)
        self.header_label.setObjectName("previewHeader")
        header.addWidget(self.header_label)
        header.addStretch(1)
        self.refresh_button = IconToolButton(
            "refresh", "重新读取", object_name="messageAction", icon_size=14, parent=self
        )
        self.refresh_button.clicked.connect(lambda: self.refresh(force=True))
        header.addWidget(self.refresh_button)
        root.addLayout(header)

        self.body = QStackedWidget(self)

        self.text_view = RichTextLabel(self)
        self.text_scroll = self._scrolled(self.text_view)
        self.body.addWidget(self.text_scroll)

        self.image_label = QLabel(self)
        self.image_label.setObjectName("previewImage")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_scroll = self._scrolled(self.image_label)
        self.body.addWidget(self.image_scroll)

        self.hint_label = QLabel(self)
        self.hint_label.setObjectName("previewHint")
        self.hint_label.setWordWrap(True)
        self.body.addWidget(self.hint_label)

        root.addWidget(self.body)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self.refresh)

        if path:
            self.set_path(path)

    # ------------------------------------------------------------------ 接口
    @property
    def path(self) -> str:
        return self._path

    def set_path(self, path: str) -> None:
        self._path = path
        self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        """重新读取文件；``force=False`` 时只在文件确实变化后重读。"""
        if not self._path:
            self._show_hint("没有可预览的文件")
            return
        stamp = self._file_stamp()
        if not force and stamp is not None and stamp == self._stamp:
            return
        self._stamp = stamp

        preview = build_preview(self._path)
        if preview.kind == "image":
            self._show_image(preview.content)
        elif preview.kind in {"markdown", "text"} and preview.content:
            self._show_text(preview)
        else:
            self._show_hint(preview.note or "暂无内容可预览")

    # ------------------------------------------------------------------ 内部
    def _scrolled(self, widget: QWidget) -> QScrollArea:
        area = QScrollArea(self)
        area.setObjectName("previewScroll")
        area.setWidgetResizable(True)
        area.setMaximumHeight(PREVIEW_MAX_HEIGHT)
        area.setWidget(widget)
        return area

    def _file_stamp(self) -> tuple[float, int] | None:
        from pathlib import Path

        try:
            stat = Path(self._path).stat()
        except OSError:
            return None
        return (stat.st_mtime, stat.st_size)

    def _header_text(self, preview: Preview) -> str:
        from pathlib import Path

        suffix = Path(self._path).suffix.lstrip(".").upper() or "文件"
        parts = [f"预览 · {suffix}"]
        if preview.truncated:
            parts.append("仅显示开头部分")
        return " · ".join(parts)

    def _show_text(self, preview: Preview) -> None:
        if preview.kind == "markdown":
            self.text_view.set_markdown(preview.content)
        else:
            self.text_view.set_plain(preview.content)
        self.body.setCurrentWidget(self.text_scroll)
        self.header_label.setText(self._header_text(preview))

    def _show_image(self, path: str) -> None:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self._show_hint("图片无法预览，点卡片可用系统默认程序打开")
            return
        self.image_label.setPixmap(
            pixmap.scaled(
                IMAGE_MAX_WIDTH,
                PREVIEW_MAX_HEIGHT,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.body.setCurrentWidget(self.image_scroll)
        self.header_label.setText(self._header_text(Preview(kind="image")))

    def _show_hint(self, text: str) -> None:
        self.hint_label.setText(text)
        self.body.setCurrentWidget(self.hint_label)

    # ------------------------------------------------------------------ 事件
    def showEvent(self, event) -> None:      # noqa: N802
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event) -> None:      # noqa: N802
        self._timer.stop()
        super().hideEvent(event)
