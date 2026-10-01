"""别再把已经生成好的视频重生成一遍。

根因是同一个「记忆偏差」的另一头（见 tests/test_video_claim_guard.py）：
模型**真的调过** generate_video、片子也挂在聊天里了，但它过几轮回头再判定时
是靠上下文里的记忆 —— 记忆里那一句可能读作「失败了」，于是它再发一次同样的调用。
视频一次要等一两分钟、还花额度，这一遍完全是白跑。

所以这里要的是两道硬保证，都不依赖模型的判断：

* 同名同参的调用本轮已经成功过 → **不真的再跑**，直接把上次的返回原文交回去；
* 每次工具观察都附上「本轮此刻磁盘上真的有什么 / 什么都没有」的事实清单 ——
  有了这份事实，它不必靠记忆去猜。
"""

from __future__ import annotations

import json

import pytest

from paper_agent.services.agents.engine import AgentOrchestrator
from paper_agent.services.skills.base import Tool, ToolRegistry, ToolResult

CALL_A = {"prompt": "一只橘猫趴在窗台上", "seconds": "5"}
CALL_B = {"prompt": "橘猫跳下窗台走过来", "seconds": "8"}


class FakeClient:
    """按轮次回放事件；记下每一轮收到的消息。"""

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
        "calls": [
            {"id": "call-1", "name": name, "arguments": json.dumps(args, ensure_ascii=False)}
        ],
    }


class _VideoTool(Tool):
    """替身 generate_video：每次真跑就落一个不同的 .mp4，并记录入参。

    ``fail_times`` 让它前几次返回失败（**不产出文件**），用来验证失败不入缓存。
    """

    name = "generate_video"
    description = "生成视频"
    parameters: list = []

    def __init__(self, tmp_path, *, fail_times: int = 0) -> None:
        self.calls: list[dict] = []
        self.tmp_path = tmp_path
        self.fail_times = fail_times

    def run(self, **kwargs) -> ToolResult:
        if len(self.calls) < self.fail_times:
            self.calls.append(kwargs)
            return ToolResult(success=False, error="文生视频接口未返回视频")
        self.calls.append(kwargs)
        clip = self.tmp_path / f"视频-real-{len(self.calls)}.mp4"
        clip.write_bytes(b"mp4")
        return ToolResult(content=f"已生成 1 段视频：{clip.name}", artifact_paths=[str(clip)])


def _run(client: FakeClient, tool, goal: str = "给我拍一段橘猫视频") -> tuple[list, _VideoTool]:
    registry = ToolRegistry()
    registry.register(tool)
    engine = AgentOrchestrator(client=client, registry=registry, use_plan=False)
    engine._messages = [{"role": "user", "content": goal}]
    engine.run(emit=lambda kind, data: None)
    return client.calls, tool


def _tool_messages(messages: list[dict]) -> list[str]:
    return [item["content"] for item in messages if item.get("role") == "tool"]


# ---------------------------------------------------------------- 重复调用去重
def test_identical_call_is_not_run_again(tmp_path):
    """同样的参数再来一次：工具一次都没多跑，也没有多花一份额度。

    注意这里的轮次写法：``_react`` 一旦收到只有正文、不带工具调用的一轮就收尾，
    所以两次调用必须**连着**排在一起。
    """
    tool = _VideoTool(tmp_path)
    client = FakeClient(
        [
            [_call("generate_video", CALL_A)],
            [_call("generate_video", CALL_A)],      # 忘了自己刚做过
            [_text("好了，见上方卡片。")],
        ]
    )

    calls, tool = _run(client, tool)

    assert len(tool.calls) == 1, "重复调用必须被拦下，不能真的再生成一遍"
    notice = "".join(_tool_messages(calls[-1]))
    assert "重复调用已跳过" in notice, "得让模型知道自己被复用了上次的结果"
    assert "已生成 1 段视频" in notice, "上次的返回原文要给它，不然它没东西可转述"


def test_key_order_does_not_escape_the_cache(tmp_path):
    """参数一样、只是键的顺序不同 —— 也算同一次调用。"""
    tool = _VideoTool(tmp_path)
    client = FakeClient(
        [
            [_call("generate_video", CALL_A)],
            [_call("generate_video", dict(reversed(list(CALL_A.items()))))],
            [_text("好了。")],
        ]
    )

    _run(client, tool)

    assert len(tool.calls) == 1


def test_different_prompt_really_generates(tmp_path):
    """内容真的不一样（另一条镜头）→ 不能拦，那是一次新生成。"""
    tool = _VideoTool(tmp_path)
    client = FakeClient(
        [
            [_call("generate_video", CALL_A)],
            [_call("generate_video", CALL_B)],
            [_text("两段都好了。")],
        ]
    )

    _run(client, tool)

    assert len(tool.calls) == 2, "不同的画面是新的生成，不该被去重吃掉"


def test_failed_call_is_not_cached(tmp_path):
    """失败的调用不能进缓存 —— 否则「重试」会永远收到上一次的报错。"""
    tool = _VideoTool(tmp_path, fail_times=1)
    client = FakeClient(
        [
            [_call("generate_video", CALL_A)],
            [_call("generate_video", CALL_A)],
            [_text("这次好了。")],
        ]
    )

    _run(client, tool)

    assert len(tool.calls) == 2, "失败后的重试必须真的跑"


def test_cache_is_per_run(tmp_path):
    """下一轮新对话要从头来：换了一条才是新需求，别沿用上一轮的返回。"""
    tool = _VideoTool(tmp_path)
    first = FakeClient(
        [[_call("generate_video", CALL_A)], [_call("generate_video", CALL_A)], [_text("好了。")]]
    )
    _run(first, tool)
    assert len(tool.calls) == 1

    second = FakeClient([[_call("generate_video", CALL_A)], [_text("这次是新的一条。")]])
    _run(second, tool)

    assert len(tool.calls) == 2, "换一轮就得真的生成，不能拿上一轮的结果冒充"


# ---------------------------------------------------------------- 产出事实回灌
def test_observation_states_the_real_files(tmp_path):
    """每一次工具观察都写明本轮真实产出 —— 模型不必靠记忆去猜有没有生成。"""
    tool = _VideoTool(tmp_path)
    client = FakeClient([[_call("generate_video", CALL_A)], [_text("好了。")]])

    calls, _tool = _run(client, tool)

    notice = "".join(_tool_messages(calls[-1]))
    assert "本轮产出事实" in notice
    assert "视频-real-1.mp4" in notice, "要说出**真实**文件名，让它照抄而不是自己编"
    assert "不需要再生成一遍" in notice


def test_observation_says_nothing_when_nothing_was_produced(tmp_path):
    """工具失败、一个文件都没有时，事实清单要把话说死 —— 堵住「没生成却说生成了」。"""
    tool = _VideoTool(tmp_path, fail_times=1)
    client = FakeClient([[_call("generate_video", CALL_A)], [_text("已生成，见卡片。")]])

    calls, _tool = _run(client, tool)

    notice = "".join(_tool_messages(calls[-1]))
    assert "还没有产出任何视频文件" in notice
    assert "不要说「已生成" in notice


@pytest.mark.parametrize("name", ["list_files", "list_artifacts", "execute_python"])
def test_only_media_tools_get_the_fact_block(name):
    """别把这份清单塞给每个工具的返回 —— 无关工具只会白白占用上下文。"""
    from paper_agent.services.agents.engine import FACT_TOOLS, artifact_fact

    assert name not in FACT_TOOLS
    assert artifact_fact(name, [("视频-a.mp4", "/tmp/视频-a.mp4")]) == ""


def test_artifact_fact_counts_only_matching_suffixes():
    """Docx 之类的产物不能被算成视频。"""
    from paper_agent.services.agents.engine import artifact_fact

    text = artifact_fact(
        "generate_video",
        [("论文.docx", "/tmp/论文.docx"), ("视频-a.mp4", "/tmp/视频-a.mp4")],
    )

    assert "视频-a.mp4" in text
    assert "论文.docx" not in text
    assert "1 个视频文件" in text
