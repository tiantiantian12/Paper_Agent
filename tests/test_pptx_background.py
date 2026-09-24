"""PPT 背景素材：本地优先、没有就联网生成、按主题缓存、失败回退主题色。"""

from __future__ import annotations

import shutil
import threading

import pytest
from pptx import Presentation
from pptx.util import Inches

from paper_agent.services import background_assets as bg
from paper_agent.services.skills.document_skills import CreatePptxTool, build_pptx

DECK = """# 基于YOLOv8的车牌识别检测算法研究

答辩人：张三

# 研究背景

- 车牌识别是智能交通的核心环节
"""


@pytest.fixture
def fake_generator(sample_png, tmp_path):
    """模拟 ImageClient.generate：写出一张图并返回产物信息。"""
    calls: list[str] = []

    def generate(prompt: str) -> list[dict]:
        calls.append(prompt)
        target = tmp_path / f"生成-{len(calls)}.png"
        shutil.copyfile(sample_png, target)
        return [{"name": target.name, "path": str(target), "kind": "image", "prompt": prompt}]

    generate.calls = calls          # type: ignore[attr-defined]
    return generate


# ---------------------------------------------------------------- 提示词
def test_background_prompt_mentions_topic_and_constraints():
    prompt = bg.background_prompt("车牌识别算法", "深蓝科技感")
    assert "车牌识别算法" in prompt
    assert "深蓝科技感" in prompt
    assert "不要出现任何文字" in prompt and "水印" in prompt
    assert "留白" in prompt


@pytest.mark.parametrize("hint", ["auto", "自动", "自动生成", "联网"])
def test_auto_hints_are_not_treated_as_description(hint):
    assert bg.is_auto(hint) is True
    prompt = bg.background_prompt("某个主题", hint)
    assert f"具体要求：{hint}" not in prompt


def test_empty_value_is_auto():
    assert bg.is_auto("") is True
    assert bg.is_auto("  ") is True
    assert bg.is_auto("深蓝渐变、电路纹理") is False


def test_cache_path_is_stable_per_prompt():
    first = bg.cache_path("同样的提示词")
    assert first == bg.cache_path("同样的提示词")
    assert first != bg.cache_path("另一个提示词")


def test_cache_path_changes_with_generation_profile(monkeypatch):
    """换了出图参数（尺寸/水印）要让旧缓存失效，否则会一直用带水印的图。"""
    before = bg.cache_path("同一个提示词")
    monkeypatch.setattr(bg, "GENERATION_PROFILE", "v3-其他参数")
    assert bg.cache_path("同一个提示词") != before


# ---------------------------------------------------------------- 生成器包装
def test_background_generator_uses_landscape_without_watermark():
    """平台默认打水印，而背景图铺满每页，必须显式关掉。"""
    from paper_agent.core.constants import IMAGE_BACKGROUND_SIZE

    calls: list[dict] = []

    class FakeClient:
        available = True

        def generate(self, prompt, size="", number=1, output_format="png", watermark=True):
            calls.append(
                {"prompt": prompt, "size": size, "watermark": watermark}
            )
            return [{"path": "x.png"}]

    generator = bg.background_generator_for(FakeClient())
    assert generator is not None
    generator("给 PPT 用的底图")

    assert calls == [
        {"prompt": "给 PPT 用的底图", "size": IMAGE_BACKGROUND_SIZE, "watermark": False}
    ]


def test_background_generator_none_when_client_unavailable():
    class NoKey:
        available = False

        def generate(self, prompt, **kwargs):      # pragma: no cover - 不该被调用
            raise AssertionError("没有 Key 不该发起请求")

    assert bg.background_generator_for(NoKey()) is None
    assert bg.background_generator_for(None) is None


# ---------------------------------------------------------------- 获取
def test_generates_and_moves_into_cache(fake_generator, bg_cache):
    local, reason = bg.acquire_background(fake_generator, "讲题", "auto")

    assert local and bg_cache in __import__("pathlib").Path(local).parents
    assert "生成" in reason
    assert len(fake_generator.calls) == 1
    assert not list(bg_cache.parent.glob("生成-*.png")), "生成结果应搬进缓存，不留孤儿文件"


def test_second_call_reuses_cache(fake_generator, bg_cache):
    first, _ = bg.acquire_background(fake_generator, "讲题", "auto")
    second, reason = bg.acquire_background(fake_generator, "讲题", "auto")

    assert first == second
    assert len(fake_generator.calls) == 1, "同主题第二次应直接用缓存"
    assert "缓存" in reason


def test_without_generator_falls_back(bg_cache):
    local, reason = bg.acquire_background(None, "讲题")
    assert local == "" and "未配置文生图模型" in reason


def test_empty_result_falls_back(bg_cache):
    local, reason = bg.acquire_background(lambda prompt: [], "讲题")
    assert local == "" and "未返回" in reason


def test_cancel_skips_generation(fake_generator, bg_cache):
    cancel = threading.Event()
    cancel.set()
    local, reason = bg.acquire_background(fake_generator, "讲题", cancel=cancel)
    assert local == "" and "取消" in reason
    assert fake_generator.calls == []


# ---------------------------------------------------------------- 工具接线
def _slides_have_background(path) -> bool:
    deck = Presentation(str(path))
    return all(
        slide.shapes and slide.shapes[0].shape_type == 13 for slide in deck.slides
    )


def test_tool_auto_generates_background(tmp_path, artifacts_dir, fake_generator, bg_cache):
    tool = CreatePptxTool(image_generator=fake_generator)
    result = tool.run(filename="开题答辩.pptx", markdown=DECK)      # 默认 background="auto"

    assert "背景图" in result.content
    assert _slides_have_background(result.artifact_paths[0])
    assert "基于YOLOv8的车牌识别检测算法研究" in fake_generator.calls[0], "提示词要带讲题"


def test_tool_uses_local_background_without_generating(
    tmp_path, artifacts_dir, sample_png, fake_generator, bg_cache
):
    tool = CreatePptxTool(image_generator=fake_generator)
    result = tool.run(filename="答辩.pptx", markdown=DECK, background=str(sample_png))

    assert "指定的背景图" in result.content
    assert fake_generator.calls == [], "本地已有素材就不该联网"
    assert _slides_have_background(result.artifact_paths[0])


def test_tool_falls_back_when_generation_fails(tmp_path, artifacts_dir, bg_cache):
    def broken(prompt: str):
        raise RuntimeError("网络超时")

    tool = CreatePptxTool(image_generator=broken)
    result = tool.run(filename="答辩.pptx", markdown=DECK)

    assert "生成失败" in result.content and "主题色底" in result.content
    assert not _slides_have_background(result.artifact_paths[0]), "失败时应回退主题色"
    assert Presentation(result.artifact_paths[0]).slides, "PPT 仍要能生成"


def test_tool_falls_back_when_no_generator(tmp_path, artifacts_dir, bg_cache):
    result = CreatePptxTool().run(filename="答辩.pptx", markdown=DECK)
    assert "未配置文生图模型" in result.content
    assert not _slides_have_background(result.artifact_paths[0])


def test_tool_missing_local_file_falls_back(tmp_path, artifacts_dir, fake_generator, bg_cache):
    tool = CreatePptxTool(image_generator=fake_generator)
    result = tool.run(filename="答辩.pptx", markdown=DECK, background="不存在的背景.png")
    assert "不存在" in result.content
    assert fake_generator.calls == [], "给了图片路径就该按路径处理，不联网"


def test_tool_description_passes_custom_prompt(tmp_path, artifacts_dir, fake_generator, bg_cache):
    tool = CreatePptxTool(image_generator=fake_generator)
    tool.run(filename="答辩.pptx", markdown=DECK, background="深蓝科技感、电路纹理")
    assert "深蓝科技感、电路纹理" in fake_generator.calls[0]


def test_tool_respects_cancel(tmp_path, artifacts_dir, fake_generator, bg_cache):
    cancel = threading.Event()
    cancel.set()
    tool = CreatePptxTool(image_generator=fake_generator)
    tool.set_cancel(cancel)
    result = tool.run(filename="答辩.pptx", markdown=DECK)
    assert "已取消" in result.content
    assert fake_generator.calls == []


# ---------------------------------------------------------------- 铺满方式
def test_background_is_cover_fit_not_stretched(tmp_path, artifacts_dir, fake_generator, bg_cache):
    """4:3 / 方形素材铺 16:9 页面要裁切而不是拉伸变形。"""
    tool = CreatePptxTool(image_generator=fake_generator)
    result = tool.run(filename="答辩.pptx", markdown=DECK)
    deck = Presentation(result.artifact_paths[0])
    picture = deck.slides[0].shapes[0]

    assert picture.width == Inches(13.333) and picture.height == Inches(7.5)
    assert picture.crop_top > 0 and abs(picture.crop_top - picture.crop_bottom) < 1e-6
    assert picture.crop_left == 0, "比页面更扁的图应裁上下，而不是裁左右"


def test_build_pptx_missing_background_uses_theme(tmp_path):
    path = tmp_path / "无背景.pptx"
    build_pptx(DECK, path, background="不存在的图.png")     # 传进构造函数的路径缺失
    deck = Presentation(str(path))
    assert deck.slides[0].shapes[0].shape_type != 13
    assert "gradFill" in deck.slides[0]._element.xml
