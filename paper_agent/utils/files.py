"""文件工具：类型判定、大小格式化、附件落盘。"""

from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QImage

from paper_agent.core.constants import (
    ARTIFACTS_DIR,
    ATTACHMENTS_DIR,
    DOCUMENT_FILE_EXTENSIONS,
    IMAGE_FILE_EXTENSIONS,
    MAX_ATTACHMENT_BYTES,
    MAX_PASTE_IMAGE_EDGE,
    TEXT_FILE_EXTENSIONS,
)


def file_kind(path: str | Path) -> str:
    """返回 'image' | 'document' | 'text' | 'file'。"""
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_FILE_EXTENSIONS:
        return "image"
    if suffix in DOCUMENT_FILE_EXTENSIONS:
        return "document"
    if suffix in TEXT_FILE_EXTENSIONS:
        return "text"
    return "file"


def is_supported_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in (
        TEXT_FILE_EXTENSIONS | DOCUMENT_FILE_EXTENSIONS | IMAGE_FILE_EXTENSIONS
    )


def human_readable_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def file_size(path: str | Path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def too_large(path: str | Path) -> bool:
    return file_size(path) > MAX_ATTACHMENT_BYTES


def iter_managed_files() -> list[Path]:
    """列出应用数据目录（附件 / 产物）下的全部文件。"""
    files: list[Path] = []
    for directory in (ATTACHMENTS_DIR, ARTIFACTS_DIR):
        if directory.exists():
            files.extend(item for item in directory.rglob("*") if item.is_file())
    return files


def normalized_path(path: str | Path) -> str:
    """路径归一化字符串，用于比较（Windows 下大小写、正反斜杠都不敏感）。"""
    try:
        return str(Path(path).resolve()).lower()
    except OSError:
        return str(path).lower().replace("\\", "/")


def is_managed_file(path: str | Path) -> bool:
    """文件是否位于应用的附件 / 产物目录内（只有这些文件才允许真正删除）。"""
    if not path:
        return False
    try:
        target = Path(path).resolve()
    except OSError:
        return False
    for directory in (ATTACHMENTS_DIR, ARTIFACTS_DIR):
        try:
            target.relative_to(Path(directory).resolve())
        except ValueError:
            continue
        return True
    return False


def delete_managed_file(path: str | Path) -> tuple[bool, str]:
    """删除应用数据目录内的文件，返回 ``(是否已删除, 说明)``。

    外部路径（例如未经拷贝就引用的用户原文件）只移除引用，不动磁盘文件。
    """
    target = Path(path)
    if not target.exists():
        return False, "文件已不存在，仅移除记录"
    if not is_managed_file(target):
        return False, "该文件不在应用数据目录内，仅移除记录"
    try:
        target.unlink()
    except OSError as exc:
        return False, f"磁盘文件删除失败（{exc}），仅移除记录"
    return True, "已删除"


def urls_to_paths(urls: list[QUrl]) -> list[str]:
    paths: list[str] = []
    for url in urls:
        if url.isLocalFile():
            paths.append(url.toLocalFile())
    return paths


def save_pasted_image(image: QImage, directory: Path | None = None) -> str | None:
    """将粘贴的图片保存为 PNG，返回文件路径。"""
    if image.isNull():
        return None

    img = image
    if max(img.width(), img.height()) > MAX_PASTE_IMAGE_EDGE:
        img = img.scaled(
            MAX_PASTE_IMAGE_EDGE,
            MAX_PASTE_IMAGE_EDGE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    directory = Path(directory or ATTACHMENTS_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S-") + hashlib.md5(
        str(time.time_ns()).encode()
    ).hexdigest()[:6]
    path = directory / f"{name}.png"
    if img.save(str(path), "PNG"):
        return str(path)
    return None


def import_file(path: str | Path) -> str:
    """把外部文件复制到附件目录，返回新路径（便于会话持久化后仍可访问）。"""
    source = Path(path)
    if not source.exists():
        return str(source)
    target = unique_target_path(ATTACHMENTS_DIR / source.name)
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(source, target)
        return str(target)
    except OSError:
        return str(source)


def unique_target_path(path: str | Path) -> Path:
    """复制附件时避免同名覆盖。"""
    target = Path(path)
    if not target.exists():
        return target
    stem, suffix, parent = target.stem, target.suffix, target.parent
    index = 1
    while True:
        candidate = parent / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1
