"""带 SVG 图标的工具按钮，随主题自动换色。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QToolButton

from paper_agent.core.signals import signals
from paper_agent.core.theme import theme_manager
from paper_agent.resources.icons import build_icon


class IconToolButton(QToolButton):
    """图标按钮。

    Args:
        icon_name: ``resources.icons`` 中的图标名
        tooltip: 悬浮提示
        color_token: 图标颜色对应的主题 token 名
        icon_size: 图标像素尺寸
        object_name: QSS 选择器名称
    """

    clicked_with_pos = Signal(object)

    def __init__(
        self,
        icon_name: str,
        tooltip: str = "",
        color_token: str = "text_secondary",
        icon_size: int = 18,
        object_name: str = "composerButton",
        parent=None,
        stroke_width: float = 1.75,
    ) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._color_token = color_token
        self._icon_size = icon_size
        self._stroke_width = stroke_width

        self.setObjectName(object_name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setIconSize(QSize(icon_size, icon_size))
        if tooltip:
            self.setToolTip(tooltip)
        self.setAutoRaise(True)

        self._refresh_icon()
        signals.theme_changed.connect(self._refresh_icon)

    # ------------------------------------------------------------------ 接口
    def set_icon_name(self, name: str) -> None:
        self._icon_name = name
        self._refresh_icon()

    def set_color_token(self, token: str) -> None:
        self._color_token = token
        self._refresh_icon()

    def icon_name(self) -> str:
        return self._icon_name

    # ------------------------------------------------------------------ 内部
    def _refresh_icon(self, *_args) -> None:
        color = theme_manager.color(self._color_token)
        # 以 2 倍尺寸渲染再交给 QIcon 缩放，保证高分屏下线条清晰
        self.setIcon(
            build_icon(self._icon_name, color, self._icon_size * 2, self._stroke_width)
        )
        self.setIconSize(QSize(self._icon_size, self._icon_size))
