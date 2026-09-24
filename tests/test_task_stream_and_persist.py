"""终端任务卡片：输出实时流 + 记录持久化。

两件事：
1. 命令跑着的时候就能看到输出（不是等结束才一次性出现）；
2. 跑过什么、跑了多久、输出是什么要落库，重开软件后卡片还得在。
"""

from __future__ import annotations

import json
import sys

import pytest

from paper_agent.core.models import Message, TaskRecord
from paper_agent.services.agents.engine import (
    OUTPUT,
    OUTPUT_FLUSH_CHARS,
    AgentOrchestrator,
    _OutputPump,
)
from paper_agent.services.skills.base import Tool, ToolParam, ToolResult
from paper_agent.services.skills.code_workspace import RunCommandTool
from paper_agent.ui.chat.task_card import TerminalTaskCard


def _assistant_widget():
    from paper_agent.ui.chat.message_widget import MessageWidget

    return MessageWidget(Message(role="assistant", content="", status="streaming"))


# ---------------------------------------------------------------- 输出合并泵
def test_output_pump_coalesces_and_flushes_tail():
    emitted: list[str] = []
    pump = _OutputPump(emitted.append)

    for index in range(200):
        pump.feed(f"line {index}\n")
    pump.close()

    assert emitted, "合并后仍要发出事件"
    assert "".join(emitted).count("\n") == 200, "内容不能丢"
    assert len(emitted) < 200, "不该一行一个事件"


def test_output_pump_ignores_feed_after_close():
    emitted: list[str] = []
    pump = _OutputPump(emitted.append)
    pump.close()

    pump.feed("后台服务又打了一行日志")

    assert emitted == []


# ---------------------------------------------------------------- 工具侧回调
def test_run_command_streams_output_to_hook(tmp_path):
    seen: list[str] = []
    tool = RunCommandTool(tmp_path)
    tool.set_output_hook(seen.append)

    result = tool.run(command=f'"{sys.executable}" -c "print(\'hello stream\')"', timeout=30)

    assert result.success
    assert any("hello stream" in chunk for chunk in seen), seen


def test_engine_emits_output_events():
    class _ChattyTool(Tool):
        name = "run_command"
        description = "跑命令"
        parameters = [ToolParam("command", "string", "命令")]

        def set_output_hook(self, hook) -> None:
            self._hook = hook

        def run(self, **kwargs) -> ToolResult:
            self._hook("第一行\n")
            self._hook("第二行\n")
            return ToolResult(content="done")

    class _Client:
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
                        {"id": "c1", "name": "run_command", "arguments": '{"command": "x"}'}
                    ],
                }
                return
            yield {"type": "content", "text": "跑完了"}

    from paper_agent.services.skills.base import ToolRegistry

    registry = ToolRegistry()
    registry.register(_ChattyTool())
    events: list[tuple[str, str]] = []
    engine = AgentOrchestrator(client=_Client(), registry=registry)
    engine._messages = [{"role": "user", "content": "跑一下"}]

    engine.run(lambda kind, text: events.append((kind, text)))

    chunks = [json.loads(text)["chunk"] for kind, text in events if kind == OUTPUT]
    assert "".join(chunks) == "第一行\n第二行\n"
    assert all(json.loads(text)["args"] for kind, text in events if kind == OUTPUT), (
        "事件要带上入参，界面靠「工具名 + 入参」找卡片"
    )


# ---------------------------------------------------------------- 卡片实时渲染
def test_card_streams_output_and_auto_expands(qapp):
    card = TerminalTaskCard("pytest -q", "run_command")

    card.append_output("collecting ...\n")
    card._flush_output()

    assert card.expanded, "有输出就该展开，否则看不到进度"
    assert "collecting" in card.output_text()


def test_card_keeps_only_tail_of_huge_output(qapp):
    card = TerminalTaskCard("pip install -r requirements.txt", "run_command")

    card.append_output("".join(f"line {index}\n" for index in range(900)))
    card._flush_output()

    lines = card.output_text().splitlines()
    assert len(lines) <= TerminalTaskCard.MAX_OUTPUT_LINES
    assert lines[-1] == "line 899", "留的应该是最新的输出"


def test_card_flushes_pending_output_on_finish(qapp):
    card = TerminalTaskCard("pytest -q", "run_command")

    card.append_output("3 passed\n")
    card.finish(True, output="结果摘要")

    assert "3 passed" in card.output_text(), "结束时要把没渲染完的输出补上"
    assert "结果摘要" not in card.output_text(), "已经实时流过的内容不该被截断的摘要覆盖"


def test_worker_forwards_output_signal():
    """事件名要一路对得上：引擎 emit 的 kind → worker 信号 → 界面写进卡片。"""
    from paper_agent.services.agent_service import _StreamWorker

    payload = '{"name": "run_command", "args": "{}", "chunk": "collecting\\n"}'
    worker = _StreamWorker(producer=lambda emit: emit("output", payload))
    seen: list[str] = []
    worker.output.connect(seen.append)

    worker.run()

    assert seen == [payload]


def test_message_widget_forwards_output_to_card(qapp):
    widget = _assistant_widget()
    card = widget.begin_terminal_task("run_command", "pytest -q", "k")

    assert widget.append_terminal_output("k", "collecting\n") is True
    card._flush_output()
    assert "collecting" in card.output_text()

    assert widget.append_terminal_output("不存在", "x") is False


# ---------------------------------------------------------------- 记录持久化
def test_finished_task_is_recorded_on_message(qapp):
    widget = _assistant_widget()
    widget.begin_terminal_task("run_command", "pytest -q", "k")
    widget.finish_terminal_task("k", success=True, output="3 passed in 0.4s")

    records = widget.message.tasks
    assert len(records) == 1
    record = records[0]
    assert record.tool == "run_command"
    assert record.command == "pytest -q"
    assert record.status == "done"
    assert record.output == "3 passed in 0.4s"
    assert record.anchor == 0


def test_aborted_task_is_recorded_as_aborted(qapp):
    widget = _assistant_widget()
    widget.begin_terminal_task("run_command", "sleep 100", "k")

    widget.finish_stream("aborted", "已停止生成")

    assert widget.message.tasks[0].status == "aborted"


def test_service_url_is_recorded(qapp):
    widget = _assistant_widget()
    widget.begin_terminal_task("start_service", "npm run dev", "k")
    widget.finish_terminal_task("k", success=True, url="http://localhost:5173")

    assert widget.message.tasks[0].url == "http://localhost:5173"


def test_task_record_survives_serialisation():
    message = Message(role="assistant", content="正文")
    message.tasks.append(
        TaskRecord(
            tool="run_command",
            command="pytest -q",
            status="failed",
            seconds=1.5,
            output="boom",
            url="http://127.0.0.1:8000",
            anchor=2,
        )
    )

    revived = Message.from_dict(json.loads(json.dumps(message.to_dict())))

    assert len(revived.tasks) == 1
    record = revived.tasks[0]
    assert (record.tool, record.command, record.status) == ("run_command", "pytest -q", "failed")
    assert record.seconds == 1.5
    assert record.output == "boom"
    assert record.url == "http://127.0.0.1:8000"
    assert record.anchor == 2
    assert record.id == message.tasks[0].id


def test_cards_are_restored_after_restart(qapp):
    """重开软件：卡片按记录重建，状态 / 输出 / 地址都要在。"""
    from paper_agent.ui.chat.message_widget import MessageWidget

    live = _assistant_widget()
    live.begin_terminal_task("run_command", "pytest -q", "k1")
    live.finish_terminal_task("k1", success=True, output="3 passed")
    live.begin_terminal_task("start_service", "npm run dev", "k2")
    live.finish_terminal_task("k2", success=True, url="http://localhost:5173")

    revived = MessageWidget(
        Message.from_dict(json.loads(json.dumps(live.message.to_dict())))
    )
    cards = revived.task_cards()

    assert [card.command for card in cards] == ["pytest -q", "npm run dev"]
    assert [card.state for card in cards] == ["done", "done"]
    assert cards[0].output_text() == "3 passed"
    assert cards[1].url == "http://localhost:5173"
    assert cards[1].meta_text().startswith("后台运行中")


def test_restored_running_task_shows_aborted(qapp):
    """上次退出时任务还在跑：重建后不能显示成还在运行。"""
    from paper_agent.ui.chat.message_widget import MessageWidget

    message = Message(role="assistant", content="", status="done")
    message.tasks.append(TaskRecord(tool="run_command", command="sleep 100", status="running"))

    widget = MessageWidget(message)

    assert widget.task_cards()[0].state == "aborted"


def test_cards_interleave_with_artifacts_by_anchor(qapp):
    """重建时卡片按锚点插回正文中间，而不是全堆到最后。"""
    from paper_agent.core.models import Attachment
    from paper_agent.ui.chat.message_widget import MessageWidget

    message = Message(role="assistant", content="前半段正文后半段正文", status="done")
    message.artifacts.append(
        Attachment(name="配图.png", path="C:/tmp/配图.png", kind="file", anchor=5)
    )
    message.tasks.append(
        TaskRecord(tool="run_command", command="pytest -q", status="done", anchor=2)
    )

    widget = MessageWidget(message)
    layout = widget.content_layout
    order = [
        type(layout.itemAt(index).widget()).__name__
        for index in range(layout.count())
        if layout.itemAt(index).widget() is not None
    ]

    assert order[0] == "RichTextLabel"
    assert order[1] == "TerminalTaskCard", "锚点更靠前的任务卡片要排在产物卡片前面"
    # 每张卡片后面会新开一个正片段（后续正文写在卡片下方），因此中间夹着文字片段
    assert order.index("TerminalTaskCard") < order.index("ArtifactCard")


def test_sync_restores_missing_task_card(qapp):
    """卡片丢了（消息控件当时不在等）：生成结束时对账补上。"""
    widget = _assistant_widget()
    widget.begin_terminal_task("run_command", "pytest -q", "k")
    widget.finish_terminal_task("k", success=True, output="3 passed")

    widget._clear_task_cards()          # 模拟卡片没渲染成功
    widget.sync_artifacts()

    assert len(widget.task_cards()) == 1


def test_long_output_is_trimmed_before_saving(qapp):
    """刷了几千行的构建命令不该把会话文件撑大。"""
    widget = _assistant_widget()
    widget.begin_terminal_task("run_command", "pip install -r req.txt", "k")
    widget.finish_terminal_task("k", success=True, output="x" * 50000)

    record = widget.message.tasks[0]
    assert len(record.output) <= widget.TASK_SAVE_OUTPUT_CHARS


@pytest.mark.parametrize("size", [OUTPUT_FLUSH_CHARS])
def test_pump_flushes_when_buffer_is_big(size):
    emitted: list[str] = []
    pump = _OutputPump(emitted.append)

    pump.feed("y" * size)

    assert emitted == ["y" * size]
