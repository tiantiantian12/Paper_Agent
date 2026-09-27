"""会话工作区布局：**唯一事实来源**。

每个会话一棵树，两种模式（文档 / 编程）共用：

    data/workspace/<会话 id>/
    ├── upload/        ← 用户上传 / 粘贴的文件
    └── generated/     ← 生成的配图 / 视频 / Word / PPT / 下载

以前三处各管各的：产物在 ``data/artifacts``、附件在 ``data/attachments``、编程模式的
工程在 ``workspace/<会话 id>``。两个后果：

1. 产物平铺，而「按名字取文件」会扫整个目录兜底，各会话命名规则又一样
   （``视频-20260923-144501.mp4``），于是可能拿到**别的会话**的那份 —— 「串文件」；
2. 同一会话的文件分散在三处，模型在工程目录里找不到刚生成的片子。

现在只有这一棵树，别处不要再自己拼路径：

* 上传 → :func:`upload_dir`，生成 → :func:`generated_dir`；
* 工程目录（编程模式）就是 :func:`generated_dir`（见 ``skills.code_workspace.code_root``）；
* 拿不到会话上下文时（脚本 / 测试 / 示例引擎）落到 ``_shared``，不至于写不进去。

「当前会话」记在**线程**上：worker 起跑时由
:func:`paper_agent.services.session_artifacts.use` 设好，工具都在那个线程里同步跑，
所以落盘一路都看得见。
"""

from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# 两个固定子目录的名字：界面面板、系统提示、沙箱白名单都读这两个常量
UPLOAD_DIR_NAME = "upload"
GENERATED_DIR_NAME = "generated"
# 拿不到会话（脚本 / 测试 / mock）时的兜底目录
SHARED_DIR_NAME = "_shared"

# 会话 id 直接当目录名用，先清洗：id 本来是我们自己生成的，但别给路径穿越留口子
_UNSAFE = re.compile(r"[^0-9A-Za-z_.-]+")
MAX_DIR_NAME = 64

_state = threading.local()


def safe_name(session_id: str) -> str:
    """会话 id → 安全的目录名（只留字母数字与 ``._-``）。"""
    text = _UNSAFE.sub("_", str(session_id or "").strip()).strip("._")
    return text[:MAX_DIR_NAME]


def root() -> Path:
    """工作区根目录（默认是 ``<数据目录>/workspace``）。

    走 ``code_workspace.default_root`` 而不是自己拼一次：那边认
    ``PAPERAGENT_CODE_ROOT`` / 设置里选过的目录，测试也靠替换它把落点指到临时目录
    （见 ``tests/conftest.py::_isolated_workspace``）。
    """
    from paper_agent.services.skills import code_workspace

    return Path(code_workspace.default_root())


def current() -> str:
    """当前线程正在服务的会话 id（没有就是空串）。"""
    return str(getattr(_state, "session_id", "") or "")


@contextmanager
def use(session_id: str) -> Iterator[str]:
    """把「当前会话」设成 ``session_id``（worker 起跑时设，结束自动还原）。"""
    previous = current()
    value = str(session_id or "")
    _state.session_id = value
    try:
        yield value
    finally:
        _state.session_id = previous


def dir_of(session_id: str | None = None, *, create: bool = False) -> Path:
    """某个会话的工作区目录：``<工作区根>/<会话 id>``。

    ``session_id`` 省略时取 :func:`current`；连当前会话都没有（脚本 / 测试）
    就落到 ``_shared`` —— 宁可多一个共享目录，也别把文件丢了。
    """
    name = safe_name(current() if session_id is None else session_id)
    folder = root() / (name or SHARED_DIR_NAME)
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def upload_dir(session_id: str | None = None, *, create: bool = True) -> Path:
    """本会话的上传目录 ``<会话工作区>/upload``。"""
    folder = dir_of(session_id) / UPLOAD_DIR_NAME
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def generated_dir(session_id: str | None = None, *, create: bool = True) -> Path:
    """本会话的产物目录 ``<会话工作区>/generated``（编程模式的工程目录也是它）。"""
    folder = dir_of(session_id) / GENERATED_DIR_NAME
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def _drop_if_empty(folder: Path) -> bool:
    try:
        if any(folder.iterdir()):
            return False
        folder.rmdir()
        return True
    except OSError:
        return False


def prune_empty(base: Path | None = None) -> int:
    """收掉空的 ``upload`` / ``generated`` 与随之变空的会话目录，返回删掉的个数。

    清理完文件（删会话 / 「清理未引用的文件」）之后调用：不收的话每清一次就沉淀
    一批空壳目录。**只收空目录** —— 里面还有文件就原样留着。
    """
    root_dir = Path(base) if base is not None else root()
    if not root_dir.is_dir():
        return 0
    removed = 0
    for session_dir in list(root_dir.iterdir()):
        if not session_dir.is_dir():
            continue
        for child in list(session_dir.iterdir()):
            if child.is_dir() and _drop_if_empty(child):
                removed += 1
        if _drop_if_empty(session_dir):
            removed += 1
    return removed
