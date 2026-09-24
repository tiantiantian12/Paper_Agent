"""视频「口述出片」的事实校验：不能只是说生成了，实际一段都没有。

和图片那条同一个毛病（见 tests/test_image_claim_guard.py），但**代价更重**：
视频一次要等一两分钟、还花额度。小模型会照着历史里工具返回的格式
（``已生成 1 段视频（文生视频）：视频-20260922-011406.mp4``）凭空写一段完成汇报，
实际一次 generate_video 都没调 —— 用户以为片子好了，去产物目录找什么都没有。

用户实际反馈过这件事（sensenova-6.8-flash-lite 编造已生成视频）。
"""

from __future__ import annotations

import json

import pytest

from paper_agent.services.skills.base import Tool, ToolResult

FAKE_CLAIM = (
    "拍好了 ✅\n\n## 🎬 视频已生成\n\n"
    "| 项目 | 内容 |\n|---|---|\n"
    "| **文件名** | `视频-20260923-034426.mp4` |\n"
    "| **时长** | 30 秒 |"
)


class FakeClient:
    """按轮次回放事件。"""

    def __init__(self, rounds: list[list[dict]]) -> None:
        self.rounds = list(rounds)
        self.calls: list[list[dict]] = []

    def stream_events(self, messages, tools=None, temperature=0.7):
        self.calls.append(list(messages))
        yield from (self.rounds.pop(0) if self.rounds else [])

    def chat(self, *args, **kwargs):
        return "[]"


def _text(text: str) -> dict:
    return {"type": "content", "text": text}


def _call(name: str, args: dict) -> dict:
    return {
        "type": "tool_calls",
        "calls": [{"id": "call-1", "name": name, "arguments": json.dumps(args, ensure_ascii=False)}],
    }


class _VideoTool(Tool):
    """替身 generate_video：真调用就**落一个 .mp4 文件**并交出去。

    校验看的是「本轮有没有真的产出视频文件」（按扩展名判断），不是模型嘴上说的，
    所以替身也得真产出，这个用例才有意义。
    """

    name = "generate_video"
    description = "生成视频"
    parameters: list = []

    def __init__(self, produced: list[str], *, tmp_path, succeed: bool = True) -> None:
        self.produced = produced
        self.succeed = succeed
        self.tmp_path = tmp_path

    def run(self, **kwargs) -> ToolResult:
        if not self.succeed:
            return ToolResult(success=False, error="文生视频接口未返回视频")
        self.produced.append("called")
        clip = self.tmp_path / "视频-real.mp4"
        clip.write_bytes(b"mp4")
        return ToolResult(
            content="已生成 1 段视频（文生视频）：视频-real.mp4", artifact_paths=[str(clip)]
        )


def _engine(client: FakeClient, tool, tmp_path):
    from paper_agent.services.agents.engine import AgentOrchestrator
    from paper_agent.services.skills.base import ToolRegistry

    registry = ToolRegistry()
    registry.register(tool)
    engine = AgentOrchestrator(client=client, registry=registry, use_plan=False)
    engine._messages = [{"role": "user", "content": "给我拍一段 30 秒的丧尸片头"}]
    return engine


def _run(engine):
    events: list[tuple[str, str]] = []
    engine.run(emit=lambda kind, data: events.append((kind, data)))
    return events


def _finals(events) -> str:
    return "".join(data for kind, data in events if kind == "content_final")


# ---------------------------------------------------------------- 只说不做
def test_lying_about_video_triggers_a_real_call(tmp_path):
    """先口述出片 → 纠偏后真的调工具，不该挂警告。"""
    produced: list[str] = []
    client = FakeClient(
        [
            [_text(FAKE_CLAIM)],                                  # 只说不做
            [_call("generate_video", {"prompt": "丧尸片头 废墟", "seconds": "30"})],
            [_text("这次真的在拍了，见上方卡片。")],
        ]
    )
    engine = _engine(client, _VideoTool(produced, tmp_path=tmp_path), tmp_path)

    events = _run(engine)

    assert produced == ["called"], "必须真的调到视频工具"
    assert "事实校验" not in _finals(events), "真调了就别再警告"
    assert len(client.calls) == 3


def test_warning_when_still_no_video(tmp_path):
    """补一轮还是只说不做：正文末尾必须挂一句醒目的说明。"""
    produced: list[str] = []
    client = FakeClient([[_text(FAKE_CLAIM)], [_text(FAKE_CLAIM)]])
    engine = _engine(client, _VideoTool(produced, tmp_path=tmp_path), tmp_path)

    events = _run(engine)

    assert produced == [], "模型没调工具，就不该有产出"
    final = _finals(events)
    assert "事实校验" in final
    assert "没有产出任何视频" in final


def test_no_warning_when_video_really_produced(tmp_path):
    """一轮就真出片：不该有任何额外请求。"""
    produced: list[str] = []
    client = FakeClient(
        [
            [_call("generate_video", {"prompt": "一只橘猫", "seconds": "5"})],
            [_text("视频已生成，见上方卡片。")],
        ]
    )
    engine = _engine(client, _VideoTool(produced, tmp_path=tmp_path), tmp_path)

    events = _run(engine)

    assert produced == ["called"]
    assert "事实校验" not in _finals(events)
    assert len(client.calls) == 2, "不该多跑一轮"


def test_tool_failure_is_not_treated_as_produced(tmp_path):
    """工具调了但失败（比如「接口未返回视频」）→ 不能当成已出片，照样要警告。"""
    produced: list[str] = []
    client = FakeClient(
        [
            [_call("generate_video", {"prompt": "丧尸片"})],
            [_text("视频已生成：视频-20260923-034426.mp4")],
        ]
    )
    engine = _engine(client, _VideoTool(produced, tmp_path=tmp_path, succeed=False), tmp_path)

    events = _run(engine)

    assert produced == [], "工具失败就没有产出"
    assert "事实校验" in _finals(events), "失败了还说「已生成」，必须纠正"


def test_no_video_tool_means_no_guard(tmp_path):
    """没配视频模型时不折腾：让模型自己解释（否则用户会莫名多等一轮）。"""
    from paper_agent.services.agents.engine import AgentOrchestrator
    from paper_agent.services.skills.base import ToolRegistry

    client = FakeClient([[_text(FAKE_CLAIM)]])
    engine = AgentOrchestrator(client=client, registry=ToolRegistry(), use_plan=False)
    engine._messages = [{"role": "user", "content": "拍一段视频"}]

    _run(engine)

    assert len(client.calls) == 1, "不该多跑一轮"


# ---------------------------------------------------------------- 误判防护
@pytest.mark.parametrize(
    "text",
    [
        "这一步会生成视频，需要先在看板里配置视频模型。",
        "我无法生成视频：当前没有可用的视频模型。",
        "如果视频生成失败，可以换个提示词再试。",
        "我没有视频生成能力。",
        "视频生成中，请稍等 1–3 分钟。",
        "长视频会拆成几段依次生成，最后自动合成。",
        # 如实报错的说法不能被当成完成汇报（否则又要白跑一轮、白花额度）
        "第 3 段失败了，我重试一下。",
        "3 段出了点问题，稍等我重跑。",
        "已合成背景图，接着出片。",
    ],
)
def test_ordinary_talk_does_not_look_like_a_claim(text):
    from paper_agent.services.agents.engine import VIDEO_CLAIM_RE

    assert VIDEO_CLAIM_RE.search(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "拍好了 ✅ 视频已生成",
        "视频生成好了，见上方卡片。",
        "已生成 1 段视频（文生视频）：视频-20260923-034426.mp4",
        "已合成一条长视频：长视频-20260923-033543-60秒.mp4",
        "两段视频都已经生成好了。",
        "出片完成，请查看。",
        # 2026-09-24 实测漏过的说法：模型用「跑完 / 长片 / 自己起的文件名」汇报，
        # 旧正则一条都没接住，这条虚构的「5 段全跑完 + 已合成」就原样发给了用户
        "5 段全部跑完，现在合成 60 秒长片：",
        "5 段已全部跑完 + 已合成《高铁怪谈-开篇60秒.mp4》",
        "已合成《高铁怪谈-开篇60秒.mp4》",
        "两段已经拍完了",
        "我把 5 段跑完了",
        "长片已经合成好了",
    ],
)
def test_real_claims_are_caught(text):
    from paper_agent.services.agents.engine import VIDEO_CLAIM_RE

    assert VIDEO_CLAIM_RE.search(text) is not None
