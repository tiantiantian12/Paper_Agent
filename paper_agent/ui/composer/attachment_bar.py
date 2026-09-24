"""输入区上方的附件栏。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QScrollArea, QWidget

from paper_agent.core.models import Attachment
from paper_agent.ui.widgets.attachment_chip import AttachmentChip


class AttachmentBar(QScrollArea):
    """水平排布的附件列表（超出时横向滚动）。"""

    attachment_removed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedHeight(46)
        self.hide()

        self._container = QWidget(self)
        self._container.setObjectName("attachmentContainer")
        self._layout = QHBoxLayout(self._container)
        self._layout.setContentsMargins(2, 2, 2, 2)
        self._layout.setSpacing(6)
        self._layout.addStretch(1)
        self.setWidget(self._container)

        self._chips: dict[str, AttachmentChip] = {}

    # ------------------------------------------------------------------ 接口
    def add(self, attachment: Attachment) -> None:
        if attachment.id in self._chips:
            return
        chip = AttachmentChip(attachment, removable=True, parent=self._container)
        chip.remove_requested.connect(self.remove)
        self._layout.insertWidget(self._layout.count() - 1, chip)
        self._chips[attachment.id] = chip
        self.setVisible(True)

    def remove(self, attachment_id: str) -> None:
        chip = self._chips.pop(attachment_id, None)
        if chip is None:
            return
        self._layout.removeWidget(chip)
        chip.deleteLater()
        if not self._chips:
            self.hide()
        self.attachment_removed.emit(attachment_id)

    def clear(self) -> None:
        for chip in list(self._chips.values()):
            self._layout.removeWidget(chip)
            chip.deleteLater()
        self._chips.clear()
        self.hide()

    def items(self) -> list[Attachment]:
        return [chip.attachment for chip in self._chips.values()]

    def count(self) -> int:
        return len(self._chips)
