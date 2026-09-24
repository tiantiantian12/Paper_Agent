"""视频参数下拉按钮：时长 / 画幅。

只有选中「文生视频」模型时才显示（由 :meth:`Composer.set_video_mode` 控制）；
主窗口发送时直接读 :attr:`params`，不需要额外的信号。
"""

from __future__ import annotations

from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QLabel, QMenu, QWidgetAction

from paper_agent.services.video_client import (
    ASPECT_OPTIONS,
    DEFAULT_ASPECT,
    DEFAULT_SECONDS,
    SECOND_OPTIONS,
    VIDEO_SIZE,
)
from paper_agent.ui.widgets.model_combo import MenuButton


class VideoOptionsButton(MenuButton):
    """视频参数选择：时长（4–12 秒）+ 画幅（分辨率固定 720P）。"""

    def __init__(
        self,
        seconds: str = DEFAULT_SECONDS,
        aspect_ratio: str = DEFAULT_ASPECT,
        parent=None,
    ) -> None:
        super().__init__(icon_name="video", object_name="modelButton", parent=parent)
        self._seconds = seconds if seconds in SECOND_OPTIONS else DEFAULT_SECONDS
        self._aspect = aspect_ratio if aspect_ratio in ASPECT_OPTIONS else DEFAULT_ASPECT
        self._build_menu()
        self._sync_text()
        self.setToolTip("视频参数")

    # ------------------------------------------------------------------
    @property
    def params(self) -> dict:
        """发给服务端的视频参数。"""
        return {"seconds": self._seconds, "aspect_ratio": self._aspect, "size": VIDEO_SIZE}

    def set_video_mode(self, visible: bool) -> None:
        """只在选中视频模型时露出来（文本一起收起，免得挤满窄工具条）。"""
        self.setVisible(visible)
        self.set_text_visible(visible)

    # ------------------------------------------------------------------ 菜单
    def _build_menu(self) -> None:
        menu = self.styled_menu
        menu.clear()
        menu.setObjectName("modelMenu")

        self._add_section(menu, "时长")
        seconds_group = QActionGroup(menu)
        seconds_group.setExclusive(True)
        for value in SECOND_OPTIONS:
            self._add_choice(menu, seconds_group, "seconds", value, f"{value} 秒",
                             value == self._seconds)

        self._add_section(menu, f"画幅（分辨率固定 {VIDEO_SIZE}）")
        aspect_group = QActionGroup(menu)
        aspect_group.setExclusive(True)
        for value in ASPECT_OPTIONS:
            self._add_choice(menu, aspect_group, "aspect", value, value, value == self._aspect)

    def _add_choice(
        self,
        menu: QMenu,
        group: QActionGroup,
        kind: str,
        value: str,
        label: str,
        checked: bool,
    ) -> None:
        action = QAction(label, menu)
        action.setCheckable(True)
        action.setChecked(checked)
        action.setData((kind, value))
        action.setActionGroup(group)
        action.triggered.connect(self._on_action_triggered)
        menu.addAction(action)

    def _on_action_triggered(self) -> None:
        action = self.sender()
        if not isinstance(action, QAction):
            return
        kind, value = action.data()
        if kind == "seconds":
            self._seconds = value
        else:
            self._aspect = value
        self._sync_text()

    @staticmethod
    def _add_section(menu: QMenu, text: str) -> None:
        label = QLabel(text)
        label.setObjectName("sectionLabel")
        label.setContentsMargins(10, 6, 0, 4)
        action = QWidgetAction(menu)
        action.setDefaultWidget(label)
        menu.addAction(action)

    # ------------------------------------------------------------------ 显示
    def _sync_text(self) -> None:
        self.set_display_text(
            f"{self._seconds} 秒 · {self._aspect}",
            f"视频参数：{self._seconds} 秒 / {self._aspect} / {VIDEO_SIZE}（点击修改）",
        )
