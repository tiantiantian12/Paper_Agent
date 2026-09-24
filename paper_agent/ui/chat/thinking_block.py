"""可折叠的「思考过程」区块（Codex 风格）。

- 生成中：自动展开，标题显示"正在思考…"并实时计时
- 生成结束：自动折叠为一行摘要（"已思考 · N 秒"），可点击展开
- 内容与正式回答完全分离，使用等宽小字 + 左侧竖线呈现
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
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
from paper_agent.ui.widgets.icon_button import IconToolButton


class ThinkingBlock(QFrame):
    """推理过程折叠面板。"""

    toggled = Signal(bool)

    MAX_BODY_HEIGHT = 220

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("thinkingBlock")
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._expanded = True
        self._streaming = False
        self._started_at = 0.0
        self._elapsed_ms = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---------------------------------------------------------- 头部
        self.header = QWidget(self)
        self.header.setObjectName("thinkingHeader")
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

        self.title_label = QLabel("思考过程")
        self.title_label.setObjectName("thinkingTitle")
        header_layout.addWidget(self.title_label)

        self.time_label = QLabel("")
        self.time_label.setObjectName("thinkingTime")
        header_layout.addWidget(self.time_label)
        header_layout.addStretch(1)

        root.addWidget(self.header)

        # ---------------------------------------------------------- 内容
        self.body = QPlainTextEdit(self)
        self.body.setObjectName("thinkingBody")
        self.body.setReadOnly(True)
        self.body.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.body.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.body.setMaximumHeight(self.MAX_BODY_HEIGHT)
        self.body.setPlaceholderText("")
        root.addWidget(self.body)

        # ---------------------------------------------------------- 计时器
        self._ticker = QTimer(self)
        self._ticker.setInterval(1000)
        self._ticker.timeout.connect(self._tick)

        self._refresh_assets()
        self.set_expanded(False)
        signals.theme_changed.connect(self._refresh_assets)

    # ------------------------------------------------------------------ 状态
    @property
    def is_expanded(self) -> bool:
        return self._expanded

    def begin(self) -> None:
        """开始思考：展开、启动计时。"""
        self._streaming = True
        self._started_at = time.time()
        self._elapsed_ms = 0
        self.body.clear()
        self.set_expanded(True)
        self._ticker.start()
        self._update_title()

    def append(self, text: str) -> None:
        """追加推理文本（流式）。"""
        cursor = self.body.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self.body.setTextCursor(cursor)
        self.body.ensureCursorVisible()
        self._fit_height()

    def finish(self, elapsed_ms: int = 0) -> None:
        """结束思考：停止计时、自动折叠。"""
        self._streaming = False
        self._ticker.stop()
        if elapsed_ms:
            self._elapsed_ms = elapsed_ms
        elif self._started_at:
            self._elapsed_ms = int((time.time() - self._started_at) * 1000)
        self._update_title()
        if self.body.toPlainText().strip():
            self.set_expanded(False)

    def set_thinking(self, text: str, elapsed_ms: int = 0) -> None:
        """一次性设置内容（用于历史消息回填）。"""
        self.body.setPlainText(text)
        self._elapsed_ms = elapsed_ms
        self._streaming = False
        self._ticker.stop()
        self._update_title()
        self.setVisible(bool(text.strip()))
        self.set_expanded(False)
        self._fit_height()

    def text(self) -> str:
        return self.body.toPlainText()

    def elapsed_ms(self) -> int:
        return self._elapsed_ms

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

    def _tick(self) -> None:
        if not self._streaming or not self._started_at:
            return
        self._elapsed_ms = int((time.time() - self._started_at) * 1000)
        self._update_title()

    def _update_title(self) -> None:
        seconds = self._elapsed_ms / 1000
        if self._streaming:
            self.title_label.setText("正在思考…")
            self.time_label.setText(f"{seconds:.0f}s")
        else:
            self.title_label.setText("已思考")
            if self._elapsed_ms:
                self.time_label.setText(f"{seconds:.1f}s")
            else:
                self.time_label.setText("")

    def _refresh_arrow(self) -> None:
        color = theme_manager.color("text_muted")
        name = "chevron-down" if self._expanded else "chevron-right"
        self.arrow_label.setPixmap(build_icon(name, color, 28).pixmap(14, 14))

    def _refresh_assets(self, *_args) -> None:
        self.icon_label.setPixmap(
            build_icon("sparkles", theme_manager.color("text_muted"), 28).pixmap(14, 14)
        )
        self._refresh_arrow()

    # ------------------------------------------------------------------ 事件
    def mousePressEvent(self, event):  # noqa: N802
        if self.header.geometry().contains(event.position().toPoint()):
            self.toggle()
            return
        super().mousePressEvent(event)
