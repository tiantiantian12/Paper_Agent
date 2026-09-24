"""工具 / Skill 抽象层。

所有可被智能体调用的 Skill 都实现 :class:`Tool`，经 :class:`ToolRegistry`
注册后即可导出为 OpenAI 兼容的 function calling schema，供 ReAct /
Plan-and-Execute / 多智能体编排调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """工具执行结果。"""

    content: str = ""                                       # 回传给模型的文本摘要
    artifact_paths: list[str] = field(default_factory=list)  # 产物文件路径
    success: bool = True
    error: str = ""
    # 需要在用户浏览器里打开的地址（如刚启动的本地服务）。
    # 模型拿不到 GUI，界面上由应用代它打开。
    open_url: str = ""

    def for_model(self) -> str:
        """转成回传给模型的 ``tool`` 消息内容。"""
        if not self.success:
            return f"执行失败：{self.error}"
        text = self.content
        if self.artifact_paths:
            text += "\n\n生成的文件：\n" + "\n".join(f"- {p}" for p in self.artifact_paths)
        return text


@dataclass
class ToolParam:
    """工具参数描述。"""

    name: str
    type: str = "string"
    description: str = ""
    required: bool = True
    enum: list[str] | None = None
    # 数组元素类型：写给模型看的 schema 会变成 ``{"type": "array", "items": {...}}``。
    # 声明了它，模型才会给**字符串数组**；不声明时它可能塞对象进来
    # （实测 shots 收到过 ``{"prompt": …, "subject": …}``，被 str() 成字典字面量塞给视频模型）。
    items: str = ""


class Tool:
    """一个可被调用的 Skill。

    子类需声明 ``name`` / ``description`` / ``parameters`` 并实现 :meth:`run`。
    需要支持手动停止的长任务（如代码执行）可覆盖 :meth:`set_cancel`。
    """

    name: str = ""
    description: str = ""
    parameters: list[ToolParam] = []

    def run(self, **kwargs: Any) -> ToolResult:
        raise NotImplementedError

    def set_cancel(self, cancel: Any) -> None:
        """注入取消信号（默认忽略；长任务工具可覆盖）。"""
        return None

    def set_output_hook(self, hook: Any) -> None:
        """注入输出回调（默认忽略；跑命令 / 执行代码这类工具可覆盖）。

        回调收到执行过程中的输出片段，界面就能实时显示「终端在跑什么、跑到哪了」，
        而不是等命令结束才看到结果。传 ``None`` 表示停止推送。
        """
        return None

    def set_artifact_hook(self, hook: Any) -> None:
        """注入产物回调（默认忽略；跑很久的工具可覆盖）。

        回调收到 ``{"name", "path", "kind"}``，界面立刻挂一张产物卡片。用于
        「分段出片」这类**边跑边出结果**的长任务：一段要一两分钟，等整轮跑完
        才展示的话，用户只能干等，而且发现内容不对时已经来不及了。
        传 ``None`` 表示停止推送。
        """
        return None

    # ------------------------------------------------------------------ 导出
    def spec(self) -> dict[str, Any]:
        """导出为 OpenAI function calling 的 tool schema。"""
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in self.parameters:
            item: dict[str, Any] = {"type": param.type, "description": param.description}
            if param.enum:
                item["enum"] = param.enum
            if param.items:
                item["items"] = {"type": param.items}
            properties[param.name] = item
            if param.required:
                required.append(param.name)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def safe_run(self, **kwargs: Any) -> ToolResult:
        """捕获异常，保证单个工具失败不影响整体循环。"""
        try:
            return self.run(**kwargs)
        except Exception as exc:  # pragma: no cover - 兜底
            return ToolResult(success=False, error=f"{type(exc).__name__}: {exc}")


class ToolRegistry:
    """工具注册表。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[dict[str, Any]]:
        return [tool.spec() for tool in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(success=False, error=f"未注册的工具：{name}")

        # 必填参数校验：模型偶尔会发出参数为空的调用（例如 args 是 {}）。
        # 不拦的话工具会拿默认值跑一遍，产出「output-时间戳」这类垃圾文件，
        # 模型还以为自己干成了。这里直接拒掉并把缺失项回灌，模型会自己补参数重发。
        missing = [
            param.name
            for param in tool.parameters
            if param.required and args.get(param.name) in (None, "")
        ]
        if missing:
            return ToolResult(
                success=False,
                error=(
                    f"缺少必填参数：{'、'.join(missing)}。本次调用没有执行，"
                    "请补全这个参数后重新调用。"
                ),
            )
        return tool.safe_run(**args)
