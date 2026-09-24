"""产物落盘位置：**按会话分目录**，别让模型把别的会话的文件当成自己的。

以前所有产物（配图 / 视频 / Word / PPT）都平铺在 ``data/artifacts/`` 一个目录里
（实测攒到 200+ 个文件、58 个 mp4）。而模型按名字取文件时（``resolve_asset`` /
``_resolve_reference`` / ``list_artifacts``）会**扫这个目录兜底**，各会话的产物命名
规则又是一样的（``视频-20260923-144501.mp4``）—— 于是它拿到的可能是**别的会话**
刚生成的文件，用户看到的就是「串文件」。

现在每个会话一个子目录，和编程模式的 ``workspace/<会话 id>`` 一个思路：

    data/artifacts/
    ├── a09ebfabace8/          # A 会话的配图 / 视频 / 文档
    │   ├── 配图-20260923-144501-1.png
    │   └── 视频-20260923-144502.mp4
    ├── feaaf2a9f281/          # B 会话
    └── 旧版平铺文件…          # 升级前留下的仍在根目录（会话记录存的是绝对路径，照旧能打开）

三条规则：

* **写文件** → :func:`unique_path` / :func:`dir_for`，落在**当前会话**目录；
* **按名字找文件** → :func:`candidates`，只认「会话清单 + 本会话目录 + 根目录（旧版）」，
  **绝不进别的会话目录**；
* **当前会话从哪来** → worker 起跑时用 :func:`use` 设好（见 ``agent_service._StreamWorker``）。
  工具都在这个线程里同步跑，所以图片 / 视频 / 文档 / 下载一路都看得见；拿不到会话
  （脚本、测试、mock）时退回旧的平铺行为，不至于写不进去。
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from paper_agent.core.constants import ARTIFACTS_DIR

# 会话 id 直接当目录名用，先清洗：id 本来是我们自己生成的，但别给路径穿越留口子
_UNSAFE = re.compile(r"[^0-9A-Za-z_.-]+")
MAX_DIR_NAME = 64

_current: ContextVar[str] = ContextVar("paper_agent_artifacts_session", default="")


def _norm(path: Any) -> str:
    try:
        return os.path.normcase(os.path.abspath(str(path)))
    except (OSError, ValueError):      # pragma: no cover - 路径异常时退回原串比较
        return os.path.normcase(str(path))


def safe_name(session_id: str) -> str:
    """会话 id → 安全的目录名（只留字母数字与 ``._-``）。"""
    text = _UNSAFE.sub("_", str(session_id or "").strip()).strip("._")
    return text[:MAX_DIR_NAME]


def _dir(session_id: str, base: Path | None, create: bool) -> Path:
    root = Path(base if base is not None else ARTIFACTS_DIR)
    name = safe_name(session_id)
    folder = (root / name) if name else root
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def session_dir(session_id: str, base: Path | None = None) -> Path:
    """某个会话的产物目录 ``<产物根>/<会话 id>``（不存在就建好）。

    会话 id 为空时返回产物根目录 —— 与 :func:`dir_for` 保持同一套规则，
    别让「有没有会话」出现两种说法。
    """
    return _dir(session_id, base, True)


def current() -> str:
    """当前线程正在服务的会话 id（没有就是空串）。"""
    return _current.get()


@contextmanager
def use(session_id: str) -> Iterator[str]:
    """把「当前会话」设成 ``session_id``（worker 起跑时设，结束自动还原）。"""
    value = str(session_id or "")
    token = _current.set(value)
    try:
        yield value
    finally:
        _current.reset(token)


def dir_for(base: Path | None = None, *, create: bool = True) -> Path:
    """当前会话的产物目录；没有会话上下文时就是 ``base``（旧的平铺行为）。"""
    return _dir(current(), base, create)


def unique_path(
    name: str,
    *,
    base: Path | None = None,
    taken: Iterable[str] = (),
) -> Path:
    """在本会话产物目录里取一个不重名的路径（同名加 ``-1`` / ``-2``）。

    ``taken`` 是「已经属于本会话的文件路径」：重名就地覆盖、不堆副本 —— 平铺时代靠它
    认出「这是我自己之前生成的」，现在本会话目录里同名本来就是自己的；留着是为了兼容
    根目录里的旧文件（会话清单里存的是那些绝对路径）。
    """
    folder = dir_for(base)
    target = folder / Path(str(name or "")).name
    if not target.exists():
        return target
    if _norm(target) in {_norm(item) for item in taken}:
        return target
    stem, suffix, index = target.stem, target.suffix, 1
    while True:
        candidate = folder / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _session_paths(session_files: Any) -> list[str]:
    """会话清单里的路径：既收 ``{"path": ...}`` 这种条目，也收现成的路径字符串。"""
    paths: list[str] = []
    for item in session_files or ():
        value = item.get("path", "") if isinstance(item, dict) else str(item or "")
        if value:
            paths.append(str(value))
    return paths


def candidates(
    session_files: Any = None,
    *,
    base: Path | None = None,
) -> list[str]:
    """本会话「按名字找文件」的候选：会话清单 → 本会话目录 → 根目录（旧版平铺）。

    **不进别的会话目录**：这正是「串文件」的来源 —— 别的会话也会生成
    ``视频-20260923-144501.mp4`` 这种同规则的名字，一起扫就会取错。
    """
    paths: list[str] = _session_paths(session_files)
    root = Path(base if base is not None else ARTIFACTS_DIR)
    folders = [dir_for(base, create=False), root]
    for folder in folders:
        if not folder.is_dir():
            continue
        paths.extend(str(item) for item in folder.iterdir() if item.is_file())

    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        key = _norm(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def extra_paths(extra_dirs: Sequence[str] | None) -> list[str]:
    """额外的「按名字找文件」目录里的文件：**编程模式的会话工程目录**。

    用户上传的图会被复制进工程目录（见 ``code_workspace.import_attachments``）、模型自己
    写进工作区的文件也在那儿，而它们都不在 ``data/artifacts`` 里 —— 不带上这一份，
    编程模式下按文件名给 anchor / first_frame 就会一律「找不到」（真实会话
    ``c754edb8c3d0``：模型只好改用绝对路径重试，多烧一轮）。

    复用工作区面板那套扫描（跳过依赖 / 构建目录、有深度与条数上限），所以这里能解析到的
    正好是模型在 ``list_files`` 里看得到的那批文件。
    """
    from paper_agent.services.skills.code_workspace import file_paths

    found: list[str] = []
    for item in extra_dirs or ():
        text = str(item or "").strip()
        if not text:
            continue
        try:
            found.extend(file_paths(Path(text)))
        except Exception:      # noqa: BLE001 - 目录扫不动就当它不存在，别拖垮出片
            continue
    return found


def available_names(
    session_files: Any = None,
    suffixes: Sequence[str] = (),
    extra_dirs: Sequence[str] | None = None,
    *,
    base: Path | None = None,
    limit: int = 6,
) -> list[str]:
    """本会话里**确实存在**的、符合后缀的文件名（报错时列给模型看）。"""
    wanted = tuple(str(item).lower() for item in suffixes or ())
    names: list[str] = []
    for path in [*candidates(session_files, base=base), *extra_paths(extra_dirs)]:
        item = Path(path)
        if wanted and item.suffix.lower() not in wanted:
            continue
        if item.name not in names:
            names.append(item.name)
        if len(names) >= limit:
            break
    return names


def name_hint(
    session_files: Any = None,
    suffixes: Sequence[str] = (),
    extra_dirs: Sequence[str] | None = None,
    *,
    base: Path | None = None,
) -> str:
    """报错时附一句「本会话现在能用的文件：a.png、b.png…」。

    模型找不到文件时只会瞎猜：真实会话里它去 list_files 看了一眼、名字看着一模一样、
    再试一次还是失败。把真实文件名直接写进报错里，它一次就能改对。
    """
    names = available_names(session_files, suffixes, extra_dirs, base=base)
    return ("本会话现在能用的文件：" + "、".join(names) + "。") if names else ""


def find_named(
    name: str,
    session_files: Any = None,
    *,
    base: Path | None = None,
) -> Path | None:
    """按**文件名**在本会话范围里找一个已存在的文件（精确匹配，不做模糊）。

    本会话目录优先，再认会话清单里的路径（升级前生成的旧文件在根目录里）。
    """
    wanted = os.path.normcase(Path(str(name or "")).name)
    if not wanted:
        return None
    target = dir_for(base, create=False) / Path(str(name)).name
    if target.is_file():
        return target
    for path in candidates(session_files, base=base):
        item = Path(path)
        if item.is_file() and os.path.normcase(item.name) == wanted:
            return item
    return None


def drop_session(session_id: str, base: Path | None = None) -> bool:
    """删掉某个会话的产物目录 —— **只在它空的时候删**。

    会话删除时文件已经按引用清过一轮（见 ``main_window._cleanup_session_files``），
    这里只负责收掉空壳；万一里面还有别的会话在引用的文件，就原样留着。
    """
    session_id = safe_name(session_id)
    if not session_id:
        return False
    folder = Path(base if base is not None else ARTIFACTS_DIR) / session_id
    if not folder.is_dir():
        return False
    try:
        if any(folder.iterdir()):
            return False
        folder.rmdir()
        return True
    except OSError:
        return False


def prune_empty(base: Path | None = None) -> int:
    """收掉所有空的会话产物目录（清理之后调用），返回删掉的个数。"""
    root = Path(base if base is not None else ARTIFACTS_DIR)
    if not root.is_dir():
        return 0
    removed = 0
    for item in list(root.iterdir()):
        if not item.is_dir():
            continue
        try:
            if not any(item.iterdir()):
                item.rmdir()
                removed += 1
        except OSError:
            continue
    return removed
