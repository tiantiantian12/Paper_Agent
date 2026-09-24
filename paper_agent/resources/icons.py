"""内联 SVG 图标库（Lucide 风格线性图标，无需外部图片资源）。

所有图标按当前主题前景色着色，切换主题时由控件自动刷新。
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_SVG_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
    'viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="{width}" '
    'stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
)

# ------------------------------------------------------------------ 图标路径
ICONS: dict[str, str] = {
    # 通用
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.6-3.6"/>',
    "check": '<path d="M20 6L9 17l-5-5"/>',
    "close": '<path d="M18 6L6 18M6 6l12 12"/>',
    "copy": (
        '<rect x="9" y="9" width="12" height="12" rx="2"/>'
        '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'
    ),
    "trash": (
        '<path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/>'
        '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>'
        '<path d="M10 11v6M14 11v6"/>'
    ),
    "pencil": (
        '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/>'
    ),
    "more": (
        '<circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/>'
        '<circle cx="19" cy="12" r="1.4"/>'
    ),
    "refresh": (
        '<path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v5h-5"/>'
    ),
    "eye": (
        '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/>'
        '<circle cx="12" cy="12" r="3"/>'
    ),
    "chevron-down": '<path d="M6 9l6 6 6-6"/>',
    "chevron-right": '<path d="M9 6l6 6-6 6"/>',
    "chevron-left": '<path d="M15 6l-6 6 6 6"/>',
    "arrow-up": '<path d="M12 19V5M5 12l7-7 7 7"/>',
    "arrow-down": '<path d="M12 5v14M19 12l-7 7-7-7"/>',
    "download": (
        '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>'
        '<path d="M7 10l5 5 5-5M12 15V3"/>'
    ),
    "link": (
        '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/>'
        '<path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>'
    ),
    # 输入区
    "paperclip": (
        '<path d="M21.4 11.05l-9.19 9.19a6 6 0 0 1-8.48-8.48l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19'
        'a2 2 0 0 1-2.82-2.83l8.49-8.48"/>'
    ),
    "image": (
        '<rect x="3" y="4" width="18" height="16" rx="2"/>'
        '<circle cx="8.5" cy="9.5" r="1.5"/><path d="M21 16l-5-5-6 6"/>'
    ),
    "send": '<path d="M12 19V5M5 12l7-7 7 7"/>',
    "stop": '<rect x="7" y="7" width="10" height="10" rx="2"/>',
    "mic": (
        '<rect x="9" y="2" width="6" height="11" rx="3"/>'
        '<path d="M5 10a7 7 0 0 0 14 0M12 18v4"/>'
    ),
    "sparkles": (
        '<path d="M12 3l1.8 4.8L18.6 9.6 13.8 11.4 12 16.2 10.2 11.4 5.4 9.6 10.2 7.8z"/>'
        '<path d="M18 15l.8 2.2L21 18l-2.2.8L18 21l-.8-2.2L15 18l2.2-.8z"/>'
    ),
    "terminal": (
        '<rect x="3" y="4" width="18" height="16" rx="2"/>'
        '<path d="M7 9l3 3-3 3M13 15h4"/>'
    ),
    "globe": (
        '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/>'
        '<path d="M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18z"/>'
    ),
    # 侧栏 / 会话
    "message": '<path d="M21 12a8 8 0 0 1-8 8H8l-4 3v-5.5A8 8 0 0 1 12 4h1a8 8 0 0 1 8 8z"/>',
    "chat-plus": (
        '<path d="M20 12a7 7 0 0 1-7 7H9l-4 3v-4.5A7 7 0 0 1 11 5h2a7 7 0 0 1 7 7z"/>'
        '<path d="M12 9v6M9 12h6"/>'
    ),
    "book": (
        '<path d="M4 4.5A2.5 2.5 0 0 1 6.5 2H20v18H6.5A2.5 2.5 0 0 0 4 22z"/>'
        '<path d="M4 17.5A2.5 2.5 0 0 1 6.5 15H20"/>'
    ),
    "folder": (
        '<path d="M3 6.5A2.5 2.5 0 0 1 5.5 4h3.2l2 2.4H18.5A2.5 2.5 0 0 1 21 8.9V17 '
        'a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17z"/>'
    ),
    "folder-open": (
        '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h6.5A2.5 2.5 0 0 1 20 9.5V10"/>'
        '<path d="M3 9.5h17.2L18.6 18a2 2 0 0 1-2 1.6H5.1A2 2 0 0 1 3.2 17z"/>'
    ),
    "file-text": (
        '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
        '<path d="M14 3v5h5M9 13h6M9 17h4"/>'
    ),
    "list-tree": '<path d="M4 6h5M4 12h5M4 18h5M13 6h7M13 12h7M13 18h7M13 6v12"/>',
    "shield-check": (
        '<path d="M12 3l7 3v5.5c0 4.6-3 7.9-7 9.5-4-1.6-7-4.9-7-9.5V6z"/>'
        '<path d="M9 12l2.2 2.2L15.5 10"/>'
    ),
    "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5M12 3v12"/>',
    "video": (
        '<rect x="2" y="6" width="14" height="12" rx="2"/>'
        '<path d="M16 10.5l5-3v9l-5-3z"/>'
    ),
    "play": '<path d="M7 4l12 8-12 8z"/>',
    "settings": (
        '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3"/>'
        '<path d="M1 14h6M9 8h6M17 16h6"/>'
    ),
    "panel-left": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
    "panel-right": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    # 主题
    "sun": (
        '<circle cx="12" cy="12" r="4"/>'
        '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2'
        'M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'
    ),
    "moon": '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
    # 窗口控制（Windows 风格）
    "win-minimize": '<path d="M5 12h14"/>',
    "win-maximize": '<rect x="6" y="6" width="12" height="12" rx="1"/>',
    "win-restore": (
        '<rect x="6" y="8" width="12" height="12" rx="1"/>'
        '<path d="M9 8V6a1 1 0 0 1 1-1h8a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1h-2"/>'
    ),
    "win-close": '<path d="M18 6L6 18M6 6l12 12"/>',
}

_ICON_CACHE: dict[tuple[str, str, int, float], QIcon] = {}


def icon_svg(name: str, color: str = "currentColor", stroke_width: float = 1.75) -> str:
    """返回指定图标的 SVG 文本。"""
    body = ICONS.get(name)
    if body is None:
        body = ICONS["more"]
    return _SVG_TEMPLATE.format(color=color, width=stroke_width, body=body)


def build_icon(name: str, color: str, size: int = 20, stroke_width: float = 1.75) -> QIcon:
    """按颜色生成 QIcon（带缓存）。"""
    key = (name, color, size, stroke_width)
    cached = _ICON_CACHE.get(key)
    if cached is not None:
        return cached

    renderer = QSvgRenderer(QByteArray(icon_svg(name, color, stroke_width).encode("utf-8")))
    ratio = 1.0
    pixmap = QPixmap(int(size * ratio), int(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()

    result = QIcon(pixmap)
    _ICON_CACHE[key] = result
    return result


def build_pixmap(name: str, color: str, size: int = 20, stroke_width: float = 1.75) -> QPixmap:
    """按颜色生成 QPixmap。"""
    return build_icon(name, color, size, stroke_width).pixmap(QSize(size, size))


def clear_cache() -> None:
    _ICON_CACHE.clear()
