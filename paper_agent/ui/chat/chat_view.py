"""消息滚动区：承载消息列表与欢迎页。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from paper_agent.core.config import MODE_DOC
from paper_agent.core.models import Message
from paper_agent.ui.chat.message_widget import MessageWidget
from paper_agent.ui.chat.video_card import pause_active_if_out_of_view
from paper_agent.ui.chat.welcome_view import WelcomeView
from paper_agent.ui.widgets.icon_button import IconToolButton

AUTO_SCROLL_THRESHOLD = 120  # 距底部小于该像素时自动跟随
SCROLL_SETTLE_MS = 240      # 切换会话后跟随修正滚动位置的时长


class ChatView(QWidget):
    """聊天输出区。"""

    message_copy_requested = Signal(str)
    message_regenerate_requested = Signal(str)
    message_delete_requested = Signal(str)
    suggestion_selected = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        mode: str = MODE_DOC,
        user_name: str = "",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("chatPage")
        self._mode = mode            # 传给每条消息：决定助手称谓与生成中文案

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ------------------------------ 滚动区
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("chatScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        root.addWidget(self.scroll_area, 1)

        self.viewport = QWidget()
        self.viewport.setObjectName("chatViewport")
        # 滚动区的内部容器默认最小宽度跟着内容走：一旦某条消息的最小宽度
        # （附件条、超宽卡片）超过聊天栏，QScrollArea 会把容器撑到那个宽度，
        # 而横向滚动条是关掉的，右侧内容就被直接裁掉（看起来像被结构面板盖住）。
        # 显式给个极小值，让容器始终等于可视宽度。
        self.viewport.setMinimumWidth(1)
        viewport_layout = QHBoxLayout(self.viewport)
        viewport_layout.setContentsMargins(0, 0, 0, 0)
        viewport_layout.setSpacing(0)
        # 两侧留白 stretch 设为 0：让消息列优先吃满可用宽度（上限 980），
        # 只有超出上限的部分才分给留白做居中。否则消息少时列会缩成很窄一条。
        viewport_layout.addStretch(0)

        self.column = QWidget(self.viewport)
        self.column.setObjectName("chatColumn")
        self.column.setMaximumWidth(980)
        self.column_layout = QVBoxLayout(self.column)
        self.column_layout.setContentsMargins(24, 18, 24, 24)
        self.column_layout.setSpacing(2)
        self.column_layout.addStretch(1)
        viewport_layout.addWidget(self.column, 1)
        viewport_layout.addStretch(0)
        self.scroll_area.setWidget(self.viewport)

        # ------------------------------ 欢迎页（独立容器，避免被消息流挤压）
        # 欢迎页也按模式走：编程模式的空会话要给工程类引导
        self.welcome = WelcomeView(self.viewport, mode=mode, user_name=user_name)
        self.welcome.suggestion_selected.connect(self.suggestion_selected)
        # 单独用一个全宽居中容器承载欢迎页，独立于消息流
        self._welcome_host = QWidget(self.viewport)
        self._welcome_host.setObjectName("chatWelcomeHost")
        welcome_layout = QHBoxLayout(self._welcome_host)
        welcome_layout.setContentsMargins(0, 12, 0, 18)
        welcome_layout.setSpacing(0)
        welcome_layout.addStretch(1)
        welcome_layout.addWidget(self.welcome)
        welcome_layout.addStretch(1)
        self.column_layout.insertWidget(0, self._welcome_host)
        self._welcome_host.setVisible(False)

        # ------------------------------ 回到底部
        self.scroll_bottom_button = IconToolButton(
            "arrow-down", "回到底部", object_name="scrollToBottom", parent=self
        )
        self.scroll_bottom_button.setFixedSize(30, 30)
        self.scroll_bottom_button.setVisible(False)
        self.scroll_bottom_button.clicked.connect(self.scroll_to_bottom)

        self.scroll_area.verticalScrollBar().valueChanged.connect(self._on_scrolled)

        # 会话切换后的滚动位置结算
        self._scroll_target: int | None = None
        self._scroll_settle = QTimer(self)
        self._scroll_settle.setSingleShot(True)
        self._scroll_settle.setInterval(SCROLL_SETTLE_MS)
        self._scroll_settle.timeout.connect(self._clear_scroll_target)
        self.scroll_area.verticalScrollBar().rangeChanged.connect(
            self._on_scroll_range_changed
        )

        self._widgets: dict[str, MessageWidget] = {}

    # ------------------------------------------------------------------ 消息
    def set_mode(self, mode: str) -> None:
        """切换工作模式：已显示的消息也要跟着改称谓，不用重启或重建列表。"""
        self._mode = mode
        for widget in self._widgets.values():
            widget.set_mode(mode)
        self.welcome.set_mode(mode)

    def add_message(self, message: Message) -> MessageWidget:
        widget = MessageWidget(message, self.column, mode=self._mode)
        widget.copy_requested.connect(self.message_copy_requested)
        widget.regenerate_requested.connect(self.message_regenerate_requested)
        widget.delete_requested.connect(self.message_delete_requested)
        # 插到拉伸项之前
        self.column_layout.insertWidget(self.column_layout.count() - 1, widget)
        self._widgets[message.id] = widget
        self.set_welcome_visible(False)
        QTimer.singleShot(0, self.scroll_to_bottom)
        return widget

    def widget_for(self, message_id: str) -> MessageWidget | None:
        return self._widgets.get(message_id)

    def remove_message(self, message_id: str) -> None:
        widget = self._widgets.pop(message_id, None)
        if widget is None:
            return
        self.column_layout.removeWidget(widget)
        widget.deleteLater()

    def clear_messages(self) -> None:
        for widget in list(self._widgets.values()):
            self.column_layout.removeWidget(widget)
            widget.deleteLater()
        self._widgets.clear()
        self.set_welcome_visible(True)

    def set_welcome_visible(self, visible: bool) -> None:
        self._welcome_host.setVisible(visible)
        self.welcome.setVisible(visible)

    def message_count(self) -> int:
        return len(self._widgets)

    # ------------------------------------------------------------------ 滚动
    def scroll_to_bottom(self) -> None:
        # 本方法会被 QTimer.singleShot(0, ...) 排到下一个事件循环：期间窗口可能
        # 已经销毁（重建窗口、用例结束后被回收），再碰控件会抛 RuntimeError。
        if not isValid(self):
            return
        bar = self.scroll_area.verticalScrollBar()
        bar.setValue(bar.maximum())

    def scroll_position(self) -> int:
        """当前垂直滚动位置（像素）。"""
        return self.scroll_area.verticalScrollBar().value()

    def apply_scroll(self, value: int | None) -> None:
        """滚到指定位置（``None`` 表示底部），并在布局结算期间跟随修正。

        消息控件的高度要分几轮才结算完，滚动条最大値会在重建后继续变化，
        单次设值会被截断（表现为停在最顶部），因此在结算窗口内跟随范围变化重设。
        """
        self._scroll_target = value
        self._scroll_settle.start()
        self._apply_scroll_target()

    def _apply_scroll_target(self) -> None:
        bar = self.scroll_area.verticalScrollBar()
        target = self._scroll_target
        if target is None:
            bar.setValue(bar.maximum())
        else:
            bar.setValue(max(0, min(int(target), bar.maximum())))

    def _on_scroll_range_changed(self, _minimum: int, _maximum: int) -> None:
        if self._scroll_target is not None or self._scroll_settle.isActive():
            self._apply_scroll_target()

    def _clear_scroll_target(self) -> None:
        self._scroll_target = None

    def maybe_follow(self) -> None:
        """仅在用户处于底部附近时自动跟随（流式输出用）。"""
        bar = self.scroll_area.verticalScrollBar()
        if bar.maximum() - bar.value() <= AUTO_SCROLL_THRESHOLD:
            self.scroll_to_bottom()

    def _on_scrolled(self, _value: int) -> None:
        bar = self.scroll_area.verticalScrollBar()
        at_bottom = bar.maximum() - bar.value() <= AUTO_SCROLL_THRESHOLD
        self.scroll_bottom_button.setVisible(not at_bottom and bar.maximum() > 0)
        # 视频滚出视野就停掉：它一边解码一边跟着滚动，最拖界面
        pause_active_if_out_of_view(self.scroll_area.viewport())

    # ------------------------------------------------------------------ 事件
    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self.scroll_bottom_button.move(
            self.width() - self.scroll_bottom_button.width() - 24,
            self.height() - self.scroll_bottom_button.height() - 16,
        )
