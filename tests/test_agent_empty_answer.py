"""模型只输出推理、正文为空时的兜底（避免聊天里只剩一个空消息）。"""

from __future__ import annotations

from paper_agent.services.agents.engine import CONTENT, AgentOrchestrator
from paper_agent.services.skills.base import Tool, ToolRegistry, ToolResult


class _FakeClient:
    """按脚本逐轮返回事件；脚本用完后返回空正文。"""

    def __init__(self, rounds: list[list[dict]]) -> None:
        self.rounds = list(rounds)
        self.calls = 0
        self.last_finish_reason = "stop"
        self.reasoning_dropped = False

    def stream_events(self, messages, tools=None, temperature=0.7, max_tokens=0):
        self.calls += 1
        events = self.rounds.pop(0) if self.rounds else [{"type": "content", "text": ""}]
        yield from events


class _DocTool(Tool):
    name = "create_docx"
    description = "生成文档"
    parameters = []

    def __init__(self, path: str) -> None:
        self._path = path

    def run(self, **kwargs) -> ToolResult:
        return ToolResult(content="已生成", artifact_paths=[self._path])


def run_engine(client: _FakeClient, registry: ToolRegistry | None = None) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    engine = AgentOrchestrator(client=client, registry=registry or ToolRegistry())
    engine._messages = [{"role": "user", "content": "做个 PPT"}]
    engine.run(lambda kind, text: events.append((kind, text)))
    return events


def content_of(events: list[tuple[str, str]]) -> str:
    return "".join(text for kind, text in events if kind == CONTENT)


def test_empty_answer_is_replaced_by_file_list(tmp_path):
    path = tmp_path / "答辩PPT.pptx"
    path.write_bytes(b"x")
    registry = ToolRegistry()
    registry.register(_DocTool(str(path)))

    client = _FakeClient(
        [
            [
                {"type": "thinking", "text": "PPT 做完了"},
                {
                    "type": "tool_calls",
                    "calls": [
                        {
                            "id": "c1",
                            "name": "create_docx",
                            "arguments": '{"filename": "答辩PPT.pptx"}',
                        }
                    ],
                },
            ],
            # 工具跑完后模型什么都没说（正文为空、也不再调工具）
            [{"type": "thinking", "text": "已经生成好了"}, {"type": "content", "text": ""}],
            # 兜底请求：模型仍然不吐正文
            [{"type": "content", "text": ""}],
        ]
    )
    answer = content_of(run_engine(client, registry))

    assert "答辩PPT.pptx" in answer, "正文为空时要给出已产出文件清单"
    assert str(path) in answer


def test_empty_answer_uses_nudge_reply_when_model_responds():
    client = _FakeClient(
        [
            [{"type": "thinking", "text": "想了一下"}],           # 第一轮：只有推理，没有正文
            [{"type": "content", "text": "这轮我没有产出文件，建议先补充选题。"}],  # 兜底请求
        ]
    )
    answer = content_of(run_engine(client))
    assert answer == "这轮我没有产出文件，建议先补充选题。"
    assert client.calls == 2, "空正文时应再请求一次，让模型把结论写进正文"


def test_empty_answer_falls_back_to_notice():
    client = _FakeClient(
        [
            [{"type": "thinking", "text": "想了一下"}],
            [{"type": "content", "text": ""}],      # 兜底请求也没吐正文
        ]
    )
    answer = content_of(run_engine(client))
    assert "没有输出正文" in answer


def test_normal_answer_is_untouched():
    client = _FakeClient([[{"type": "content", "text": "正常回答"}]])
    assert content_of(run_engine(client)) == "正常回答"
    assert client.calls == 1, "有正文时不该触发兜底请求"
