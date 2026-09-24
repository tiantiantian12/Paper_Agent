"""编程模式：工程目录与配套工具（读写文件 / 列目录 / 跑命令）。

与文档模式的严格沙箱不同，编程模式要在工程目录里**真正干活**：建项目、写代码、
装依赖、跑测试。安全边界因此换了个思路：

- **可写可执行的范围只有一个工程目录**（默认 ``<数据目录>/workspace``，
  可用环境变量 ``PAPERAGENT_CODE_ROOT`` 指到你自己已有的项目）；
- 目录之外的写入一律拒绝；读取放开（只读不会破坏东西，但要能看用户的代码）；
- 命令做一层破坏性指令黑名单，并把工作目录钉在工程目录内；
- 输出截断、超时可打断，和文档模式的执行器共用同一套收集逻辑。
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from paper_agent.core.constants import DATA_DIR
from paper_agent.services.skills.base import Tool, ToolParam, ToolResult

MAX_READ_BYTES = 2 * 1024 * 1024        # 单文件读取上限
MAX_OUTPUT = 8000                       # 命令输出截断
MAX_TIMEOUT = 900
DEFAULT_TIMEOUT = 120
MAX_LIST_ENTRIES = 200

# ---------------------------------------------------------------- 后台服务
SERVICE_WAIT = 3            # 启动后默认观察几秒（当场退出 = 命令本身有问题）
SERVICE_MAX_WAIT = 30
SERVICE_PROBE_TIMEOUT = 6   # 探活：端口最长等多久
SERVICE_LOG_LINES = 60      # 留多少行启动日志给模型看

RE_URL_IN_TEXT = re.compile(r"https?://[\w.\-]+(?::\d+)?(?:/[^\s\"'<>`]*)?")
RE_LOCAL_URL = re.compile(r"\b(?:localhost|127\.0\.0\.1)(?::\d{2,5})(?:/[^\s\"'<>`]*)?")
RE_PORT_HINT = re.compile(r"(?:--port[= ]\s*|-p\s+|PORT=)(\d{2,5})")
# 一看就是常驻服务的命令：误用 run_command 跑会一直阻塞到超时，要提示改走 start_service
RE_SERVICE_HINT = re.compile(
    r"http\.server|npm (run )?(dev|start)|yarn (dev|start)|pnpm dev|uvicorn|"
    r"gunicorn|flask run|runserver|vite|next dev|serve -s"
)

# 已启动的服务：命令 → 进程 / 日志。同一条命令重复调用时复用，避免端口打架
_SERVICES: dict[str, subprocess.Popen] = {}
_SERVICE_LOGS: dict[str, deque] = {}
_SERVICE_URLS: dict[str, str] = {}

# 破坏性指令（编程模式允许执行普通命令，但装机级别的动作必须挡住）
DANGEROUS_COMMANDS = (
    "format ", "diskpart", "shutdown", "mkfs", "cipher /w", "takeown",
    "reg delete", "bcdedit", "rm -rf /", "del /f /s c:", "del /q /s c:",
    ":(){", "taskkill /f /im explorer",
)


def configured_root() -> str:
    """设置里选定的工作区目录（没选过返回空串）。"""
    try:
        from paper_agent.core.config import AppConfig

        return AppConfig().code_root
    except Exception:            # noqa: BLE001 - 配置读不到就用默认目录
        return ""


def default_root() -> Path:
    """默认工作区：数据目录下的 ``workspace``。"""
    return DATA_DIR / "workspace"


def code_root(session_id: str = "", session_path: str = "") -> Path:
    """工程目录（**每个会话一份**）。

    优先级：环境变量 ``PAPERAGENT_CODE_ROOT`` → 会话自己选的目录（``session_path``）
    → 会话专属目录 ``workspace/<会话 id>`` → 共享默认目录 ``workspace``。

    会话之间不共用同一份工程文件：A 会话的前端项目和 B 会话的脚本不会混在一起，
    右侧工作区面板与模型看到的文件清单也各自独立。
    """
    override = os.environ.get("PAPERAGENT_CODE_ROOT")
    if override:
        root = Path(override).expanduser()
    elif session_path:
        root = Path(session_path).expanduser()
    elif session_id:
        root = default_root() / session_id
    else:
        shared = configured_root()
        root = Path(shared).expanduser() if shared else default_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        root = default_root()     # 目录不可写：退回默认目录，别让工具直接用不了
        root.mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------- 目录树
# 这些目录又大又没用（依赖、缓存、构建产物），列表里跳过后目录树才看得清
SKIP_DIRS = {
    ".git", ".idea", ".vscode", ".venv", "venv", "env", "__pycache__",
    "node_modules", "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
}
TREE_MAX_DEPTH = 4
TREE_MAX_ENTRIES = 400


def build_tree(
    root: Path | None = None,
    max_depth: int = TREE_MAX_DEPTH,
    max_entries: int = TREE_MAX_ENTRIES,
) -> tuple[list[dict], int, int]:
    """扫描工作区，返回 ``(节点列表, 文件数, 目录数)``。

    节点形如 ``{"name", "path", "is_dir", "size", "children"}``；目录在前、
    按名称排序，超过 ``max_entries`` 或超过 ``max_depth`` 就不再展开。
    """
    base = root or code_root()
    counter = {"files": 0, "dirs": 0, "left": max(1, int(max_entries))}

    def scan(directory: Path, depth: int) -> list[dict]:
        if depth > max_depth or counter["left"] <= 0:
            return []
        try:
            entries = sorted(
                directory.iterdir(), key=lambda item: (item.is_file(), item.name.lower())
            )
        except OSError:
            return []
        nodes: list[dict] = []
        for entry in entries:
            if counter["left"] <= 0:
                break
            name = entry.name
            if entry.is_dir():
                if name in SKIP_DIRS:
                    continue
                counter["left"] -= 1
                counter["dirs"] += 1
                nodes.append(
                    {
                        "name": name,
                        "path": str(entry),
                        "is_dir": True,
                        "size": 0,
                        "children": scan(entry, depth + 1),
                    }
                )
                continue
            counter["left"] -= 1
            counter["files"] += 1
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            nodes.append(
                {
                    "name": name,
                    "path": str(entry),
                    "is_dir": False,
                    "size": size,
                    "children": [],
                }
            )
        return nodes

    return scan(base, 1), counter["files"], counter["dirs"]


def file_index(
    root: Path | None = None,
    max_files: int = 60,
    max_chars: int = 2000,
) -> tuple[list[str], int]:
    """工作区文件清单（给模型看「这里都有什么」）。

    只列文件不列目录（结构已经体现在路径里）；超过上限就截断并注明还剩多少，
    免得大工程把上下文撑爆 —— 那时模型可以自己调 ``list_files`` 看子目录。

    Returns:
        ``(清单行, 文件总数)``。
    """
    base = root or code_root()
    nodes, total, _dirs = build_tree(base)

    lines: list[str] = []
    size = 0

    def walk(items: list[dict]) -> None:
        nonlocal size
        for item in items:
            if item.get("is_dir"):
                walk(item.get("children") or [])
                continue
            if len(lines) >= max_files or size >= max_chars:
                return
            try:
                # 统一用 / ：模型写相对路径也是这种写法，看起来才一致
                relative = Path(item["path"]).resolve().relative_to(base.resolve()).as_posix()
            except (OSError, ValueError):
                relative = str(item.get("name", ""))
            line = f"- {relative}（{_human_size_show(int(item.get('size') or 0))}）"
            lines.append(line)
            size += len(line)

    walk(nodes)
    left = total - len(lines)
    if left > 0:
        lines.append(f"- …还有 {left} 个文件未列出，需要时用 list_files 看具体目录")
    return lines, total


def file_paths(root: Path | None = None) -> list[str]:
    """工作区里的**全部文件路径**（按文件名找文件时用）。

    与 ``file_index`` / 右侧工作区面板同一套扫描：跳过 .git / node_modules / dist 这些
    又大又没用的目录，并且有深度与条数上限。所以这里能解析到的，正好是模型在
    ``list_files`` 里看得到的那一批文件 —— 不会为了找一个名字去递归整个工程。

    （用处：编程模式下模型手里只有文件名，而用户上传的图 / 它自己写进工作区的文件都不在
    ``data/artifacts`` 里，按名字找就会「找不到参考图」。）
    """
    base = root or code_root()
    nodes, _files, _dirs = build_tree(base)
    found: list[str] = []
    stack = list(nodes)
    while stack:
        node = stack.pop()
        if node.get("is_dir"):
            stack.extend(node.get("children") or [])
            continue
        path = str(node.get("path") or "")
        if path:
            found.append(path)
    return found


def _human_size_show(size: int) -> str:
    from paper_agent.utils.files import human_readable_size

    return human_readable_size(size)


def _unique_path(path: Path) -> Path:
    """同名时加 ``-1`` / ``-2``，绝不覆盖工程目录里已有的文件。"""
    if not path.exists():
        return path
    index = 1
    while True:
        candidate = path.parent / f"{path.stem}-{index}{path.suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def import_attachments(root: Path | None = None, attachments=None) -> list[str]:
    """把本轮上传的附件复制进工程目录，返回复制进去的**文件名**列表。

    为什么必须复制：附件平时存在应用的 ``data/attachments`` 里，而编程模式的工具
    （``list_files`` / ``read_file`` / ``run_command``）都钉在**工程目录**内 ——
    不复制进来的话，模型在工程目录里根本看不到用户传的文件，只能回「找不到这个文件」；
    图片也一样（模型能"看图"，但要写进代码 ``<img src="...">`` 得先有真实文件）。

    幂等：同名且大小一致就跳过（重试 / 连续对话不会堆一堆副本）；
    同名但内容不同则加 ``-1`` 后缀，**绝不覆盖**用户已有的工程文件。
    """
    import shutil

    base = Path(root or code_root())
    copied: list[str] = []
    for item in attachments or []:
        source = Path(str(getattr(item, "path", "") or ""))
        if not source.is_file():
            continue
        try:
            base.mkdir(parents=True, exist_ok=True)
            existing = base / source.name
            if existing.is_file() and existing.stat().st_size == source.stat().st_size:
                copied.append(existing.name)
                continue
            target = _unique_path(existing)
            shutil.copy2(source, target)
        except OSError:
            continue
        copied.append(target.name)
    return copied


def resolve(path: str, *, root: Path | None = None) -> Path:
    """把模型给的文件名解析成绝对路径（相对路径按工程目录算）。"""
    target = Path(path or "").expanduser()
    if not target.is_absolute():
        target = (root or code_root()) / target
    try:
        return target.resolve()
    except OSError:
        return target


def is_inside(target: Path, root: Path | None = None) -> bool:
    """目标是否在工程目录内（Windows 下大小写不敏感）。"""
    base = (root or code_root()).resolve()
    text = str(target).lower().rstrip("\\/")
    prefix = str(base).lower().rstrip("\\/")
    return text == prefix or text.startswith(prefix + "\\") or text.startswith(prefix + "/")


def _run(
    command: str,
    cwd: Path,
    timeout: int,
    cancel: threading.Event | None,
    on_output: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """执行一条命令并收集输出（供 shell 命令与代码执行共用）。

    ``on_output`` 会在每读到一行时被调用：界面据此实时显示命令进度，
    而不是等命令跑完才看到结果。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.Popen(      # noqa: S602 - 工程目录内执行，已过黑名单
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd),
            env=env,
        )
    except Exception as exc:          # noqa: BLE001
        return False, f"无法启动进程：{type(exc).__name__}: {exc}"

    chunks: list[str] = []

    def _reader() -> None:
        try:
            for line in proc.stdout:      # type: ignore[union-attr]
                chunks.append(line)
                if on_output is not None:
                    try:
                        on_output(line)
                    except Exception:      # noqa: BLE001 - 推送失败不该影响命令
                        pass
        except Exception:                 # noqa: BLE001 - 进程被杀/管道关闭
            pass

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    def _collect() -> str:
        reader.join(timeout=3)
        text = "".join(chunks)
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + f"\n…（输出已截断，最多 {MAX_OUTPUT} 字符）"
        return text

    deadline = time.time() + timeout
    while True:
        try:
            code = proc.wait(timeout=0.3)
            output = _collect()
            if code != 0 and not output.strip():
                output = f"命令失败，退出码 {code}"
            return code == 0, output or "（无输出）"
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                proc.kill()
                return False, _collect() + "\n…已手动停止执行。"
            if time.time() >= deadline:
                proc.kill()
                return False, _collect() + f"\n…执行超时（{timeout} 秒），进程已终止。"
            continue


def _log_text(log: deque) -> str:
    text = "".join(log).strip()
    if len(text) > MAX_OUTPUT:
        text = text[-MAX_OUTPUT:]
    return text or "（没有输出）"


def _drain_output(
    proc: subprocess.Popen,
    log: deque,
    hook_getter: Callable[[], Callable[[str], None] | None] | None = None,
) -> None:
    """持续把子进程输出读进环形缓冲。

    必须一直读：管道写满后服务会阻塞在 printf/write 上，看起来就是「起不来」。
    ``hook_getter`` 每次现取回调（引擎在本次工具调用结束后会把它清掉），
    这样服务启动日志能实时显示，而之后的长跑日志不会一直往界面推。
    """
    try:
        for line in proc.stdout:      # type: ignore[union-attr]
            log.append(line)
            hook = hook_getter() if hook_getter is not None else None
            if hook is not None:
                try:
                    hook(line)
                except Exception:      # noqa: BLE001 - 推送失败不该影响服务
                    pass
    except Exception:                 # noqa: BLE001 - 进程退出/管道关闭
        pass


def _infer_url(command: str, output: str) -> str:
    """从启动日志 / 命令里猜访问地址。"""
    for pattern in (RE_URL_IN_TEXT, RE_LOCAL_URL):
        found = pattern.search(output or "")
        if found:
            return found.group(0).rstrip(".,;。，、")
    port = RE_PORT_HINT.search(command or "")
    if port:
        return f"http://127.0.0.1:{port.group(1)}"
    if "http.server" in (command or ""):
        # python -m http.server 8000 / python -m http.server（默认 8000）
        trailing = re.search(r"http\.server\s+(\d{2,5})", command)
        return f"http://127.0.0.1:{trailing.group(1) if trailing else 8000}"
    return ""


def _reachable(url: str) -> bool:
    """端口能不能连上（服务是否已经能接受请求）。"""
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _wait_reachable(url: str, timeout: float = SERVICE_PROBE_TIMEOUT) -> bool:
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if _reachable(url):
            return True
        time.sleep(0.4)
    return _reachable(url)


# ---------------------------------------------------------------- 工具
class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "在工程目录里写代码 / 配置文件。相对路径按工程目录算；只能写到工程目录内。"
        "写多文件项目时逐个调用即可，append=true 可往已有文件尾部追加。"
    )

    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = workspace or code_root()
        self.parameters = [
            ToolParam("path", "string", "文件路径，如 src/main.py（相对工程目录）"),
            ToolParam("content", "string", "文件内容"),
            ToolParam(
                "append", "string", "true=追加到文件末尾；false/留空=覆盖",
                required=False, enum=["true", "false"],
            ),
        ]

    def run(self, path: str = "", content: str = "", append: str = "false", **kwargs) -> ToolResult:
        target = resolve(path, root=self._workspace)
        if not is_inside(target, self._workspace):
            return ToolResult(
                success=False,
                error=f"只能写到工程目录内（{self._workspace}）：{target}",
            )
        if target.is_dir():
            return ToolResult(success=False, error=f"{target} 是目录，不是文件")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if str(append).strip().lower() == "true" else "w"
            with open(target, mode, encoding="utf-8") as file:
                file.write(content or "")
        except OSError as exc:
            return ToolResult(success=False, error=f"写入失败：{exc}")
        size = target.stat().st_size
        action = "已追加到" if mode == "a" else "已写入"
        return ToolResult(
            content=f"{action} {target}（{size} 字节）",
            artifact_paths=[str(target)] if is_inside(target, DATA_DIR) else [],
        )


class ReadCodeFileTool(Tool):
    name = "read_file"
    description = (
        "读取一个文件的内容（代码 / 配置 / 日志），默认最多 8000 字符、"
        "可用 offset 分页读大文件。相对路径按工程目录算。"
    )

    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = workspace or code_root()
        self.parameters = [
            ToolParam("path", "string", "文件路径（相对工程目录或绝对路径）"),
            ToolParam("offset", "integer", "从第几个字符开始读，默认 0", required=False),
            ToolParam("limit", "integer", "最多读多少字符，默认 8000", required=False),
        ]

    def run(self, path: str = "", offset: int = 0, limit: int = 8000, **kwargs) -> ToolResult:
        target = resolve(path, root=self._workspace)
        if not target.is_file():
            return ToolResult(success=False, error=f"文件不存在：{target}")
        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            return ToolResult(
                success=False,
                error=f"文件过大（{size} 字节），请用 offset/limit 分页读取，或先用命令查看摘要",
            )
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(success=False, error=f"读取失败：{exc}")
        start = max(0, int(offset or 0))
        count = max(1, min(int(limit or 8000), MAX_READ_BYTES))
        chunk = text[start : start + count]
        note = ""
        if start + count < len(text):
            note = f"\n…（还有 {len(text) - start - count} 字符，用 offset={start + count} 继续读）"
        return ToolResult(content=f"{target}\n\n{chunk}{note}")


class ListFilesTool(Tool):
    name = "list_files"
    description = "列出工程目录下的文件与子目录（最多 200 条），用于了解项目结构。"

    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = workspace or code_root()
        self.parameters = [
            ToolParam("path", "string", "要列出的子目录，留空表示工程根目录", required=False),
        ]

    def run(self, path: str = "", **kwargs) -> ToolResult:
        target = resolve(path, root=self._workspace)
        if not target.is_dir():
            return ToolResult(success=False, error=f"目录不存在：{target}")
        lines: list[str] = [f"{target}"]
        try:
            entries = sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name))
        except OSError as exc:
            return ToolResult(success=False, error=f"读取目录失败：{exc}")
        for item in entries[:MAX_LIST_ENTRIES]:
            if item.is_dir():
                lines.append(f"  [目录] {item.name}/")
            else:
                try:
                    size = item.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"  {item.name}  ({size} 字节)")
        if len(entries) > MAX_LIST_ENTRIES:
            lines.append(f"  …还有 {len(entries) - MAX_LIST_ENTRIES} 项")
        return ToolResult(content="\n".join(lines))


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "在工程目录内执行命令：装依赖（pip install xxx）、跑脚本（python main.py）、"
        "跑测试（pytest）、初始化项目（npm init）等。工作目录固定为工程目录，"
        "破坏性系统指令会被拒绝；长时间任务可给 timeout（秒，默认 120，上限 900）。"
    )

    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = workspace or code_root()
        self._cancel = None
        self._output_hook: Callable[[str], None] | None = None
        self.parameters = [
            ToolParam("command", "string", "要执行的命令，如 pip install requests"),
            ToolParam(
                "timeout", "integer",
                f"超时秒数，默认 {DEFAULT_TIMEOUT}，最大 {MAX_TIMEOUT}",
                required=False,
            ),
        ]

    def set_cancel(self, cancel) -> None:      # noqa: D102 - 支持 Esc 停止
        self._cancel = cancel

    def set_output_hook(self, hook) -> None:   # noqa: D102 - 输出实时回传界面
        self._output_hook = hook

    def run(self, command: str = "", timeout: int = DEFAULT_TIMEOUT, **kwargs) -> ToolResult:
        text = (command or "").strip()
        if not text:
            return ToolResult(success=False, error="命令为空")
        lowered = text.lower()
        for token in DANGEROUS_COMMANDS:
            if token in lowered:
                return ToolResult(
                    success=False,
                    error=f"拒绝执行：命令包含破坏性操作「{token.strip()}」",
                )
        limit = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
        success, output = _run(text, self._workspace, limit, self._cancel, self._output_hook)
        if not success and "执行超时" in output and RE_SERVICE_HINT.search(lowered):
            # 常驻服务用 run_command 跑必然撞超时：直接点出正确做法，
            # 免得模型一轮一轮地加 timeout 硬扛
            output += (
                "\n提示：这条命令是长期运行的服务，run_command 会一直阻塞到超时。"
                "请改用 start_service 后台启动，它会立刻返回并由应用打开访问地址。"
            )
        return ToolResult(
            content=f"$ {text}\n{output}",
            success=success,
            error="" if success else output,
        )


class StartServiceTool(Tool):
    """后台启动长期运行的服务（dev server / API / 静态服务器）。

    与 :class:`RunCommandTool` 的区别是**不等进程结束**：命令一起就返回，
    进程继续在后台跑。这样模型才敢启动 ``npm run dev`` 这类常驻命令
    （用 run_command 跑会一直阻塞到超时）。

    模型所在的环境没有 GUI，开不了浏览器；这里把地址随结果一起交出去，
    由界面用系统默认浏览器代开（``ToolResult.open_url``）。
    """

    name = "start_service"
    description = (
        "在后台启动需要长期运行的服务（前端 dev server、后端 API、静态文件服务器等）："
        "命令立刻返回、进程在后台继续跑，识别到访问地址后应用会用默认浏览器自动打开。"
        "用法示例：command=\"python -m http.server 8000\"、command=\"npm run dev\"。"
        "注意：一次性命令（pip install、pytest、python script.py）请用 run_command。"
    )

    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = workspace or code_root()
        self._cancel = None
        self._output_hook: Callable[[str], None] | None = None
        self.parameters = [
            ToolParam("command", "string", "启动命令，如 npm run dev / python -m http.server 8000"),
            ToolParam(
                "url", "string",
                "服务访问地址，如 http://127.0.0.1:8000（留空则从启动日志里自动识别）",
                required=False,
            ),
            ToolParam(
                "wait", "integer",
                f"启动后观察几秒再返回，默认 {SERVICE_WAIT}，最大 {SERVICE_MAX_WAIT}",
                required=False,
            ),
        ]

    def set_cancel(self, cancel) -> None:      # noqa: D102 - 支持 Esc 停止
        self._cancel = cancel

    def set_output_hook(self, hook) -> None:   # noqa: D102 - 启动日志实时回传界面
        self._output_hook = hook

    def run(self, command: str = "", url: str = "", wait: int = SERVICE_WAIT, **kwargs) -> ToolResult:
        text = (command or "").strip()
        if not text:
            return ToolResult(success=False, error="启动命令为空")
        lowered = text.lower()
        for token in DANGEROUS_COMMANDS:
            if token in lowered:
                return ToolResult(
                    success=False,
                    error=f"拒绝执行：命令包含破坏性操作「{token.strip()}」",
                )

        running = _SERVICES.get(text)
        if running is not None and running.poll() is None:
            # 同一条命令已经在跑：复用，别把同一个端口占两遍
            target = (url or "").strip() or _SERVICE_URLS.get(text, "")
            return ToolResult(
                content=(
                    f"这条命令已经在后台运行（PID {running.pid}），没有重复启动。"
                    + (f"\n访问地址：{target}" if target else "")
                ),
                open_url=target,
            )

        seconds = max(1, min(int(wait or SERVICE_WAIT), SERVICE_MAX_WAIT))
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        flags = 0
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # 别弹黑框
        try:
            proc = subprocess.Popen(      # noqa: S602 - 工程目录内启动，已过黑名单
                text,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(self._workspace),
                env=env,
                creationflags=flags,
            )
        except Exception as exc:          # noqa: BLE001
            return ToolResult(success=False, error=f"无法启动进程：{type(exc).__name__}: {exc}")

        log: deque = deque(maxlen=SERVICE_LOG_LINES)
        _SERVICES[text] = proc
        _SERVICE_LOGS[text] = log
        threading.Thread(
            target=_drain_output,
            args=(proc, log, lambda: self._output_hook),
            daemon=True,
        ).start()

        # 先观察几秒：启动命令写错的话进程会当场退出，日志要原样回灌给模型
        deadline = time.time() + seconds
        while time.time() < deadline:
            if proc.poll() is not None:
                _SERVICES.pop(text, None)
                return ToolResult(
                    success=False,
                    error=(
                        f"命令启动后立刻退出（退出码 {proc.returncode}），服务没起来。\n"
                        f"启动日志：\n{_log_text(log)}"
                    ),
                )
            if self._cancel is not None and self._cancel.is_set():
                proc.kill()
                _SERVICES.pop(text, None)
                return ToolResult(success=False, error="已手动停止启动")
            time.sleep(0.2)

        target = (url or "").strip() or _infer_url(text, _log_text(log))
        _SERVICE_URLS[text] = target
        ready = _wait_reachable(target) if target else False

        lines = [f"服务已在后台运行（PID {proc.pid}，命令：{text}）"]
        if target:
            lines.append(f"访问地址：{target}")
            lines.append("端口已就绪，可以直接访问。" if ready else "端口暂时还没响应（可能仍在编译 / 启动中，稍等刷新即可）。")
        else:
            lines.append(
                "没能从启动日志里识别出访问地址。若服务有固定端口，请把 url 参数填上再调一次。"
            )
        lines.append(f"启动日志：\n{_log_text(log)}")
        return ToolResult(content="\n".join(lines), open_url=target)


def register_code_skills(registry, workspace: Path | None = None):
    """注册编程模式专用工具。"""
    root = workspace or code_root()
    for tool in (
        WriteFileTool(root),
        ReadCodeFileTool(root),
        ListFilesTool(root),
        RunCommandTool(root),
        StartServiceTool(root),
    ):
        registry.register(tool)
    return registry


__all__ = [
    "code_root",
    "configured_root",
    "default_root",
    "build_tree",
    "file_index",
    "resolve",
    "is_inside",
    "register_code_skills",
    "WriteFileTool",
    "ReadCodeFileTool",
    "ListFilesTool",
    "RunCommandTool",
    "StartServiceTool",
]
