"""全局常量与路径定义。"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------- 应用信息
APP_ID = "PaperAgent"
ORG_NAME = "PaperAgent"
APP_NAME = "Paper Agent"
APP_VERSION = "0.1.0"

# 访问自家服务端（登录 / 内置模型代理）时带的 User-Agent。
# 服务端「请求日志」靠它把桌面端的正常流量和脚本刷接口区分开 —— 默认的
# Python-urllib/3.x 看起来和随便写的爬虫一模一样。
SERVER_USER_AGENT = f"{APP_ID}-Desktop/{APP_VERSION}"

# ---------------------------------------------------------------- 目录
def _system_data_dir() -> Path:
    """返回系统用户数据目录（Windows: %APPDATA%/PaperAgent）。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home())
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "PaperAgent"


PACKAGE_DIR = Path(__file__).resolve().parent.parent
DATA_SUBDIRS = ("sessions", "attachments", "artifacts", "cache", "avatars")
DATA_FILES = ("settings.ini",)

RESOURCES_DIR = PACKAGE_DIR / "resources"
STYLES_DIR = RESOURCES_DIR / "styles"


def _resolve_data_dir() -> Path:
    """确定数据目录。

    优先级：``PAPERAGENT_DATA_DIR`` 环境变量 → 项目根目录 ``data/``（源码启动，
    数据与项目同盘，不占用系统盘）→ 系统用户数据目录。
    """
    env = os.environ.get("PAPERAGENT_DATA_DIR")
    if env:
        path = Path(env).expanduser()
    else:
        root = PACKAGE_DIR.parent
        # 源码布局（项目根有 main.py）就落地到项目内，打包安装时回落到系统目录
        path = root / "data" if (root / "main.py").is_file() else _system_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _migrate_legacy_data(target: Path) -> None:
    """把系统盘旧目录下的历史数据搬到新数据目录（一次性，失败不影响启动）。"""
    legacy = _system_data_dir()
    if legacy == target or not legacy.is_dir():
        return
    moved = False
    for name in DATA_SUBDIRS:
        src, dst = legacy / name, target / name
        if not src.is_dir() or dst.exists():
            continue
        try:
            shutil.move(str(src), str(dst))
            moved = True
        except OSError:
            pass
    for name in DATA_FILES:
        src, dst = legacy / name, target / name
        if src.is_file() and not dst.exists():
            try:
                shutil.move(str(src), str(dst))
            except OSError:
                pass
    if moved:
        _rewrite_session_paths(target / "sessions", legacy, target)
        print(f"[数据] 已把历史数据迁移到 {target}", file=sys.stderr)
    try:
        if not any(legacy.iterdir()):
            legacy.rmdir()
    except OSError:
        pass


def _rewrite_session_paths(directory: Path, src_root: Path, dst_root: Path) -> None:
    """会话 JSON 里记录的是旧的绝对路径，搬迁后批量改成新路径。"""
    if not directory.is_dir():
        return
    forms = (
        (str(src_root), str(dst_root)),
        (str(src_root).replace("\\", "\\\\"), str(dst_root).replace("\\", "\\\\")),
        (str(src_root).replace("\\", "/"), str(dst_root).replace("\\", "/")),
    )
    for file in directory.glob("*.json*"):
        try:
            text = file.read_text(encoding="utf-8")
        except OSError:
            continue
        updated = text
        for old, new in forms:
            updated = updated.replace(old, new)
        if updated != text:
            try:
                file.write_text(updated, encoding="utf-8")
            except OSError:
                pass


DATA_DIR = _resolve_data_dir()
try:
    _migrate_legacy_data(DATA_DIR)
except Exception:      # noqa: BLE001 - 迁移失败不能让应用起不来
    pass
SESSIONS_DIR = DATA_DIR / "sessions"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
ARTIFACTS_DIR = DATA_DIR / "artifacts"   # 智能体生成的文件产物
CACHE_DIR = DATA_DIR / "cache"
AVATARS_DIR = DATA_DIR / "avatars"      # 账号头像（上传的本地图片）
AIGC_DIR = DATA_DIR / "aigc"            # AIGC 检测记录（一次检测一个 JSON）
SETTINGS_FILE = DATA_DIR / "settings.ini"

for _dir in (SESSIONS_DIR, ATTACHMENTS_DIR, ARTIFACTS_DIR, CACHE_DIR, AVATARS_DIR, AIGC_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

# 检测记录最多保留多少条：每条都带全文逐句标注，留太多会占空间
AIGC_RECORD_LIMIT = 50

# ---------------------------------------------------------------- 交互
WINDOW_MIN_WIDTH = 1080
WINDOW_MIN_HEIGHT = 700
WINDOW_DEFAULT_WIDTH = 1360
WINDOW_DEFAULT_HEIGHT = 880
TITLE_BAR_HEIGHT = 40

SIDEBAR_WIDTH = 268
SIDEBAR_MIN_WIDTH = 200
OUTLINE_PANEL_WIDTH = 300

SHADOW_MARGIN = 0  # 无边框窗口当前不使用自绘阴影
RESIZE_BORDER = 6  # 无边框窗口可拖拽缩放的边框宽度

# ---------------------------------------------------------------- 输入区
MAX_ATTACHMENT_SIZE_MB = 32
MAX_ATTACHMENT_BYTES = MAX_ATTACHMENT_SIZE_MB * 1024 * 1024
MAX_PASTE_IMAGE_EDGE = 1600  # 粘贴图片缩放到该尺寸以内保存

# ---------------------------------------------------------------- 文生图
# 默认使用 SenseNova 文生图；可在 settings.ini 的 image/* 段覆盖，无需改代码
IMAGE_API_BASE_URL = "https://token.sensenova.cn/v1"
IMAGE_MODEL_ID = "sensenova-u1.5-lite"
IMAGE_API_KEY = "sk-mqxi4DRpohd7TYn4ze9p9bZIftoDXXwk"
IMAGE_DEFAULT_SIZE = "1024x1024"
IMAGE_SIZE_OPTIONS = ("1024x1024", "768x1024", "1024x768", "512x512")
IMAGE_BACKGROUND_SIZE = "1024x768"  # PPT 整页背景用横版，接近 16:9 便于裁切

TEXT_FILE_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".json", ".csv", ".tex", ".bib",
    ".py", ".js", ".ts", ".java", ".cpp", ".c", ".h", ".hpp", ".cs",
    ".go", ".rs", ".rb", ".sql", ".yaml", ".yml", ".xml", ".html", ".css",
}
DOCUMENT_FILE_EXTENSIONS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
IMAGE_FILE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"}
ATTACHMENT_FILE_EXTENSIONS = (
    TEXT_FILE_EXTENSIONS | DOCUMENT_FILE_EXTENSIONS | IMAGE_FILE_EXTENSIONS
)

FILE_OPEN_DIALOG_FILTER = (
    "所有支持的文件 (*.pdf *.doc *.docx *.ppt *.pptx *.xls *.xlsx *.txt *.md "
    "*.tex *.bib *.csv *.json *.py *.png *.jpg *.jpeg *.webp);;"
    "文档 (*.pdf *.doc *.docx *.ppt *.pptx *.xls *.xlsx);;"
    "文本 (*.txt *.md *.markdown *.tex *.bib *.csv *.json);;"
    "图片 (*.png *.jpg *.jpeg *.gif *.bmp *.webp);;"
    "所有文件 (*.*)"
)

# ---------------------------------------------------------------- 字体
FONT_FAMILY_UI = '"Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif'
FONT_FAMILY_MONO = '"Cascadia Code", "JetBrains Mono", "Consolas", "Microsoft YaHei", monospace'
FONT_SIZE_BASE = 13
FONT_SIZE_SMALL = 12
FONT_SIZE_MESSAGE = 14

# ---------------------------------------------------------------- 流式渲染
STREAM_RENDER_INTERVAL_MS = 60  # 流式输出时的重绘节流间隔
