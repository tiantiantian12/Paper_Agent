"""首尾帧控制（keyframe）：让多段视频接得上。

需求场景：用户要「拍续集」时，上一段的**末帧**应当成为下一段的**首帧**，否则两段
画面会跳。这条链路上有三段：

    取末帧（video_frames）→ 工具解析「上一段视频」（video_skills）→ 拼 keyframe 请求（video_client）
"""

from __future__ import annotations

import base64

import pytest

from paper_agent.services.video_client import VideoClient


# ---------------------------------------------------------------- 请求拼装
def _payload_for(monkeypatch, **kwargs) -> dict:
    """抓住真正发给供应商的请求体。"""
    captured: dict = {}
    monkeypatch.setattr(
        VideoClient, "_create", lambda self, payload: captured.update(payload) or {"video_id": "v"}
    )
    monkeypatch.setattr(
        VideoClient,
        "_wait",
        lambda self, vid, on_status, on_progress, is_stopped: {
            "status": "completed", "url": "https://x/a.mp4"
        },
    )
    monkeypatch.setattr(VideoClient, "_download", lambda self, url: b"MP4")
    monkeypatch.setattr(VideoClient, "_save", lambda self, data, prompt: {
        "name": "视频.mp4", "path": "C:/tmp/视频.mp4", "kind": "video"
    })

    client = VideoClient(base_url="http://x/v1", api_key="k", model_id="m", poll_interval=0.001)
    client.generate("一段视频", **kwargs)
    return captured


def test_first_frame_switches_to_keyframe(monkeypatch):
    payload = _payload_for(monkeypatch, first_frame="data:image/jpeg;base64,AAA")

    assert payload["mode"] == "keyframe"
    assert payload["first_frame"] == "data:image/jpeg;base64,AAA"
    assert "images" not in payload


def test_first_and_last_frame_together(monkeypatch):
    payload = _payload_for(
        monkeypatch,
        first_frame="https://x/first.png",
        last_frame="https://x/last.png",
    )

    assert payload["mode"] == "keyframe"
    assert payload["first_frame"] == "https://x/first.png"
    assert payload["last_frame"] == "https://x/last.png"


def test_last_frame_alone_still_keyframe(monkeypatch):
    payload = _payload_for(monkeypatch, last_frame="https://x/last.png")

    assert payload["mode"] == "keyframe"
    assert payload["last_frame"] == "https://x/last.png"
    assert "first_frame" not in payload


def test_reference_mode_untouched(monkeypatch):
    """只有参考图时还是图生视频，别被 keyframe 抢走。"""
    payload = _payload_for(monkeypatch, images=["data:image/png;base64,BBB"])

    assert payload["mode"] == "reference"
    assert payload["images"] == ["data:image/png;base64,BBB"]
    assert "first_frame" not in payload


def test_text_mode_by_default(monkeypatch):
    assert _payload_for(monkeypatch)["mode"] == "text"


def test_keyframe_wins_over_reference_images(monkeypatch):
    """首帧和参考图都给了：以首尾帧为准（衔接优先于参考风格）。"""
    payload = _payload_for(
        monkeypatch, images=["data:image/png;base64,BBB"], first_frame="https://x/first.png"
    )

    assert payload["mode"] == "keyframe"
    assert payload["first_frame"] == "https://x/first.png"


# ---------------------------------------------------------------- 取末帧
def test_encode_image_produces_jpeg(qapp):
    from PySide6.QtGui import QImage

    from paper_agent.services.video_frames import encode_image

    image = QImage(64, 36, QImage.Format.Format_RGB32)
    image.fill(0x336699)
    raw = encode_image(image)

    assert raw[:2] == b"\xff\xd8", "JPEG 魔数"
    assert len(raw) < 20_000, "帧要压得住，别把请求体撑爆"


def test_missing_video_returns_empty():
    from paper_agent.services.video_frames import grab_frame, last_frame_data_url

    assert last_frame_data_url("C:/不存在/视频.mp4") == ""
    assert grab_frame("C:/不存在/视频.mp4") is None


# ---------------------------------------------------------------- 工具解析
def _tool(tmp_path, files, monkeypatch):
    from paper_agent.services.skills import video_skills
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    monkeypatch.setattr(
        video_skills, "last_frame_data_url", lambda path, timeout=None: "data:image/jpeg;base64,FRAME"
    )
    calls: list[dict] = []

    def generator(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        return [{"name": "第二段.mp4", "path": str(tmp_path / "第二段.mp4")}]

    tool = GenerateVideoTool(generator, None, files)
    return tool, calls


def test_continue_from_uses_last_frame(qapp, tmp_path, monkeypatch):
    """核心场景：接上一段视频 → 取它末帧当首帧。"""
    first = tmp_path / "视频-20260922-033106.mp4"
    first.write_bytes(b"mp4")
    tool, calls = _tool(
        tmp_path, [{"name": first.name, "path": str(first)}], monkeypatch
    )

    result = tool.run(prompt="人物走向窗边", continue_from=first.name, seconds="5")

    assert result.success is True
    assert calls[0]["first_frame"] == "data:image/jpeg;base64,FRAME"
    assert calls[0]["last_frame"] == ""
    assert "末帧作为首帧" in result.content
    assert result.artifact_paths == [str(tmp_path / "第二段.mp4")]


def test_continue_from_accepts_name_without_suffix(qapp, tmp_path, monkeypatch):
    """模型经常漏掉后缀，得能认出来。"""
    first = tmp_path / "视频-20260922-033106.mp4"
    first.write_bytes(b"mp4")
    tool, calls = _tool(tmp_path, [{"name": first.name, "path": str(first)}], monkeypatch)

    result = tool.run(prompt="续集", continue_from="视频-20260922-033106")

    assert result.success is True
    assert calls[0]["first_frame"]


def test_continue_from_missing_video_reports_error(qapp, tmp_path, monkeypatch):
    tool, calls = _tool(tmp_path, [], monkeypatch)

    result = tool.run(prompt="续集", continue_from="不存在的视频.mp4")

    assert result.success is False
    assert "没找到上一段视频" in result.error
    assert calls == [], "解析不到就别去调接口"


def test_continue_from_without_frame_reports_error(qapp, tmp_path, monkeypatch):
    """取不到末帧（文件损坏 / 编解码缺失）时要说清楚，别发一个半成品请求。"""
    from paper_agent.services.skills import video_skills

    first = tmp_path / "视频.mp4"
    first.write_bytes(b"broken")
    tool, calls = _tool(tmp_path, [{"name": first.name, "path": str(first)}], monkeypatch)
    monkeypatch.setattr(video_skills, "last_frame_data_url", lambda path, timeout=None: "")

    result = tool.run(prompt="续集", continue_from=first.name)

    assert result.success is False
    assert "末帧" in result.error
    assert calls == []


def test_explicit_frames_pass_through(qapp, tmp_path, monkeypatch):
    tool, calls = _tool(tmp_path, [], monkeypatch)

    result = tool.run(prompt="起止都定死", first_frame="https://x/a.png", last_frame="https://x/b.png")

    assert result.success is True
    assert calls[0]["first_frame"] == "https://x/a.png"
    assert calls[0]["last_frame"] == "https://x/b.png"
    assert "首尾帧控制" in result.content


def test_tool_description_mentions_continuity():
    """工具描述要告诉模型「拍续集用 continue_from」，否则这功能永远不会被调用。"""
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    text = GenerateVideoTool.description
    assert "continue_from" in text
    assert "续集" in text and "末帧" in text and "首帧" in text


def test_registry_passes_session_files(tmp_path, monkeypatch):
    """接线回归：注册表要把本会话文件清单交给视频工具（否则解析不到上一段）。"""
    from paper_agent.services.skills import build_default_registry
    from paper_agent.services.skills import video_skills

    video = tmp_path / "上一段.mp4"
    video.write_bytes(b"mp4")
    monkeypatch.setattr(
        video_skills, "last_frame_data_url", lambda path, timeout=None: "data:image/jpeg;base64,F"
    )
    calls: list[dict] = []

    registry = build_default_registry(
        video_generator=lambda prompt, **kw: calls.append(kw) or [
            {"name": "x.mp4", "path": str(tmp_path / "x.mp4")}
        ],
        session_files=[{"name": video.name, "path": str(video)}],
        mode="doc",
    )
    result = registry.execute("generate_video", {"prompt": "续集", "continue_from": video.name})

    assert result.success is True
    assert calls[0]["first_frame"].startswith("data:image/jpeg;base64,")


# ---------------------------------------------------------------- 编程模式：按名字找素材
# 真实会话 c754edb8c3d0：用户在编程模式传了人物照，模型 anchor 给的就是工作区里那个
# 文件名，工具却一律回「没找到基准图 / 基准视频」——因为编程模式注册表**没有**把
# session_files 与工程目录交给视频 / 生图工具，而照片在 data/attachments 与
# workspace/<会话> 里，工具一个都扫不到。模型只好改用绝对路径重试，白烧一轮；
# 它按名字重试时又失败，最后出片就没带上用户那张脸。
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _code_registry(tmp_path, session_files, calls):
    from paper_agent.services.skills import build_default_registry

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    registry = build_default_registry(
        video_generator=lambda prompt, **kw: calls.append(kw) or [
            {"name": "x.mp4", "path": str(tmp_path / "x.mp4")}
        ],
        image_generator=lambda prompt, **kw: [],
        session_files=session_files,
        mode="code",
        workspace=workspace,
    )
    return registry, workspace


def test_code_mode_finds_uploaded_photo_by_name(qapp, tmp_path):
    """上传的照片（在 data/attachments，只出现在 session_files 里）要按文件名找得到。"""
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    photo = attachments / "3e0112eb-b8c1-4fb9-9fab-8704fde1b0af.png"
    photo.write_bytes(PNG_BYTES)
    calls: list[dict] = []

    registry, _workspace = _code_registry(
        tmp_path, [{"name": photo.name, "path": str(photo)}], calls
    )
    result = registry.execute("generate_video", {"prompt": "主角就是他", "anchor": photo.name})

    assert result.success is True, result.error
    assert len(calls[0]["images"]) == 1
    assert calls[0]["images"][0].startswith("data:image/png;base64,")
    assert "作为人物 / 场景基准" in result.content


def test_code_mode_finds_workspace_file_by_name(qapp, tmp_path):
    """工程目录里的素材（模型自己写在那儿的定妆图 / 截图）也要按名字找得到。"""
    calls: list[dict] = []
    registry, workspace = _code_registry(tmp_path, [], calls)
    poster = workspace / "定妆图-主角.png"
    poster.write_bytes(PNG_BYTES)

    result = registry.execute("generate_video", {"prompt": "用它当首帧", "first_frame": poster.name})

    assert result.success is True, result.error
    assert calls[0]["first_frame"].startswith("data:image/png;base64,")


def test_code_mode_image_tool_finds_reference_by_name(qapp, tmp_path):
    """同一个毛病在生图工具上也在（模型说的「工具没找到参考图」就是它）。"""
    from paper_agent.services.skills import build_default_registry

    attachments = tmp_path / "attachments"
    attachments.mkdir()
    photo = attachments / "主角.png"
    photo.write_bytes(PNG_BYTES)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    refs: list[list[str]] = []

    def generator(prompt, **kwargs):
        refs.append(list(kwargs.get("reference_images") or []))
        return [{"name": "配图-1.png", "path": str(tmp_path / "配图-1.png")}]

    registry = build_default_registry(
        image_generator=generator,
        session_files=[{"name": photo.name, "path": str(photo)}],
        mode="code",
        workspace=workspace,
    )
    result = registry.execute(
        "generate_image", {"prompt": "按这张照片画一张定妆图", "reference_image": photo.name}
    )

    assert result.success is True, result.error
    assert refs[0] and refs[0][0].startswith("data:image/png;base64,")


def test_code_mode_missing_asset_lists_real_names(qapp, tmp_path):
    """报错要把**真实文件名**列出来：模型只会瞎猜，猜错了就来回重试。"""
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    photo = attachments / "主角.png"
    photo.write_bytes(PNG_BYTES)
    calls: list[dict] = []

    registry, _workspace = _code_registry(
        tmp_path, [{"name": photo.name, "path": str(photo)}], calls
    )
    result = registry.execute("generate_video", {"prompt": "x", "anchor": "照片.png"})

    assert result.success is False
    assert "没找到基准图" in result.error
    assert photo.name in result.error, "报错里要给真实候选文件名"
    assert "list_artifacts / list_files" in result.error, "编程模式没有 list_artifacts"
