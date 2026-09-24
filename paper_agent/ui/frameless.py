"""无边框窗口支持：拖动、八向缩放、最大化管理与 Win11 圆角。

用法：窗口类继承 ``FramelessMixin`` 并设置 ``Qt.FramelessWindowHint`` 后，
在 ``showEvent`` 中调用 :meth:`apply_window_effects`。
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication, QScreen
from PySide6.QtWidgets import QWidget

from paper_agent.core.constants import RESIZE_BORDER


def enable_windows_round_corner(window: QWidget) -> None:
    """Windows 11 下为无边框窗口启用系统圆角。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = int(window.winId())
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        preference = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(preference),
            ctypes.sizeof(preference),
        )
    except Exception:  # pragma: no cover - 旧系统不支持
        pass


class FramelessMixin:
    """为无边框窗口提供系统级拖动 / 缩放能力。"""

    # 需要宿主窗口提供（或由子类覆盖）
    _maximized: bool = False
    _normal_geometry = None

    # ------------------------------------------------------------------ 状态
    @property
    def is_maximized(self) -> bool:
        return bool(self._maximized)

    def title_bar_height(self) -> int:
        """可拖动区域高度（子类通常返回标题栏高度）。"""
        return 40

    # ------------------------------------------------------------------ 效果
    def apply_window_effects(self) -> None:
        enable_windows_round_corner(self)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ 事件
    def mousePressEvent(self, event):  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        handle = self.windowHandle()
        pos = event.position().toPoint()
        if handle is None:
            super().mousePressEvent(event)
            return

        edges = self._edges_at(pos)
        if edges and not self._maximized:
            handle.startSystemResize(edges)
            return
        if pos.y() <= self.title_bar_height() and not self._maximized:
            handle.startSystemMove()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and event.position().toPoint().y() <= self.title_bar_height():
            self.toggle_maximized()
            return
        super().mouseDoubleClickEvent(event)

    # ------------------------------------------------------------------ 窗口控制
    def toggle_maximized(self) -> None:
        if self._maximized:
            self.restore_window()
        else:
            self.maximize_window()

    def maximize_window(self) -> None:
        if self._maximized:
            return
        self._normal_geometry = self.geometry()  # type: ignore[attr-defined]
        screen = self._current_screen()
        if screen is not None:
            self.setGeometry(screen.availableGeometry())  # type: ignore[attr-defined]
        self._maximized = True
        self._update_maximized_state()

    def restore_window(self) -> None:
        if not self._maximized:
            return
        if self._normal_geometry is not None:
            self.setGeometry(self._normal_geometry)  # type: ignore[attr-defined]
        self._maximized = False
        self._update_maximized_state()

    def _update_maximized_state(self) -> None:
        """切换容器中控属性，供 QSS 去掉最大化时的圆角与边框。"""
        container = getattr(self, "container", None)
        if container is None:
            return
        container.setProperty("maximized", self._maximized)
        container.style().unpolish(container)
        container.style().polish(container)

    def _current_screen(self) -> QScreen | None:
        return self.screen() or QGuiApplication.primaryScreen()  # type: ignore[attr-defined]

    def _edges_at(self, pos: QPoint) -> Qt.Edges:
        """判断鼠标位于窗口的哪个可缩放边缘。"""
        edges = Qt.Edges()
        width = self.width()  # type: ignore[attr-defined]
        height = self.height()  # type: ignore[attr-defined]
        if pos.x() <= RESIZE_BORDER:
            edges |= Qt.Edge.LeftEdge
        if pos.x() >= width - RESIZE_BORDER:
            edges |= Qt.Edge.RightEdge
        if pos.y() <= RESIZE_BORDER:
            edges |= Qt.Edge.TopEdge
        if pos.y() >= height - RESIZE_BORDER:
            edges |= Qt.Edge.BottomEdge
        return edges
