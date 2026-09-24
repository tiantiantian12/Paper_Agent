"""start_service：后台起服务 + 把访问地址交出去让应用开浏览器。

模型跑在无 GUI 的工具环境里，它自己开不了浏览器（所以常回一句「我这边无法弹出
浏览器」）；能做的是把服务跑起来并把地址交出来。
"""

from __future__ import annotations

import socket
import subprocess
import sys

import pytest

from paper_agent.services.agents.engine import OPEN, AgentOrchestrator
from paper_agent.services.skills.base import Tool, ToolParam, ToolRegistry, ToolResult
from paper_agent.services.skills.code_workspace import StartServiceTool, _infer_url


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def kill_tree(proc: subprocess.Popen) -> None:
    """连子进程一起收掉（Windows 下 shell=True 会多一层 cmd.exe）。"""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        proc.terminate()


def test_infer_url_from_log_command_and_blank():
    assert _infer_url("npm run dev", "  Local:   http://localhost:5173/\n") == (
        "http://localhost:5173/"
    )
    assert _infer_url("python -m http.server 9000", "") == "http://127.0.0.1:9000"
    assert _infer_url("flask run --port=5000", "") == "http://127.0.0.1:5000"
    assert _infer_url("node server.js", "") == ""


def test_command_that_exits_immediately_is_reported(tmp_path):
    tool = StartServiceTool(tmp_path)

    result = tool.run(command=f'"{sys.executable}" -c "pass"', wait=1)

    assert not result.success
    assert "立刻退出" in result.error
    assert result.open_url == ""


def test_starts_local_server_and_returns_url(tmp_path):
    port = free_port()
    tool = StartServiceTool(tmp_path)
    command = f'"{sys.executable}" -m http.server {port}'
    streamed: list[str] = []
    tool.set_output_hook(streamed.append)

    result = tool.run(command=command, wait=1)

    from paper_agent.services.skills import code_workspace

    proc = code_workspace._SERVICES.get(command)
    try:
        assert result.success, result.error
        assert result.open_url == f"http://127.0.0.1:{port}"
        assert "端口已就绪" in result.content
        assert proc is not None and proc.poll() is None, "后台进程应该还活着"

        assert any("Serving HTTP" in chunk for chunk in streamed), (
            f"启动日志要实时推给卡片，实际收到：{streamed}"
        )

        # 同一条命令再调一次：复用而不是把端口占两遍
        again = tool.run(command=command, wait=1)
        assert again.success
        assert "已经在后台运行" in again.content
        assert again.open_url == result.open_url
    finally:
        if proc is not None:
            kill_tree(proc)
        code_workspace._SERVICES.pop(command, None)
        code_workspace._SERVICE_URLS.pop(command, None)


def test_dangerous_command_is_refused(tmp_path):
    result = StartServiceTool(tmp_path).run(command="shutdown -s -t 0", wait=1)

    assert not result.success
    assert "破坏性" in result.error


def test_empty_command_is_refused(tmp_path):
    result = StartServiceTool(tmp_path).run(command="  ", wait=1)

    assert not result.success
    assert "命令为空" in result.error


# ---------------------------------------------------------------- 事件打通
class StartServiceStub(Tool):
    """假的 start_service：只负责交出一个地址。"""

    name = "start_service"
    description = "起服务"
    parameters = [ToolParam("command", "string", "启动命令")]

    def run(self, **kwargs) -> ToolResult:
        return ToolResult(content="服务已在后台运行", open_url="http://127.0.0.1:8000")


class _FakeClient:
    def __init__(self) -> None:
        self.round = 0
        self.last_finish_reason = "tool_calls"
        self.reasoning_dropped = False

    def stream_events(self, messages, tools=None, temperature=0.7, max_tokens=0):
        self.round += 1
        if self.round == 1:
            yield {
                "type": "tool_calls",
                "calls": [
                    {
                        "id": "c1",
                        "name": "start_service",
                        "arguments": '{"command": "python -m http.server 8000"}',
                    }
                ],
            }
            return
        yield {"type": "content", "text": "服务已启动"}


def test_engine_emits_open_event():
    registry = ToolRegistry()
    registry.register(StartServiceStub())
    events: list[tuple[str, str]] = []
    engine = AgentOrchestrator(client=_FakeClient(), registry=registry)
    engine._messages = [{"role": "user", "content": "把项目跑起来"}]

    engine.run(lambda kind, text: events.append((kind, text)))

    opened = [text for kind, text in events if kind == OPEN]
    assert opened, "工具交出 open_url 时引擎要发 open_url 事件"
    assert "http://127.0.0.1:8000" in opened[0]


def test_worker_forwards_open_url_signal():
    """事件名必须一路对得上：引擎 emit 的 kind → worker 信号 → 界面打开浏览器。"""
    from paper_agent.services.agent_service import _StreamWorker

    payload = '{"url": "http://127.0.0.1:8000", "name": "start_service"}'
    worker = _StreamWorker(producer=lambda emit: emit("open_url", payload))
    seen: list[str] = []
    worker.open_url.connect(seen.append)

    worker.run()

    assert seen == [payload]
