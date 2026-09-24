"""「正在写入的正文」折叠区块。

模型写论文时，正文是塞在 ``create_docx`` 的 ``markdown`` 参数里的：参数写完
（也就是工具开始执行）之前，界面上没有任何内容可看。这个区块把参数流实时解码
出来，像打字机一样显示正在拼的那篇文档，工具真正开始生成文件后自动折叠。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from paper_agent.core.signals import signals
from paper_agent.core.theme import theme_manager
from paper_agent.resources.icons import build_icon


class DraftBlock(QFrame):
    """正在写入的正文（只读、可折叠、实时追加）。"""

    toggled = Signal(bool)

    MAX_BODY_HEIGHT = 260

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("draftBlock")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVisible(False)

        self._expanded = True
        self._streaming = False
        self._chars = 0
        self._tool = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.header = QWidget(self)
        self.header.setObjectName("draftHeader")
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(8, 5, 8, 5)
        header_layout.setSpacing(6)

        self.arrow_label = QLabel(self.header)
        self.arrow_label.setFixedSize(14, 14)
        header_layout.addWidget(self.arrow_label)

        self.icon_label = QLabel(self.header)
        self.icon_label.setFixedSize(14, 14)
        header_layout.addWidget(self.icon_label)

        self.title_label = QLabel("正在写入正文")
        self.title_label.setObjectName("draftTitle")
        header_layout.addWidget(self.title_label)

        self.meta_label = QLabel("")
        self.meta_label.setObjectName("draftMeta")
        header_layout.addWidget(self.meta_label)
        header_layout.addStretch(1)
        root.addWidget(self.header)

        self.body = QPlainTextEdit(self)
        self.body.setObjectName("draftBody")
        self.body.setReadOnly(True)
        self.body.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.body.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.body.setMaximumHeight(self.MAX_BODY_HEIGHT)
        root.addWidget(self.body)

        self._refresh_assets()
        signals.theme_changed.connect(self._refresh_assets)

    # ------------------------------------------------------------------ 状态
    @property
    def is_expanded(self) -> bool:
        return self._expanded

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    def begin(self, tool: str = "") -> None:
        """开始一篇新的写入（同一轮里模型可能分多次调用工具）。"""
        self._streaming = True
        self._chars = 0
        self._tool = tool
        self.body.clear()
        self.setVisible(True)
        self.set_expanded(True)
        self._update_title()

    def resume(self) -> None:
        """分段写入的下一段（上一段已经折叠）：接着同一个区块往下写。"""
        self._streaming = True
        self._tool = ""
        self.setVisible(True)
        self.set_expanded(True)
        self._update_title()

    def has_content(self) -> bool:
        """是否已有草稿内容（判断「开始新的一篇」还是「继续往下写」）。

        不能用 ``isVisible()`` 判断：控件所在窗口还没显示时它一直是 False。
        """
        return self._chars > 0 or bool(self.body.toPlainText())

    def append(self, text: str) -> None:
        """追加一段刚解出的正文。"""
        if not text:
            return
        if not self._streaming and not self.has_content():
            self.begin()
        cursor = self.body.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self.body.setTextCursor(cursor)
        self.body.ensureCursorVisible()
        self._chars += len(text)
        self._fit_height()
        self._update_title()

    def finish(self, filename: str = "") -> None:
        """参数写完、工具开始执行：折叠成一行摘要。"""
        if not self._streaming:
            return
        self._streaming = False
        if filename:
            self._tool = ""
            self.meta_label.setText(filename)
        self._update_title()
        if self.body.toPlainText().strip():
            self.set_expanded(False)

    def clear_draft(self) -> None:
        """新一轮生成开始：清掉上一轮的草稿。"""
        self._streaming = False
        self._chars = 0
        self._tool = ""
        self.body.clear()
        self.setVisible(False)
        self.set_expanded(False)

    def text(self) -> str:
        return self.body.toPlainText()

    def char_count(self) -> int:
        return self._chars

    # ------------------------------------------------------------------ 折叠
    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self.body.setVisible(expanded)
        self._refresh_arrow()
        self.toggled.emit(expanded)

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    # ------------------------------------------------------------------ 内部
    def _fit_height(self) -> None:
        """内容不足一屏时按行数收缩，过多时封顶滚动。"""
        line_height = self.body.fontMetrics().lineSpacing()
        lines = max(1, self.body.document().lineCount())
        margins = self.body.contentsMargins()
        height = lines * line_height + margins.top() + margins.bottom() + 8
        self.body.setFixedHeight(min(height, self.MAX_BODY_HEIGHT))

    def _update_title(self) -> None:
        if self._streaming:
            self.title_label.setText("正在写入正文…")
        else:
            self.title_label.setText("已写入正文")
        if self.meta_label.text() and not self._streaming:
            return
        self.meta_label.setText(f"{self._chars} 字" if self._chars else "")

    def _refresh_arrow(self) -> None:
        color = theme_manager.color("text_muted")
        name = "chevron-down" if self._expanded else "chevron-right"
        self.arrow_label.setPixmap(build_icon(name, color, 28).pixmap(14, 14))

    def _refresh_assets(self, *_args) -> None:
        self.icon_label.setPixmap(
            build_icon("file-text", theme_manager.color("text_muted"), 28).pixmap(14, 14)
        )
        self._refresh_arrow()

    # ------------------------------------------------------------------ 事件
    def mousePressEvent(self, event):  # noqa: N802
        if self.header.geometry().contains(event.position().toPoint()):
            self.toggle()
            return
        super().mousePressEvent(event)
