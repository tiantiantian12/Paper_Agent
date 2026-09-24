"""文生视频链路：响应解析、轮询落盘、工具注册、界面参数。

视频是「提交任务 → 轮询 → 下载」三步，返回字段各家不统一，所以解析要宽容；
另外它和生图一样要能挂进智能体的工具表（对话模型也能生成视频）。
"""

from __future__ import annotations

import pytest

from paper_agent.services import video_client as video_module
from paper_agent.services.video_client import (
    ASPECT_OPTIONS,
    SECOND_OPTIONS,
    VideoClient,
    VideoGenerationError,
    pick_progress,
    pick_status,
    pick_video_id,
    pick_video_url,
)


@pytest.fixture
def artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(video_module, "ARTIFACTS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """把配置文件指到临时目录，避免动到用户真实设置。"""
    from paper_agent.core import config as config_module

    monkeypatch.setattr(config_module, "SETTINGS_FILE", tmp_path / "settings.ini")
    config = config_module.AppConfig()
    yield config
    config.sync()


# ---------------------------------------------------------------- 响应解析
@pytest.mark.parametrize(
    "payload",
    [
        {"video_id": "v-1"},
        {"id": "v-2"},
        {"task_id": "v-3"},
        {"data": {"video_id": "v-4"}},
        {"data": [{"id": "v-5"}]},
    ],
)
def test_video_id_is_found(payload):
    assert pick_video_id(payload) in {"v-1", "v-2", "v-3", "v-4", "v-5"}


def test_video_id_missing():
    assert pick_video_id({}) == ""
    assert pick_video_id(None) == ""


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"status": "completed"}, "completed"),
        ({"status": "SUCCESS"}, "completed"),
        ({"status": "done"}, "completed"),
        ({"status": "failed"}, "failed"),
        ({"status": "error"}, "failed"),
        ({"status": "queued"}, "pending"),
        ({"data": {"status": "Completed"}}, "completed"),
        ({}, ""),
    ],
)
def test_status_normalisation(payload, expected):
    assert pick_status(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        {"video_url": "https://x/a.mp4"},
        {"url": "https://x/b.mp4"},
        {"download_url": "https://x/c.mp4"},
        {"data": {"video_url": "https://x/d.mp4"}},
        {"result": {"items": [{"url": "https://x/e.mp4"}]}},
    ],
)
def test_video_url_is_found(payload):
    assert pick_video_url(payload).endswith(".mp4")


def test_video_url_missing():
    assert pick_video_url({"status": "completed"}) == ""


# ---------------------------------------------------------------- 进度
@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"progress": 40}, 40),
        ({"progress": 0}, 0),
        ({"progress": 100}, 100),
        ({"percent": "75%"}, 75),
        ({"data": {"progress": 30}}, 30),
        ({"progress": 0.4}, 40),          # 有的服务商给 0–1 的比例
        ({"progress": 1.0}, 100),         # 比例里的「完成」
        ({"progress": 1}, 1),             # 整数 1 还是按 1% 算
        ({"progress": 250}, 100),         # 越界夹到 100
        ({"status": "completed"}, -1),    # 没给进度
        ({"progress": "unknown"}, -1),
        ({}, -1),
        (None, -1),
    ],
)
def test_progress_is_parsed(payload, expected):
    assert pick_progress(payload) == expected


def test_progress_prefers_known_key_over_nested():
    """真实响应里 progress 与 internal_progress 同时存在：以 progress 为准。"""
    payload = {
        "id": "task_gQVyidqaRSK0XGDXTfSOzsPa0d12P2nU",
        "object": "video",
        "status": "in_progress",
        "progress": 70,
        "internal_progress": 0,
    }
    assert pick_progress(payload) == 70


def test_progress_creeps_between_real_updates(monkeypatch):
    """回归：供应商只在 10% / 100% 这种档位上跳，界面不能卡在 10% 一动不动。

    两次真实上报之间要按时间外推，越接近上限走得越慢，但**出片前不越过 95**。
    """
    from paper_agent.services.video_client import _ProgressPacer

    class _Clock:
        """假时钟：外推是按时间算的，测试里不能真等两分钟。"""

        def __init__(self) -> None:
            self.now = 1000.0

        def monotonic(self) -> float:
            return self.now

    clock = _Clock()
    monkeypatch.setattr(video_module, "time", clock)

    seen: list[int] = []
    pacer = _ProgressPacer(seen.append)
    pacer.real(10)
    assert seen == [10]

    for delta in (15, 30, 60, 120, 300, 600):
        clock.now = 1000.0 + delta
        pacer.tick()

    assert seen == sorted(seen), "只进不退"
    assert len(set(seen)) >= 4, f"要一路在动，实际只有 {seen}"
    assert seen[-1] > 50, f"两分钟后还贴着 10% 就是没动：{seen}"
    assert max(seen) <= _ProgressPacer.CEILING
    assert 100 not in seen, "还没出片就不该显示 100"


def test_pacer_only_hits_100_when_done(monkeypatch):
    from paper_agent.services.video_client import _ProgressPacer

    seen: list[int] = []
    pacer = _ProgressPacer(seen.append)
    pacer.real(10)
    pacer.real(100)

    assert seen[-1] == 100
    pacer.tick()
    assert seen[-1] == 100, "完成之后不能再被外推改小"


def test_pacer_ignores_unknown_and_backwards(monkeypatch):
    """还没拿到进度时一声不吭（界面保持「不确定」动画）；供应商回退也不跟着倒退。"""
    from paper_agent.services.video_client import _ProgressPacer

    seen: list[int] = []
    pacer = _ProgressPacer(seen.append)
    pacer.tick()
    pacer.real(-1)
    assert seen == []

    assert pacer.known is False
    pacer.real(70)
    pacer.real(10)
    assert seen == [70], "回退的真实值不该让进度条倒着走"
    assert pacer.known is True


def test_wait_keeps_progress_moving_between_polls(artifacts, monkeypatch):
    """轮询间隔里的空档也要推进度 —— 用户看到的「卡住」就是这段空档。"""
    from paper_agent.services import video_client as module

    monkeypatch.setattr(module, "_ProgressPacer", module._ProgressPacer)
    monkeypatch.setattr(module._ProgressPacer, "TAU", 0.02)      # 让外推在毫秒级就能看出变化
    monkeypatch.setattr(module, "PROGRESS_TICK_SECONDS", 0.01)

    monkeypatch.setattr(VideoClient, "_create", lambda self, payload: {"video_id": "v-1"})
    states = iter([
        {"status": "in_progress", "progress": 10},
        {"status": "completed", "progress": 100, "url": "https://x/a.mp4"},
    ])
    monkeypatch.setattr(VideoClient, "_status", lambda self, vid: next(states))
    monkeypatch.setattr(VideoClient, "_download", lambda self, url: b"MP4")

    seen: list[int] = []
    client = VideoClient(base_url="http://x/v1", api_key="k", model_id="m", poll_interval=0.3)
    client.generate("一只橘猫", on_progress=seen.append)

    assert seen[0] == 10
    assert seen[-1] == 100
    assert len(seen) > 3, f"两次轮询之间应当一直在推：{seen}"
    assert seen == sorted(seen)


# ---------------------------------------------------------------- 提交阶段的重试
# 供应商的「视频队列已满」是整个服务的（实测两把 Key 同一时刻都收到 video_queue_full，
# 换 Key 没用），只能等它腾出位置。长视频是一段一段串行投的，前几段都出来了、
# 最后一段撞上队列满就整条残了 —— 所以提交阶段要自己退避重投。
def _busy_client(monkeypatch, failures: int, error: str = "服务繁忙（HTTP 503）：视频队列已满，请稍后再试"):
    """前 ``failures`` 次提交报「忙」，之后成功。"""
    monkeypatch.setattr(video_module, "SUBMIT_RETRY_DELAYS", (0.01, 0.02))
    attempts = {"n": 0}

    def create(self, payload):
        attempts["n"] += 1
        if attempts["n"] <= failures:
            raise VideoGenerationError(error)
        return {"video_id": "v-1"}

    monkeypatch.setattr(VideoClient, "_create", create)
    monkeypatch.setattr(
        VideoClient, "_status", lambda self, vid: {"status": "completed", "url": "https://x/a.mp4"}
    )
    monkeypatch.setattr(VideoClient, "_download", lambda self, url: b"MP4")
    return attempts


def test_submit_retries_when_queue_full(artifacts, monkeypatch):
    """503 队列满 → 等一会儿重投，第二次就成了。"""
    attempts = _busy_client(monkeypatch, failures=1)

    client = VideoClient(base_url="http://x/v1", api_key="k", model_id="m", poll_interval=0.001)
    items = client.generate("一只橘猫")

    assert len(items) == 1
    assert attempts["n"] == 2, "该重试一次"


def test_submit_gives_up_after_retries(artifacts, monkeypatch):
    """一直满就别无限等：重试用完就把最后一次的错抛出去。"""
    attempts = _busy_client(monkeypatch, failures=99)

    client = VideoClient(base_url="http://x/v1", api_key="k", model_id="m", poll_interval=0.001)
    with pytest.raises(VideoGenerationError, match="503"):
        client.generate("一只橘猫")

    assert attempts["n"] == 3, "最多投 3 次（首投 + 2 次重试）"


def test_submit_does_not_retry_parameter_errors(artifacts, monkeypatch):
    """参数错误 / 鉴权错误不能重试 —— 再等也是同样的错。"""
    attempts = _busy_client(monkeypatch, failures=99, error="HTTP 400 · 参数不合法")

    client = VideoClient(base_url="http://x/v1", api_key="k", model_id="m", poll_interval=0.001)
    with pytest.raises(VideoGenerationError, match="400"):
        client.generate("一只橘猫")

    assert attempts["n"] == 1, "不是「忙」就别重试"


def test_submit_retry_can_be_stopped(artifacts, monkeypatch):
    """等待重投期间按 Esc 要能立刻放弃（不然界面会僵住一分钟）。"""
    _busy_client(monkeypatch, failures=99)

    client = VideoClient(base_url="http://x/v1", api_key="k", model_id="m", poll_interval=0.001)
    with pytest.raises(VideoGenerationError, match="__stopped__"):
        client.generate("一只橘猫", is_stopped=lambda: True)


def test_submit_retry_reports_status(artifacts, monkeypatch):
    """重试时要把「队列已满，稍后自动重试」报给界面，别让人以为卡死了。"""
    _busy_client(monkeypatch, failures=1)

    seen: list[str] = []
    client = VideoClient(base_url="http://x/v1", api_key="k", model_id="m", poll_interval=0.001)
    client.generate("一只橘猫", on_status=seen.append)

    assert "retrying" in seen, f"界面要看到重试状态：{seen}"


def test_generate_reports_progress(artifacts, monkeypatch):
    """出片途中的百分比要一路回调出来（界面进度条靠它），重复值不重复回调。"""
    monkeypatch.setattr(VideoClient, "_create", lambda self, payload: {"video_id": "v-1"})
    states = iter([
        {"status": "queued", "progress": 0},
        {"status": "in_progress", "progress": 10},
        {"status": "in_progress", "progress": 10},
        {"status": "in_progress", "progress": 70},
        {"status": "completed", "progress": 100, "url": "https://x/a.mp4"},
    ])
    monkeypatch.setattr(VideoClient, "_status", lambda self, vid: next(states))
    monkeypatch.setattr(VideoClient, "_download", lambda self, url: b"MP4")

    seen: list[int] = []
    client = VideoClient(base_url="http://x/v1", api_key="k", model_id="m", poll_interval=0.001)
    client.generate("一只橘猫", on_progress=seen.append)

    assert seen == [0, 10, 70, 100]


def test_generate_without_progress_field_stays_quiet(artifacts, monkeypatch):
    """服务商没给进度时不能乱报：回调一次都不该触发（界面走「排队中」样式）。"""
    monkeypatch.setattr(VideoClient, "_create", lambda self, payload: {"video_id": "v-1"})
    states = iter([
        {"status": "queued"},
        {"status": "completed", "video_url": "https://x/a.mp4"},
    ])
    monkeypatch.setattr(VideoClient, "_status", lambda self, vid: next(states))
    monkeypatch.setattr(VideoClient, "_download", lambda self, url: b"MP4")

    seen: list[int] = []
    client = VideoClient(base_url="http://x/v1", api_key="k", model_id="m", poll_interval=0.001)
    client.generate("一只橘猫", on_progress=seen.append)

    assert seen == []


# ---------------------------------------------------------------- 生成流程
def test_generate_polls_until_completed(artifacts, monkeypatch):
    seen: list[str] = []

    monkeypatch.setattr(VideoClient, "_create", lambda self, payload: {"video_id": "v-1"})
    states = iter([{"status": "queued"}, {"status": "processing"},
                   {"status": "completed", "video_url": "https://x/a.mp4"}])
    monkeypatch.setattr(VideoClient, "_status", lambda self, vid: next(states))
    monkeypatch.setattr(VideoClient, "_download", lambda self, url: b"MP4DATA")

    client = VideoClient(base_url="http://x/v1", api_key="k", model_id="m", poll_interval=0.001)
    items = client.generate("一只橘猫", on_status=seen.append)

    assert len(items) == 1
    assert items[0]["kind"] == "video"
    assert items[0]["name"].endswith(".mp4")
    with open(items[0]["path"], "rb") as file:
        assert file.read() == b"MP4DATA"
    assert seen == ["pending", "completed"]


def test_generate_reports_failure(artifacts, monkeypatch):
    monkeypatch.setattr(VideoClient, "_create", lambda self, payload: {"video_id": "v-1"})
    monkeypatch.setattr(
        VideoClient, "_status", lambda self, vid: {"status": "failed", "error": "额度不足"}
    )

    client = VideoClient(base_url="http://x/v1", api_key="k", model_id="m", poll_interval=0.001)
    with pytest.raises(VideoGenerationError, match="额度不足"):
        client.generate("一只橘猫")


def test_generate_can_be_stopped(artifacts, monkeypatch):
    """用户按停止：立刻放弃等待（异常由 worker 转成「已停止」）。"""
    monkeypatch.setattr(VideoClient, "_create", lambda self, payload: {"video_id": "v-1"})
    monkeypatch.setattr(VideoClient, "_status", lambda self, vid: {"status": "queued"})

    client = VideoClient(base_url="http://x/v1", api_key="k", model_id="m", poll_interval=0.001)
    with pytest.raises(VideoGenerationError, match="__stopped__"):
        client.generate("一只橘猫", is_stopped=lambda: True)


def test_polling_backs_off_when_throttled(artifacts, monkeypatch):
    """回归：供应商对「查任务」也限流（429 查询过于频繁），那不是生成失败。

    2 秒一次真的会被打回 429，所以轮询遇到 429 / 503 要退避重试，而不是直接报错。
    """
    monkeypatch.setattr(VideoClient, "_create", lambda self, payload: {"video_id": "v-1"})
    queries = iter([
        VideoGenerationError("服务繁忙（HTTP 429）：查询过于频繁，请稍后重试"),
        {"status": "in_progress"},
        VideoGenerationError("服务繁忙（HTTP 503）：video queue is full"),
        {"status": "completed", "url": "https://x/a.mp4"},
    ])

    def fake_status(self, vid):
        item = next(queries)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(VideoClient, "_status", fake_status)
    monkeypatch.setattr(VideoClient, "_download", lambda self, url: b"MP4")
    monkeypatch.setattr(VideoClient, "_wait", VideoClient._wait)

    client = VideoClient(base_url="http://x/v1", api_key="k", model_id="m", poll_interval=0.001)
    items = client.generate("一只橘猫")

    assert len(items) == 1, "限流退避后应当拿到成品"


def test_create_failure_is_reported(artifacts, monkeypatch):
    """提交一直被限流：重试用完（首投 + 2 次）就把错抛出来，不无限等。

    早先这里是「不重试、立刻报错」。2026-09-23 实测两把 Key 同一时刻都收到
    ``video_queue_full`` —— 队列是整个服务的，换 Key 没用，只能等它腾位置；
    而长视频一段一段串行投，最后一段被拒整条就残了，所以改成退避重投两次。
    """
    monkeypatch.setattr(video_module, "SUBMIT_RETRY_DELAYS", (0.01, 0.02))
    calls = {"n": 0}

    def fail(self, payload):
        calls["n"] += 1
        raise VideoGenerationError("服务繁忙（HTTP 429）：视频队列已满，请稍后再试")

    monkeypatch.setattr(VideoClient, "_create", fail)

    client = VideoClient(base_url="http://x/v1", api_key="k", model_id="m")
    with pytest.raises(VideoGenerationError, match="队列已满"):
        client.generate("一只橘猫")

    assert calls["n"] == 3, "首投 + 2 次重试，之后就该报错"


def test_generate_respects_options(artifacts, monkeypatch):
    sent: dict = {}

    def fake_create(self, payload):
        sent.update(payload)
        return {"video_id": "v-1"}

    monkeypatch.setattr(VideoClient, "_create", fake_create)
    monkeypatch.setattr(
        VideoClient, "_status", lambda self, vid: {"status": "completed", "url": "https://x/a.mp4"}
    )
    monkeypatch.setattr(VideoClient, "_download", lambda self, url: b"x")

    client = VideoClient(base_url="http://x/v1", api_key="k", model_id="agnes-video-2.5-flash")
    client.generate("一只橘猫", seconds="8", aspect_ratio="9:16", seed=42)

    assert sent["model"] == "agnes-video-2.5-flash"
    assert sent["mode"] == "text"
    assert sent["seconds"] == "8"
    assert sent["aspect_ratio"] == "9:16"
    assert sent["size"] == "720P"
    assert sent["seed"] == 42


def test_images_switch_to_reference_mode(artifacts, monkeypatch):
    sent: dict = {}
    monkeypatch.setattr(VideoClient, "_create", lambda self, payload: sent.update(payload) or {"video_id": "v"})
    monkeypatch.setattr(
        VideoClient, "_status", lambda self, vid: {"status": "completed", "url": "https://x/a.mp4"}
    )
    monkeypatch.setattr(VideoClient, "_download", lambda self, url: b"x")

    client = VideoClient(base_url="http://x/v1", api_key="k", model_id="m")
    client.generate("让它动起来", images=["data:image/png;base64,AAA"])

    assert sent["mode"] == "reference"
    assert sent["images"] == ["data:image/png;base64,AAA"]


def test_illegal_options_are_snapped_not_silently_shortened(artifacts, monkeypatch):
    """非法秒数**就近取档位**，绝不静默回落默认 5 秒（画幅照旧回落 16:9）。

    以前的写法是「不在 4/5/6/8/10/12 里就换成默认 5」——于是用户要 11 秒，拿到的是
    5.18 秒的片子，而结果文案还写着「约 11 秒」（2026-09-24 的真实事故）。
    现在档位外的一律往上取最近一档；比上限还长就按上限（更长的片子由 video_long 分段）。
    """
    sent: dict = {}
    monkeypatch.setattr(VideoClient, "_create", lambda self, payload: sent.update(payload) or {"video_id": "v"})
    monkeypatch.setattr(
        VideoClient, "_status", lambda self, vid: {"status": "completed", "url": "https://x/a.mp4"}
    )
    monkeypatch.setattr(VideoClient, "_download", lambda self, url: b"x")

    client = VideoClient(base_url="http://x/v1", api_key="k", model_id="m")
    client.generate("一只橘猫", seconds="99", aspect_ratio="100:1")

    assert sent["seconds"] == "12", "超上限按上限，而不是掉到 5 秒"
    assert sent["aspect_ratio"] == "16:9"


@pytest.mark.parametrize("api_key,model_id", [("", "m"), ("k", ""), ("", "")])
def test_available_requires_model_and_key(api_key, model_id):
    assert VideoClient(base_url="http://x/v1", api_key=api_key, model_id=model_id).available is False
    assert VideoClient(base_url="http://x/v1", api_key="k", model_id="m").available is True


def test_options_lists_are_consistent():
    assert "5" in SECOND_OPTIONS and "16:9" in ASPECT_OPTIONS


# ---------------------------------------------------------------- 工具注册
def _registry_names(tmp_path, mode, generator, images=None):
    from paper_agent.services.skills import build_default_registry

    return set(
        build_default_registry(
            video_generator=generator, video_images_provider=images, mode=mode, workspace=tmp_path
        ).names()
    )


def test_video_tool_registered_when_available(tmp_path):
    assert "generate_video" in _registry_names(tmp_path, "doc", lambda *a, **k: [])
    assert "generate_video" in _registry_names(tmp_path, "code", lambda *a, **k: [])


def test_video_tool_absent_without_capability(tmp_path):
    assert "generate_video" not in _registry_names(tmp_path, "doc", None)


def test_video_tool_passes_images_and_params(tmp_path):
    calls: list[dict] = []

    def generator(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        return [{"name": "a.mp4", "path": str(tmp_path / "a.mp4")}]

    names = _registry_names(tmp_path, "doc", generator, images=lambda: ["data:image/png;base64,x"])
    assert "generate_video" in names

    from paper_agent.services.skills import build_default_registry

    registry = build_default_registry(
        video_generator=generator,
        video_images_provider=lambda: ["data:image/png;base64,x"],
        mode="doc",
        workspace=tmp_path,
    )
    result = registry.execute(
        "generate_video", {"prompt": "让它跑起来", "seconds": "8", "aspect_ratio": "9:16"}
    )
    assert result.success is True
    assert calls[0]["images"] == ["data:image/png;base64,x"]
    assert calls[0]["seconds"] == "8"


def test_video_tool_without_capability_reports_error(tmp_path):
    from paper_agent.services.skills.video_skills import GenerateVideoTool

    result = GenerateVideoTool(None).run(prompt="一只猫")
    assert result.success is False
    assert "未配置" in result.error


# ---------------------------------------------------------------- 配置
def test_video_model_is_recognised_and_proxied(settings):
    """服务端配了视频模型：任意对话模型都能顺手出视频，走服务端代理。"""
    settings.set_account("token", {"id": 1, "email": "a@qq.com", "nickname": "甲"})
    settings.llm_user_key = "pk-user"
    settings.builtin_models = [
        {"model_id": "agnes-3.0-flash", "name": "Agnes", "desc": "", "kind": "chat"},
        {"model_id": "agnes-video-2.5-flash", "name": "Agnes Video", "desc": "", "kind": "video"},
    ]

    assert settings.is_video_model("agnes-video-2.5-flash") is True
    assert settings.is_video_model("agnes-3.0-flash") is False
    assert settings.is_image_model("agnes-video-2.5-flash") is False

    cfg = settings.video_config("agnes-3.0-flash")      # 选的是对话模型
    assert cfg["proxy"] is True
    assert cfg["model_id"] == "agnes-video-2.5-flash"
    assert cfg["base_url"] == settings.llm_proxy_url


def test_video_config_empty_without_server_model(settings):
    settings.set_account("token", {"id": 1, "email": "a@qq.com", "nickname": "甲"})
    settings.llm_user_key = "pk-user"

    cfg = settings.video_config()
    assert cfg["base_url"] == "" and cfg["model_id"] == ""


def test_video_config_needs_login(settings):
    settings.builtin_models = [
        {"model_id": "agnes-video-2.5-flash", "name": "V", "desc": "", "kind": "video"}
    ]
    assert settings.video_config()["model_id"] == ""


# ---------------------------------------------------------------- 界面
def test_composer_shows_video_options_only_for_video_model(qapp, tmp_path, settings):
    from paper_agent.ui.composer.composer import Composer

    settings.builtin_models = [
        {"model_id": "agnes-video-2.5-flash", "name": "Agnes Video", "desc": "", "kind": "video"}
    ]
    composer = Composer(settings)

    composer.model_button.set_current("gpt-5")            # 对话模型
    composer.sync_video_mode()
    assert composer.video_button.isHidden()

    composer.model_button.set_current("agnes-video-2.5-flash")
    composer.sync_video_mode()
    assert not composer.video_button.isHidden()
    assert composer.video_button.params["seconds"] == "5"
    assert composer.video_button.params["aspect_ratio"] == "16:9"


def test_video_options_button_holds_choices(qapp):
    from paper_agent.ui.widgets.video_options import VideoOptionsButton

    button = VideoOptionsButton(seconds="8", aspect_ratio="9:16")
    assert button.params == {"seconds": "8", "aspect_ratio": "9:16", "size": "720P"}
    assert "9:16" in button.text()


def test_placeholder_models_are_gone():
    from paper_agent.core.config import DEFAULT_MODELS

    ids = {item["id"] for item in DEFAULT_MODELS}
    assert "qwen-max" not in ids
    assert "local-llm" not in ids
    assert "gpt-5" in ids                 # 其余示例模型保留
