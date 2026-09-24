"""长视频：超过单段上限（12 秒）时自动分段续接，再合成一条连贯的长视频。

需求场景：用户要 30 秒，但接口最长只给 12 秒。做法是拆成几段**续接**生成
（上一段末帧 = 下一段首帧），每段一出片就挂到聊天里（能立刻看出内容对不对、
不对就按 Esc 停），全部出完之后再补一条合成好的长视频。

这条链路上有四段，分别对应下面四组用例：

    分段规划（video_long.plan_segments）
      → 续接编排（video_long.generate_segments）
      → 工具接线（video_skills：实时挂卡片 / Esc / 合成失败降级）
      → ffmpeg 合成（video_merge，真拼一段视频出来验证）
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from paper_agent.services import video_long, video_merge
from paper_agent.services.skills.base import Tool, ToolResult
from paper_agent.services.video_long import MAX_SEGMENT, plan_segments

# ffmpeg 相关用例需要真的能跑 ffmpeg（imageio-ffmpeg 自带一份，没装就跳过）
requires_ffmpeg = pytest.mark.skipif(
    not video_merge.ffmpeg_exe(), reason="这台机器上没有可用的 ffmpeg"
)


def _make_clip(
    path,
    color: str,
    seconds: int = 2,
    audio: bool = True,
    size: str = "320x240",
    moving: bool = False,
):
    """造一段测试视频（纯色 / 会动的测试图案 + 正弦音）。

    ``moving=True`` 用 ``testsrc``：画面一直在变，才验得出「首帧 ≠ 末帧」。
    """
    import subprocess

    exe = video_merge.ffmpeg_exe()
    source = (
        f"testsrc=size={size}:rate=24:duration={seconds}"
        if moving
        else f"color=c={color}:s={size}:d={seconds}:r=24"
    )
    args = [exe, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest" if audio else "-an", str(path)]
    assert subprocess.run(args, capture_output=True).returncode == 0
    return path


# ---------------------------------------------------------------- 分段规划
@pytest.mark.parametrize(
    ("total", "expected"),
    [
        (4, [4]),
        (5, [5]),
        (9, [10]),          # 一段 10 秒比 [5, 4] 两段划算（少一次生成、不用合成）
        (12, [12]),
        (13, [8, 5]),
        (20, [10, 10]),
        (24, [12, 12]),
        (25, [10, 10, 5]),
        (30, [10, 10, 10]),
        (36, [12, 12, 12]),
        (60, [12, 12, 12, 12, 12]),
    ],
)
def test_plan_segments_uses_known_durations(total, expected):
    assert plan_segments(total) == expected


def test_plan_segments_only_uses_supported_options():
    """每一段都必须落在供应商认的档位上，否则接口会悄悄回落到默认 5 秒。"""
    options = {int(value) for value in video_long.SEGMENT_CHOICES}
    assert options == {4, 5, 6, 8, 10, 12}

    for total in range(4, 61):
        plan = plan_segments(total)
        assert all(value in options for value in plan), (total, plan)
        assert sum(plan) >= total, f"{total} 秒不能给少了：{plan}"
        assert sum(plan) - total <= 1, f"{total} 秒最多多给 1 秒：{plan}"
        lowest = math.ceil(total / MAX_SEGMENT)
        assert len(plan) >= lowest, f"{total} 秒少切了一段：{plan}"
        assert len(plan) <= lowest + 1, f"{total} 秒没必要切这么多段：{plan}"


def test_plan_segments_prefers_fewer_segments():
    """段数优先：每多一段就多等一两分钟、多花一次额度。"""
    assert len(plan_segments(19)) == 2, "19 秒用 [10, 10]（多 1 秒）而不是四段"
    assert len(plan_segments(25)) == 3
    assert len(plan_segments(40)) == 4


def test_clamp_total_keeps_request_in_range():
    assert video_long.clamp_total("30") == 30
    assert video_long.clamp_total(45) == 45
    assert video_long.clamp_total("2") == 4, "小于最小档就按最小档"
    assert video_long.clamp_total("999") == video_long.MAX_TOTAL_SECONDS
    assert video_long.clamp_total("") == 0
    assert video_long.clamp_total("很快") == 0


# ---------------------------------------------------------------- 档位外的时长
# 真实事故（2026-09-24）：用户要 11 秒、模型也老实传了 seconds="11"，但供应商只认
# 4/5/6/8/10/12 —— 客户端发现「11 不合法」就**静默回落默认值 5**，成片 5.18 秒，
# 而结果文案还写着「约 11 秒是一整段」。用户连问三次「为什么一直给我 5 秒的」。
@pytest.mark.parametrize(
    ("wanted", "expected"),
    [(4, [4]), (5, [5]), (6, [6]), (7, [8]), (8, [8]), (9, [10]), (10, [10]), (11, [12]), (12, [12])],
)
def test_short_duration_snaps_up_to_an_allowed_step(wanted, expected):
    """档位外的时长**就近上调**，绝不掉到默认 5 秒。"""
    assert plan_segments(wanted) == expected


@pytest.mark.parametrize(("wanted", "expected"), [(7, "8"), (9, "10"), (11, "12"), (12, "12")])
def test_generator_never_receives_an_off_grid_duration(wanted, expected, chained, tmp_path):
    calls: list[dict] = []
    outcome = video_long.generate_segments(
        _fake_generator(tmp_path, calls), "一只橘猫", seconds=wanted
    )

    assert outcome["planned"] == [int(expected)]
    assert [call["seconds"] for call in calls] == [expected], "发给供应商的必须是允许档位"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("11", "12"), ("9", "10"), ("7", "8"), ("6", "6"),
        ("13", "12"), ("3", "4"), ("abc", "5"), ("", "5"), (None, "5"),
    ],
)
def test_client_snaps_seconds_instead_of_falling_back_to_5(value, expected):
    """客户端这一层也不许静默变 5 秒（直连路径 / 别的调用方同样受益）。"""
    from paper_agent.services.video_client import snap_seconds

    assert snap_seconds(value) == expected


# ---------------------------------------------------------------- 续接编排
def _fake_generator(tmp_path, calls):
    """每段返回一个「落盘文件」，并记下这次调用参数。"""

    def generate(prompt, **kwargs):
        order = len(calls) + 1
        calls.append({"prompt": prompt, **kwargs})
        path = tmp_path / f"视频-{order}.mp4"
        path.write_bytes(b"mp4")
        return [{"name": path.name, "path": str(path), "kind": "video"}]

    return generate


@pytest.fixture()
def chained(monkeypatch, tmp_path):
    """编排所需的替身：末帧抽取 + 合成（都不碰 Qt 解码与 ffmpeg）。"""
    monkeypatch.setattr(
        video_long, "last_frame_data_url",
        lambda path, timeout=None: f"data:image/jpeg;base64,{path[-8:]}",
    )
    merged = {"name": "长视频-1-30秒.mp4", "path": str(tmp_path / "长视频-1-30秒.mp4"),
              "kind": "video"}
    (tmp_path / "长视频-1-30秒.mp4").write_bytes(b"mp4")
    calls: list[list] = []
    monkeypatch.setattr(
        video_long, "merge_videos",
        lambda paths, target, **kw: calls.append(list(paths)) or {
            "path": str(target), "duration": 30.0, "reencoded": False, "bytes": 1
        },
    )
    monkeypatch.setattr(
        video_long, "new_artifact_path", lambda name: tmp_path / name
    )
    return {"calls": calls, "merged": merged, "tmp_path": tmp_path}


def test_segments_chain_through_last_frame(chained, tmp_path):
    """核心：第 2 段的首帧 = 第 1 段的末帧；最后一段才收在用户给的尾帧上。"""
    calls: list[dict] = []
    outcome = video_long.generate_segments(
        _fake_generator(tmp_path, calls),
        "一只橘猫在花园里跑",
        seconds=30,
        first_frame="data:image/jpeg;base64,START",
        last_frame="data:image/jpeg;base64,END",
    )

    assert [call["seconds"] for call in calls] == ["10", "10", "10"]
    assert calls[0]["first_frame"] == "data:image/jpeg;base64,START"
    assert calls[0]["last_frame"] == "", "中间段不该收在用户的尾帧上"
    assert calls[1]["first_frame"].endswith("视频-1.mp4"), "要取上一段末帧当首帧"
    assert calls[2]["first_frame"].endswith("视频-2.mp4")
    assert calls[2]["last_frame"] == "data:image/jpeg;base64,END", "只有最后一段收尾帧"
    assert len(outcome["segments"]) == 3
    merged_name = Path(outcome["merged"]["path"]).name
    assert merged_name.startswith("长视频-") and merged_name.endswith("-30秒.mp4")


def test_segments_shown_as_they_finish(chained, tmp_path):
    """实时展示：每段一出片就回调（界面据此立刻挂卡片，不用等整轮）。"""
    calls: list[dict] = []
    shown: list[tuple[str, int, int]] = []

    video_long.generate_segments(
        _fake_generator(tmp_path, calls),
        "一段长视频",
        seconds=30,
        on_segment=lambda item, index, count: shown.append(
            (item["name"], index, count)
        ),
    )

    assert [item[0] for item in shown] == ["视频-1.mp4", "视频-2.mp4", "视频-3.mp4"]
    assert [item[1] for item in shown] == [1, 2, 3]
    assert all(item[2] == 3 for item in shown)


def test_single_segment_does_not_merge(chained, tmp_path):
    """12 秒以内就是一段，不做任何合成（别多此一举）。"""
    calls: list[dict] = []
    outcome = video_long.generate_segments(
        _fake_generator(tmp_path, calls), "短片", seconds=12
    )

    assert [call["seconds"] for call in calls] == ["12"]
    assert outcome["merged"] is None
    assert chained["calls"] == [], "单段不该去调合成"


def test_merge_failure_keeps_segments(chained, tmp_path, monkeypatch):
    """合成失败不算整件事失败：分段照样能看，说明原因即可。"""

    def boom(paths, target, **kwargs):
        raise video_merge.VideoMergeError("没找到可用的 ffmpeg")

    monkeypatch.setattr(video_long, "merge_videos", boom)
    calls: list[dict] = []
    outcome = video_long.generate_segments(
        _fake_generator(tmp_path, calls), "一段长视频", seconds=20
    )

    assert len(outcome["segments"]) == 2
    assert outcome["merged"] is None
    assert "ffmpeg" in outcome["merge_error"]


def test_stop_keeps_finished_segments(chained, tmp_path):
    """Esc：立刻停，已经出的段要留下来（别让用户白等的那几分钟作废）。"""
    calls: list[dict] = []
    state = {"stop": False}
    generator = _fake_generator(tmp_path, calls)

    def stopping(prompt, **kwargs):
        items = generator(prompt, **kwargs)
        if len(calls) >= 2:
            state["stop"] = True
        return items

    outcome = video_long.generate_segments(
        stopping, "一段长视频", seconds=30, is_stopped=lambda: state["stop"]
    )

    assert len(calls) == 2, "第 2 段出完就该停"
    assert len(outcome["segments"]) == 2
    assert outcome["stopped"] is True
    assert outcome["merged"] is not None, "停之前出的两段仍然合成给用户"


def test_stopped_during_wait_is_reported(chained, tmp_path):
    """轮询期间被中断：生成器抛哨兵异常，编排要当成「已停止」而不是报错。"""

    def interrupted(prompt, **kwargs):
        raise RuntimeError("__stopped__")

    outcome = video_long.generate_segments(interrupted, "一段长视频", seconds=30)

    assert outcome["stopped"] is True
    assert outcome["segments"] == []
    assert outcome.get("error") is None, "用户自己停的不该报成错误"


def test_missing_frame_stops_the_chain(chained, tmp_path, monkeypatch):
    """取不到上一段末帧就别硬发请求：已出的段交出去，并说清第几段断了。"""
    monkeypatch.setattr(video_long, "last_frame_data_url", lambda path, timeout=None: "")
    calls: list[dict] = []
    outcome = video_long.generate_segments(
        _fake_generator(tmp_path, calls), "一段长视频", seconds=30
    )

    assert len(calls) == 1, "接不上的那一段不该发出去"
    assert len(outcome["segments"]) == 1
    assert "末帧" in outcome["error"] and outcome["failed_at"] == 2


# ---------------------------------------------------------------- 进度
class _Sink:
    """进度接收方替身。"""

    def __init__(self) -> None:
        self.labels: list[str] = []
        self.values: list[int] = []

    def label(self, text: str) -> None:
        self.labels.append(text)

    def report(self, percent: int) -> None:
        self.values.append(int(percent))


def test_progress_is_overall_and_never_goes_back(chained, tmp_path):
    """进度是**整体**百分比：第 2 段不能又从 0 开始（进度条会看着倒退）。"""
    sink = _Sink()

    def generate_segment(prompt, **kwargs):
        report = kwargs.get("on_progress")
        if report:
            report(-1)          # 排队中
            report(50)          # 段内一半
            report(100)         # 本段出片
        path = tmp_path / f"视频-{len(sink.labels)}.mp4"
        path.write_bytes(b"mp4")
        return [{"name": path.name, "path": str(path), "kind": "video"}]

    outcome = video_long.generate_segments(
        generate_segment, "一段长视频", seconds=30, progress=sink
    )

    assert len(outcome["segments"]) == 3
    assert sink.labels[0] == "第 1/3 段"
    assert sink.labels[2] == "第 3/3 段"
    assert any("正在合成" in text for text in sink.labels)
    assert sink.labels[-1] == "", "收尾要把文案前缀清掉"
    assert sink.values == sorted(sink.values), f"整体进度不能倒退：{sink.values}"
    assert sink.values[-1] == 100, f"最后一段出片要到 100：{sink.values}"
    assert sink.values[0] == 0, "第 1 段刚排队时整体是 0"


def test_segment_progress_maps_into_overall():
    sink = _Sink()
    report = video_long._segment_progress(sink, 2, 4)

    report(-1)
    report(50)
    report(100)

    assert sink.values == [25, 38, 50], "第 2/4 段对应整体 25% – 50%"


# ---------------------------------------------------------------- 工具接线
@pytest.fixture()
def wired(monkeypatch, tmp_path):
    """工具级替身：末帧、合成、产物命名都换掉，只留接线逻辑。"""
    monkeypatch.setattr(
        video_long, "last_frame_data_url",
        lambda path, timeout=None: "data:image/jpeg;base64,FRAME",
    )
    monkeypatch.setattr(
        video_long, "merge_videos",
        lambda paths, target, **kw: {
            "path": str(target), "duration": 30.0, "reencoded": False, "bytes": 1
        },
    )
    monkeypatch.setattr(video_long, "new_artifact_path", lambda name: tmp_path / name)


def test_tool_generates_long_video_and_publishes_each_segment(wired, tmp_path, monkeypatch):
    """模型要 30 秒：工具自己拆三段、逐段实时挂卡片，最后挂合成的长视频。"""
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    calls: list[dict] = []
    published: list[str] = []
    tool = GenerateVideoTool(_fake_generator(tmp_path, calls), None, [])
    tool.set_artifact_hook(lambda item: published.append(item["name"]))

    result = tool.run(prompt="一只橘猫在花园里跑", seconds="30")

    assert result.success is True
    assert [call["seconds"] for call in calls] == ["10", "10", "10"]
    assert published == ["视频-1.mp4", "视频-2.mp4", "视频-3.mp4"], "要边出边挂"
    assert Path(result.artifact_paths[-1]).name.startswith("长视频-"), "最后一个是合成产物"
    assert len(result.artifact_paths) == 4
    assert "长视频" in result.content and "3 段" in result.content
    assert "已经逐段展示给用户" in result.content


def test_tool_single_segment_keeps_old_behaviour(wired, tmp_path):
    """12 秒以内：还是只出一段、不合成、文案不变。"""
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    calls: list[dict] = []
    published: list[str] = []
    tool = GenerateVideoTool(_fake_generator(tmp_path, calls), None, [])
    tool.set_artifact_hook(lambda item: published.append(item["name"]))

    result = tool.run(prompt="短片", seconds="5")

    assert [call["seconds"] for call in calls] == ["5"]
    assert result.artifact_paths == [str(tmp_path / "视频-1.mp4")]
    assert published == ["视频-1.mp4"]
    assert "已生成 1 段视频" in result.content


def test_tool_reports_merge_failure_but_keeps_segments(wired, tmp_path, monkeypatch):
    """没装 ffmpeg：分段照给，明确说清为什么没有长视频。"""
    from paper_agent.services import video_merge as merge_module
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    def boom(paths, target, **kwargs):
        raise merge_module.VideoMergeError(merge_module.MISSING_FFMPEG_HINT)

    monkeypatch.setattr(video_long, "merge_videos", boom)
    calls: list[dict] = []
    tool = GenerateVideoTool(_fake_generator(tmp_path, calls), None, [])

    result = tool.run(prompt="一只橘猫", seconds="20")

    assert result.success is True, "分段是好的，不该整条失败"
    assert len(result.artifact_paths) == 2
    assert "没能合成长视频" in result.content
    assert "ffmpeg" in result.content


def test_tool_stops_on_esc(wired, tmp_path):
    """Esc 能真的停下（5 段跑下来十几分钟，必须能中断）。"""
    import threading

    from paper_agent.services.skills.video_skills import GenerateVideoTool

    cancel = threading.Event()
    calls: list[dict] = []
    generator = _fake_generator(tmp_path, calls)
    published: list[str] = []

    def generating(prompt, **kwargs):
        items = generator(prompt, **kwargs)
        if len(calls) >= 1:
            cancel.set()          # 第一段一出片用户就按了 Esc
        return items

    tool = GenerateVideoTool(generating, None, [])
    tool.set_artifact_hook(lambda item: published.append(item["name"]))
    tool.set_cancel(cancel)

    result = tool.run(prompt="一只橘猫", seconds="30")

    assert len(calls) == 1
    assert published == ["视频-1.mp4"], "已经出的那段不能丢"
    assert "中途停止" in result.content
    assert result.artifact_paths == [str(tmp_path / "视频-1.mp4")]


def test_tool_schema_and_description_advertise_long_video():
    """工具描述与参数说明必须告诉模型「直接写大 seconds」，否则这功能永远不会被用。"""
    from paper_agent.services.skills.video_skills import GenerateVideoTool
    from paper_agent.services.video_long import MAX_TOTAL_SECONDS

    text = GenerateVideoTool.description
    assert "seconds" in text
    assert "长视频" in text
    assert "锚定" in text and "continuity=seamless" in text, "要说清默认锚定 / 想要连续怎么办"

    param = next(
        item for item in GenerateVideoTool(None).parameters if item.name == "seconds"
    )
    assert str(MAX_SEGMENT) in param.description
    assert str(MAX_TOTAL_SECONDS) in param.description


def test_description_says_use_photo_directly_not_a_still():
    """要「像本人」时，模型必须知道：直接 anchor 原照片，而不是先做定妆图。

    实测（会话 004a2c5eae74）：图生图做定妆照 → 用户两次都说「不像本人」。
    每多经过一次生成就少一分像，所以这条要写在工具描述里。
    """
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    text = GenerateVideoTool.description
    assert "anchor" in text and "不要" in text
    assert "定妆图" in text, "要明确点出「别先做定妆图」这条弯路"


@requires_ffmpeg
def test_frame_extraction_survives_real_clip(tmp_path):
    """回归：抽帧（续接的命根子）必须在**真实视频**上工作。

    最早用 QtMultimedia 的 QVideoSink 抽帧，靠独立线程 + 事件循环 + 超时兜底，
    在供应商的真实 mp4 上取不到帧 —— 结果是「分段续接」悄悄退化成「只出第一段」，
    调用方还以为接上了。现在走 ffmpeg，这里就盯住三件事：能取到、首末帧不同、可复现。
    """
    from paper_agent.services import video_frames

    clip = _make_clip(tmp_path / "有内容.mp4", "red", seconds=2, moving=True)

    last = video_frames.frame_bytes(clip, last=True)
    first = video_frames.frame_bytes(clip, last=False)

    assert last[:2] == b"\xff\xd8", "抽到的应当是 JPEG"
    assert first[:2] == b"\xff\xd8"
    assert last != first, "末帧和首帧不该是同一张（否则等于没抽到末帧）"
    assert video_frames.frame_bytes(clip, last=True) == last, "同样的输入要抽到同样的帧"
    assert video_frames.last_frame_data_url(clip).startswith("data:image/jpeg;base64,")


@requires_ffmpeg
def test_frame_extraction_is_fast(tmp_path):
    """抽帧在续接里是**每段一次**的同步调用，慢到几十秒会把整个流程拖死。"""
    import time

    from paper_agent.services.video_frames import frame_bytes

    clip = _make_clip(tmp_path / "clip.mp4", "green", seconds=3)
    started = time.monotonic()
    frame_bytes(clip, last=True)
    assert time.monotonic() - started < 10, "抽一帧不该要十秒"


# ---------------------------------------------------------------- 素材翻译（首尾帧 / 拼接）
def _image(path, size=(64, 36), color=0x3366CC):
    from PySide6.QtGui import QImage

    image = QImage(*size, QImage.Format.Format_RGB32)
    image.fill(color)
    assert image.save(str(path))
    return path


def _noisy_image(path, size=(1600, 200)):
    """纯色图压缩率太高，测「压不压得下来」得用杂色图。"""
    from PySide6.QtGui import QImage

    image = QImage(*size, QImage.Format.Format_RGB32)
    for y in range(size[1]):
        for x in range(size[0]):
            image.setPixel(
                x, y,
                ((x * 7 + y * 13) & 0xFF) << 16
                | ((x * 29 + y * 3) & 0xFF) << 8
                | ((x * 11 + y * 17) & 0xFF),
            )
    assert image.save(str(path), "PNG")
    return path


def test_first_frame_accepts_local_image_name(qapp, tmp_path, wired):
    """模型只能给文件名 —— 本地图片由客户端读出内联（供应商不认本地路径）。"""
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    picture = _image(tmp_path / "起始画面.png")
    calls: list[dict] = []
    tool = GenerateVideoTool(
        _fake_generator(tmp_path, calls), None,
        [{"name": picture.name, "path": str(picture)}],
    )

    result = tool.run(prompt="从这一帧开始", first_frame=picture.name)

    assert result.success is True
    assert calls[0]["first_frame"].startswith("data:image/png;base64,"), calls[0]["first_frame"][:40]
    assert "内联" in result.content


def test_first_frame_accepts_video_name(qapp, tmp_path, wired, monkeypatch):
    """给「视频文件名」也认（语义就是取它的末帧）—— 模型经常这么试。"""
    from paper_agent.services.skills import video_skills
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    clip = tmp_path / "视频-20260922-233354.mp4"
    clip.write_bytes(b"mp4")
    monkeypatch.setattr(
        video_skills, "last_frame_data_url", lambda path, timeout=None: "data:image/jpeg;base64,FRAME"
    )
    calls: list[dict] = []
    tool = GenerateVideoTool(
        _fake_generator(tmp_path, calls), None,
        [{"name": clip.name, "path": str(clip)}],
    )

    result = tool.run(prompt="接着这段拍", first_frame=clip.name)

    assert result.success is True
    assert calls[0]["first_frame"] == "data:image/jpeg;base64,FRAME"
    assert "末帧" in result.content


def test_first_frame_passes_urls_through(qapp, tmp_path, wired):
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    calls: list[dict] = []
    tool = GenerateVideoTool(_fake_generator(tmp_path, calls), None, [])

    tool.run(prompt="x", first_frame="https://x/a.png", last_frame="data:image/jpeg;base64,ABC")

    assert calls[0]["first_frame"] == "https://x/a.png"
    assert calls[0]["last_frame"] == "data:image/jpeg;base64,ABC"


def test_unknown_frame_name_reports_actionable_error(qapp, tmp_path, wired):
    """认不出来要当场说清楚，**绝不能把本地路径原样发给供应商**（会回 400）。"""
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    calls: list[dict] = []
    tool = GenerateVideoTool(_fake_generator(tmp_path, calls), None, [])

    result = tool.run(prompt="x", first_frame="不存在的图.png")

    assert result.success is False
    assert calls == [], "认不出就别去调接口"
    assert "没找到首" in result.error and "list_artifacts" in result.error


def test_big_image_is_compressed_before_inline(qapp, tmp_path, wired, monkeypatch):
    """大图别原样塞进请求体：先压到能发出去的大小（顺便把超宽的缩下来）。"""
    import base64

    from PySide6.QtGui import QImage

    from paper_agent.services import video_frames
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    picture = _noisy_image(tmp_path / "大图.png", size=(1600, 200))
    assert picture.stat().st_size > 5000, "得有内容才测得出压缩"
    monkeypatch.setattr(video_frames, "MAX_INLINE_BYTES", 10)      # 强制走压缩
    calls: list[dict] = []
    tool = GenerateVideoTool(
        _fake_generator(tmp_path, calls), None,
        [{"name": picture.name, "path": str(picture)}],
    )

    result = tool.run(prompt="x", first_frame=picture.name)

    assert result.success is True
    url = calls[0]["first_frame"]
    assert url.startswith("data:image/jpeg;base64,"), "压完应当是 JPEG"
    image = QImage.fromData(base64.b64decode(url.split(",", 1)[1]))
    assert not image.isNull(), "压出来的得是张真图"
    assert image.width() <= 1280, "超宽的图要等比缩到 1280 以内"


def test_merge_tool_joins_existing_segments(tmp_path, monkeypatch):
    """已有几段要接起来：不重新生成、不花额度（模型自己调不动 ffmpeg）。"""
    from paper_agent.services.skills.video_skills import MergeVideosTool

    first = tmp_path / "视频-1.mp4"
    second = tmp_path / "视频-2.mp4"
    for item in (first, second):
        item.write_bytes(b"mp4")
    calls: list[list] = []
    # 工具是「from ... import merge_videos」，所以要打在它自己的命名空间上
    monkeypatch.setattr(
        "paper_agent.services.skills.video_skills.merge_videos",
        lambda paths, target, **kw: calls.append(list(paths)) or {
            "path": str(target), "duration": 30.0, "reencoded": False, "bytes": 1
        },
    )
    monkeypatch.setattr(
        "paper_agent.services.skills.video_skills.new_artifact_path", lambda name: tmp_path / name
    )
    tool = MergeVideosTool([{"name": first.name, "path": str(first)},
                            {"name": second.name, "path": str(second)}])

    result = tool.run(videos=[first.name, second.name], name="恐怖短片.mp4")

    assert result.success is True
    assert calls == [[str(first), str(second)]], "顺序必须原样传下去"
    assert result.artifact_paths == [str(tmp_path / "恐怖短片.mp4")]
    assert "拼成一条" in result.content and "段" in result.content


def test_merge_tool_guards_bad_input(tmp_path, monkeypatch):
    from paper_agent.services.skills.video_skills import MergeVideosTool

    clip = tmp_path / "视频-1.mp4"
    clip.write_bytes(b"mp4")
    tool = MergeVideosTool([{"name": clip.name, "path": str(clip)}])

    assert tool.run(videos=[clip.name]).success is False, "一段不成片"
    assert tool.run(videos="").success is False
    missing = tool.run(videos=[clip.name, "不存在.mp4"])
    assert missing.success is False and "list_artifacts" in missing.error


def test_merge_tool_says_what_to_install_without_ffmpeg(tmp_path, monkeypatch):
    from paper_agent.services.skills import video_skills
    from paper_agent.services.skills.video_skills import MergeVideosTool

    first = tmp_path / "视频-1.mp4"
    second = tmp_path / "视频-2.mp4"
    for item in (first, second):
        item.write_bytes(b"mp4")
    monkeypatch.setattr(video_skills, "ffmpeg_exe", lambda: "")
    tool = MergeVideosTool([{"name": first.name, "path": str(first)},
                            {"name": second.name, "path": str(second)}])

    result = tool.run(videos=[first.name, second.name])

    assert result.success is False
    assert "imageio-ffmpeg" in result.error, "要说清怎么装，别只说失败"


def test_cancelled_chain_error_mentions_ffmpeg_when_missing(tmp_path, monkeypatch):
    """取不到末帧时：没 ffmpeg 就把「怎么装」写进报错 —— 否则模型只能瞎猜。"""
    from paper_agent.services.skills import video_skills
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    clip = tmp_path / "视频-1.mp4"
    clip.write_bytes(b"mp4")
    monkeypatch.setattr(video_skills, "last_frame_data_url", lambda path, timeout=None: "")
    monkeypatch.setattr(video_skills, "ffmpeg_exe", lambda: "")
    calls: list[dict] = []
    tool = GenerateVideoTool(
        _fake_generator(tmp_path, calls), None,
        [{"name": clip.name, "path": str(clip)}],
    )

    result = tool.run(prompt="续集", continue_from=clip.name)

    assert result.success is False
    assert "imageio-ffmpeg" in result.error


def test_descriptions_tell_model_what_not_to_do():
    """模型之所以「自己抽帧、自己贴 Base64、自己拼」，是因为描述没把话说死。"""
    from paper_agent.services.skills.video_skills import GenerateVideoTool, MergeVideosTool

    text = GenerateVideoTool.description
    assert "Base64" in text and "不要" in text.replace("**", "")
    assert "自动" in text, "要说清分段续接是客户端自动做的"
    assert "merge_videos" in text, "已有分段要指向拼接工具"
    seconds = next(
        item for item in GenerateVideoTool(None).parameters if item.name == "seconds"
    )
    assert "自动" in seconds.description
    assert "拼接" in MergeVideosTool().description


# ---------------------------------------------------------------- 分镜（别让每段重演一遍）
def test_storyboard_gives_each_segment_its_own_prompt(chained, tmp_path):
    """用户报过的问题：要 30 秒，三段内容大量重复 —— 因为三段共用同一条提示词。

    给了 shots 就每段一条，这才是「一条 30 秒的片子」该有的样子。
    """
    calls: list[dict] = []
    shots = ["开场：废墟空镜", "中段：丧尸从雾里逼近", "收尾：特写＋标题浮现"]

    outcome = video_long.generate_segments(
        _fake_generator(tmp_path, calls), "整片提示词", seconds=30, shots=shots
    )

    # 每段的**主体**是它自己的那条分镜（后面还会被补上锚点：衔接点 / 别换人 / 接着演）
    assert [call["prompt"].startswith(shot) for call, shot in zip(calls, shots)] == [True] * 3
    assert all(shot in text for shot, text in zip(shots, outcome["prompts"]))
    # 第一段也会被补「衔接点」（收在能接上第 2 段的画面上）—— 这不是"多加东西"，
    # 而是让整条片子接得上的关键；只是不会带「别换人 / 接着演」那两句（它前面没内容）
    assert shots[1] in outcome["prompts"][0]
    assert "不要引入新人物" not in outcome["prompts"][0]


def test_single_prompt_gets_continuation_hint(chained, tmp_path):
    """没给分镜时：第 1 段原样，后面几段必须补「接着往下演、别重复上一段」。"""
    calls: list[dict] = []

    video_long.generate_segments(
        _fake_generator(tmp_path, calls), "整片提示词", seconds=30
    )

    assert calls[0]["prompt"].startswith("整片提示词"), "主体还是用户那句"
    assert "开头" in calls[0]["prompt"], "没给分镜时第一段要标出「开头」"
    assert all("整片提示词" in call["prompt"] for call in calls)
    for index, call in enumerate(calls[1:], 2):
        assert "不要重复" in call["prompt"], f"第 {index} 段要带「别重复」的提示"
        assert f"第 {index}/3 个镜头" in call["prompt"]


def test_recap_tells_each_shot_what_already_happened(chained, tmp_path):
    """用户反馈「下一段重复上一段的话和动作」的根因：视频模型只看得到这一段自己的
    提示词，**不知道前面几段演了什么**，光说「别重复」它做不到 —— 得把前面演过什么
    喂进去。"""
    calls: list[dict] = []
    shots = ["主角在卧室惊醒", "主角冲进走廊，浓雾里有黑影", "黑影扑来，片名砸出"]

    video_long.generate_segments(
        _fake_generator(tmp_path, calls), "恐怖片", seconds=30, shots=shots
    )

    assert "前面几个镜头已经演过" not in calls[0]["prompt"], "第一段前面没得可 recap"
    assert "前面几个镜头已经演过" in calls[1]["prompt"]
    assert "第 1 段" in calls[1]["prompt"] and shots[0] in calls[1]["prompt"]
    # 第 3 段要能看到前两段都演了什么
    assert shots[0] in calls[2]["prompt"] and shots[1] in calls[2]["prompt"]
    assert "第 2 段" in calls[2]["prompt"]


def test_recap_truncates_long_earlier_shots(chained, tmp_path):
    """分镜很长时摘要要截断，不能把提示词撑爆（预算见 MAX_PROMPT_CHARS）。"""
    calls: list[dict] = []
    long_shot = "长镜头描述。" * 200

    video_long.generate_segments(
        _fake_generator(tmp_path, calls), "片", seconds=20, shots=[long_shot, "第二段"]
    )

    prompt = calls[1]["prompt"]
    assert "前面几个镜头已经演过" in prompt
    assert long_shot not in prompt, "原文太长，只能带摘要"
    assert len(prompt) <= video_long.MAX_PROMPT_CHARS + 200


def test_recap_outranks_identity_hint_on_budget(chained, tmp_path, monkeypatch):
    """预算不够时先丢「别换脸」，也要保住「已演过什么」。

    丢了前者只是脸飘一点；丢了后者整段就是前面某段的重播 —— 内容没推进更致命。
    """
    monkeypatch.setattr(video_long, "MAX_PROMPT_CHARS", 900)
    calls: list[dict] = []

    video_long.generate_segments(
        _fake_generator(tmp_path, calls), "片", seconds=20,
        shots=["第一段：主角醒来", "第二段：黑影出现"],
    )

    prompt = calls[1]["prompt"]
    assert "前面几个镜头已经演过" in prompt, f"摘要必须保住，实际提示词：{prompt[-200:]}"


def test_segments_differ_without_storyboard(chained, tmp_path):
    """没给分镜时三段共用一句提示词：靠「叙事阶段」把它们岔开，别出三段一样的。"""
    calls: list[dict] = []
    video_long.generate_segments(_fake_generator(tmp_path, calls), "末日废墟", seconds=30)

    prompts = [call["prompt"] for call in calls]
    assert len(set(prompts)) == 3, "三段提示词必须各不相同"
    assert "开头" in prompts[0]
    assert "结尾" in prompts[-1]


def test_single_segment_keeps_prompt_untouched(chained, tmp_path):
    """只有一段：一个字的提示词提示都不加。"""
    calls: list[dict] = []
    video_long.generate_segments(_fake_generator(tmp_path, calls), "短片", seconds=12)

    assert calls[0]["prompt"] == "短片"


def test_storyboard_is_padded_and_trimmed(chained, tmp_path):
    """分镜条数与段数对不上时：少了沿用最后一条，多了只用前面几条。"""
    short: list[dict] = []
    video_long.generate_segments(
        _fake_generator(tmp_path, short), "整片", seconds=30, shots=["一", "二"]
    )
    assert [call["prompt"].startswith(text) for call, text in zip(short, ["一", "二", "二"])] == [True] * 3
    assert short[2]["prompt"].startswith("二"), "缺的那段沿用最后一条"

    long_shots: list[dict] = []
    video_long.generate_segments(
        _fake_generator(tmp_path, long_shots), "整片", seconds=20,
        shots=["一", "二", "三", "四"],
    )
    assert len(long_shots) == 2, "20 秒只分两段"
    assert [call["prompt"].startswith(text) for call, text in zip(long_shots, ["一", "二"])] == [True, True]


def test_identity_hint_keeps_people_and_scene(chained, tmp_path):
    """用户反馈：后面几段主角换脸、背景对不上 —— 接续只靠一帧，文字锚点必须补上。"""
    calls: list[dict] = []

    video_long.generate_segments(
        _fake_generator(tmp_path, calls), "整片提示词", seconds=30
    )

    for call in calls[1:]:
        assert "不要引入新人物" in call["prompt"], "后续段要点名「人物一致」"
        assert "脸、发型、年龄、体型与服装都一模一样" in call["prompt"]
        assert "不要更换场景" in call["prompt"]
    assert "不要引入新人物" not in calls[0]["prompt"], "第一段没有上一段可比，不必加"


def test_subject_is_repeated_in_every_segment(chained, tmp_path):
    """给了 subject（主角与场景的固定措辞）就每段原样带上，包括第一段。"""
    calls: list[dict] = []
    subject = "young man with short black hair in a white T-shirt / abandoned train cabin"

    video_long.generate_segments(
        _fake_generator(tmp_path, calls), "整片提示词", seconds=30, subject=subject
    )

    assert len(calls) == 3
    for call in calls:
        assert subject in call["prompt"], "每一段都要带同一份主角/场景描述"
        assert "每一个镜头都必须与它完全相同" in call["prompt"]


def test_subject_works_together_with_storyboard(chained, tmp_path):
    """分镜（每段演什么）+ subject（谁在演）要能一起用 —— 这才是长视频的正确姿势。"""
    calls: list[dict] = []
    shots = ["开场：空镜", "中段：逼近", "收尾：标题"]

    video_long.generate_segments(
        _fake_generator(tmp_path, calls), "整片", seconds=30,
        shots=shots, subject="短发白 T 恤年轻男子 / 废弃车厢",
    )

    assert [call["prompt"].startswith(shot) for call, shot in zip(calls, shots)] == [True] * 3
    assert all("短发白 T 恤年轻男子" in call["prompt"] for call in calls)
    assert "不要引入新人物" in calls[1]["prompt"] and "不要引入新人物" in calls[2]["prompt"]


def test_single_segment_gets_no_identity_blocks(chained, tmp_path):
    calls: list[dict] = []
    video_long.generate_segments(
        _fake_generator(tmp_path, calls), "短片", seconds=10, subject="某人"
    )

    assert calls[0]["prompt"] == "短片", "单段不必加任何块"


def test_anchors_stay_within_prompt_budget(chained, tmp_path, monkeypatch):
    """用户提示词本来就长（实测 1031 字符）：追加锚点要有预算，优先保住「别换脸」。"""
    monkeypatch.setattr(video_long, "MAX_PROMPT_CHARS", 1400)
    calls: list[dict] = []
    long_prompt = "恐怖片镜头描述，紧张刺激的片头。" * 60      # 约 960 字符

    video_long.generate_segments(
        _fake_generator(tmp_path, calls), long_prompt, seconds=30,
        subject="短发白 T 恤年轻男子 / 废弃车厢",
    )

    assert all(len(call["prompt"]) <= 1400 for call in calls), "别把提示词撑爆"
    assert all("短发白 T 恤年轻男子" in call["prompt"] for call in calls), "最高的优先级要保住"
    assert "不要重复" not in calls[2]["prompt"], "预算不够时先丢优先级低的"


def test_seed_is_passthrough_never_auto_generated(chained, tmp_path):
    """seed 只透传，**不自动生成**。

    曾想「整条片子共用一颗种子」来压跨段漂移，实测供应商不认这个字段
    （同样首帧 + 同样提示词 + 同一个 seed 跑两次，画面差 15–20；换个 seed 反而只差 6–12，
    与「同一段自己演几秒」的变化量约 21 同量级）—— 自动塞一颗只会造成「结果被控住了」的错觉。
    """
    calls: list[dict] = []

    outcome = video_long.generate_segments(
        _fake_generator(tmp_path, calls), "整片提示词", seconds=30
    )
    assert [call["seed"] for call in calls] == [None, None, None], "不传就别塞"
    assert outcome["seed"] is None

    calls.clear()
    given = video_long.generate_segments(
        _fake_generator(tmp_path, calls), "整片提示词", seconds=30, seed=42
    )
    assert [call["seed"] for call in calls] == [42, 42, 42], "显式传了就原样发过去"
    assert given["seed"] == 42


def test_resolve_seed_tolerates_junk():
    assert video_long.resolve_seed(7) == 7
    assert video_long.resolve_seed("9") == 9
    assert video_long.resolve_seed("") is None
    assert video_long.resolve_seed(None) is None
    assert video_long.resolve_seed("不是数字") is None


def test_tool_passes_subject_through(wired, tmp_path):
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    calls: list[dict] = []
    tool = GenerateVideoTool(_fake_generator(tmp_path, calls), None, [])

    tool.run(prompt="恐怖短片", seconds="30", subject="短发年轻男子 / 废弃高铁车厢")

    assert all("废弃高铁车厢" in call["prompt"] for call in calls)


def test_tool_passes_storyboard_and_says_so(wired, tmp_path):
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    calls: list[dict] = []
    tool = GenerateVideoTool(_fake_generator(tmp_path, calls), None, [])

    shots = ["开场：废墟空镜", "中段：丧尸逼近", "收尾：标题浮现"]
    result = tool.run(prompt="丧尸片头", seconds="30", shots=shots)

    assert [call["prompt"].startswith(shot) for call, shot in zip(calls, shots)] == [True] * 3
    assert "已按你给的分镜逐段生成" in result.content


def test_tool_warns_when_storyboard_missing(wired, tmp_path):
    """没给分镜时要说一句（并给下次怎么改），否则用户只会觉得「怎么又重复了」。"""
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    calls: list[dict] = []
    tool = GenerateVideoTool(_fake_generator(tmp_path, calls), None, [])

    result = tool.run(prompt="丧尸片头", seconds="30")

    assert "没给分镜" in result.content and "shots" in result.content
    assert "不要重复" in calls[1]["prompt"], "兜底提示也要真的发出去"


def test_tool_notes_storyboard_count_mismatch(wired, tmp_path):
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    calls: list[dict] = []
    tool = GenerateVideoTool(_fake_generator(tmp_path, calls), None, [])

    result = tool.run(prompt="丧尸片头", seconds="30", shots=["只有一条"])

    assert "你给了 1 条分镜" in result.content and "3 段" in result.content


def test_description_teaches_storyboard_usage():
    """模型不知道要传 shots 的话，这个功能等于不存在。"""
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    text = GenerateVideoTool.description
    assert "shots" in text and "重复" in text
    param = next(
        item for item in GenerateVideoTool(None).parameters if item.name == "shots"
    )
    assert "分镜" in param.description and "重复" in param.description


# ---------------------------------------------------------------- 人物 / 场景锚定
def _anchor_sampler():
    """假的采样器：从「第 1 段」采三帧。"""
    def sample(path: str) -> list[str]:
        return [f"data:image/jpeg;base64,{Path(path).stem}-{index}" for index in (1, 2, 3)]

    return sample


def test_character_continuity_anchors_every_later_segment(chained, tmp_path):
    """默认（character）：第 1 段出片后采帧当基准，后面每段都挂它 —— 不再接帧。

    用户的原话是「不用一镜到底，但要人物和场景别乱变」，所以默认走这条；
    段间是硬切，靠参考图把人脸与场景钉住。
    """
    calls: list[dict] = []
    outcome = video_long.generate_segments(
        _fake_generator(tmp_path, calls), "整片提示词", seconds=30,
        sampler=_anchor_sampler(),
    )

    assert "first_frame" not in calls[0] and "images" not in calls[0], "第 1 段没有可参考的东西"
    for index, call in enumerate(calls[1:], 2):
        assert "first_frame" not in call, f"第 {index} 段不该再接帧（硬切）"
        assert len(call["images"]) == 3, f"第 {index} 段要挂上基准帧"
        assert call["images"][0].endswith("视频-1-1"), "基准取自第 1 段"
    assert outcome["continuity"] == "character"
    assert outcome["reference_count"] == 3


def test_seamless_continuity_still_chains_frames(chained, tmp_path):
    """要一镜到底时仍然走首尾帧链（老行为保留，且不再挂参考图）。"""
    calls: list[dict] = []
    video_long.generate_segments(
        _fake_generator(tmp_path, calls), "整片提示词", seconds=30,
        continuity="seamless", sampler=_anchor_sampler(),
    )

    assert calls[1]["first_frame"].endswith("视频-1.mp4"), "上一段末帧当首帧"
    assert "images" not in calls[1]


def test_given_anchor_is_used_from_the_first_segment(chained, tmp_path):
    """给了基准图：第 1 段就挂上它（不采第 1 段的帧了）。"""
    calls: list[dict] = []
    anchor = ["data:image/jpeg;base64,STILL"]
    outcome = video_long.generate_segments(
        _fake_generator(tmp_path, calls), "整片提示词", seconds=30,
        anchor_frames=anchor, sampler=_anchor_sampler(),
    )

    assert calls[0]["images"] == anchor
    assert all(call["images"] == anchor for call in calls), "每一段都挂同一份基准"
    assert outcome["reference_count"] == 1


def test_reference_images_are_capped(chained, tmp_path):
    """供应商最多收 5 张参考图：基准 + 用户附件一起也不能超。"""
    calls: list[dict] = []
    video_long.generate_segments(
        _fake_generator(tmp_path, calls), "整片提示词", seconds=20,
        anchor_frames=[f"data:image/jpeg;base64,A{index}" for index in range(4)],
        images=[f"data:image/jpeg;base64,U{index}" for index in range(3)],
    )

    assert all(len(call["images"]) <= video_long.MAX_REFERENCE_IMAGES for call in calls)
    assert calls[0]["images"][:4] == [f"data:image/jpeg;base64,A{index}" for index in range(4)], \
        "基准优先"


def test_sampling_failure_falls_back_to_chaining(chained, tmp_path):
    """采不到帧（缺 ffmpeg / 文件坏了）时退回首尾帧链，而不是丢掉一致性不管。"""
    calls: list[dict] = []
    video_long.generate_segments(
        _fake_generator(tmp_path, calls), "整片提示词", seconds=20,
        sampler=lambda path: [],
    )

    assert calls[1]["first_frame"].endswith("视频-1.mp4"), "退回首尾帧链"


def test_tool_passes_anchor_and_continuity(wired, tmp_path, monkeypatch):
    from paper_agent.services.skills import video_skills
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    still = _image(tmp_path / "定妆图.png")
    clip = tmp_path / "上一段.mp4"
    clip.write_bytes(b"mp4")
    monkeypatch.setattr(
        video_skills, "sample_frames",
        lambda path, *a, **k: ["data:image/jpeg;base64,F1", "data:image/jpeg;base64,F2"],
    )
    files = [{"name": still.name, "path": str(still)}, {"name": clip.name, "path": str(clip)}]

    calls: list[dict] = []
    tool = GenerateVideoTool(_fake_generator(tmp_path, calls), None, files)
    result = tool.run(prompt="恐怖短片", seconds="30", anchor=still.name)
    assert calls[0]["images"][0].startswith("data:image/png;base64,"), "图片基准内联成 data 地址"
    assert all("images" in call for call in calls)
    assert "基准" in result.content

    calls.clear()
    tool.run(prompt="恐怖短片", seconds="30", anchor=clip.name)
    assert calls[0]["images"] == ["data:image/jpeg;base64,F1", "data:image/jpeg;base64,F2"], \
        "给视频就从它上面采帧"

    calls.clear()
    tool.run(prompt="恐怖短片", seconds="30", continuity="seamless")
    assert calls[1]["first_frame"], "seamless 要接上一段末帧"
    assert "images" not in calls[1]


def test_anchor_accepts_multiple_files(wired, tmp_path, monkeypatch):
    """anchor 可以一次给多张（人物正脸 + 场景 + 服装），逗号分开。

    供应商一轮最多收 5 张参考图，多给几张比只给一张更稳 —— 一个人的正脸照里没有场景，
    场景照里没有人脸，分开给才都覆盖到。
    """
    from paper_agent.services.skills import video_skills
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    monkeypatch.setattr(
        video_skills, "sample_frames", lambda path, *a, **k: ["data:image/jpeg;base64,SCENE"]
    )
    hero = _image(tmp_path / "主角正脸.png")
    scene = _image(tmp_path / "场景.jpg")
    clip = tmp_path / "上一段.mp4"
    clip.write_bytes(b"mp4")
    files = [
        {"name": hero.name, "path": str(hero)},
        {"name": scene.name, "path": str(scene)},
        {"name": clip.name, "path": str(clip)},
    ]

    calls: list[dict] = []
    tool = GenerateVideoTool(_fake_generator(tmp_path, calls), None, files)
    result = tool.run(prompt="恐怖短片", seconds="30", anchor="主角正脸.png, 场景.jpg")

    assert result.success is True
    assert len(calls[0]["images"]) == 2, f"两张都要挂上：{calls[0]['images']}"
    assert all(item.startswith("data:image/") for item in calls[0]["images"])
    assert "2 张" in result.content, "要说清用了几张基准图"

    # 混着视频也给：图片 + 视频采帧一起挂，并按供应商上限（5 张）截断
    calls.clear()
    tool.run(prompt="恐怖短片", seconds="30", anchor="主角正脸.png, 场景.jpg, 上一段.mp4")
    assert len(calls[0]["images"]) == 3, "两张图 + 从视频采的 1 帧"
    from paper_agent.services.video_long import MAX_REFERENCE_IMAGES

    assert len(calls[0]["images"]) <= MAX_REFERENCE_IMAGES


def test_anchor_reports_missing_file(wired, tmp_path):
    """多张里有一张找不到：明确报错，而不是悄悄少给基准（否则成片会莫名地不像）。"""
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    hero = _image(tmp_path / "主角正脸.png")
    calls: list[dict] = []
    tool = GenerateVideoTool(
        _fake_generator(tmp_path, calls), None, [{"name": hero.name, "path": str(hero)}]
    )

    result = tool.run(prompt="恐怖短片", seconds="30", anchor="主角正脸.png, 不存在的场景.jpg")

    assert result.success is False
    assert "没找到基准图" in result.error
    assert calls == [], "认不出来就别去生成"


def test_anchor_with_data_url_is_not_split(wired, tmp_path):
    """data 链接自己就带逗号，不能被当成「多张」拆开。"""
    from paper_agent.services.skills.video_skills import _split_anchor

    one = "data:image/jpeg;base64,/9j/AAAABBBB,CCCC"
    assert _split_anchor(one) == [one]
    assert _split_anchor("a.png, b.jpg") == ["a.png", "b.jpg"]
    assert _split_anchor("a.png、b.jpg；c.jpg") == ["a.png", "b.jpg", "c.jpg"]


def test_tool_reports_bad_anchor_and_continuity(wired, tmp_path):
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    calls: list[dict] = []
    tool = GenerateVideoTool(_fake_generator(tmp_path, calls), None, [])

    missing = tool.run(prompt="恐怖短片", seconds="30", anchor="不存在.png")
    assert missing.success is False and "list_artifacts" in missing.error
    assert calls == [], "认不出基准就别去调接口"

    calls.clear()
    tool.run(prompt="恐怖短片", seconds="30", continuity="乱填的")
    assert calls and calls[0], "乱填的 continuity 要退回默认（character），不能报错"
    # 假视频采不到帧（不是真 mp4），于是退回首尾帧链 —— 但绝不能「既不锚也不接」
    assert calls[1].get("first_frame") or calls[1].get("images")


def test_tool_continue_from_anchors_long_video(wired, tmp_path, monkeypatch):
    """接着上一段拍长视频：第 1 段接它的末帧，后面几段挂从它身上采的帧。"""
    from paper_agent.services.skills import video_skills
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    clip = tmp_path / "上一段.mp4"
    clip.write_bytes(b"mp4")
    monkeypatch.setattr(
        video_skills, "last_frame_data_url", lambda path, timeout=None: "data:image/jpeg;base64,LAST"
    )
    monkeypatch.setattr(
        video_skills, "sample_frames",
        lambda path, *a, **k: ["data:image/jpeg;base64,B1", "data:image/jpeg;base64,B2"],
    )
    calls: list[dict] = []
    tool = GenerateVideoTool(
        _fake_generator(tmp_path, calls), None, [{"name": clip.name, "path": str(clip)}]
    )

    result = tool.run(prompt="续集", seconds="30", continue_from=clip.name)

    assert calls[0]["first_frame"] == "data:image/jpeg;base64,LAST"
    assert calls[1]["images"] == ["data:image/jpeg;base64,B1", "data:image/jpeg;base64,B2"]
    assert "采 2 帧作为人物 / 场景基准" in result.content


# ---------------------------------------------------------------- 引擎接线
class _SegmentTool(Tool):
    """假装「分段出片」：跑到一半就把产物挂出去（真实工具就是这么回调的）。"""

    name = "generate_video"
    description = "生成视频（支持长视频）"
    parameters: list = []

    def __init__(self, paths: list[Path]) -> None:
        self._paths = paths
        self.hook = None

    def set_artifact_hook(self, hook) -> None:      # noqa: ANN001
        self.hook = hook

    def run(self, **kwargs) -> ToolResult:
        for path in self._paths:
            if self.hook:
                self.hook({"name": path.name, "path": str(path), "kind": "video"})
        return ToolResult(content="出片完成", artifact_paths=[str(self._paths[-1])])


class _FakeClient:
    """第一轮调工具，第二轮收尾。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.round = 0
        self.last_finish_reason = "tool_calls"
        self.reasoning_dropped = False

    def stream_events(self, messages, tools=None, temperature=0.7, max_tokens=0):
        self.round += 1
        if self.round == 1:
            yield {
                "type": "tool_calls",
                "calls": [{"id": "c1", "name": self.name, "arguments": '{"prompt": "猫"}'}],
            }
            return
        yield {"type": "content", "text": "好了"}


def test_engine_publishes_artifacts_while_tool_runs(tmp_path):
    """引擎要把工具的实时产物转成 ARTIFACT 事件，而且收尾不许重复挂。"""
    import json

    from paper_agent.services.agents.engine import ARTIFACT, AgentOrchestrator
    from paper_agent.services.skills.base import ToolRegistry

    paths = [tmp_path / f"视频-{index}.mp4" for index in (1, 2, 3)]
    for path in paths:
        path.write_bytes(b"mp4")
    registry = ToolRegistry()
    tool = _SegmentTool(paths)
    registry.register(tool)

    events: list[tuple[str, str]] = []
    engine = AgentOrchestrator(client=_FakeClient("generate_video"), registry=registry)
    engine._messages = [{"role": "user", "content": "来个 30 秒的视频"}]
    engine.run(lambda kind, text: events.append((kind, text)))

    names = [json.loads(text)["name"] for kind, text in events if kind == ARTIFACT]
    assert names == ["视频-1.mp4", "视频-2.mp4", "视频-3.mp4"], f"顺序与去重都不对：{names}"
    assert tool.hook is None, "收工要撤掉回调（后台线程可能还活着）"


# ---------------------------------------------------------------- ffmpeg 合成（真跑）
@requires_ffmpeg
def test_merge_is_lossless_when_params_match(tmp_path):
    """同源分段（参数一致）走无损拼接：时长等于各段之和，且不重编码。"""
    clips = [_make_clip(tmp_path / f"{index}.mp4", color) for index, color in enumerate(["red", "green", "blue"])]
    target = tmp_path / "长视频.mp4"

    info = video_merge.merge_videos(clips, target)

    assert info["reencoded"] is False, "参数一致时不该重编码（又慢又损画质）"
    assert abs(info["duration"] - 6.0) <= 0.2
    assert target.stat().st_size > 0
    assert video_merge.probe_duration(target) > 5.5


@requires_ffmpeg
def test_merge_normalizes_mismatched_segments(tmp_path):
    """参数不一致（缺音轨 / 分辨率不同）时自动先统一编码，而不是给个坏文件。"""
    first = _make_clip(tmp_path / "a.mp4", "red")
    odd = _make_clip(tmp_path / "b.mp4", "blue", audio=False, size="640x360")

    assert video_merge.params_match([first, odd]) is False
    info = video_merge.merge_videos([first, odd], tmp_path / "混.mp4")

    assert info["reencoded"] is True
    assert abs(info["duration"] - 4.0) <= 0.4


@requires_ffmpeg
def test_merge_refuses_single_clip(tmp_path):
    clip = _make_clip(tmp_path / "one.mp4", "red")
    with pytest.raises(video_merge.VideoMergeError):
        video_merge.merge_videos([clip], tmp_path / "x.mp4")


def test_merge_without_ffmpeg_explains_how_to_fix(monkeypatch):
    """没 ffmpeg 时报错要带上「怎么装」，而不是一句「合成失败」。"""
    monkeypatch.setattr(video_merge, "_candidates", lambda: [])
    video_merge.reset_probe()
    try:
        assert video_merge.ffmpeg_exe() == ""
        with pytest.raises(video_merge.VideoMergeError) as info:
            video_merge.merge_videos(["a.mp4", "b.mp4"], "out.mp4")
        assert "imageio-ffmpeg" in str(info.value)
    finally:
        video_merge.reset_probe()


def test_merge_rejects_missing_files(tmp_path, monkeypatch):
    monkeypatch.setattr(video_merge, "ffmpeg_exe", lambda: "ffmpeg")
    with pytest.raises(video_merge.VideoMergeError, match="找不到"):
        video_merge.merge_videos(
            [str(tmp_path / "没有1.mp4"), str(tmp_path / "没有2.mp4")], tmp_path / "o.mp4"
        )
