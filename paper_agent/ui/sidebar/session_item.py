"""会话列表条目。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from paper_agent.core.config import MODE_CODE
from paper_agent.core.models import ChatSession
from paper_agent.core.signals import signals
from paper_agent.core.theme import theme_manager
from paper_agent.resources.icons import build_icon
from paper_agent.ui.widgets.icon_button import IconToolButton
from paper_agent.utils.text import format_relative_time, summarize


class SessionItemWidget(QWidget):
    """单个会话条目：标题、时间摘要、悬浮操作。"""

    rename_requested = Signal(str)
    delete_requested = Signal(str)
    export_requested = Signal(str)

    def __init__(self, session: ChatSession, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sessionItem")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setProperty("active", False)
        self._session = session

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 4, 6)
        layout.setSpacing(8)

        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(16, 16)
        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(1)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)
        self.title_label = QLabel(self)
        self.title_label.setObjectName("sessionTitle")
        self.title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        title_row.addWidget(self.title_label, 1)
        # 模式徽标：只标「编程」（文档是默认，标出来反而是噪音），
        # 这样不用点开也能分清哪个会话在写代码
        self.mode_label = QLabel("编程", self)
        self.mode_label.setObjectName("sessionModeTag")
        self.mode_label.setToolTip("编程模式：这个会话在写代码 / 跑命令")
        title_row.addWidget(self.mode_label, 0, Qt.AlignmentFlag.AlignVCenter)
        text_layout.addLayout(title_row)

        self.meta_label = QLabel(self)
        self.meta_label.setObjectName("sessionMeta")
        text_layout.addWidget(self.title_label)
        text_layout.addWidget(self.meta_label)
        layout.addLayout(text_layout, 1)

        # 悬浮操作区（固定宽度，避免 hover 时布局抖动）
        action_host = QWidget(self)
        action_host.setFixedWidth(24)
        host_layout = QHBoxLayout(action_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        self.action_button = IconToolButton(
            "more", "更多操作", object_name="sessionAction", parent=action_host
        )
        self.action_button.clicked.connect(self._show_menu)
        self.action_button.setVisible(False)
        host_layout.addWidget(self.action_button)
        layout.addWidget(action_host, 0)

        self.update_session(session)
        self._refresh_icon()
        signals.theme_changed.connect(self._refresh_icon)

    # ------------------------------------------------------------------ 数据
    @property
    def session(self) -> ChatSession:
        return self._session

    def session_id(self) -> str:
        return self._session.id

    def update_session(self, session: ChatSession, active: bool = False) -> None:
        self._session = session
        self.title_label.setText(summarize(session.title, 40))
        self.mode_label.setVisible(session.mode == MODE_CODE)
        count = len(session.messages)
        meta = format_relative_time(session.updated_at)
        if count:
            meta = f"{meta} · {count} 条消息"
        self.meta_label.setText(meta)
        self.set_active(active)

    def set_active(self, active: bool) -> None:
        self.setProperty("active", active)
        self.title_label.setProperty("active", active)
        self.mode_label.setProperty("active", active)
        self._polish(self)
        self._polish(self.title_label)
        self._polish(self.mode_label)
        self._refresh_icon(active=active)

    # ------------------------------------------------------------------ 事件
    def enterEvent(self, event):  # noqa: N802
        self.action_button.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self.action_button.setVisible(False)
        super().leaveEvent(event)

    def contextMenuEvent(self, event):  # noqa: N802
        """右键同样弹出操作菜单。

        删除 / 重命名只放在 hover 才出现的「⋯」里时，用户很容易以为没有这个入口。
        """
        self._show_menu(self.mapToGlobal(event.pos()))
        event.accept()

    # ------------------------------------------------------------------ 内部
    def _show_menu(self, pos=None) -> None:
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        menu.setWindowFlags(
            menu.windowFlags()
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        menu.addAction("重命名").triggered.connect(lambda: self.rename_requested.emit(self.session_id()))
        menu.addAction("导出为 Markdown").triggered.connect(
            lambda: self.export_requested.emit(self.session_id())
        )
        menu.addSeparator()
        menu.addAction("删除对话").triggered.connect(
            lambda: self.delete_requested.emit(self.session_id())
        )
        if pos is None:
            pos = self.action_button.mapToGlobal(self.action_button.rect().bottomLeft())
        menu.exec(pos)

    def _refresh_icon(self, *_args, active: bool | None = None) -> None:
        if active is None:
            active = self.property("active") is True
        color = theme_manager.color("text_primary" if active else "text_muted")
        self.icon_label.setPixmap(build_icon("message", color, 32).pixmap(16, 16))

    @staticmethod
    def _polish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)
