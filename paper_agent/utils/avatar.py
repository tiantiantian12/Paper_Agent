"""账号头像：内置头像绘制、本地图片上传与圆形渲染。

头像统一用字符串描述（``spec``），便于存配置与同步服务端：

* ``icon:sparkles`` —— 内置头像（线性图标 + 低饱和底色）
* ``file:avatar_xxx.png`` —— 用户上传的图片，存放在数据目录 ``avatars/``
"""

from __future__ import annotations

from uuid import uuid4

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication

from paper_agent.core.constants import AVATARS_DIR
from paper_agent.resources.icons import build_icon

ICON_PREFIX = "icon:"
FILE_PREFIX = "file:"
DEFAULT_AVATAR = f"{ICON_PREFIX}user"

# 内置头像：(图标名, 底色, 展示名)。底色统一低饱和，避免破坏界面黑白灰基调
BUILTIN_AVATARS: tuple[tuple[str, str, str], ...] = (
    ("user", "#7C8AA0", "默认"),
    ("sparkles", "#8E7CC3", "灵感"),
    ("book", "#6E9C7A", "学术"),
    ("terminal", "#5F7E8C", "工程"),
    ("globe", "#7E93C4", "探索"),
    ("moon", "#8A7FA8", "夜航"),
    ("pencil", "#B58A5F", "写作"),
    ("image", "#C08A8A", "视觉"),
)

AVATAR_COLORS = {name: color for name, color, _label in BUILTIN_AVATARS}
AVATAR_LABELS = {name: label for name, _color, label in BUILTIN_AVATARS}

UPLOAD_SIZE = 256          # 上传图片统一缩放到的边长
_CACHE: dict[tuple[str, int], QPixmap] = {}


def builtin_names() -> list[str]:
    return [name for name, _color, _label in BUILTIN_AVATARS]


def icon_spec(name: str) -> str:
    return f"{ICON_PREFIX}{name}"


def file_spec(name: str) -> str:
    return f"{FILE_PREFIX}{name}"


def normalize(spec: str) -> str:
    """校验并兜底：文件或图标不存在时退回默认头像。"""
    spec = (spec or "").strip()
    if spec.startswith(FILE_PREFIX):
        name = spec[len(FILE_PREFIX) :]
        if name and not any(ch in name for ch in ("/", "\\", "..")):
            return spec
        return DEFAULT_AVATAR
    if spec.startswith(ICON_PREFIX):
        name = spec[len(ICON_PREFIX) :]
        return spec if name in AVATAR_COLORS else DEFAULT_AVATAR
    return DEFAULT_AVATAR


def avatar_color(spec: str) -> str:
    """内置头像的底色（用于需要自己绘制的场景）。"""
    spec = normalize(spec)
    if spec.startswith(ICON_PREFIX):
        return AVATAR_COLORS.get(spec[len(ICON_PREFIX) :], "#7C8AA0")
    return "#7C8AA0"


def avatar_pixmap(spec: str, size: int = 28) -> QPixmap:
    """生成圆形头像（带缓存）。"""
    spec = normalize(spec)
    key = (spec, size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    ratio = 2  # 按 2x 绘制再缩放，圆边更平滑
    canvas = QPixmap(size * ratio, size * ratio)
    canvas.fill(Qt.GlobalColor.transparent)

    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    clip = QPainterPath()
    clip.addEllipse(QRectF(0, 0, size * ratio, size * ratio))
    painter.setClipPath(clip)

    if spec.startswith(FILE_PREFIX):
        source = QPixmap(str(AVATARS_DIR / spec[len(FILE_PREFIX) :]))
        if source.isNull():
            _draw_builtin(painter, "user", size * ratio)
        else:
            side = min(source.width(), source.height())
            square = source.copy(
                (source.width() - side) // 2, (source.height() - side) // 2, side, side
            )
            painter.drawPixmap(
                0,
                0,
                square.scaled(
                    size * ratio,
                    size * ratio,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                ),
            )
    else:
        _draw_builtin(painter, spec[len(ICON_PREFIX) :], size * ratio)
    painter.end()

    canvas.setDevicePixelRatio(ratio)
    pixmap = canvas.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio)
    pixmap.setDevicePixelRatio(
        QApplication.instance().devicePixelRatio() if QApplication.instance() else 1.0
    )
    _CACHE[key] = pixmap
    return pixmap


def _draw_builtin(painter: QPainter, icon_name: str, size: int) -> None:
    """画内置头像：底色圆 + 白色线性图标。"""
    color = AVATAR_COLORS.get(icon_name, "#7C8AA0")
    painter.fillRect(0, 0, size, size, QColor(color))
    icon = build_icon(icon_name, "#FFFFFF", size)
    painter.drawPixmap(0, 0, icon.pixmap(size, size))


def save_uploaded_avatar(source_path: str) -> str:
    """把本地图片裁成正方形存进数据目录，返回 ``file:`` 规格。"""
    pixmap = QPixmap(source_path)
    if pixmap.isNull():
        raise ValueError("无法读取该图片，请换一张试试")

    side = min(pixmap.width(), pixmap.height())
    square = pixmap.copy(
        (pixmap.width() - side) // 2, (pixmap.height() - side) // 2, side, side
    )
    scaled = square.scaled(
        UPLOAD_SIZE,
        UPLOAD_SIZE,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    name = f"avatar_{uuid4().hex[:12]}.png"
    if not scaled.save(str(AVATARS_DIR / name), "PNG"):
        raise ValueError("头像保存失败")
    _CACHE.clear()
    return file_spec(name)


def clear_cache() -> None:
    _CACHE.clear()
