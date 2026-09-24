"""核心数据模型（会话 / 消息 / 附件）。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable, Literal

from paper_agent.core.constants import IMAGE_FILE_EXTENSIONS

Role = Literal["user", "assistant", "system"]
MessageStatus = Literal["pending", "streaming", "done", "error", "aborted"]
FileSource = Literal["user", "model"]   # 论文空间：user=用户上传，model=模型生成


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _file_size(path: str) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}


@dataclass
class Attachment:
    """输入区与消息中的附件（图片或文件）。"""

    id: str = field(default_factory=new_id)
    name: str = ""
    path: str = ""
    kind: str = "file"  # "image" | "file"
    size: int = 0
    mime: str = ""
    created_at: float = 0.0   # 上传 / 生成时间；0 表示历史数据，回退到所在消息的时间
    # 产物在正文中的插入位置（字符下标）：重开软件后据此还原「正文 → 卡片 → 正文」的顺序。
    # -1 表示历史数据（当时没记），按「正文在前、卡片在后」渲染。
    anchor: int = -1

    @property
    def is_image(self) -> bool:
        return self.kind == "image"

    @property
    def is_video(self) -> bool:
        """文生视频产物（mp4 之类）。"""
        if self.kind == "video":
            return True
        return Path(self.path or "").suffix.lower() in VIDEO_SUFFIXES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Attachment":
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class PlanStep:
    """任务计划 / 待办项（Plan-and-Execute 与多智能体展示用）。"""

    title: str = ""
    status: str = "pending"     # pending | doing | done | failed
    detail: str = ""
    id: str = field(default_factory=new_id)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanStep":
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class ToolRun:
    """一次工具（Skill）调用记录。"""

    name: str = ""                                            # 工具名
    args: str = ""                                            # 入参（JSON 文本）
    result: str = ""                                          # 结果摘要
    status: str = "done"                                      # running | done | failed
    id: str = field(default_factory=new_id)
    artifacts: list[str] = field(default_factory=list)        # 产出的文件路径

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolRun":
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class TaskRecord:
    """一次终端任务（跑命令 / 执行代码 / 起后台服务）的记录。

    消息里那张「终端任务卡片」是实时控件，重开软件就没了；把关键信息记在这里，
    重建消息时按 ``anchor`` 还原成同样顺序的卡片 —— 用户回头还能看到当时跑了什么、
    跑了多久、输出是什么。
    """

    tool: str = ""                    # run_command / execute_python / start_service
    command: str = ""                 # 命令行或代码（截断后）
    status: str = "done"              # running | done | failed | aborted
    seconds: float = 0.0              # 耗时
    output: str = ""                  # 输出（只留尾部，避免会话文件膨胀）
    url: str = ""                     # 后台服务的访问地址
    anchor: int = -1                  # 在正文中的插入位置（与产物卡片同一套）
    id: str = field(default_factory=new_id)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskRecord":
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class Message:
    """一条对话消息。"""

    role: Role = "user"
    content: str = ""
    id: str = field(default_factory=new_id)
    attachments: list[Attachment] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    model: str = ""
    status: MessageStatus = "done"
    error: str = ""
    thinking: str = ""          # 模型推理过程（与正式回答分开展示）
    thinking_ms: int = 0        # 推理耗时（毫秒）
    artifacts: list[Attachment] = field(default_factory=list)   # 智能体生成的文件产物
    plan: list[PlanStep] = field(default_factory=list)          # 计划 / 待办
    tool_runs: list[ToolRun] = field(default_factory=list)      # 工具调用记录
    tasks: list[TaskRecord] = field(default_factory=list)       # 终端任务卡片（重开后还原）

    @property
    def is_user(self) -> bool:
        return self.role == "user"

    def add_artifact(
        self, name: str, path: str, kind: str = "", anchor: int = -1
    ) -> Attachment | None:
        """新增产物文件（按路径去重）；``kind`` 为空时按扩展名推断。

        Args:
            anchor: 正文里的插入位置（字符下标）；重开软件后据此还原卡片位置。

        Returns:
            新建的 :class:`Attachment`；路径为空或已存在时返回 ``None``。
        """
        if not path or any(item.path == path for item in self.artifacts):
            return None
        if not kind:
            kind = "image" if Path(path).suffix.lower() in IMAGE_FILE_EXTENSIONS else "file"
        attachment = Attachment(
            name=name,
            path=path,
            kind=kind,
            size=_file_size(path),
            created_at=time.time(),
            anchor=anchor,
        )
        self.artifacts.append(attachment)
        return attachment

    def add_tool_run(
        self, name: str, args: str = "", result: str = "", status: str = "done"
    ) -> ToolRun:
        """记录一次工具调用。

        同一个调用会收到两次事件（``running`` → ``done`` / ``failed``）：**就地更新那一条**，
        不要追加第二条 —— 否则每次调用都在会话里留两条，其中一条永远停在 ``running``
        且返回为空。这不是小问题：重试 / 「继续」时 ``_resume_context`` 会把这份记录喂回
        模型，而模型看到一堆「→ running：（空）」会以为工具没跑成，于是照着上一轮的
        叙述自己编过程（用户看到的「明明没跑却说 5 段全跑完了」有一部分就是这么来的）。
        """
        runs = self.tool_runs
        if status != "running" and runs:
            last = runs[-1]
            if last.status == "running" and last.name == name and last.args == args:
                last.result = result
                last.status = status
                return last
        record = ToolRun(name=name, args=args, result=result, status=status)
        runs.append(record)
        return record

    @property
    def word_count(self) -> int:
        """粗略统计中英文字符数。"""
        text = self.content.strip()
        return len(text)


    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["attachments"] = [a.to_dict() for a in self.attachments]
        data["artifacts"] = [a.to_dict() for a in self.artifacts]
        data["plan"] = [p.to_dict() for p in self.plan]
        data["tool_runs"] = [t.to_dict() for t in self.tool_runs]
        data["tasks"] = [t.to_dict() for t in self.tasks]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        created_at = data.get("created_at", time.time())
        attachments = [Attachment.from_dict(a) for a in data.get("attachments", [])]
        artifacts = [Attachment.from_dict(a) for a in data.get("artifacts", [])]
        # 旧版本会话没有记录附件时间，退回所在消息的时间，保证论文空间仍能显示时间
        for item in (*attachments, *artifacts):
            if not item.created_at:
                item.created_at = created_at
        plan = [PlanStep.from_dict(p) for p in data.get("plan", [])]
        tool_runs = [ToolRun.from_dict(t) for t in data.get("tool_runs", [])]
        tasks = [TaskRecord.from_dict(t) for t in data.get("tasks", [])]
        return cls(
            role=data.get("role", "user"),
            content=data.get("content", ""),
            id=data.get("id") or new_id(),
            attachments=attachments,
            created_at=created_at,
            model=data.get("model", ""),
            status=data.get("status", "done"),
            error=data.get("error", ""),
            thinking=data.get("thinking", ""),
            thinking_ms=int(data.get("thinking_ms", 0) or 0),
            artifacts=artifacts,
            plan=plan,
            tool_runs=tool_runs,
            tasks=tasks,
        )


@dataclass
class OutlineNode:
    """论文大纲节点（右侧结构面板使用）。"""

    title: str = ""
    words: int = 0
    state: str = "todo"  # todo | doing | done
    children: list["OutlineNode"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "words": self.words,
            "state": self.state,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OutlineNode":
        return cls(
            title=data.get("title", ""),
            words=data.get("words", 0),
            state=data.get("state", "todo"),
            children=[cls.from_dict(c) for c in data.get("children", [])],
        )


@dataclass
class CustomModel:
    """用户自定义的 OpenAI 兼容模型。

    只需要：显示名称、模型 ID、Base URL、API Key。
    ``api_key`` 是主 Key，``api_keys`` 是额外 Key；多个 Key 会组成请求池轮换使用。
    """

    name: str = ""
    model_id: str = ""
    base_url: str = ""
    api_key: str = ""
    id: str = field(default_factory=new_id)
    desc: str = ""
    api_keys: list[str] = field(default_factory=list)     # 额外 Key（轮换用）
    # 推理强度以哪种字段发给服务端（auto/reasoning_effort/enable_thinking/thinking_budget/off）
    reasoning: str = "auto"

    @property
    def display_desc(self) -> str:
        return self.desc or self.base_url or "自定义 OpenAI 兼容接口"

    @property
    def key_pool(self) -> list[str]:
        """有效 Key 列表：主 Key + 额外 Key，去重且保持顺序。"""
        pool: list[str] = []
        for key in [self.api_key, *self.api_keys]:
            text = (key or "").strip()
            if text and text not in pool:
                pool.append(text)
        return pool

    def is_valid(self) -> tuple[bool, str]:
        """校验必填项，返回 (是否合法, 错误说明)。"""
        if not self.name.strip():
            return False, "请填写显示名称"
        if not self.model_id.strip():
            return False, "请填写模型 ID"
        if not self.base_url.strip():
            return False, "请填写 Base URL"
        if not self.base_url.startswith(("http://", "https://")):
            return False, "Base URL 需要以 http:// 或 https:// 开头"
        if not self.key_pool:
            return False, "请填写 API Key"
        return True, ""

    def masked_key(self) -> str:
        key = self.api_key
        if not key:
            return ""
        if len(key) <= 8:
            return "*" * len(key)
        return f"{key[:4]}{'*' * 6}{key[-4:]}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CustomModel":
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        keys = valid.get("api_keys")
        if keys is not None and not isinstance(keys, list):
            valid["api_keys"] = [str(keys)]      # 旧数据 / 手改配置兼容
        return cls(**valid)


@dataclass
class PaperFile:
    """论文空间中的一条文件记录（用户上传的附件或模型生成的产物）。"""

    attachment: Attachment
    source: FileSource = "user"
    created_at: float = 0.0
    message_id: str = ""

    @property
    def name(self) -> str:
        return self.attachment.name

    @property
    def path(self) -> str:
        return self.attachment.path

    @property
    def is_user_upload(self) -> bool:
        return self.source == "user"


@dataclass
class ChatSession:
    """一次对话（左侧会话列表中的一条记录）。"""

    title: str = "新的论文对话"
    id: str = field(default_factory=new_id)
    messages: list[Message] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    model: str = ""
    outline: list[OutlineNode] = field(default_factory=list)
    outline_source: str = ""     # 当前结构对应的论文文件路径（一个会话可能有多篇）
    # 工作模式跟会话走：写论文的会话是文档模式，搭工程的会话是编程模式，
    # 切换会话时界面跟着它变；新建会话默认文档模式。
    mode: str = "doc"
    # 编程模式的代码工作区（每个会话一份）：留空则用 workspace/<会话 id>，
    # 用户在工作区面板里选过目录就记在这里（不跟别的会话共享）
    code_root: str = ""

    def touch(self) -> None:
        self.updated_at = time.time()

    @property
    def last_message_preview(self) -> str:
        for msg in reversed(self.messages):
            text = msg.content.strip().replace("\n", " ")
            if text:
                return text[:80]
        return "暂无消息"

    def add_message(self, message: Message) -> Message:
        self.messages.append(message)
        self.touch()
        return message

    def get_message(self, message_id: str) -> Message | None:
        for msg in self.messages:
            if msg.id == message_id:
                return msg
        return None

    def remove_message(self, message_id: str) -> None:
        self.messages = [m for m in self.messages if m.id != message_id]

    def paper_files(self) -> list[PaperFile]:
        """汇总本会话的全部文件：用户上传的附件 + 模型生成的产物（按时间倒序）。"""
        files: list[PaperFile] = []
        for message in self.messages:
            for attachment in message.attachments:
                files.append(
                    PaperFile(
                        attachment=attachment,
                        source="user",
                        created_at=attachment.created_at or message.created_at,
                        message_id=message.id,
                    )
                )
            for artifact in message.artifacts:
                files.append(
                    PaperFile(
                        attachment=artifact,
                        source="model",
                        created_at=artifact.created_at or message.created_at,
                        message_id=message.id,
                    )
                )
        files.sort(key=lambda item: item.created_at, reverse=True)
        return files

    def remove_paper_file(self, target: PaperFile) -> bool:
        """从所属消息中移除一条文件记录（用户附件或模型产物）。"""
        message = self.get_message(target.message_id)
        if message is None:
            return False
        bucket = message.attachments if target.is_user_upload else message.artifacts
        for index, item in enumerate(bucket):
            if item is target.attachment or item.id == target.attachment.id:
                del bucket[index]
                self.touch()
                return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "model": self.model,
            "messages": [m.to_dict() for m in self.messages],
            "outline": [o.to_dict() for o in self.outline],
            "outline_source": self.outline_source,
            "mode": self.mode,
            "code_root": self.code_root,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatSession":
        messages = [Message.from_dict(m) for m in data.get("messages", [])]
        return cls(
            id=data.get("id") or new_id(),
            title=data.get("title", "新的论文对话"),
            messages=messages,
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            model=data.get("model", ""),
            outline=[OutlineNode.from_dict(o) for o in data.get("outline", [])],
            outline_source=data.get("outline_source", ""),
            mode=_resolve_session_mode(data.get("mode"), messages),
            code_root=str(data.get("code_root") or ""),
        )


def sort_sessions(sessions: Iterable[ChatSession]) -> list[ChatSession]:
    return sorted(sessions, key=lambda s: s.updated_at, reverse=True)


# 只有编程模式才会注册的工具：老会话（模式当时存在全局配置里、会话没记过）
# 里调用过它们，说明那个会话当时是编程会话
CODE_ONLY_TOOLS = ("write_file", "run_command", "start_service", "list_files", "read_file")


def _resolve_session_mode(value: Any, messages: list[Message]) -> str:
    """确定会话的工作模式。

    新数据直接用记录的值；老会话文件没有这个字段，就按历史工具调用推断
    （否则那些正在写代码的会话重启后会突然变回文档模式）。
    非法值一律回落到文档模式，避免手改过的文件把界面搞成空模式。
    """
    if value in ("doc", "code"):
        return str(value)
    if value:
        return "doc"
    for message in messages:
        if any(run.name in CODE_ONLY_TOOLS for run in message.tool_runs):
            return "code"
    return "doc"
