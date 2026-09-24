"""时间线：模型写清「每个时间点演什么」，客户端按实际分段切开、原样送到视频模型。

用户的原话：「**要模型划分好时间线，每个时间段是什么画面后，能够正常切换画面 —— 你告诉
视频模型哪几秒干什么，模型是会遵守的，有这个能力的**」（他实测过：一条 12 秒提示词里写
``0-4秒 / 4-8秒 / 8-12秒``，成片三个时间点都照着演了）。

以前的问题：``shots`` 被当成「每段一条提示词」，``texts[:count]`` 直接把多出来的**丢掉** ——
12 秒只有一段时，模型把用户的时间线拆成 3 条交上来，结果只有前 4 秒的画面被发出去，
后 8 秒整段消失（会话 ``5bb3daf924d8``）。
"""

from __future__ import annotations

from paper_agent.services.skills import video_timeline as vt
from paper_agent.services.video_long import plan_prompts

# 用户真发过的那条提示词（截自会话 5bb3daf924d8，只把画面描述缩短）
USER_PROMPT = (
    "深夜大学男生宿舍室内，电影感写实风格，冷蓝屏幕光为主光源。"
    "0-4秒：镜头从电脑屏幕特写开始，屏幕上是《英雄联盟》游戏画面。"
    "4-8秒：镜头缓慢向右横移，从屏幕反打到三人的背影中景。"
    "8-12秒：镜头推近到中景，三人在屏幕冷光里安静地对视一眼。"
)


# ---------------------------------------------------------------- 解析
def test_parse_keeps_preamble_and_every_time_point():
    preamble, beats = vt.parse(USER_PROMPT)

    assert "电影感写实风格" in preamble
    assert [(beat.start, beat.end) for beat in beats] == [(0, 4), (4, 8), (8, 12)]
    assert "英雄联盟" in beats[0].body and "背影中景" in beats[1].body
    assert vt.timeline_end(beats) == 12


def test_parse_accepts_common_timestamp_spellings():
    for text in (
        "0-4秒：A。4-8秒：B",
        "0秒-4秒：A。4秒-8秒：B",
        "0-4s: A. 4-8s: B",
        "0～4秒：A。4～8秒：B",
    ):
        _, beats = vt.parse(text)
        assert [beat.start for beat in beats] == [0, 4], text


def test_parse_ignores_clock_like_numbers():
    """「23:13」这种时刻不能被当成秒数标记（单位是必须的）。"""
    preamble, beats = vt.parse("凌晨 23:13 的宿舍，他坐着")

    assert beats == [] and "23:13" in preamble


def test_entry_without_end_runs_until_the_next_one():
    _, beats = vt.parse("0秒：A。5秒：B")

    assert [(beat.start, beat.end) for beat in beats] == [(0, 5), (5, None)]


# ---------------------------------------------------------------- 不丢内容（核心回归）
def test_short_video_keeps_every_shot_instead_of_dropping_them():
    """12 秒 + 3 条分镜：以前只发第 1 条、后两条消失；现在整条时间线都发出去。"""
    shots = [
        "0-4秒：镜头从屏幕特写开始。",
        "4-8秒：横移反打到背影中景。",
        "8-12秒：推近到中景，三人对视。",
    ]

    texts, meta = plan_prompts("", shots, 1, segment_seconds=[12])

    assert len(texts) == 1
    for shot in shots:
        assert shot in texts[0], f"内容被丢了：{shot}"
    assert meta["merged"] == 2, "要如实记下「3 条并成 1 条」"


def test_short_video_without_shots_sends_the_prompt_unchanged():
    """用户要过「一个字都不要改」：单段时时间线必须原样发出去。"""
    texts, meta = plan_prompts(USER_PROMPT, None, 1, segment_seconds=[12])

    assert texts[0] == USER_PROMPT
    assert meta["mode"] == "prompt" and meta["timeline_end"] == 12


def test_short_video_pads_when_timeline_does_not_fill_it():
    """单段也会「没铺满」（写 0-7 秒却要 10 秒）—— 以前这里直接 return，没有兜底。"""
    texts, _ = plan_prompts("0-7秒：他沉睡后惊醒坐起", None, 1, segment_seconds=[10])

    assert "占满全部 10 秒" in texts[0]
    assert texts[0].startswith("0-7秒：他沉睡后惊醒坐起"), "原文不能动"


# ---------------------------------------------------------------- 按实际分段切开
def test_long_video_is_sliced_by_the_real_segment_lengths():
    """30 秒整片时间线 + 实际分段 10+10+10：每段只拿到属于自己那几秒的画面。"""
    prompt = (
        "风格：冷灰蓝调。"
        "0-12秒：全景，三人在宿舍打游戏。"
        "12-24秒：镜头推向胡主任，他摇头说话。"
        "24-30秒：三人对视，屏幕光打在脸上。"
    )

    texts, meta = plan_prompts(prompt, None, 3, segment_seconds=[10, 10, 10])

    assert meta["mode"] == "timeline"
    # 第 1 段：只覆盖 0-10 秒，时间戳从 0 起
    assert "0-10秒：全景，三人在宿舍打游戏。" in texts[0]
    assert "12-24" not in texts[0] and "胡主任" not in texts[0], "别的段的内容不该跑进来"
    # 第 2 段（10-20 秒）：上一段的尾巴 + 这一段的头，时间戳重算成 0 起点
    assert "0-2秒：全景，三人在宿舍打游戏。" in texts[1]
    assert "2-10秒：镜头推向胡主任，他摇头说话。" in texts[1]
    # 第 3 段（20-30 秒）
    assert "0-4秒：镜头推向胡主任，他摇头说话。" in texts[2]
    assert "4-10秒：三人对视，屏幕光打在脸上。" in texts[2]


def test_long_video_slicing_keeps_the_style_preamble_in_every_segment():
    """整片风格（时间线之前那句）每段都要带上，否则各段画风立刻分家。"""
    prompt = "赛博朋克夜景，冷紫霓虹。0-10秒：A。10-20秒：B。"

    texts, _ = plan_prompts(prompt, None, 2, segment_seconds=[10, 10])

    assert all("赛博朋克夜景" in text for text in texts)


def test_timeline_shorter_than_total_pads_the_tail():
    """时间线只写到 8 秒、总时长 20 秒：后面没写内容的段要补「往下演」。"""
    texts, meta = plan_prompts("0-8秒：他推门进来", None, 2, segment_seconds=[10, 10])

    assert meta["timeline_end"] == 8
    assert "占满全部 10 秒" in texts[1], "没铺满的那一段要补兜底"


def test_timeline_longer_than_total_is_reported():
    """写的时间线比时长还长：不能悄悄压，要在 meta 里报出来（结果备注会告诉用户）。"""
    _, meta = plan_prompts(USER_PROMPT, None, 1, segment_seconds=[8])

    assert meta["timeline_end"] == 12 and meta["total"] == 8


def test_beats_keep_their_separator_when_sliced():
    """切片后每个时间点要独立成行、标点保留。

    踩过：解析时把「…动了一下；」的分号一起剥掉，拼回来成了
    「动了一下 5-10秒：…」—— 两个时间点粘成一句，视频模型会读混。
    """
    prompt = "0-5秒：A；5-10秒：B；10-20秒：C；"

    texts, _ = plan_prompts(prompt, None, 2, segment_seconds=[10, 10])

    assert "0-5秒：A；\n5-10秒：B；" in texts[0]
    assert "0-10秒：C；" in texts[1]


def test_prompt_preamble_is_kept_when_the_timeline_sits_in_shots():
    """模型把风格写在 prompt、把时间线拆进 shots 时，风格不能丢。

    踩过：只取 shots 自己的前言，prompt 里那句「冷青绿调、35mm 胶片颗粒」被丢掉，
    切片后每一段都没有风格关键词（两段画风立刻分家）。
    """
    texts, meta = plan_prompts(
        "冷青绿调、低照度、35mm 胶片颗粒，恐怖片质感。",
        ["0-10秒：空镜推近走廊。", "10-20秒：门缝伸出一只手。"],
        2,
        segment_seconds=[10, 10],
    )

    assert meta["mode"] == "timeline"
    assert all("35mm 胶片颗粒" in text for text in texts)
    assert "0-10秒：空镜推近走廊。" in texts[0] and "0-10秒：门缝伸出一只手。" in texts[1]


# ---------------------------------------------------------------- 老写法仍然照旧
def test_per_shot_timeline_is_not_mistaken_for_a_whole_timeline():
    """每条都从 0 重新计时（老写法）→ 不按整片时间线切，仍是一段一条。"""
    shots = ["0-4秒：沉睡。4-10秒：坐起", "0-3秒：下床走向门口"]

    texts, meta = plan_prompts("片", shots, 2, segment_seconds=[10, 10])

    assert meta["mode"] == "shots"
    assert texts[0].startswith(shots[0]) and texts[1].startswith(shots[1])


def test_extra_untimed_shots_are_merged_into_the_last_segment():
    """老写法给多了：并进最后一段，不再丢掉。"""
    shots = ["第一幕", "第二幕", "第三幕", "第四幕"]

    texts, meta = plan_prompts("片", shots, 2, segment_seconds=[10, 10])

    assert meta["merged"] == 2
    assert texts[0].startswith("第一幕")
    assert "第三幕" in texts[1] and "第四幕" in texts[1], "多出来的两条要落到最后一段"


def test_missing_shots_are_padded_by_repeating_the_last_one():
    texts, meta = plan_prompts("片", ["只有一条"], 2, segment_seconds=[10, 10])

    assert texts[0].startswith("只有一条") and texts[1].startswith("只有一条")
    assert meta["merged"] == 0


# ---------------------------------------------------------------- 端到端（走工具）
def _tool(tmp_path, monkeypatch, calls):
    from paper_agent.services import video_long
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    monkeypatch.setattr(video_long, "new_artifact_path", lambda name: tmp_path / name)
    monkeypatch.setattr(
        video_long, "last_frame_data_url",
        lambda path, timeout=None: "data:image/jpeg;base64,FRAME",
    )
    monkeypatch.setattr(
        video_long, "sample_frames",
        lambda path, count=3: [f"data:image/jpeg;base64,基准{index}" for index in range(count)],
    )
    monkeypatch.setattr(
        video_long, "merge_videos",
        lambda paths, target, **kw: {"path": str(target), "duration": 30.0, "bytes": 1},
    )

    def generate(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        path = tmp_path / f"视频-{len(calls)}.mp4"
        path.write_bytes(b"mp4")
        return [{"name": path.name, "path": str(path), "kind": "video"}]

    return GenerateVideoTool(generate, None, [])


def test_tool_sends_the_whole_timeline_for_a_short_video(tmp_path, monkeypatch):
    """12 秒 + 3 条分镜：只调一次，三条都发出去，并如实备注「合成一条时间线」。"""
    shots = [
        "0-4秒：镜头从电脑屏幕特写开始。",
        "4-8秒：横移，反打到三人的背影中景。",
        "8-12秒：推近到中景，三人对视。",
    ]
    calls: list[dict] = []
    tool = _tool(tmp_path, monkeypatch, calls)

    result = tool.run(prompt="", seconds="12", shots=shots)

    assert result.success is True
    assert len(calls) == 1, "12 秒只有一段，不该多调"
    assert calls[0]["seconds"] == "12"
    for shot in shots:
        assert shot in calls[0]["prompt"], f"被丢掉了：{shot}"
    assert "合成一条时间线" in result.content, "要让用户知道分镜被合并、内容没丢"


def test_tool_says_when_the_duration_had_to_be_snapped(tmp_path, monkeypatch):
    """要 11 秒 → 按 12 秒生成：必须**如实说**，不许悄悄改也不许文案吹牛。

    真实事故：成片 5.18 秒，文案却写「约 11 秒是一整段」。
    """
    calls: list[dict] = []
    tool = _tool(tmp_path, monkeypatch, calls)

    result = tool.run(prompt="一只橘猫在窗台上打哈欠", seconds="11")

    assert len(calls) == 1
    assert calls[0]["seconds"] == "12", "不能把 11 原样发下去（供应商会回落成 5）"
    assert "已按 12 秒生成" in result.content
    assert "约 11 秒" not in result.content, "文案不能拿请求值当实际时长"


def test_tool_keeps_prompt_when_shots_are_also_given(tmp_path, monkeypatch):
    """模型同时给 prompt（风格 / 场景）与 shots（时间线）时，prompt 不能被吃掉。

    踩过：``storyboard = [text for item in … if (text := _shot_text(item))]`` 用海象
    运算符覆盖了存 prompt 的 ``text`` 变量 —— 于是 prompt 被换成**最后一条分镜**，
    成片里「冷青绿调、35mm 胶片颗粒」这些整片风格整段消失。
    """
    calls: list[dict] = []
    tool = _tool(tmp_path, monkeypatch, calls)

    result = tool.run(
        prompt="深夜的老旧居民楼走廊，冷青绿调、低照度、35mm 胶片颗粒。",
        seconds="20",
        shots=["0-10秒：空镜，镜头贴地推近走廊。", "10-20秒：门缝里伸出一只手。"],
    )

    assert result.success is True
    assert len(calls) == 2
    for index, call in enumerate(calls, 1):
        assert "35mm 胶片颗粒" in call["prompt"], f"第 {index} 段的整片风格被吃掉了"
    assert "空镜，镜头贴地推近走廊。" in calls[0]["prompt"]


def test_tool_slices_a_long_timeline_into_segments(tmp_path, monkeypatch):
    """30 秒 + 整片时间线：切成 3 段，每段只收到属于自己那几秒的画面。"""
    prompt = "冷灰蓝调。0-10秒：全景打游戏。10-20秒：推向胡主任。20-30秒：三人对视。"
    calls: list[dict] = []
    tool = _tool(tmp_path, monkeypatch, calls)

    result = tool.run(prompt=prompt, seconds="30")

    assert len(calls) == 3
    assert "0-10秒：全景打游戏。" in calls[0]["prompt"]
    assert "推向胡主任" not in calls[0]["prompt"], "第 1 段不该演到第 2 段的内容"
    assert "0-10秒：推向胡主任。" in calls[1]["prompt"], "第 2 段的时间戳要重算成 0 起点"
    assert "已按你写的整片时间线切成 3 段" in result.content
