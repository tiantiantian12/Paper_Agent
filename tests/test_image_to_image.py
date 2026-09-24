"""图生图：把「参考图」解析成接口认识的地址，并交给生成器。

需求场景：用户说「把刚才那张图改成赛博朋克夜景」，模型要能指着一张已有图片做图生图。
模型只会给文件名 / URL，不会给绝对路径，所以这层要负责解析并把本地图内联成 data URI。
"""

from __future__ import annotations

import base64

import pytest

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake" * 8
PNG_B64 = base64.b64encode(PNG_BYTES).decode()
PNG_DATA_URL = "data:image/png;base64," + PNG_B64


def _tool(files, tmp_path, monkeypatch, images_provider=None):
    from paper_agent.services.image_client import ImageClient
    from paper_agent.services.skills.image_skills import GenerateImageTool

    # 产物目录隔离，免得被测机器的真实产物混进来
    from paper_agent.services import image_client as image_module

    monkeypatch.setattr(image_module, "ARTIFACTS_DIR", tmp_path / "artifacts")

    calls: list[dict] = []

    def generator(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        return [{"name": "配图.png", "path": str(tmp_path / "配图.png"), "kind": "image"}]

    return GenerateImageTool(generator, files, images_provider=images_provider), calls, ImageClient


# ---------------------------------------------------------------- 解析参考图
def test_http_reference_passes_through(tmp_path, monkeypatch):
    tool, calls, _ = _tool([], tmp_path, monkeypatch)

    result = tool.run(prompt="改成夜景", reference_image="https://x/input.png")

    assert result.success is True
    assert calls[0]["reference_images"] == ["https://x/input.png"]
    assert "图生图" in result.content


def test_data_url_reference_passes_through(tmp_path, monkeypatch):
    tool, calls, _ = _tool([], tmp_path, monkeypatch)

    tool.run(prompt="改造", reference_image=PNG_DATA_URL)

    assert calls[0]["reference_images"] == [PNG_DATA_URL]


def test_local_path_becomes_data_url(tmp_path, monkeypatch):
    source = tmp_path / "原图.png"
    source.write_bytes(PNG_BYTES)
    tool, calls, _ = _tool([], tmp_path, monkeypatch)

    tool.run(prompt="改造", reference_image=str(source))

    assert calls[0]["reference_images"] == [PNG_DATA_URL]


def test_session_file_name_is_resolved(tmp_path, monkeypatch):
    """模型只会给文件名（可能还不带后缀），要能在本会话文件里找到。"""
    source = tmp_path / "配图-20260922-033106-1.png"
    source.write_bytes(PNG_BYTES)
    tool, calls, _ = _tool(
        [{"name": source.name, "path": str(source)}], tmp_path, monkeypatch
    )

    tool.run(prompt="改造", reference_image="配图-20260922-033106-1")

    assert calls[0]["reference_images"] == [PNG_DATA_URL]


def test_multiple_references_are_all_sent(tmp_path, monkeypatch):
    """「这两个人各来一张定妆图」：一次给多个文件名，要都带上。"""
    first = tmp_path / "角色A.png"
    second = tmp_path / "角色B.png"
    first.write_bytes(PNG_BYTES)
    second.write_bytes(PNG_BYTES)
    tool, calls, _ = _tool(
        [
            {"name": first.name, "path": str(first)},
            {"name": second.name, "path": str(second)},
        ],
        tmp_path,
        monkeypatch,
    )

    result = tool.run(prompt="两个主角的定妆图", reference_image="角色A.png, 角色B.png")

    assert result.success is True
    assert calls[0]["reference_images"] == [PNG_DATA_URL, PNG_DATA_URL]


# ---------------------------------------------------------------- 定妆 / 人物形象兜底
# 真实教训：用户传了照片说「做定妆图」，模型只填了 prompt 没填 reference_image，
# 结果是纯文生图 —— 出来的脸是另一张脸，用户以为没按他的照片做（会话 004a2c5eae74
# 之前那一版就是这样）。所以「有上传图 + 提示词在说人」时由工具兜底挂上参考图。
def test_character_prompt_uses_uploaded_photo(tmp_path, monkeypatch):
    """传了照片 + 提示词在说人物/定妆 → 自动走图生图，别按文字另画一张脸。"""
    tool, calls, _ = _tool([], tmp_path, monkeypatch, images_provider=lambda: [PNG_DATA_URL])

    result = tool.run(prompt="电影级角色定妆照，保留五官特征")

    assert result.success is True
    assert calls[0]["reference_images"] == [PNG_DATA_URL]
    assert "图生图" in result.content
    assert "自动带上" in result.content, "要让用户知道是按他的照片做的"


def test_explicit_reference_beats_auto(tmp_path, monkeypatch):
    """模型自己填了参考图就以它为准（用户指名要哪张时，不该被上传的图盖掉）。"""
    source = tmp_path / "指定.png"
    source.write_bytes(PNG_BYTES)
    tool, calls, _ = _tool(
        [{"name": source.name, "path": str(source)}],
        tmp_path,
        monkeypatch,
        images_provider=lambda: ["data:image/png;base64,UPLOADED"],
    )

    tool.run(prompt="主角定妆照", reference_image="指定.png")

    assert calls[0]["reference_images"] == [PNG_DATA_URL]


def test_unrelated_prompt_stays_text_to_image(tmp_path, monkeypatch):
    """传了照片但提示词在画流程图 / 风景 → **不**自动挂，免得把无关照片掺进去。"""
    tool, calls, _ = _tool([], tmp_path, monkeypatch, images_provider=lambda: [PNG_DATA_URL])

    result = tool.run(prompt="一张极简白底的系统架构流程图")

    assert result.success is True
    assert calls[0].get("reference_images") is None, "不该带上用户的照片"


def test_auto_reference_capped(tmp_path, monkeypatch):
    """上传了很多张也只带前 2 张：多了会把提示词冲淡、请求体也大。"""
    from paper_agent.services.skills.image_skills import MAX_AUTO_REFERENCES

    many = [f"data:image/png;base64,PHOTO{index}" for index in range(5)]
    tool, calls, _ = _tool([], tmp_path, monkeypatch, images_provider=lambda: many)

    tool.run(prompt="按这个人物形象做定妆图")

    assert calls[0]["reference_images"] == many[:MAX_AUTO_REFERENCES]


def test_provider_failure_falls_back_to_text_to_image(tmp_path, monkeypatch):
    """读取上传图失败时退回文生图，不能把整轮出图搞崩。"""
    def broken():
        raise OSError("读不到")

    tool, calls, _ = _tool([], tmp_path, monkeypatch, images_provider=broken)

    result = tool.run(prompt="主角定妆照")

    assert result.success is True
    assert calls[0].get("reference_images") is None


def test_missing_reference_reports_error_without_calling_api(tmp_path, monkeypatch):
    tool, calls, _ = _tool([], tmp_path, monkeypatch)

    result = tool.run(prompt="改造", reference_image="不存在的图.png")

    assert result.success is False
    assert "没找到参考图" in result.error
    assert calls == [], "解析不到就别去调接口"


def test_non_image_file_is_not_used_as_reference(tmp_path, monkeypatch):
    """会话里同名的是个 .txt：不能拿它当参考图。"""
    other = tmp_path / "说明.txt"
    other.write_text("不是图片", encoding="utf-8")
    tool, calls, _ = _tool([{"name": other.name, "path": str(other)}], tmp_path, monkeypatch)

    result = tool.run(prompt="改造", reference_image="说明")

    assert result.success is False
    assert calls == []


# ---------------------------------------------------------------- 兼容与接线
def test_plain_generation_keeps_original_call_shape(tmp_path, monkeypatch):
    """纯文生图还是老调用形态（只传 prompt），别的生成器实现不会被打断。"""
    from paper_agent.services.skills.image_skills import GenerateImageTool

    calls: list[tuple] = []

    def prompt_only(prompt):        # 老签名：只有一个参数
        calls.append((prompt,))
        return [{"name": "配图.png", "path": str(tmp_path / "配图.png")}]

    result = GenerateImageTool(prompt_only).run(prompt="一只海豹")

    assert result.success is True
    assert calls == [("一只海豹",)]


def test_description_mentions_reference_image():
    """工具描述要告诉模型「图生图用 reference_image」，否则这功能不会被调用。"""
    from paper_agent.services.skills.image_skills import GenerateImageTool

    text = GenerateImageTool.description
    assert "reference_image" in text
    assert "图生图" in text


def test_description_tells_model_to_use_photo_for_character():
    """定妆 / 人物形象这类，描述里必须写明「必须填参考图」——不然模型会全靠想象。"""
    from paper_agent.services.skills.image_skills import GenerateImageTool

    text = GenerateImageTool.description
    assert "定妆" in text and "必须填" in text, "要让模型知道：传了照片时必须带参考图"


def test_description_warns_against_intermediate_still_for_video():
    """用户要的是「视频里像本人」时，别先出定妆图 —— 每多一次生成就少一分像。

    实测（会话 004a2c5eae74）：图生图两次都被判「不像本人」。
    """
    from paper_agent.services.skills.image_skills import GenerateImageTool

    assert "anchor" in GenerateImageTool.description
    assert "少一次重画" in GenerateImageTool.description


def test_registry_passes_session_files(tmp_path, monkeypatch):
    """接线回归：注册表要把本会话文件清单交给图片工具（否则解析不到参考图）。"""
    from paper_agent.services import image_client as image_module
    from paper_agent.services.skills import build_default_registry

    monkeypatch.setattr(image_module, "ARTIFACTS_DIR", tmp_path / "artifacts")
    source = tmp_path / "底图.png"
    source.write_bytes(PNG_BYTES)
    calls: list[dict] = []

    registry = build_default_registry(
        image_generator=lambda prompt, **kw: calls.append(kw) or [
            {"name": "配图.png", "path": str(tmp_path / "配图.png")}
        ],
        session_files=[{"name": source.name, "path": str(source)}],
        mode="doc",
    )
    result = registry.execute("generate_image", {"prompt": "改造", "reference_image": source.name})

    assert result.success is True
    assert calls[0]["reference_images"] == [PNG_DATA_URL]


def test_build_worker_passes_uploaded_images(qapp, monkeypatch, tmp_path):
    """**真**走一遍 ``_build_worker``：本轮附件要一路传到图片工具。

    注册表那两条测的是「装配」，这一条测的是「调用链」—— 出过的事故：
    ``image_images`` 在 ``_build_worker`` 里被引用但**没有定义**（NameError），
    而这里只在「配了生图模型 + 走自定义模型分支」时才执行到，所以注册表测试
    和 mock 模式的用例都抓不到，只有用户点发送才会炸。
    """
    from paper_agent.services.agent_service import AgentService
    from paper_agent.services.image_client import ImageClient

    monkeypatch.setattr(ImageClient, "available", property(lambda self: True))
    service = AgentService(max_parallel=1)
    worker = service._build_worker(
        messages=[{"role": "user", "content": "按这个人物做一张定妆图"}],
        model="m",
        effort="",
        endpoint={"base_url": "http://x/v1", "api_key": "k", "model_id": "m"},
        attachments=[],
        image={"base_url": "", "api_key": "k", "model_id": "img"},
        image_mode=False,
        session_files=[],
        cancel_event=None,
        is_stopped=lambda: False,
        mode="doc",
        code_root="",
        video={},
        video_mode=False,
        video_params={},
    )

    # producer 是绑定的 engine.run，从它身上回查编排器（这里就是要看下层的接线）
    engine = worker._producer.__self__
    tool = engine._registry.get("generate_image")
    assert tool is not None, "自定义模型分支也要能生图"
    assert tool._images_provider is not None, "本轮附件要传给图片工具（定妆兜底靠它）"


@pytest.mark.parametrize("mode", ["doc", "code"])
def test_registry_passes_uploaded_images(mode, tmp_path, monkeypatch):
    """接线回归：本轮上传的照片要传到图片工具（定妆兜底靠它），两种模式都要。

    视频那条线早就有（图生视频自动用本轮的图），图片这条线一直没接 ——
    于是「传了照片做定妆图」只能靠模型自己想起来填 reference_image。
    """
    from paper_agent.services import image_client as image_module
    from paper_agent.services.skills import build_default_registry

    monkeypatch.setattr(image_module, "ARTIFACTS_DIR", tmp_path / "artifacts")
    calls: list[dict] = []

    registry = build_default_registry(
        image_generator=lambda prompt, **kw: calls.append(kw) or [
            {"name": "配图.png", "path": str(tmp_path / "配图.png")}
        ],
        mode=mode,
        image_images_provider=lambda: [PNG_DATA_URL],
    )
    registry.execute("generate_image", {"prompt": "角色定妆照，保留这个人的五官"})

    assert calls[-1]["reference_images"] == [PNG_DATA_URL]
