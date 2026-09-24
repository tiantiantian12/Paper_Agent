"""左侧会话面板：新建对话、搜索、会话列表、底部设置入口。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from paper_agent.core.models import ChatSession
from paper_agent.core.signals import signals
from paper_agent.core.theme import theme_manager
from paper_agent.resources.icons import build_icon
from paper_agent.ui.sidebar.session_item import SessionItemWidget
from paper_agent.ui.widgets.icon_button import IconToolButton
from paper_agent.utils.avatar import avatar_pixmap
from paper_agent.utils.text import time_group


class Sidebar(QWidget):
    """会话侧栏。"""

    new_session_requested = Signal()
    session_selected = Signal(str)
    session_rename_requested = Signal(str, str)  # session id, new title
    session_delete_requested = Signal(str)
    session_export_requested = Signal(str)
    theme_toggle_requested = Signal()
    collapse_requested = Signal()
    paper_space_requested = Signal()             # 打开「我的论文空间」
    account_requested = Signal()                 # 打开「账号资料」
    aigc_requested = Signal()                    # 打开「AIGC 检测」

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sidebar")
        self._sessions: list[ChatSession] = []
        self._active_id: str = ""
        self._keyword: str = ""
        self._account_name = ""
        self._avatar_spec = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 8)
        root.setSpacing(8)

        # ------------------------------ 品牌区
        brand_row = QHBoxLayout()
        brand_row.setSpacing(6)
        self.brand_icon = QLabel(self)
        self.brand_icon.setFixedSize(20, 20)
        brand_row.addWidget(self.brand_icon)

        self.brand_label = QLabel("Paper Agent", self)
        self.brand_label.setObjectName("sidebarBrand")
        brand_row.addWidget(self.brand_label)
        brand_row.addStretch(1)

        self.collapse_button = IconToolButton(
            "panel-left", "收起会话列表 (Ctrl+B)", object_name="sessionAction", parent=self
        )
        self.collapse_button.clicked.connect(self.collapse_requested)
        brand_row.addWidget(self.collapse_button)
        root.addLayout(brand_row)

        # ------------------------------ 新建对话
        self.new_button = QPushButton("  新建论文对话", self)
        self.new_button.setObjectName("primaryButton")
        self.new_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_button.clicked.connect(self.new_session_requested)
        root.addWidget(self.new_button)

        # ------------------------------ 搜索
        self.search_container = QWidget(self)
        self.search_container.setObjectName("searchContainer")
        search_layout = QHBoxLayout(self.search_container)
        search_layout.setContentsMargins(6, 0, 4, 0)
        search_layout.setSpacing(4)

        self.search_icon = QLabel(self.search_container)
        self.search_icon.setFixedSize(16, 16)
        search_layout.addWidget(self.search_icon)

        self.search_edit = QLineEdit(self.search_container)
        self.search_edit.setObjectName("searchEdit")
        self.search_edit.setPlaceholderText("搜索对话")
        self.search_edit.textChanged.connect(self._on_search_changed)
        search_layout.addWidget(self.search_edit, 1)

        self.clear_button = IconToolButton(
            "close", "清除", object_name="sessionAction", icon_size=14, parent=self.search_container
        )
        self.clear_button.clicked.connect(self.search_edit.clear)
        self.clear_button.setVisible(False)
        search_layout.addWidget(self.clear_button)
        root.addWidget(self.search_container)

        # ------------------------------ 会话列表
        self.session_list = QListWidget(self)
        self.session_list.setObjectName("sessionList")
        self.session_list.setSpacing(0)
        self.session_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.session_list.setFrameShape(QFrame.Shape.NoFrame)
        self.session_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.session_list.currentItemChanged.connect(self._on_current_item_changed)
        root.addWidget(self.session_list, 1)

        # ------------------------------ 空态
        self.empty_label = QLabel("还没有对话记录\n点击上方按钮开始写作", self)
        self.empty_label.setObjectName("footerLabel")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setVisible(False)
        root.addWidget(self.empty_label)

        # ------------------------------ 底部
        footer = QWidget(self)
        footer.setObjectName("sidebarFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(4, 8, 4, 4)
        footer_layout.setSpacing(4)

        self.theme_button = IconToolButton(
            "moon", "切换深色 / 浅色", object_name="footerButton", parent=footer
        )
        self.theme_button.clicked.connect(self.theme_toggle_requested)
        footer_layout.addWidget(self.theme_button)

        self.settings_button = IconToolButton(
            "settings", "设置", object_name="footerButton", parent=footer
        )
        footer_layout.addWidget(self.settings_button)

        self.space_button = IconToolButton(
            "book", "我的论文空间", object_name="footerButton", parent=footer
        )
        self.space_button.setToolTip("查看本对话中上传与生成的文件")
        self.space_button.clicked.connect(self.paper_space_requested)
        footer_layout.addWidget(self.space_button)

        self.aigc_button = IconToolButton(
            "shield-check", "AIGC 检测", object_name="footerButton", parent=footer
        )
        self.aigc_button.setToolTip("检测论文里的 AI 生成内容并导出标注 PDF")
        self.aigc_button.clicked.connect(self.aigc_requested)
        footer_layout.addWidget(self.aigc_button)

        footer_layout.addStretch(1)

        # ------------------------------ 账号：头像 + 昵称
        self.avatar_button = QPushButton(footer)
        self.avatar_button.setObjectName("avatarButton")
        self.avatar_button.setFixedSize(26, 26)
        self.avatar_button.setIconSize(QSize(26, 26))
        self.avatar_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.avatar_button.setToolTip("账号资料")
        self.avatar_button.clicked.connect(self.account_requested)
        footer_layout.addWidget(self.avatar_button)

        self.name_button = QPushButton("未登录", footer)
        self.name_button.setObjectName("accountNameButton")
        self.name_button.setMaximumWidth(112)
        self.name_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.name_button.setToolTip("账号资料")
        self.name_button.clicked.connect(self.account_requested)
        footer_layout.addWidget(self.name_button)
        root.addWidget(footer)

        self._refresh_assets()
        signals.theme_changed.connect(self._refresh_assets)

    # ------------------------------------------------------------------ 数据
    def set_sessions(self, sessions: list[ChatSession], active_id: str = "") -> None:
        self._sessions = list(sessions)
        if active_id:
            self._active_id = active_id
        self._rebuild()

    def set_active(self, session_id: str) -> None:
        self._active_id = session_id
        for index in range(self.session_list.count()):
            item = self.session_list.item(index)
            widget = self.session_list.itemWidget(item)
            if isinstance(widget, SessionItemWidget):
                widget.set_active(widget.session_id() == session_id)

    def active_id(self) -> str:
        return self._active_id

    # ------------------------------------------------------------------ 账号
    def set_account(self, name: str, avatar_spec: str = "") -> None:
        """更新底部账号区：头像 + 昵称（未登录显示「未登录」）。"""
        self._account_name = name or ""
        if avatar_spec:
            self._avatar_spec = avatar_spec
        self.name_button.setText(self._account_name or "未登录")
        self.name_button.setToolTip(self._account_name or "点击登录 / 注册")
        self._refresh_avatar()

    def _refresh_avatar(self) -> None:
        self.avatar_button.setIcon(QIcon(avatar_pixmap(self._avatar_spec, 26)))

    # ------------------------------------------------------------------ 内部
    def _on_search_changed(self, text: str) -> None:
        self._keyword = text.strip().lower()
        self.clear_button.setVisible(bool(self._keyword))
        self._rebuild()

    def _filtered_sessions(self) -> list[ChatSession]:
        if not self._keyword:
            return list(self._sessions)
        keyword = self._keyword
        result = []
        for session in self._sessions:
            haystack = session.title + " " + " ".join(m.content for m in session.messages)
            if keyword in haystack.lower():
                result.append(session)
        return result

    def _rebuild(self) -> None:
        self.session_list.blockSignals(True)
        self.session_list.clear()

        sessions = self._filtered_sessions()
        self.empty_label.setVisible(not sessions)
        self.session_list.setVisible(bool(sessions))

        groups: dict[str, list[ChatSession]] = {}
        order: list[str] = []
        for session in sessions:
            group = time_group(session.updated_at)
            if group not in groups:
                groups[group] = []
                order.append(group)
            groups[group].append(session)

        for group in order:
            header_item = QListWidgetItem(self.session_list)
            header_item.setFlags(Qt.ItemFlag.NoItemFlags)
            header_label = QLabel(group)
            header_label.setObjectName("sectionLabel")
            header_label.setContentsMargins(8, 6, 0, 2)
            header_item.setSizeHint(header_label.sizeHint())
            self.session_list.addItem(header_item)
            self.session_list.setItemWidget(header_item, header_label)

            for session in groups[group]:
                item = QListWidgetItem(self.session_list)
                widget = SessionItemWidget(session, self.session_list)
                widget.update_session(session, session.id == self._active_id)
                widget.rename_requested.connect(self._on_rename)
                widget.delete_requested.connect(self.session_delete_requested)
                widget.export_requested.connect(self.session_export_requested)
                item.setSizeHint(widget.sizeHint())
                item.setData(Qt.ItemDataRole.UserRole, session.id)
                self.session_list.addItem(item)
                self.session_list.setItemWidget(item, widget)

        self.session_list.blockSignals(False)

    def _on_current_item_changed(self, current: QListWidgetItem | None, _previous) -> None:
        if current is None:
            return
        session_id = current.data(Qt.ItemDataRole.UserRole)
        if session_id:
            self.session_selected.emit(str(session_id))

    def _on_rename(self, session_id: str) -> None:
        session = next((s for s in self._sessions if s.id == session_id), None)
        if session is None:
            return
        title, ok = QInputDialog.getText(self, "重命名对话", "对话名称：", text=session.title)
        if ok and title.strip():
            self.session_rename_requested.emit(session_id, title.strip())

    # ------------------------------------------------------------------ 主题
    def _refresh_assets(self, *_args) -> None:
        accent = theme_manager.color("accent")
        muted = theme_manager.color("text_muted")
        self.brand_icon.setPixmap(build_icon("sparkles", accent, 40).pixmap(20, 20))
        self.search_icon.setPixmap(build_icon("search", muted, 32).pixmap(16, 16))
        self._refresh_avatar()
        self.theme_button.set_icon_name("sun" if theme_manager.is_dark else "moon")
        self.theme_button.setToolTip("切换为浅色" if theme_manager.is_dark else "切换为深色")

        self.new_button.setIcon(build_icon("plus", theme_manager.color("accent_text"), 36))
        self.new_button.setIconSize(QSize(18, 18))

        self.collapse_button.set_icon_name("panel-left")
        self.collapse_button.setToolTip("收起会话列表")
