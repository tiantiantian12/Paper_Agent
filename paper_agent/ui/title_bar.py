"""自定义标题栏（替代系统原生标题栏）。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QWidget

from paper_agent.core.signals import signals
from paper_agent.core.theme import theme_manager
from paper_agent.resources.icons import build_icon
from paper_agent.ui.widgets.icon_button import IconToolButton


class WindowButton(IconToolButton):
    """标题栏右上角窗口控制按钮。"""

    def __init__(
        self,
        icon_name: str,
        tooltip: str = "",
        danger: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            icon_name,
            tooltip=tooltip,
            color_token="text_secondary",
            icon_size=16,
            object_name="windowButton",
            parent=parent,
        )
        self._danger = danger
        if danger:
            self.setProperty("danger", True)

    def enterEvent(self, event):  # noqa: N802
        if self._danger:
            self.set_color_token("text_inverse")
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        if self._danger:
            self.set_color_token("text_secondary")
        super().leaveEvent(event)


class TitleBar(QWidget):
    """应用顶部导航栏。"""

    toggle_sidebar_requested = Signal()
    toggle_outline_requested = Signal()
    theme_toggle_requested = Signal()
    settings_requested = Signal()
    minimize_requested = Signal()
    maximize_requested = Signal()
    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("titleBar")
        self.setFixedHeight(40)

        root = QHBoxLayout(self)
        root.setContentsMargins(8, 0, 4, 0)
        root.setSpacing(4)

        # ---------------- 左侧：侧栏开关 + 品牌
        self.sidebar_button = IconToolButton(
            "panel-left", "会话列表 (Ctrl+B)", object_name="windowButton", parent=self
        )
        self.sidebar_button.clicked.connect(self.toggle_sidebar_requested)
        root.addWidget(self.sidebar_button)

        self.brand_icon = QLabel(self)
        self.brand_icon.setFixedSize(18, 18)
        root.addWidget(self.brand_icon)

        self.brand_label = QLabel("Paper Agent", self)
        self.brand_label.setObjectName("titleBarTitle")
        root.addWidget(self.brand_label)

        separator = QFrame(self)
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFixedWidth(1)
        separator.setStyleSheet("background: %s;" % theme_manager.color("border"))
        self._separator = separator
        root.addWidget(separator)

        self.session_label = QLabel("", self)
        self.session_label.setObjectName("titleBarSubtitle")
        self.session_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        root.addWidget(self.session_label, 1)

        # ---------------- 右侧：工具 + 窗口控制
        self.theme_button = IconToolButton(
            "moon", "切换深色 / 浅色", object_name="windowButton", parent=self
        )
        self.theme_button.clicked.connect(self._on_theme_clicked)
        root.addWidget(self.theme_button)

        self.outline_button = IconToolButton(
            "panel-right", "论文结构 (Ctrl+Shift+O)", object_name="windowButton", parent=self
        )
        self.outline_button.clicked.connect(self.toggle_outline_requested)
        root.addWidget(self.outline_button)

        self.settings_button = IconToolButton(
            "settings", "设置", object_name="windowButton", parent=self
        )
        self.settings_button.clicked.connect(self.settings_requested)
        root.addWidget(self.settings_button)

        root.addSpacing(6)

        self.minimize_button = WindowButton("win-minimize", "最小化", parent=self)
        self.minimize_button.clicked.connect(self.minimize_requested)
        root.addWidget(self.minimize_button)

        self.maximize_button = WindowButton("win-maximize", "最大化", parent=self)
        self.maximize_button.clicked.connect(self.maximize_requested)
        root.addWidget(self.maximize_button)

        self.close_button = WindowButton("win-close", "关闭", danger=True, parent=self)
        self.close_button.clicked.connect(self.close_requested)
        root.addWidget(self.close_button)

        self._refresh_theme_assets()
        signals.theme_changed.connect(self._refresh_theme_assets)

    # ------------------------------------------------------------------ 接口
    def set_session_title(self, title: str) -> None:
        self.session_label.setText(title)

    def set_maximized(self, maximized: bool) -> None:
        icon = "win-restore" if maximized else "win-maximize"
        tip = "向下还原" if maximized else "最大化"
        self.maximize_button.set_icon_name(icon)
        self.maximize_button.setToolTip(tip)
        self.setProperty("maximized", maximized)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_sidebar_active(self, active: bool) -> None:
        self.sidebar_button.set_icon_name("panel-left")
        self.sidebar_button.setDown(active)

    # ------------------------------------------------------------------ 内部
    def _on_theme_clicked(self) -> None:
        # 交给主窗口处理：它会把主题写进配置，下次启动才能沿用
        self.theme_toggle_requested.emit()

    def _refresh_theme_assets(self, *_args) -> None:
        accent = theme_manager.color("accent")
        self.brand_icon.setPixmap(build_icon("sparkles", accent, 36).pixmap(18, 18))
        self.theme_button.set_icon_name("sun" if theme_manager.is_dark else "moon")
        self.theme_button.setToolTip("切换为浅色" if theme_manager.is_dark else "切换为深色")
        self._separator.setStyleSheet("background: %s;" % theme_manager.color("border"))
