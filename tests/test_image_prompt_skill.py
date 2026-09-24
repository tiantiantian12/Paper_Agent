"""图片提示词规范（自制 skill，图片侧）：模型要**自动**按它来写提示词、决定给不给参考图。

钉的是两次真实事故：

1. 用户传了人物照片要求做定妆图，模型只填 ``prompt`` 没填 ``reference_image`` →
   纯文生图 → **出来的是另一张脸**，用户以为没按他的照片做；
2. 第二次填了参考图，但提示词写成「电影级 3D 渲染 + 灰尘 + 疲惫感」→ 风格词盖掉五官，
   用户还是判「不像本人」。

所以规则必须写在**模型看得到的地方**（工具说明 + 参数说明），不能只写在代码注释里 ——
注释只有我们看得见，模型下次照样犯。
"""

from __future__ import annotations


def _tool_text() -> str:
    from paper_agent.services.skills.image_skills import GenerateImageTool

    tool = GenerateImageTool(None)
    params = "\n".join(f"{item.name}: {item.description}" for item in tool.parameters)
    return f"{tool.description}\n{params}"


# ---------------------------------------------------------------- 模型看得到吗
def test_description_forces_reference_image_for_character_work():
    """第 1 条：人物 / 定妆类**必须**填参考图 —— 不给就会换脸。"""
    text = _tool_text()
    assert "必须填" in text and "reference_image" in text
    assert "另一张脸" in text, "要说清后果，否则模型不当回事"
    assert "定妆" in text


def test_description_says_multiple_references_are_supported():
    """第 2 条：一次能给多张（两个角色各来一张 / 人物 + 场景各一张）。"""
    text = _tool_text()
    assert "一次能给多张" in text
    assert "逗号" in text
    assert "文件名" in text and "本地" in text, "要说清参考图能从哪来"


def test_description_warns_that_img2img_still_redraws():
    """第 3 条：图生图也是重画，风格词重了会盖掉五官 —— 要写明保留五官。"""
    text = _tool_text()
    assert "重画" in text
    assert "严格保留五官" in text
    assert "风格词" in text, "要提示把强风格词放轻"


def test_description_warns_against_intermediate_still_for_video():
    """第 4 条：要「视频里像本人」就别先做定妆图当中间产物，直接 anchor 原照片。"""
    text = _tool_text()
    assert "anchor" in text
    assert "少一次重画" in text
    assert "中间产物" in text


def test_rules_reach_the_tool_spec():
    """模型拿到的其实是 spec()（JSON schema）—— 规则得真的在里面。"""
    import json

    from paper_agent.services.skills.image_skills import GenerateImageTool

    spec = json.dumps(GenerateImageTool(None).spec(), ensure_ascii=False)
    assert "必须填 reference_image" in spec
    assert "严格保留五官" in spec
    assert "anchor" in spec


def test_prompt_param_tells_model_to_keep_face():
    """写 prompt 的时候就该知道要保护五官（不是等出图了才发现不像）。"""
    from paper_agent.services.skills.image_skills import GenerateImageTool

    params = {item.name: item.description for item in GenerateImageTool(None).parameters}
    assert "保留五官" in params["prompt"]
    assert "必须" in params["reference_image"]
