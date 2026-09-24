"""视频提示词规范（自制 skill）：模型要**自动**按三条来写提示词与分镜。

三条取自开源社区里给「即梦 Seedance 2.0」写的提示词 skill，但只留与平台无关的部分：

1. 五段式：主体 → 动作序列 → 环境/光影 → 镜头语言 → 风格关键词
2. 时间线：按整片时间轴写清每个时间点（`0-4秒：… 4-8秒：…`），客户端按实际分段切开
   （时间戳的解析与切片见 ``tests/test_video_timeline.py``）
3. 显式衔接点：每段收在一个明确的画面上，下一段从它接

它平台的 ``@图片1`` 引用语法、视频延长、原生音效那些**没抄**（我们的供应商不认），
这里也顺便钉住"别把平台专有写法带进来"。
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------- 模型看得到吗
def test_description_carries_the_three_rules():
    """工具说明里必须带这三条 —— 这是「自动」的全部机制：模型读了才会照做。"""
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    text = GenerateVideoTool.description
    assert "五段式" in text and "动作序列" in text and "镜头语言" in text, "第 1 条"
    # 第 2 条是**整片时间线**：从 0 开始、连续铺满总时长，客户端按实际分段切开
    assert "时间戳" in text and "0-4秒" in text, "第 2 条"
    assert "铺满" in text and "切开" in text, "要说清怎么写、以及客户端会怎么处理"
    assert "收在一个明确的画面上" in text, "第 3 条"


def test_prompt_and_shots_params_repeat_the_rules():
    """写 prompt / shots 时模型看的是参数说明，所以这两处也要带上。"""
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    params = {item.name: item.description for item in GenerateVideoTool(None).parameters}
    assert "五段式" in params["prompt"]
    assert "时间戳" in params["prompt"]
    assert "时间戳" in params["shots"]
    assert "收在什么画面上" in params["shots"]


def test_guide_tells_the_model_characters_can_talk():
    """模型默认会把台词「贴心」地改写成画外音 / 字幕（以为视频模型做不了口型），
    成片就成了没人说话的默片，用户还得回头纠正 —— 规范里必须写明**人物能说话**，
    而且三处都带上：工具说明、prompt 参数说明、PROMPT_GUIDE。
    """
    from paper_agent.services.skills.video_prompt import DIALOGUE_RULE, PROMPT_GUIDE
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    assert "说话" in DIALOGUE_RULE and "画外音" in DIALOGUE_RULE
    assert DIALOGUE_RULE in PROMPT_GUIDE, "规范里要有这一条"
    assert "说话" in GenerateVideoTool.description, "模型先读的是工具说明"
    params = {item.name: item.description for item in GenerateVideoTool(None).parameters}
    assert "说话" in params["prompt"], "写 prompt 时看的是参数说明"
    assert "秒" in DIALOGUE_RULE, "要给出台词长度的估算（每秒几个字）"


def test_no_platform_specific_syntax_leaks_in():
    """即梦专有的写法不能混进来：@图片1 会被我们的供应商当普通文本。"""
    from paper_agent.services.skills.video_prompt import PROMPT_GUIDE, SHOTS_GUIDE
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    for text in (PROMPT_GUIDE, SHOTS_GUIDE, GenerateVideoTool.description):
        assert "@图片" not in text and "@视频" not in text and "@音频" not in text
        assert "延长" not in text.replace("视频延长", "")   # 「将@视频1延长15秒」那种手工操作


# ---------------------------------------------------------------- 装配出来的提示词
def test_each_shot_gets_a_handoff_pointing_at_the_next_shot():
    """衔接点要说清「下一段是什么」，而不是空泛地讲一句注意衔接。"""
    from paper_agent.services.video_long import segment_prompts

    shots = ["主角在卧室惊醒", "主角冲进走廊，浓雾里有黑影", "黑影扑来，片名砸出"]
    texts = segment_prompts("恐怖片", shots, 3)

    assert shots[1] in texts[0], "第 1 段要指向第 2 段的画面"
    assert shots[2] in texts[1]
    assert "好让下一段从这里接下去" in texts[0]
    assert "不要收在模糊或被遮挡的画面上" in texts[1]


def test_last_shot_gets_a_closing_instruction():
    """最后一段没有下一段可接：换成「收干净」的提示，别再要求衔接。"""
    from paper_agent.services.video_long import segment_prompts

    texts = segment_prompts("恐怖片", ["开场", "中段", "收尾"], 3)

    assert "最后一个镜头" in texts[-1]
    assert "好让下一段从这里接下去" not in texts[-1]


def test_handoff_truncates_a_long_next_shot():
    """下一段分镜很长时只引用一小截，别把提示词预算吃光。"""
    from paper_agent.services.skills.video_prompt import SHOT_SNIPPET_CHARS
    from paper_agent.services.video_long import segment_prompts

    long_shot = "很长的一段分镜描述。" * 60
    texts = segment_prompts("片", ["第一段", long_shot], 2)

    assert long_shot not in texts[0], "整段原文不该被塞进来"
    assert f"{long_shot[:SHOT_SNIPPET_CHARS]}…" in texts[0]


def test_single_prompt_gets_generic_handoff():
    """没给分镜（几段共用一句话）时退化成通用衔接说法，仍然强调收在清晰画面。"""
    from paper_agent.services.video_long import segment_prompts

    texts = segment_prompts("末日废墟", None, 3)

    assert "主体与场景保持一致" in texts[0]
    assert "最后一个镜头" in texts[-1]


# ---------------------------------------------------------------- 分镜条目归一化
# 实测（2026-09-23 的丧尸短片）：模型把 shots 写成对象数组
# `[{"prompt": "…", "subject": "…"}]`，旧代码 `str(item)` 得到
# `{'prompt': '…', 'subject': '…'}` 这种字典字面量并原样发给视频模型 ——
# 开头一堆括号引号是噪音、主体还被重复写两遍，成片出现「原地重演上一段」。
def test_object_shot_is_reduced_to_its_text():
    from paper_agent.services.skills.video_skills import _shot_text

    item = {"prompt": "0-2秒：她沉睡", "subject": "长直黑发女孩"}

    text = _shot_text(item)

    assert text == "0-2秒：她沉睡"
    assert "prompt" not in text and "{" not in text, "不能把字典字面量发出去"
    assert "长直黑发女孩" not in text, "条目自带的 subject 不用（全局 subject 才作准）"


@pytest.mark.parametrize("key", ["prompt", "text", "description", "shot", "content"])
def test_common_shot_key_names_are_recognized(key):
    from paper_agent.services.skills.video_skills import _shot_text

    assert _shot_text({key: "正文"}) == "正文"


def test_unknown_shot_keys_are_joined_not_stringified():
    from paper_agent.services.skills.video_skills import _shot_text

    text = _shot_text({"画面": "卧室", "动作": "起身"})

    assert "{" not in text and "'" not in text
    assert "卧室" in text and "起身" in text


def test_plain_string_shot_passes_through():
    from paper_agent.services.skills.video_skills import _shot_text

    assert _shot_text("  一段分镜  ") == "一段分镜"


def test_tool_schema_declares_shots_as_strings():
    """schema 里写明 items 是字符串，模型才会给字符串数组（而不是对象）。"""
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    spec = GenerateVideoTool(None).spec()
    shots = spec["function"]["parameters"]["properties"]["shots"]

    assert shots["type"] == "array"
    assert shots["items"] == {"type": "string"}


# ---------------------------------------------------------------- 时间戳没铺满
def test_short_timestamps_get_a_fill_the_duration_hint():
    """段长 10 秒、分镜只写到 7 秒 → 补一句「剩下的时间往前演」。

    不补的话，视频模型自己补，最常见的补法是原地重演刚才的画面
    （用户看到的就是「第 1 段和第 2 段内容重复」）。
    """
    from paper_agent.services.video_long import segment_prompts

    shots = ["0-2秒：沉睡。2-4秒：惊醒。4-7秒：坐起", "0-3秒：下床。3-8秒：走到门口"]
    texts = segment_prompts("片", shots, 2, segment_seconds=[10, 10])

    assert "占满全部 10 秒" in texts[0]
    assert "占满全部 10 秒" in texts[1]


def test_full_timestamps_do_not_get_the_hint():
    from paper_agent.services.video_long import segment_prompts

    texts = segment_prompts("片", ["0-4秒：沉睡。4-10秒：惊醒坐起"], 1, segment_seconds=[10])

    assert "占满全部 10 秒" not in texts[0]


def test_shot_without_timestamps_is_not_nagged():
    """没写时间戳（单件事的短镜头）就别啰嗦。"""
    from paper_agent.services.video_long import segment_prompts

    texts = segment_prompts("片", ["女孩走向门口"], 1, segment_seconds=[10])

    assert "占满全部 10 秒" not in texts[0]


def test_seconds_in_anchors_do_not_confuse_the_check():
    """锚点里引用了前几段的秒数（摘要），判断只看**这条分镜自己**的正文。"""
    from paper_agent.services.video_long import segment_prompts

    shots = ["0-4秒：沉睡。4-10秒：坐起", "0-3秒：下床走向门口"]
    texts = segment_prompts("片", shots, 2, segment_seconds=[10, 10])

    # 第 2 段自己的时间戳只到 3 秒 → 要补；摘要里出现的「10秒」不该让它以为铺满了
    assert "占满全部 10 秒" in texts[1]


def test_recap_still_outranks_handoff_on_budget(monkeypatch):
    """预算不够时先丢衔接点，也要保住「已演过什么」（丢了整段就成重播）。"""
    from paper_agent.services import video_long

    monkeypatch.setattr(video_long, "MAX_PROMPT_CHARS", 700)
    texts = video_long.segment_prompts("片", ["第一段：主角醒来", "第二段：黑影出现"], 2)

    assert "前面几个镜头已经演过" in texts[1]
