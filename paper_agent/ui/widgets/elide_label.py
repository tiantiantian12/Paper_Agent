"""可按宽度自动省略的 QLabel。

普通 QLabel 的最小宽度默认跟着文字走。把它们放进没有横向滚动能力的容器
（消息气泡、附件标签、产物卡片）时，一个长文件名或一句长说明就能把整条
消息列的最小宽度顶到几百像素——窄栏（打开论文结构面板）下只能溢出被裁，
看起来像内容被盖住了。这个控件允许被压缩，并在 resize 时主动省略文字。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget


class ElideLabel(QLabel):
    """文字超宽时自动省略的标签（完整文字放进 tooltip）。"""

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
        max_width: int = 0,
    ) -> None:
        super().__init__(parent)
        self._full_text = text
        self._elide_mode = mode
        self._max_width = max(0, int(max_width))
        # Preferred：布局照旧按文字宽度摆放（挤在一起的行里也能正常显示），
        # 但最小宽度交给 minimumSizeHint() 接管，文字长度不再绑架父级最小宽度。
        #
        # 注意别用 Ignored：行里只要有一个 addStretch，Ignored 的控件会直接
        # 被压到 0 宽——文件名字就整个看不见了。
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        if self._max_width > 0:
            self.setMaximumWidth(self._max_width)
        self.setText(text)

    def full_text(self) -> str:
        return self._full_text

    def setText(self, text: str) -> None:      # noqa: N802
        self._full_text = text
        super().setText(text)
        self.setToolTip(text)
        self._apply_elide()

    def sizeHint(self) -> QSize:               # noqa: N802
        """按完整文字给宽度提示。

        必须用 _full_text 算：显示用的文字是省略过的，如果拿它当 sizeHint，
        会形成「省略 → 变短 → 布局给得更窄 → 更省略」的死循环，文件名最后只剩「…」。
        """
        width = self.fontMetrics().horizontalAdvance(self._full_text)
        if self._max_width > 0:
            width = min(width, self._max_width)
        return QSize(width, super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:        # noqa: N802
        """最小宽度放开到 0：父级（卡片 / 气泡 / 消息列）才压得下来。"""
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, event) -> None:      # noqa: N802
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        width = self.width()
        if width <= 0:
            return
        elided = self.fontMetrics().elidedText(self._full_text, self._elide_mode, width)
        if elided != QLabel.text(self):
            QLabel.setText(self, elided)
