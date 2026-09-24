"""剪贴板相关工具：读取图片 / 文件 / 文本。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication


@dataclass
class ClipboardPayload:
    """剪贴板解析结果。"""

    text: str = ""
    html: str = ""
    image: QImage | None = None
    urls: list[QUrl] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.urls is None:
            self.urls = []

    @property
    def has_image(self) -> bool:
        return self.image is not None and not self.image.isNull()

    @property
    def has_files(self) -> bool:
        return any(u.isLocalFile() for u in self.urls)

    @property
    def is_empty(self) -> bool:
        return not (self.text or self.html or self.has_image or self.urls)


def read_clipboard() -> ClipboardPayload:
    """读取当前剪贴板内容。"""
    clipboard = QApplication.clipboard()
    mime: QMimeData | None = clipboard.mimeData()
    if mime is None:
        return ClipboardPayload()

    image: QImage | None = None
    if mime.hasImage():
        image = clipboard.image()
        if image.isNull():
            image = None

    text = mime.text() or ""
    html = mime.html() or ""
    urls = list(mime.urls())
    return ClipboardPayload(text=text, html=html, image=image, urls=urls)


def copy_text(text: str) -> None:
    """复制纯文本到剪贴板。"""
    QApplication.clipboard().setText(text)


def copy_files(paths: list[str]) -> int:
    """把文件放进剪贴板（资源管理器 / 其它程序里可直接粘贴）。

    Returns:
        成功放入的文件个数；一个都不存在时返回 0。
    """
    urls = [QUrl.fromLocalFile(str(Path(item))) for item in paths if item and Path(item).exists()]
    if not urls:
        return 0
    mime = QMimeData()
    mime.setUrls(urls)
    QApplication.clipboard().setMimeData(mime)
    return len(urls)


def copy_image(image: QImage) -> None:
    """复制图片到剪贴板。"""
    QApplication.clipboard().setImage(image)
