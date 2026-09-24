"""附件标签（输入区与消息区共用）。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from paper_agent.core.models import Attachment
from paper_agent.core.signals import signals
from paper_agent.core.theme import theme_manager
from paper_agent.resources.icons import build_icon
from paper_agent.ui.widgets.elide_label import ElideLabel
from paper_agent.ui.widgets.icon_button import IconToolButton
from paper_agent.utils.files import file_kind, human_readable_size


class AttachmentChip(QWidget):
    """单个附件（图片显示缩略图，文件显示类型图标）。"""

    remove_requested = Signal(str)

    NAME_MAX_WIDTH = 180

    def __init__(
        self,
        attachment: Attachment,
        removable: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("attachmentChip")
        self._attachment = attachment

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 6, 5)
        layout.setSpacing(6)

        self.thumb = QLabel(self)
        self.thumb.setFixedSize(28, 28)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setScaledContents(False)
        layout.addWidget(self.thumb)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(0)
        # ElideLabel：可被压缩 + 自动省略。消息气泡里的附件条没有横向滚动区，
        # 几个长文件名的 chip 就能把整条消息列的最小宽度顶到 700+（窄栏下溢出被裁）。
        self.name_label = ElideLabel(
            attachment.name, self,
            mode=Qt.TextElideMode.ElideMiddle, max_width=self.NAME_MAX_WIDTH,
        )
        self.name_label.setObjectName("chipName")
        self.size_label = ElideLabel(self._meta_text(), self)
        self.size_label.setObjectName("chipSize")
        text_layout.addWidget(self.name_label)
        text_layout.addWidget(self.size_label)
        layout.addLayout(text_layout, 1)

        self.close_button = IconToolButton(
            "close", "移除附件", object_name="chipClose", icon_size=12, parent=self
        )
        self.close_button.clicked.connect(lambda: self.remove_requested.emit(self._attachment.id))
        self.close_button.setVisible(removable)
        layout.addWidget(self.close_button)

        self._refresh()
        signals.theme_changed.connect(self._refresh)

    # ------------------------------------------------------------------
    @property
    def attachment(self) -> Attachment:
        return self._attachment

    def _meta_text(self) -> str:
        size = self._attachment.size or 0
        kind = file_kind(self._attachment.path or self._attachment.name)
        label = {"image": "图片", "document": "文档", "text": "文本"}.get(kind, "文件")
        if size:
            return f"{label} · {human_readable_size(size)}"
        return label

    def _refresh(self, *_args) -> None:
        attachment = self._attachment
        if attachment.is_image:
            pixmap = QPixmap(attachment.path)
            if not pixmap.isNull():
                self.thumb.setPixmap(
                    pixmap.scaled(
                        QSize(28, 28),
                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                return
        icon_name = "image" if attachment.is_image else "file-text"
        color = theme_manager.color("text_secondary")
        self.thumb.setPixmap(build_icon(icon_name, color, 36).pixmap(20, 20))
