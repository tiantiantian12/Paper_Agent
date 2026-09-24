"""自适应高度的富文本显示控件（用于渲染 Markdown）。"""

from __future__ import annotations

import math

from PySide6.QtCore import QSize, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QTextOption
from PySide6.QtWidgets import QSizePolicy, QTextBrowser

from paper_agent.core.signals import signals
from paper_agent.ui.chat.markdown_renderer import MarkdownRenderer
from paper_agent.utils.clipboard import copy_text


class RichTextLabel(QTextBrowser):
    """按内容自动调整高度、支持 Markdown 与代码复制锚点的只读文本控件。"""

    code_copied = Signal()

    # 纯文本模式（用户气泡）按内容估算宽度时的上限
    PLAIN_HINT_MAX_WIDTH = 700

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("messageText")
        self.setReadOnly(True)
        self.setFrameShape(QTextBrowser.Shape.NoFrame)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 横向保持「需要时才出现」：结构面板拉宽、窗口变窄时，宽内容（大表格、
        # 长代码行）宁可出现滚动条，也不能被静默裁掉看不着
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        # QTextBrowser 的最小宽会跟着内容走；显式给个极小值，否则聊天区
        # 撑到内容宽度后无法被压缩，拖动分隔条只会去挤左侧栏
        self.setMinimumWidth(1)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document().setDocumentMargin(0)

        self._renderer = MarkdownRenderer()
        self._plain = ""
        self._markdown_mode = True
        self._plain_hint_width = 0

        self.document().documentLayout().documentSizeChanged.connect(self._adjust_height)
        self.anchorClicked.connect(self._on_anchor_clicked)
        # Markdown 渲染会把主题色写进 HTML（表格 / 代码块 / 引用线等），
        # 切换主题后必须按新配色重新渲染，否则会出现「暗底暗字」这类问题。
        signals.theme_changed.connect(self._rerender)

    # ------------------------------------------------------------------ 接口
    def set_markdown(self, text: str) -> None:
        self._markdown_mode = True
        self._plain = text
        self._plain_hint_width = 0
        self.setHtml(self._renderer.render(text))
        self._adjust_height()

    def set_plain(self, text: str) -> None:
        """按纯文本展示（保留换行），用于用户消息。"""
        from html import escape

        self._markdown_mode = False
        self._plain = text
        self._plain_hint_width = self._measure_plain_width(text)
        style = (
            "white-space:pre-wrap; word-break:break-word; "
            f"font-family:{self.font().family()}; "
        )
        self.setHtml(f'<div style="{style}">{escape(text)}</div>')
        self._adjust_height()
        self.updateGeometry()

    def _measure_plain_width(self, text: str) -> int:
        """估算纯文本的自然宽度。

        QTextBrowser 的 sizeHint 是个与内容无关的固定值（约 256px），后面又
        setFixedHeight，用户气泡就永远是同一条窄条、长消息被挤成竖排。
        这里按最长一行给出宽度提示（只是一个 sizeHint，仍然可以被压缩）。
        """
        metrics = self.fontMetrics()
        width = 0
        for line in text.splitlines() or [""]:
            width = max(width, metrics.horizontalAdvance(line))
        return min(width, self.PLAIN_HINT_MAX_WIDTH)

    def sizeHint(self) -> QSize:      # noqa: N802
        hint = super().sizeHint()
        if not self._markdown_mode and self._plain_hint_width > 0:
            return QSize(self._plain_hint_width, hint.height())
        return hint

    def plain_text(self) -> str:
        return self._plain

    def code_blocks(self) -> list[str]:
        return self._renderer.code_blocks

    # ------------------------------------------------------------------ 内部
    def _rerender(self, *_args) -> None:
        """按当前主题配色重新渲染已有内容。"""
        if self._markdown_mode:
            self.set_markdown(self._plain)
        else:
            self.set_plain(self._plain)

    def _adjust_height(self, *_args) -> None:
        height = int(math.ceil(self.document().size().height())) + 2
        # 表格 / 长代码行会撑出横向滚动条，它占掉的正是视口高度；
        # 不计入这里的话，最后一行会被固定高度裁掉一半（看起来像内容没写完）。
        bar = self.horizontalScrollBar()
        if bar.isVisible():
            height += bar.sizeHint().height()
        if height != self.height():
            self.setFixedHeight(max(height, 10))
            self.updateGeometry()

    def _on_anchor_clicked(self, url: QUrl) -> None:
        if url.scheme() == "app" and url.host() == "copy-code":
            index = url.path().strip("/")
            if index.isdigit():
                blocks = self.code_blocks()
                pos = int(index)
                if 0 <= pos < len(blocks):
                    copy_text(blocks[pos])
                    self.code_copied.emit()
            return
        if url.isValid() and url.scheme() in ("http", "https", "file"):
            QDesktopServices.openUrl(url)

    # ------------------------------------------------------------------ 交互
    def contextMenuEvent(self, event):  # noqa: N802
        menu = self.createStandardContextMenu()
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        menu.setWindowFlags(
            menu.windowFlags()
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        menu.exec(event.globalPos())
