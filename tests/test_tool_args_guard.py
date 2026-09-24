"""工具调用参数校验：空对象是合法调用，别把合法调用也砍掉。

回归的坑：``_parse_args`` 曾经把 ``{}`` 一律当成坏参数拒掉，而
``list_files`` / ``list_artifacts`` 的入参全是可选的 —— 模型第一次调用就收到
「调用被跳过」，弱模型会据此认定工具不可用、写不了文件，随后整轮跑偏
（实测出现过 16~18 万字的推理空转，一个文件都没产出）。
"""

from __future__ import annotations

from paper_agent.services.agents.engine import TOOL, AgentOrchestrator
from paper_agent.services.skills.base import Tool, ToolParam, ToolRegistry, ToolResult


class _OptionalArgsTool(Tool):
    """入参全可选：空对象调用必须能正常跑。"""

    name = "list_files"
    description = "列目录"
    parameters = [ToolParam("path", "string", "子目录", required=False)]

    def __init__(self) -> None:
        self.calls = 0

    def run(self, path: str = "", **kwargs) -> ToolResult:
        self.calls += 1
        return ToolResult(content=f"已列出：{path or '（根目录）'}")


class _RequiredArgsTool(Tool):
    name = "create_docx"
    description = "生成文档"
    parameters = [
        ToolParam("filename", "string", "文件名"),
        ToolParam("markdown", "string", "正文"),
    ]

    def run(self, **kwargs) -> ToolResult:
        return ToolResult(content="不该被执行")


class _FakeClient:
    """第一轮调工具（参数由用例指定），第二轮收尾。"""

    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments
        self.round = 0
        self.last_finish_reason = "tool_calls"
        self.reasoning_dropped = False

    def stream_events(self, messages, tools=None, temperature=0.7, max_tokens=0):
        self.round += 1
        if self.round == 1:
            yield {
                "type": "tool_calls",
                "calls": [{"id": "c1", "name": self.name, "arguments": self.arguments}],
            }
            return
        yield {"type": "content", "text": "好的"}


def run_tool(name: str, arguments: str, registry: ToolRegistry) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    engine = AgentOrchestrator(client=_FakeClient(name, arguments), registry=registry)
    engine._messages = [{"role": "user", "content": "干活"}]
    engine.run(lambda kind, text: events.append((kind, text)))
    return events


def tool_events(events: list[tuple[str, str]]) -> list[str]:
    return [text for kind, text in events if kind == TOOL]


def test_empty_object_is_valid_for_optional_tools():
    tool = _OptionalArgsTool()
    registry = ToolRegistry()
    registry.register(tool)

    events = run_tool("list_files", "{}", registry)

    assert tool.calls == 1, "空对象是合法调用，工具要真的被执行"
    assert any('"status": "done"' in text for text in tool_events(events)), tool_events(events)
    assert not any("调用被跳过" in text for text in tool_events(events))


def test_empty_object_reports_missing_required_params():
    """必填参数缺失时给出可读提示，而不是笼统说「参数有问题」。"""
    registry = ToolRegistry()
    registry.register(_RequiredArgsTool())

    events = run_tool("create_docx", "{}", registry)
    joined = " ".join(tool_events(events))

    assert "缺少必填参数" in joined and "filename" in joined
    assert "调用被跳过" not in joined, "交给工具注册表按 schema 报错，措辞更准确"


def test_truncated_arguments_are_still_rejected():
    """有必填项的工具遇到空参数（输出被长度截断）时仍要跳过执行。"""
    registry = ToolRegistry()
    registry.register(_RequiredArgsTool())

    events = run_tool("create_docx", "", registry)
    joined = " ".join(tool_events(events))

    assert "调用被跳过" in joined
    assert "缺少必填参数" not in joined, "空参数与「传了 {} 但缺项」要区分开"


def test_empty_arguments_run_optional_tools():
    """全可选入参的工具完全不传参数也是合法调用（list_files() 就是这样用的）。"""
    tool = _OptionalArgsTool()
    registry = ToolRegistry()
    registry.register(tool)

    run_tool("list_files", "", registry)
    assert tool.calls == 1
