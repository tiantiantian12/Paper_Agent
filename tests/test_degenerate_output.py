"""模型死循环输出（同一句反复刷屏）的检测与止损。"""

from __future__ import annotations

from paper_agent.services.agents.engine import (
    CONTENT,
    NOTE,
    THINKING,
    AgentOrchestrator,
    looks_degenerate,
)
from paper_agent.services.skills.base import ToolRegistry


# ---------------------------------------------------------------- 检测
def test_repeated_phrase_is_degenerate():
    text = ("I'll write the form styles now. " * 20)[-1500:]
    assert looks_degenerate(text)


def test_repeated_line_is_degenerate():
    text = "\n".join(["Writing form styles..."] * 15)
    assert looks_degenerate(text)


def test_normal_text_is_not_degenerate():
    text = (
        "先看一下工程目录结构，然后创建 index.html、styles.css 和 app.js。"
        "接口返回的数据按状态码分类展示，页面顶部放筛选按钮。"
    )
    assert not looks_degenerate(text)
    assert not looks_degenerate("")


def test_long_paragraphs_are_not_degenerate():
    text = "\n".join(f"第 {index} 段：这里是一段各不相同的说明文字。" for index in range(20))
    assert not looks_degenerate(text)


# ---------------------------------------------------------------- 引擎止损
class _FakeClient:
    """第一轮疯狂重复，第二轮给出正常回答。"""

    def __init__(self) -> None:
        self.calls = 0
        self.last_finish_reason = "stop"
        self.reasoning_dropped = False

    def stream_events(self, messages, tools=None, temperature=0.7, max_tokens=0):
        self.calls += 1
        if self.calls == 1:
            for _ in range(200):
                yield {"type": "thinking", "text": "Writing form styles... "}
            return
        yield {"type": "content", "text": "我会先创建工程文件，然后运行。"}


def test_degenerate_stream_is_cut_off_and_recovers():
    events: list[tuple[str, str]] = []
    engine = AgentOrchestrator(client=_FakeClient(), registry=ToolRegistry())
    engine._messages = [{"role": "user", "content": "做个网页"}]
    engine.run(lambda kind, text: events.append((kind, text)))

    notes = [text for kind, text in events if kind == NOTE]
    thinking = "".join(text for kind, text in events if kind == THINKING)
    content = "".join(text for kind, text in events if kind == CONTENT)

    assert any("重复输出" in note for note in notes), notes
    assert "检测到重复输出" in thinking
    assert len(thinking) < 6000, "重复内容不该被完整读进来（应尽早掐断）"
    assert "我会先创建工程文件" in content, "掐断后要能通过兜底继续给出回答"


# ---------------------------------------------------------------- 存档上限
def test_thinking_storage_is_capped(qapp):
    """死循环模型能吐几万字推理：界面要有上限，别把会话文件撑爆。"""
    from paper_agent.core.models import Message
    from paper_agent.ui.chat.message_widget import MessageWidget

    widget = MessageWidget(Message(role="assistant", content="", status="streaming"))
    widget.begin_stream()
    for _ in range(60):
        widget.append_thinking("重" * 1000)

    assert len(widget._message.thinking) <= MessageWidget.THINKING_STORE_LIMIT + 1000
    assert "已停止记录" in widget.thinking_block.text()
