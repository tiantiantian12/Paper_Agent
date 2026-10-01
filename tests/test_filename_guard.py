"""文件名守卫：**不许猜文件名** + 「已经生成过的不要重拍」+ 「别断言没有文件」。

真实事故（2026-09-24，会话 ``28eb046862b0``）：模型每轮都照
``视频-20260924-HHMMSS.mp4`` 的格式**预测**一个名字写进正文（真实产物是 ``111902``，
正文写 ``032357``）。连锁反应有三条：

1. 它自己发现名字对不上 → 断定「上一条没真调工具」→ 把**已经生成好的 5-1 又重拍一遍**
   （白等一两分钟、白花一次额度，用户的原话是「他已经生成好了视频，又重新生成？」）；
2. 用户拿着一个不存在的文件名去找文件；
3. 用户让它「把前面生成的视频合成一下」，它回「工作区里没有任何视频文件，之前那些名字
   都是我编的」—— 因为它只调了 ``list_files``（**只看工程目录**），而产物收在
   ``data/artifacts/<会话 id>/``；更要命的是编程模式既不注入产物清单、也没注册
   ``list_artifacts``，它没有任何办法知道真实文件名。
"""

from __future__ import annotations

import json

from paper_agent.services.skills.base import Tool, ToolResult


# ---------------------------------------------------------------- 规则写进提示里
def test_both_system_prompts_forbid_guessing_filenames():
    """文件名带生成时刻，模型不可能预知 —— 两套系统提示都要写明不许猜、怎么查。"""
    from paper_agent.services.agent_service import CODE_SYSTEM_PROMPT, SYSTEM_PROMPT

    for label, prompt in (("doc", SYSTEM_PROMPT), ("code", CODE_SYSTEM_PROMPT)):
        assert "文件名一律不许猜" in prompt, label
        assert "list_artifacts" in prompt, label
        assert "不要编一个像模像样的名字" in prompt, label


def test_retry_hints_say_do_not_reshoot():
    """纠偏提示要先让它列清单、并明确「已有的不要重拍」。"""
    from paper_agent.services.agents import engine

    for hint in (engine.VIDEO_RETRY_HINT, engine.IMAGE_RETRY_HINT):
        assert "list_artifacts" in hint, "先查已有产物"
        assert "不要重拍" in hint or "不要重画" in hint, "别把已有的再生成一遍"


# ---------------------------------------------------------------- 产物清单：两种模式都要给
def test_code_mode_injects_the_artifact_list():
    """编程模式也要列产物，否则它只能看到工程目录，会答「没有任何视频文件」。"""
    from paper_agent.services.agent_service import build_chat_messages

    workspace = [
        {
            "name": "视频-20260924-034037.mp4",
            "path": r"D:\x\data\artifacts\s1\视频-20260924-034037.mp4",
            "source": "model",
            "kind": "video",
            "size": 4096,
            "time": "2026-09-24 03:40",
        }
    ]
    messages = build_chat_messages(
        [],
        "把前面生成的视频合成一下",
        [],
        workspace=workspace,
        mode="code",
        code_files=["main.py"],
        code_root=r"D:\x\workspace\s1",
    )

    text = "\n".join(str(item["content"]) for item in messages if item["role"] == "system")
    assert "视频-20260924-034037.mp4" in text, "产物清单必须进编程模式"
    assert "generated" in text, "要点明产物就收在工作区的 generated 里（就在工程目录内）"
    assert "merge_videos" in text, "要告诉它这些名字能直接喂给哪个工具"
    assert "本会话工作区已有以下文件" not in text, "那是文档模式的措辞，别混用"


def test_code_mode_registers_list_artifacts():
    """编程模式以前只有 list_files（只看工程目录），模型根本没法查产物。"""
    from paper_agent.services.skills import build_default_registry

    registry = build_default_registry(None, [], mode="code")

    assert registry.get("list_artifacts") is not None
    assert registry.get("list_files") is not None


def test_list_files_points_at_list_artifacts(tmp_path):
    """list_files 的结果要自证「我只列工程目录」，免得被当成「什么都没生成」。"""
    from paper_agent.services.skills.code_workspace import ListFilesTool

    (tmp_path / "main.py").write_text("print(1)", encoding="utf-8")
    result = ListFilesTool(tmp_path).run()

    assert "list_artifacts" in ListFilesTool.description
    assert "main.py" in result.content
    assert "list_artifacts" in result.content, "输出里也要提示去哪里看产物"


# ---------------------------------------------------------------- 名字写错：更正，但不重拍
FAKE_CALL = {
    "type": "tool_calls",
    "calls": [{"id": "call-1", "name": "generate_video", "arguments": json.dumps({"prompt": "走廊戏"})}],
}


class _FakeClient:
    """按轮次回放事件。"""

    def __init__(self, rounds: list[list[dict]]) -> None:
        self.rounds = list(rounds)
        self.calls: list[list[dict]] = []

    def stream_events(self, messages, tools=None, temperature=0.7):
        self.calls.append(list(messages))
        yield from (self.rounds.pop(0) if self.rounds else [])

    def chat(self, *args, **kwargs):
        return "[]"


class _VideoTool(Tool):
    """替身 generate_video：真调用就落一个 .mp4 并交出去（校验只看真产物）。"""

    name = "generate_video"
    description = "生成视频"
    parameters: list = []

    def __init__(self, produced: list[str], tmp_path) -> None:
        self.produced = produced
        self.tmp_path = tmp_path

    def run(self, **kwargs) -> ToolResult:
        self.produced.append("called")
        clip = self.tmp_path / "视频-real.mp4"
        clip.write_bytes(b"mp4")
        return ToolResult(
            content="已生成 1 段视频（文生视频）：视频-real.mp4", artifact_paths=[str(clip)]
        )


def _engine(client, tool):
    from paper_agent.services.agents.engine import AgentOrchestrator
    from paper_agent.services.skills.base import ToolRegistry

    registry = ToolRegistry()
    registry.register(tool)
    engine = AgentOrchestrator(client=client, registry=registry, use_plan=False)
    engine._messages = [{"role": "user", "content": "拍一段走廊戏"}]
    return engine


def _run(engine) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    engine.run(emit=lambda kind, data: events.append((kind, data)))
    return events


def _finals(events) -> str:
    return "".join(data for kind, data in events if kind == "content_final")


def test_wrong_filename_is_corrected_without_reshooting(tmp_path, monkeypatch):
    """调了工具、但正文名字是编的 → 只更正名字，**不许再拍一遍**。"""
    from paper_agent.services import session_workspace

    root = session_workspace.generated_dir(create=True)

    produced: list[str] = []
    client = _FakeClient(
        [
            [FAKE_CALL],
            [{"type": "content", "text": "## 幕 5-1 已生成：`视频-20260924-034605.mp4`（12 秒）"}],
        ]
    )
    events = _run(_engine(client, _VideoTool(produced, tmp_path)))

    assert produced == ["called"], "名字写错不需要重拍，只该生成一次"
    final = _finals(events)
    assert "文件名校正" in final
    assert "视频-20260924-034605.mp4" in final, "要点名哪个名字是不存在的"
    assert "视频-real.mp4" in final, "要给出真实产物"
    assert "事实校验" not in final, "这不是「只说不做」，别挂警示"


def test_existing_artifact_name_is_not_flagged(tmp_path, monkeypatch):
    """正文提到的名字磁盘上真有（前几轮生成的）→ 不算编造，不加更正。"""
    from paper_agent.services import session_workspace

    root = session_workspace.generated_dir(create=True)
    (root / "视频-20260924-111902.mp4").write_bytes(b"mp4")

    produced: list[str] = []
    client = _FakeClient(
        [
            [FAKE_CALL],
            [{"type": "content", "text": "接在 `视频-20260924-111902.mp4` 后面继续拍好了。"}],
        ]
    )
    events = _run(_engine(client, _VideoTool(produced, tmp_path)))

    assert produced == ["called"]
    assert "文件名校正" not in _finals(events)


def test_real_filename_in_prose_is_not_flagged(tmp_path, monkeypatch):
    """正文只提本轮真实产物：不该有任何更正（防误报）。"""
    from paper_agent.services import session_workspace

    session_workspace.generated_dir(create=True)

    produced: list[str] = []
    client = _FakeClient(
        [[FAKE_CALL], [{"type": "content", "text": "拍好了，见上方卡片。"}]]
    )
    events = _run(_engine(client, _VideoTool(produced, tmp_path)))

    assert "文件名校正" not in _finals(events)
