"""模型 / 推理强度下拉选择按钮（Codex 风格的下拉菜单）。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)


from paper_agent.core.config import CHAT_MODES, DEFAULT_MODELS, REASONING_EFFORTS
from paper_agent.core.signals import signals
from paper_agent.core.theme import theme_manager
from paper_agent.resources.icons import build_icon


def _styled_menu(parent: QWidget, object_name: str = "") -> QMenu:
    """创建无原生阴影、可由 QSS 控制圆角的菜单。"""
    menu = QMenu(parent)
    if object_name:
        menu.setObjectName(object_name)
    menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    menu.setWindowFlags(
        menu.windowFlags() | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint
    )
    return menu


class MenuButton(QToolButton):
    """通用下拉按钮：图标 + 文本。"""

    # 按钮上文字最多占这么宽，超出用省略号。
    # 模型名 / 描述可能很长，不截断的话按钮的最小宽度会把整个输入工具条顶宽，
    # 聊天区就再也压不下去（拖动结构面板时只能去挤左侧栏）。
    MAX_TEXT_WIDTH = 150

    def __init__(
        self,
        text: str = "",
        icon_name: str = "",
        object_name: str = "modelButton",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self._icon_name = icon_name
        self._display_text = text
        self._display_tooltip = ""
        self._text_width = self.MAX_TEXT_WIDTH
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setAutoRaise(True)
        self.setProperty("menu_visible", False)

        menu = _styled_menu(self)
        self.setMenu(menu)
        menu.aboutToShow.connect(self._on_menu_show)
        menu.aboutToHide.connect(self._on_menu_hide)

        self._refresh_icon()
        signals.theme_changed.connect(self._refresh_icon)

    # ------------------------------------------------------------------
    @property
    def styled_menu(self) -> QMenu:
        menu = self.menu()
        assert menu is not None
        return menu

    def _on_menu_show(self) -> None:
        self.setProperty("menu_visible", True)
        self._polish()

    def _on_menu_hide(self) -> None:
        self.setProperty("menu_visible", False)
        self._polish()

    def _polish(self) -> None:
        self.style().unpolish(self)
        self.style().polish(self)

    def _refresh_icon(self, *_args) -> None:
        if not self._icon_name:
            return
        color = theme_manager.color("text_secondary")
        self.setIcon(build_icon(self._icon_name, color, 32))
        self.setIconSize(QSize(16, 16))

    def set_text_width(self, width: int) -> None:
        """改按钮文字的省略宽度（窄栏下切紧凑宽度，别把工具条顶宽）。"""
        width = max(24, int(width))
        if width == self._text_width:
            return
        self._text_width = width
        self._sync_display_text()

    def set_text_visible(self, visible: bool) -> None:
        """窄栏下只留图标（标题在 tooltip 与菜单里，不丢信息）。"""
        style = (
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            if visible
            else Qt.ToolButtonStyle.ToolButtonIconOnly
        )
        if self.toolButtonStyle() != style:
            self.setToolButtonStyle(style)

    def set_icon_name(self, name: str) -> None:
        """换图标（例如按工作模式在「书」和「终端」之间切换）。"""
        if name == self._icon_name:
            return
        self._icon_name = name
        self._refresh_icon()

    def set_display_text(self, text: str, tooltip: str = "") -> None:
        """按钮上显示截断后的文本，完整内容放进提示 / 菜单里。"""
        self._display_text = text
        self._display_tooltip = tooltip
        self._sync_display_text()

    def _sync_display_text(self) -> None:
        text = self._display_text
        self.setText(
            self.fontMetrics().elidedText(
                text, Qt.TextElideMode.ElideRight, self._text_width
            )
        )
        self.setToolTip(self._display_tooltip or text)


class _ModelItemWidget(QWidget):
    """模型菜单项：名称 + 描述 + 选中标记。"""

    def __init__(self, name: str, desc: str, checked: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("modelItem")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setProperty("hover", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(268)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)

        self._check = QLabel(self)
        self._check.setObjectName("modelItemCheck")
        self._check.setFixedWidth(14)
        self._check.setPixmap(
            build_icon("check", theme_manager.color("text_primary"), 28).pixmap(14, 14)
        )
        self._check.setVisible(checked)
        layout.addWidget(self._check, 0, Qt.AlignmentFlag.AlignTop)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        self._name = QLabel(name)
        self._name.setObjectName("modelItemName")
        self._desc = QLabel(desc)
        self._desc.setObjectName("modelItemDesc")
        self._desc.setWordWrap(True)
        text_layout.addWidget(self._name)
        text_layout.addWidget(self._desc)
        layout.addLayout(text_layout, 1)

    def set_checked(self, checked: bool) -> None:
        self._check.setVisible(checked)

    # ------------------------------------------------------------------ 事件
    def enterEvent(self, event) -> None:  # noqa: N802
        self.setProperty("hover", True)
        self._polish()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.setProperty("hover", False)
        self._polish()
        super().leaveEvent(event)

    def _polish(self) -> None:
        self.style().unpolish(self)
        self.style().polish(self)


class ModelMenuButton(MenuButton):
    """模型选择下拉框：内置模型 + 自定义模型（分组展示）。"""

    model_changed = Signal(str)
    manage_requested = Signal()

    def __init__(
        self,
        models: list[dict] | None = None,
        current: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(icon_name="sparkles", object_name="modelButton", parent=parent)
        self._models = models or [dict(m, custom=False) for m in DEFAULT_MODELS]
        self._current = self._resolve(current)
        self._items: dict[str, _ModelItemWidget] = {}

        self._build_menu()
        # 按钮上的名字会按宽度截断，完整名称交给 _sync_text 放进 tooltip
        self._sync_text()

    # ------------------------------------------------------------------
    @property
    def current_model(self) -> str:
        return self._current

    def _resolve(self, model_id: str) -> str:
        """把要选中的模型收敛成列表里真实存在的 id。

        自定义模型被删掉后，配置 / 历史会话里可能还留着那个本地 id；不收敛的话
        按钮上会直接显示一串 id（``_sync_text`` 的兜底分支），点开菜单也对不上号。
        """
        if any(m["id"] == model_id for m in self._models):
            return model_id
        return self._models[0]["id"] if self._models else ""

    def reload(self, models: list[dict], current: str = "") -> None:
        """模型配置变更后重建菜单。"""
        self._models = models
        if current:
            self._current = self._resolve(current)
        else:
            self._current = self._resolve(self._current)
        self._items.clear()
        self.styled_menu.clear()
        self._build_menu()
        self._sync_text()

    def set_current(self, model_id: str) -> None:
        model_id = self._resolve(model_id)
        if model_id == self._current:
            return
        self._current = model_id
        for mid, item in self._items.items():
            item.set_checked(mid == model_id)
        self._sync_text()
        self.model_changed.emit(model_id)

    def _sync_text(self) -> None:
        name = next((m["name"] for m in self._models if m["id"] == self._current), "")
        self.set_display_text(name, f"当前模型：{name}（点击切换）")

    def _build_menu(self) -> None:
        menu = self.styled_menu
        menu.setObjectName("modelMenu")
        group = QActionGroup(menu)
        group.setExclusive(True)

        builtin = [m for m in self._models if not m.get("custom")]
        custom = [m for m in self._models if m.get("custom")]

        if builtin:
            self._add_section(menu, "内置模型")
            for model in builtin:
                self._add_model_action(menu, group, model)

        self._add_section(menu, "自定义模型")
        if custom:
            for model in custom:
                self._add_model_action(menu, group, model)
        else:
            placeholder = QAction("尚未添加自定义模型", menu)
            placeholder.setEnabled(False)
            menu.addAction(placeholder)

        menu.addSeparator()
        manage = QAction("管理自定义模型…", menu)
        manage.triggered.connect(self.manage_requested)
        menu.addAction(manage)

    def _add_model_action(self, menu: QMenu, group: QActionGroup, model: dict) -> None:
        item = _ModelItemWidget(model["name"], model["desc"], model["id"] == self._current)
        action = QWidgetAction(menu)
        action.setDefaultWidget(item)
        action.setCheckable(True)
        action.setChecked(model["id"] == self._current)
        action.setData(model["id"])
        action.setActionGroup(group)
        action.triggered.connect(self._on_action_triggered)
        menu.addAction(action)
        self._items[model["id"]] = item

    @staticmethod
    def _add_section(menu: QMenu, text: str) -> None:
        label = QLabel(text)
        label.setObjectName("sectionLabel")
        label.setContentsMargins(10, 6, 0, 4)
        action = QWidgetAction(menu)
        action.setDefaultWidget(label)
        menu.addAction(action)

    def _on_action_triggered(self) -> None:
        action = self.sender()
        if isinstance(action, QAction):
            self.set_current(str(action.data()))


class ModeMenuButton(MenuButton):
    """工作模式选择：文档（写论文/做 PPT）/ 编程（配环境/写代码/跑代码）。"""

    mode_changed = Signal(str)

    ICONS = {"doc": "book", "code": "terminal"}

    def __init__(self, current: str = "doc", parent: QWidget | None = None) -> None:
        super().__init__(
            icon_name=self.ICONS.get(current, "book"),
            object_name="modelButton",
            parent=parent,
        )
        self._current = current
        self._current_name = ""

        menu = self.styled_menu
        group = QActionGroup(menu)
        group.setExclusive(True)
        for mode in CHAT_MODES:
            action = QAction(f"{mode['name']}模式 · {mode['desc']}", menu)
            action.setCheckable(True)
            action.setChecked(mode["id"] == current)
            action.setData(mode["id"])
            action.setActionGroup(group)
            action.triggered.connect(self._on_action_triggered)
            menu.addAction(action)
            if mode["id"] == current:
                self._current_name = mode["name"]

        self._sync_text()

    # ------------------------------------------------------------------
    @property
    def current_mode(self) -> str:
        return self._current

    def set_current(self, mode: str) -> None:
        """外部设置当前模式（切会话时用）：不发 mode_changed，只更新显示。"""
        if mode == self._current:
            return
        self._current = mode
        self._current_name = next(
            (item["name"] for item in CHAT_MODES if item["id"] == mode), mode
        )
        for action in self.styled_menu.actions():
            if action.data() is not None:
                action.setChecked(str(action.data()) == mode)
        self._sync_text()

    def _sync_text(self) -> None:
        name = self._current_name or self._current
        self.set_display_text(f"{name}模式", f"工作模式：{name}（点击切换）")
        self.set_icon_name(self.ICONS.get(self._current, "book"))

    def _on_action_triggered(self) -> None:
        action = self.sender()
        if not isinstance(action, QAction):
            return
        self._current = str(action.data())
        self._current_name = action.text().split("模式")[0]
        self._sync_text()
        self.mode_changed.emit(self._current)


class EffortMenuButton(MenuButton):
    """推理强度选择（低 / 中 / 高）。"""

    effort_changed = Signal(str)

    def __init__(self, current: str = "medium", parent: QWidget | None = None) -> None:
        super().__init__(icon_name="terminal", object_name="modelButton", parent=parent)
        self._current = current
        self._current_name = ""

        menu = self.styled_menu
        group = QActionGroup(menu)
        group.setExclusive(True)
        for effort in REASONING_EFFORTS:
            action = QAction(f"{effort['name']} · {effort['desc']}", menu)
            action.setCheckable(True)
            action.setChecked(effort["id"] == current)
            action.setData(effort["id"])
            action.setActionGroup(group)
            action.triggered.connect(self._on_action_triggered)
            menu.addAction(action)
            if effort["id"] == current:
                self._current_name = effort["name"]

        self._sync_text()
        self.setToolTip("推理强度")

    # ------------------------------------------------------------------
    @property
    def current_effort(self) -> str:
        return self._current

    def _sync_text(self) -> None:
        name = self._current_name or self._current
        self.set_display_text(f"推理：{name}", f"推理强度：{name}（点击切换）")

    def _on_action_triggered(self) -> None:
        action = self.sender()
        if not isinstance(action, QAction):
            return
        self._current = str(action.data())
        self._current_name = action.text().split(" · ")[0]
        self._sync_text()
        self.effort_changed.emit(self._current)
