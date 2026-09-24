"""代码执行沙箱：让智能体自己写 Python 完成系统任务（扫盘、统计、清理等）。

安全分三层（尽力而为的防护，非绝对隔离）：

1. **静态检查**：AST 扫描，拦截子进程/系统命令、动态执行逃逸；
2. **路径分级**：删除仅限「可清理白名单」（临时目录、浏览器缓存、回收站）；
   写入仅限产物目录与临时目录；受保护目录（Windows / Program Files / 用户主目录等）
   一律禁止写入与删除；
3. **运行时隔离**：子进程执行 + 超时 + 输出截断，崩溃不影响主程序。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from paper_agent.core.constants import ARTIFACTS_DIR
from paper_agent.services.skills.base import Tool, ToolParam, ToolResult

MAX_TIMEOUT = 600           # 全盘扫描等重任务可能耗时数分钟
DEFAULT_TIMEOUT = 60
MAX_OUTPUT = 8000

# ---------------------------------------------------------------- 路径策略
def _env_path(name: str, fallback: str = "") -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else (Path(fallback) if fallback else None)


def _workspace_dirs() -> list[Path]:
    """本会话工作区里允许读写的一对目录（upload / generated）。

    产物已经从 ``data/artifacts`` 搬进会话工作区（见 ``services/session_workspace.py``），
    沙箱不放开这两个目录的话，文档模式下用 execute_python 写出来的文件就落不进
    本会话的工作区，模型会回「不允许写入」。
    """
    from paper_agent.services import session_workspace

    return [
        session_workspace.upload_dir(create=False),
        session_workspace.generated_dir(create=False),
    ]


def cleanable_dirs() -> list[Path]:
    """允许删除的目录白名单（临时文件 / 缓存 / 回收站 / 产物目录）。"""
    dirs: list[Path] = [
        Path(tempfile.gettempdir()),
        ARTIFACTS_DIR,
        *_workspace_dirs(),
    ]
    windir = _env_path("WINDIR", r"C:\Windows")
    if windir:
        dirs.append(windir / "Temp")
    local = _env_path("LOCALAPPDATA")
    if local:
        dirs.append(local / "Temp")
        for vendor in (
            "Google\\Chrome\\User Data\\Default\\Cache",
            "Microsoft\\Edge\\User Data\\Default\\Cache",
        ):
            dirs.append(local / vendor)
    for drive in ("C:\\", "D:\\"):
        if Path(drive).exists():
            dirs.append(Path(drive) / "$Recycle.Bin")
    return [d for d in dirs if d]


def protected_dirs() -> list[Path]:
    """禁止写入 / 删除的受保护目录。"""
    dirs: list[Path] = [Path("C:\\")]
    windir = _env_path("WINDIR", r"C:\Windows")
    if windir:
        dirs.append(windir)
    for folder in ("Program Files", "Program Files (x86)", "ProgramData"):
        candidate = Path("C:\\") / folder
        if candidate.exists():
            dirs.append(candidate)
    home = Path.home()
    if home:
        dirs.append(home)
    return dirs


def writable_dirs() -> list[Path]:
    """允许写入的目录（本会话工作区 + 旧产物目录 + 临时目录）。"""
    return [ARTIFACTS_DIR, *_workspace_dirs(), Path(tempfile.gettempdir())]


def _under(target: Path, parent: Path) -> bool:
    """路径包含判断（Windows 下忽略大小写）。"""
    text = str(target).lower().rstrip("\\/")
    base = str(parent).lower().rstrip("\\/")
    return text == base or text.startswith(base + "\\") or text.startswith(base + "/")


def is_cleanable(path: str) -> bool:
    """是否允许删除。"""
    try:
        target = Path(os.path.abspath(os.path.expanduser(path)))
    except Exception:            # noqa: BLE001 - 路径非法
        return False
    return any(_under(target, allowed) for allowed in cleanable_dirs())


def is_writable(path: str, workspace: Path | None = None) -> bool:
    """是否允许写入（编程模式下工程目录也算合法写入范围）。"""
    try:
        target = Path(os.path.abspath(os.path.expanduser(path)))
    except Exception:            # noqa: BLE001
        return False
    if any(_under(target, allowed) for allowed in writable_dirs()):
        return True
    return workspace is not None and _under(target, workspace)


# ---------------------------------------------------------------- 静态检查
FORBIDDEN_CALLS = {
    "os.system", "os.popen", "os.execv", "os.execve", "os.execvp", "os.execvpe",
    "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnv", "os.spawnve", "os.spawnvp",
    "subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "subprocess.getoutput", "subprocess.getstatusoutput",
    "pty.spawn", "commands.getoutput", "commands.getstatusoutput",
}
FORBIDDEN_NAMES = {"eval", "exec", "compile", "__import__"}
DELETE_CALLS = {
    "os.remove", "os.unlink", "os.rmdir", "os.removedirs", "shutil.rmtree",
    "remove", "unlink", "rmdir", "removedirs", "rmtree",
}
# 纯文本黑名单：防御经字符串绕过执行系统命令
TEXT_BLACKLIST = (
    "format c:", "del /f", "del /s", "rm -rf /", "shutdown", "taskkill",
    "reg delete", "diskpart", "cipher /w", "takeown", "icacls",
)


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        parent = _dotted(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _as_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class _SafetyVisitor(ast.NodeVisitor):
    def __init__(self, relaxed: bool = False, workspace: Path | None = None) -> None:
        self.violations: list[str] = []
        # 编程模式（relaxed）：允许子进程 / 动态执行，但写删仍限制在工程目录内
        self.relaxed = relaxed
        self.workspace = workspace

    def visit_Call(self, node: ast.Call) -> None:      # noqa: N802
        name = _dotted(node.func)
        last = name.split(".")[-1] if name else ""

        if not self.relaxed:
            if name in FORBIDDEN_CALLS:
                self.violations.append(f"禁止执行系统命令或子进程：{name}()")
            if last in FORBIDDEN_NAMES and name in FORBIDDEN_NAMES:
                self.violations.append(f"禁止动态代码执行：{name}()")
        if name in DELETE_CALLS or last in DELETE_CALLS:
            self._check_delete(node, name)
        if last == "open" and len(node.args) >= 2:
            mode = (_as_str(node.args[1]) or "").lower()
            if any(token in mode for token in ("w", "a", "x")):
                self._check_write(node.args[0])

        self.generic_visit(node)

    def _check_delete(self, node: ast.Call, name: str) -> None:
        if not node.args:
            self.violations.append(f"{name}() 缺少路径参数，已拒绝")
            return
        path = _as_str(node.args[0])
        if path is None:
            self.violations.append(
                f"{name}() 的路径不是字面量，无法安全校验，已拒绝。"
                "请写成具体路径字符串（如 r'C:\\Users\\xxx\\AppData\\Local\\Temp\\a.tmp'）。"
            )
            return
        if not is_cleanable(path) and not self._in_workspace(path):
            self.violations.append(
                f"不允许删除「{path}」：只能清理临时目录、浏览器缓存、回收站等白名单位置，"
                "或工程目录内的文件。"
            )

    def _check_write(self, node: ast.AST) -> None:
        path = _as_str(node)
        if path is None:
            # 动态路径无法校验，交由运行时处理；此处不阻断
            return
        if not is_writable(path, self.workspace):
            self.violations.append(
                f"不允许写入「{path}」：仅可写入产物目录、系统临时目录"
                + ("或工程目录。" if self.workspace else "。")
            )

    def _in_workspace(self, path: str) -> bool:
        if self.workspace is None:
            return False
        try:
            target = Path(os.path.abspath(os.path.expanduser(path)))
        except Exception:            # noqa: BLE001
            return False
        return _under(target, self.workspace)


def static_check(
    code: str, *, relaxed: bool = False, workspace: Path | None = None
) -> list[str]:
    """静态安全检查，返回违规说明列表（空列表表示通过）。

    ``relaxed``（编程模式）放开「禁止子进程 / 动态执行」的限制，
    因为编程模式本身就要装依赖、跑构建；写入与删除仍限定在工程目录内。
    """
    violations: list[str] = []
    lowered = (code or "").lower()
    for token in TEXT_BLACKLIST:
        if token in lowered:
            violations.append(f"代码包含危险指令：{token}")

    try:
        tree = ast.parse(code or "")
    except SyntaxError as exc:
        return [f"代码语法错误：{exc}"]

    visitor = _SafetyVisitor(relaxed=relaxed, workspace=workspace)
    visitor.visit(tree)
    violations.extend(visitor.violations)
    return violations


# ---------------------------------------------------------------- 执行
def run_python(
    code: str,
    timeout: int = DEFAULT_TIMEOUT,
    cancel: threading.Event | None = None,
    workspace: Path | None = None,
    relaxed: bool = False,
    on_output: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """执行 Python 代码。

    Args:
        code: 待执行代码
        timeout: 超时秒数
        cancel: 取消事件，置位后会立即终止子进程（用于用户按 Esc 停止生成）
        workspace: 编程模式的工程目录：作为运行目录，并放开写删范围到该目录内
        relaxed: 编程模式下放开「禁止子进程 / 动态执行」的限制，允许装依赖、跑构建
        on_output: 每读到一行输出就回调一次，供界面实时展示执行进度

    Returns:
        ``(是否成功, 输出文本)``。
    """
    violations = static_check(code, relaxed=relaxed, workspace=workspace)
    if violations:
        return False, "安全检查未通过：\n" + "\n".join(f"- {v}" for v in violations)

    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))

    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".py", delete=False, encoding="utf-8"
    )
    try:
        handle.write(code)
        script = handle.name
    finally:
        handle.close()

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    run_dir = str(workspace) if workspace is not None else tempfile.gettempdir()
    try:
        proc = subprocess.Popen(        # noqa: S603 - 已做静态检查，输入为模型生成代码
            [sys.executable, "-u", script],     # -u：无缓冲，超时时才能拿到已输出部分
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=run_dir,
            env=env,
        )
    except Exception as exc:            # noqa: BLE001 - 兜底
        return False, f"无法启动执行进程：{type(exc).__name__}: {exc}"

    # 用后台线程持续读取输出：这样即使超时，也能拿到已完成部分的结果
    chunks: list[str] = []

    def _reader() -> None:
        try:
            for line in proc.stdout:      # type: ignore[union-attr]
                chunks.append(line)
                if on_output is not None:
                    try:
                        on_output(line)
                    except Exception:      # noqa: BLE001 - 推送失败不该影响执行
                        pass
        except Exception:                 # noqa: BLE001 - 进程被杀或管道关闭
            pass

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    def _collect() -> str:
        reader.join(timeout=3)
        text = "".join(chunks)
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + f"\n…（输出已截断，最多显示 {MAX_OUTPUT} 字符）"
        return text

    deadline = time.time() + timeout
    try:
        while True:
            try:
                return_code = proc.wait(timeout=0.3)
                output = _collect()
                success = return_code == 0
                if not success and not output.strip():
                    output = f"执行失败，退出码 {return_code}"
                return success, output or "（无输出）"
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    partial = _collect()
                    note = "\n…已手动停止执行，以上是已完成部分的输出。"
                    if partial.strip():
                        return False, partial + note
                    return False, "已手动停止执行（无输出）"
                if time.time() >= deadline:
                    raise
    except subprocess.TimeoutExpired:
        # 超时也保留已产出的部分结果，便于模型基于进度继续或收窄范围重试
        proc.kill()
        partial = _collect()
        note = (
            f"\n…执行超过 {timeout} 秒被中断，以上是已完成部分的输出。"
            "建议缩小扫描范围或按目录分批后再执行。"
        )
        if partial.strip():
            return False, partial + note
        return False, f"执行超时（超过 {timeout} 秒），且尚未产生任何输出"
    except Exception as exc:            # noqa: BLE001 - 兜底
        proc.kill()
        return False, f"执行异常：{type(exc).__name__}: {exc}"
    finally:
        try:
            os.unlink(script)
        except OSError:
            pass


# ---------------------------------------------------------------- Tool
class ExecutePythonTool(Tool):
    """执行 Python 代码完成系统任务（扫盘、统计、清理等）。"""

    name = "execute_python"
    description = (
        "执行 Python 代码完成系统任务：扫描磁盘占用、统计大文件、"
        "查找临时/缓存文件、清理系统临时目录与回收站、生成报告、绘图出图等。"
        "注意：**不要**用它拼装 Word / PPT / PDF / Excel 成品 —— 自己拼的文档不会带"
        "论文排版、三线表、目录域与页码，请改用 create_docx / create_pptx / create_pdf 等工具。"
        "安全限制：禁止执行系统命令与子进程；写文件仅限临时目录与产物目录；"
        "删除仅限系统临时目录、浏览器缓存、回收站等白名单，"
        "禁止删除用户文档、桌面、Program Files 与 Windows 目录。"
        "扫描是全盘只读允许的，但删除只认白名单路径。\n"
        "注意：全盘扫描等重任务耗时很长，请显式传入更大的 timeout（最大 600 秒），"
        "并优先按单个目录分批处理；若超时，会返回已完成部分的结果，可据此继续或收窄范围。"
    )

    def __init__(self, workspace=None, relaxed: bool = False) -> None:
        """``workspace`` / ``relaxed`` 供编程模式使用（运行目录 + 放开子进程限制）。"""
        self.parameters = [
            ToolParam("code", "string", "要执行的 Python 代码（需自带 import）"),
            ToolParam(
                "timeout", "integer",
                f"超时秒数，默认 {DEFAULT_TIMEOUT}，最大 {MAX_TIMEOUT}",
                required=False,
            ),
        ]
        self._cancel = None
        self._workspace = workspace
        self._relaxed = relaxed
        self._output_hook = None

    def set_cancel(self, cancel) -> None:
        self._cancel = cancel

    def set_output_hook(self, hook) -> None:
        self._output_hook = hook

    def run(self, code: str = "", timeout: int = DEFAULT_TIMEOUT, **kwargs) -> ToolResult:
        success, output = run_python(
            code,
            timeout,
            cancel=self._cancel,
            workspace=self._workspace,
            relaxed=self._relaxed,
            on_output=self._output_hook,
        )
        return ToolResult(
            content=output,
            success=success,
            error="" if success else output,
        )
