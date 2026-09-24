"""模型「口述出图」的事实校验：不能只是说生成了，实际一张都没有。

回归的坑：小参数模型会照着上一次工具返回的格式（``已生成 1 张配图：配图-xxx.png``）
凭空写一段完成汇报，连文件名都编好，实际一次 ``generate_image`` 都没调。用户按路径
去找，文件根本不存在 —— 对话里也没有任何图片卡片。
"""

from __future__ import annotations

import json

import pytest

from paper_agent.services.agents.engine import build_orchestrator

FAKE_PATH_CLAIM = (
    "出好了 ✅\n\n## 🖼 图片已生成\n\n"
    "| 项目 | 内容 |\n|---|---|\n"
    "| **文件名** | `配图-20260922-004457-1.png` |\n"
    "| **本地路径** | `D:\\work\\data\\artifacts\\配图-20260922-004457-1.png` |"
)


class FakeClient:
    """按轮次回放事件；每轮一次 ``stream_events`` 调用。"""

    def __init__(self, rounds: list[list[dict]]) -> None:
        self.rounds = list(rounds)
        self.calls: list[list[dict]] = []

    def stream_events(self, messages, tools=None, temperature=0.7):
        self.calls.append(list(messages))
        yield from (self.rounds.pop(0) if self.rounds else [])

    def chat(self, *args, **kwargs):      # Planner 用；本测试关掉了计划
        return "[]"


def _text(text: str) -> dict:
    return {"type": "content", "text": text}


def _call(name: str, args: dict) -> dict:
    return {
        "type": "tool_calls",
        "calls": [{"id": "call-1", "name": name, "arguments": json.dumps(args, ensure_ascii=False)}],
    }


def _engine(client: FakeClient, generator, tmp_path):
    engine = build_orchestrator(
        client=client,
        messages=[{"role": "user", "content": "生成一张亚索的图片"}],
        image_generator=generator,
        mode="doc",
        workspace=tmp_path,
    )
    engine._use_plan = False      # 只测出图这条线，不跑 Planner
    return engine


def _run(engine):
    events: list[tuple[str, str]] = []
    engine.run(emit=lambda kind, data: events.append((kind, data)))
    return events


def _artifacts(events) -> list[dict]:
    return [json.loads(data) for kind, data in events if kind == "artifact"]


def _finals(events) -> list[str]:
    return [data for kind, data in events if kind == "content_final"]


# ---------------------------------------------------------------- 只说不做
def test_lying_about_image_triggers_a_real_call(tmp_path):
    """先口述成功 → 纠偏后真的调工具出图，不该挂警告。"""
    images: list[str] = []

    def generator(prompt: str):
        images.append(prompt)
        path = tmp_path / "配图-real.png"
        path.write_bytes(b"png")
        return [{"name": path.name, "path": str(path)}]

    client = FakeClient(
        [
            [_text(FAKE_PATH_CLAIM)],                                     # 只说不做
            [_call("generate_image", {"prompt": "亚索 白发 太刀 剑冢"})],   # 被纠偏后真调
            [_text("这次是真的生成了，见上图。")],                          # 收尾
        ]
    )
    engine = _engine(client, generator, tmp_path)

    events = _run(engine)

    assert images == ["亚索 白发 太刀 剑冢"], "必须真的调到生图生成器"
    assert [item["name"] for item in _artifacts(events)] == ["配图-real.png"]
    assert "事实校验" not in "".join(_finals(events)), "真出图了就别再警告"
    assert len(client.calls) == 3


def test_warning_when_still_no_image(tmp_path):
    """补一轮还是只说不做：正文末尾必须挂一句醒目的说明。"""
    calls: list[str] = []

    def generator(prompt: str):      # 不该被调用
        calls.append(prompt)
        return []

    client = FakeClient([[_text(FAKE_PATH_CLAIM)], [_text(FAKE_PATH_CLAIM)]])
    engine = _engine(client, generator, tmp_path)

    events = _run(engine)

    assert calls == [], "模型没调工具，生成器就不该被调用"
    assert not _artifacts(events)
    final = "".join(_finals(events))
    assert "事实校验" in final
    assert "实际没有产出任何图片" in final


def test_no_warning_when_image_really_produced(tmp_path):
    """一轮就真出图：不该有任何额外请求。"""
    def generator(prompt: str):
        path = tmp_path / "配图-ok.png"
        path.write_bytes(b"png")
        return [{"name": path.name, "path": str(path)}]

    client = FakeClient(
        [
            [_call("generate_image", {"prompt": "一只柴犬"})],
            [_text("图片已生成，见上图。")],
        ]
    )
    engine = _engine(client, generator, tmp_path)

    events = _run(engine)

    assert [item["name"] for item in _artifacts(events)] == ["配图-ok.png"]
    assert not _finals(events) or "事实校验" not in "".join(_finals(events))
    assert len(client.calls) == 2, "不该多跑一轮"


# ---------------------------------------------------------------- 误判防护
@pytest.mark.parametrize(
    "text",
    [
        "论文里图 3-1 已生成并嵌入了 Word。",          # 说的是文档里的插图
        "这一步会生成图片，需要先配置生图模型。",      # 说的是流程，不是完成
        "我没有图像生成能力，无法完成。",              # 明确说做不了
        "如果生成图片失败，可以换个提示词再试。",
    ],
)
def test_ordinary_talk_does_not_look_like_a_claim(text, tmp_path):
    """普通表述不能被当成「声称已出图」，否则会平白多跑一轮。"""
    from paper_agent.services.agents.engine import IMAGE_CLAIM_RE

    assert IMAGE_CLAIM_RE.search(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "出好了 ✅ 图片已生成",
        "配图生成好了，见上图。",
        "已生成 2 张配图：配图-20260922-004457-1.png",
        "出图完成，请查看。",
        "本地路径：D:\\data\\artifacts\\配图-20260922-004457-1.png",
    ],
)
def test_real_claims_are_caught(text):
    from paper_agent.services.agents.engine import IMAGE_CLAIM_RE

    assert IMAGE_CLAIM_RE.search(text) is not None


def test_no_generator_means_no_guard(tmp_path):
    """没配生图能力时不折腾：直接放行，用户看到的是模型自己的解释。"""
    client = FakeClient([[_text(FAKE_PATH_CLAIM)]])
    engine = _engine(client, None, tmp_path)

    events = _run(engine)

    assert len(client.calls) == 1
    assert not _artifacts(events)
