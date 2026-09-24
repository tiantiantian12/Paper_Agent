"""输入编辑器：自适应高度，支持粘贴 / 拖入图片与文件。"""

from __future__ import annotations

from PySide6.QtCore import QMimeData, QSize, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage, QTextOption
from PySide6.QtWidgets import QTextEdit

from paper_agent.utils.files import is_supported_file, urls_to_paths

MAX_EDITOR_HEIGHT = 220
MIN_EDITOR_HEIGHT = 26


class ComposerEditor(QTextEdit):
    """论文助手的输入框。"""

    submitted = Signal()
    image_pasted = Signal(QImage)
    files_dropped = Signal(list)  # list[str]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("composerEditor")
        self.setAcceptRichText(False)
        self.setFrameShape(QTextEdit.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setPlaceholderText("描述你的论文需求，可粘贴图片或拖入参考文献…")
        self.document().setDocumentMargin(2)
        self.document().documentLayout().documentSizeChanged.connect(self._adjust_height)
        self.setFixedHeight(MIN_EDITOR_HEIGHT)
        self._send_shortcut = "enter"

    # ------------------------------------------------------------------ 配置
    def set_send_shortcut(self, mode: str) -> None:
        """``enter``: Enter 发送；``ctrl+enter``: Ctrl+Enter 发送。"""
        self._send_shortcut = mode

    def set_placeholder(self, text: str) -> None:
        self.setPlaceholderText(text)

    # ------------------------------------------------------------------ 高度
    def _adjust_height(self, *_args) -> None:
        height = int(self.document().size().height()) + 6
        target = max(MIN_EDITOR_HEIGHT, min(height, MAX_EDITOR_HEIGHT))
        if abs(target - self.height()) > 1:
            self.setFixedHeight(target)
            self.updateGeometry()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(200, self.height())

    # ------------------------------------------------------------------ 键盘
    def keyPressEvent(self, event):  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            modifiers = event.modifiers()
            ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
            shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
            if (self._send_shortcut == "enter" and not ctrl and not shift) or (
                self._send_shortcut == "ctrl+enter" and ctrl
            ):
                self.submitted.emit()
                return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------ 粘贴
    def canInsertFromMimeData(self, source: QMimeData) -> bool:  # noqa: N802
        return source.hasImage() or source.hasUrls() or source.hasText()

    def insertFromMimeData(self, source: QMimeData) -> None:  # noqa: N802
        if source.hasImage():
            image = self._image_from_mime()
            if not image.isNull():
                self.image_pasted.emit(image)
                return

        if source.hasUrls():
            paths = [p for p in urls_to_paths(source.urls()) if is_supported_file(p)]
            if paths:
                self.files_dropped.emit(paths)
                return

        text = source.text()
        if text:
            self.insertPlainText(text)

    @staticmethod
    def _image_from_mime() -> QImage:
        """从剪贴板取图（QMimeData 在 PySide6 中不直接暴露 QImage）。"""
        from PySide6.QtWidgets import QApplication

        return QApplication.clipboard().image()

    # ------------------------------------------------------------------ 拖放
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        mime = event.mimeData()
        if mime.hasUrls() or mime.hasImage() or mime.hasText():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        mime = event.mimeData()
        if mime.hasImage():
            image = self._image_from_mime()
            if not image.isNull():
                self.image_pasted.emit(image)
                event.acceptProposedAction()
                return
        if mime.hasUrls():
            paths = [p for p in urls_to_paths(mime.urls()) if is_supported_file(p)]
            if paths:
                self.files_dropped.emit(paths)
                event.acceptProposedAction()
                return
        text = mime.text()
        if text:
            self.insertPlainText(text)
            event.acceptProposedAction()
            return
        super().dropEvent(event)
