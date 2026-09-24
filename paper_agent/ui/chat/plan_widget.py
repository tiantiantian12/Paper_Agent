"""任务计划 / 待办列表（Plan-and-Execute 展示）。"""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from paper_agent.core.signals import signals
from paper_agent.core.theme import theme_manager
from paper_agent.resources.icons import build_icon

STATUS_STYLE = {
    "pending": ("chevron-right", "text_muted"),
    "doing": ("sparkles", "warning"),
    "done": ("check", "success"),
    "failed": ("close", "danger"),
}


class PlanWidget(QFrame):
    """计划步骤列表，按状态着色。"""

    def __init__(self, steps: list[dict] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("planWidget")
        self.setFrameShape(QFrame.Shape.NoFrame)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 6, 10, 8)
        root.setSpacing(4)

        self.title_label = QLabel("执行计划")
        self.title_label.setObjectName("roleName")
        root.addWidget(self.title_label)

        self.list_host = QWidget(self)
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 2, 0, 0)
        self.list_layout.setSpacing(4)
        root.addWidget(self.list_host)

        self._rows: list[tuple[QLabel, QLabel, str]] = []
        self.set_steps(steps or [])
        signals.theme_changed.connect(self._refresh_rows)

    # ------------------------------------------------------------------ 数据
    def set_steps(self, steps: list[dict]) -> None:
        """重建列表。每项含 title / status / detail。"""
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows.clear()

        for index, step in enumerate(steps):
            self._add_row(index, step)

        self.setVisible(bool(steps))

    def update_step(self, index: int, status: str) -> None:
        """更新某一步的状态。"""
        if 0 <= index < len(self._rows):
            icon, text, _previous = self._rows[index]
            self._rows[index] = (icon, text, status)
            self._paint_row(index)

    # ------------------------------------------------------------------ 内部
    def _add_row(self, index: int, step: dict) -> None:
        title = str(step.get("title", "")).strip() or f"步骤 {index + 1}"
        detail = str(step.get("detail", "")).strip()
        status = str(step.get("status", "pending"))

        row = QWidget(self.list_host)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        icon = QLabel(row)
        icon.setFixedSize(14, 14)
        layout.addWidget(icon, 0)

        text = QLabel(f"{index + 1}. {title}" + (f" — {detail}" if detail else ""))
        text.setObjectName("chipName")
        text.setWordWrap(True)
        layout.addWidget(text, 1)

        self.list_layout.addWidget(row)
        self._rows.append((icon, text, status))
        self._paint_row(index)

    def _paint_row(self, index: int) -> None:
        icon, text, status = self._rows[index]
        icon_name, color_token = STATUS_STYLE.get(status, STATUS_STYLE["pending"])
        color = theme_manager.color(color_token)
        icon.setPixmap(build_icon(icon_name, color, 28).pixmap(13, 13))
        text.setStyleSheet(f"color: {color};")

    def _refresh_rows(self, *_args) -> None:
        """主题切换后按新配色重绘（图标与文字色都是动态生成的）。"""
        for index in range(len(self._rows)):
            self._paint_row(index)
