"""智能体 Skills（工具）层。

``base`` 提供工具抽象与注册表，``document_skills`` 提供文档读写能力。
"""

from paper_agent.services.skills.base import (
    Tool,
    ToolParam,
    ToolRegistry,
    ToolResult,
)
from paper_agent.services.skills.code_sandbox import ExecutePythonTool, run_python
from paper_agent.services.skills.document_skills import build_default_registry

__all__ = [
    "Tool",
    "ToolParam",
    "ToolRegistry",
    "ToolResult",
    "ExecutePythonTool",
    "run_python",
    "build_default_registry",
]
