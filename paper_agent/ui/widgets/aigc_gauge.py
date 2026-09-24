"""AIGC 率环形仪表：把百分数画成一圈进度环。"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from paper_agent.core.theme import theme_manager

# 风险等级 → 环的颜色 token
_RISK_COLORS = {
    "low": "success",
    "medium": "warning",
    "high": "danger",
    "critical": "danger",
}

ARC_START = 135.0        # 起始角（Qt 单位为 1/16 度，这里先按角度算再换算）
ARC_SPAN = 270.0         # 环的总跨度：留下方 90° 做缺口
PEN_WIDTH_RATIO = 0.09   # 环宽相对控件尺寸


class AigcGauge(QWidget):
    """显示一个 0~100 的百分比，环的颜色随风险等级变化。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("aigcGauge")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setMinimumSize(140, 140)
        self._value = 0.0
        self._risk = "low"
        self._caption = "AIGC 率"

    # ------------------------------------------------------------------ 接口
    def set_value(self, value: float, risk: str = "low", caption: str = "AIGC 率") -> None:
        self._value = max(0.0, min(float(value), 100.0))
        self._risk = risk
        self._caption = caption
        self.update()

    # ------------------------------------------------------------------ 绘制
    def sizeHint(self):  # noqa: N802
        return self.minimumSizeHint()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        side = min(self.width(), self.height())
        width = max(side * PEN_WIDTH_RATIO, 6)
        margin = width / 2 + 2
        rect = QRectF(margin, margin, side - 2 * margin, side - 2 * margin)

        # 底环
        track = QPen(QColor(theme_manager.color("bg_hover_strong")), width)
        track.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track)
        painter.drawArc(rect, int(ARC_START * 16), int(ARC_SPAN * 16))

        # 进度环
        if self._value > 0:
            token = _RISK_COLORS.get(self._risk, "accent")
            progress = QPen(QColor(theme_manager.color(token)), width)
            progress.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(progress)
            painter.drawArc(rect, int(ARC_START * 16), -int(self._value / 100 * ARC_SPAN * 16))

        # 中心数值
        painter.setPen(QColor(theme_manager.color("text_primary")))
        font = QFont(theme_manager.theme.font_ui.split(",")[0].strip('"'), int(side * 0.22))
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"{self._value:.1f}%")

        # 下方说明
        caption_font = QFont(theme_manager.theme.font_ui.split(",")[0].strip('"'), int(side * 0.085))
        painter.setFont(caption_font)
        painter.setPen(QColor(theme_manager.color("text_muted")))
        painter.drawText(
            QRectF(0, side * 0.70, side, side * 0.16),
            Qt.AlignmentFlag.AlignCenter,
            self._caption,
        )
        painter.end()
