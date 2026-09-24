"""文生视频的进度显示：事件协议 → 界面进度条。

用户的原话是「做视频任务时要能看到进度」——出片要一两分钟，界面只有一行
「正在写作·」会让人以为卡死了。这里盯住三件事：

1. 生成器报了百分比，聊天界面就得看到进度条和数字；
2. 查不到进度（排队中）时走「不确定」动画，而不是一个停在 0% 的死条；
3. 本轮一结束（成功 / 失败 / 停止）进度条必须收起来，别留在那儿误导人。
"""

from __future__ import annotations

import json

from paper_agent.core.models import ChatSession, Message


def _progress(percent: int, text: str = "") -> str:
    return json.dumps({"percent": percent, "text": text}, ensure_ascii=False)


# ---------------------------------------------------------------- 消息控件
def test_progress_bar_shows_percentage(qapp):
    from paper_agent.ui.chat.message_widget import MessageWidget

    widget = MessageWidget(Message(role="assistant", content="", status="streaming"))
    widget.mark_progress(_progress(40, "视频生成中 40%（出片要 1–3 分钟）…"))

    bar = widget.progress_bar
    assert not bar.isHidden(), "有进度就得露出来"
    assert (bar.minimum(), bar.maximum()) == (0, 100)
    assert bar.value() == 40
    assert "40%" in widget.status_label.text(), "状态栏也要说清进度，只有一条条看不出多少"


def test_queued_uses_indeterminate_bar(qapp):
    """排队中还没有百分比：用不确定进度条，别让 0% 看着像卡死。"""
    from paper_agent.ui.chat.message_widget import MessageWidget

    widget = MessageWidget(Message(role="assistant", content="", status="streaming"))
    widget.mark_progress(_progress(-1, "视频任务已提交，正在排队…"))

    bar = widget.progress_bar
    assert not bar.isHidden()
    assert (bar.minimum(), bar.maximum()) == (0, 0), "0/0 才是 Qt 的不确定进度动画"
    assert "排队" in widget.status_label.text()


def test_progress_beats_busy_text(qapp):
    """进度比「正在写作·」有用：两个都在的时候显示进度。"""
    from paper_agent.ui.chat.message_widget import MessageWidget

    widget = MessageWidget(Message(role="assistant", content="", status="streaming"))
    widget.mark_progress(_progress(70, "视频生成中 70%…"))
    widget._update_status()

    assert "70%" in widget.status_label.text()


def test_finish_hides_progress_bar(qapp):
    """本轮结束就收起来，且状态栏回到常规文案。"""
    from paper_agent.ui.chat.message_widget import MessageWidget

    widget = MessageWidget(Message(role="assistant", content="好了", status="streaming"))
    widget.mark_progress(_progress(100, "视频已出片，正在下载…"))
    widget.finish_stream("done")

    assert widget.progress_bar.isHidden()
    assert widget.progress_bar.value() == 0
    assert "下载" not in widget.status_label.text()


def test_broken_progress_payload_does_not_crash(qapp):
    """协议解析要宽容：脏数据不能把界面搞崩。"""
    from paper_agent.ui.chat.message_widget import MessageWidget

    widget = MessageWidget(Message(role="assistant", content="", status="streaming"))
    for payload in ("", "{}", "not json", '{"percent": "abc"}', "[]"):
        widget.mark_progress(payload)

    assert not widget.progress_bar.isHidden()
    assert widget.progress_bar.value() == 0


def test_user_message_has_no_progress_bar(qapp):
    """用户消息不该有进度条（只有助手消息挂了长任务）。"""
    from paper_agent.ui.chat.message_widget import MessageWidget

    widget = MessageWidget(Message(role="user", content="做个视频"))
    assert not hasattr(widget, "progress_bar")


# ---------------------------------------------------------------- 事件协议
class _FakeVideoClient:
    """假视频客户端：按真实顺序回调状态与进度。"""

    model_id = "agnes-video-2.5-flash"

    def generate(self, prompt, **kwargs):
        kwargs["on_status"]("pending")
        kwargs["on_progress"](10)
        kwargs["on_progress"](70)
        kwargs["on_status"]("completed")
        return [{"name": "视频.mp4", "path": "C:/tmp/视频.mp4", "kind": "video"}]


def test_video_producer_emits_progress_events():
    from paper_agent.services.agent_service import _video_producer

    events: list[tuple[str, str]] = []
    producer = _video_producer(_FakeVideoClient(), "一只橘猫")
    producer(lambda kind, text: events.append((kind, text)))

    percents = [
        json.loads(text)["percent"] for kind, text in events if kind == "progress"
    ]
    texts = [json.loads(text)["text"] for kind, text in events if kind == "progress"]

    assert percents == [-1, -1, 10, 70, 100], "排队 → 生成 → 出片下载，一路都有进度"
    assert "排队" in texts[0] and "下载" in texts[-1]
    assert any("10%" in text for text in texts)


class _RecordingVideoClient:
    """记下生成器收到的 on_progress：验证工具链路把进度接上了。"""

    model_id = "agnes-video-2.5-flash"

    def __init__(self) -> None:
        self.reported: list[int] = []

    def generate(self, prompt, **kwargs):
        callback = kwargs.get("on_progress")
        for percent in (10, 60, 100):
            self.reported.append(percent)
            if callback:
                callback(percent)
        return [{"name": "视频.mp4", "path": "C:/tmp/视频.mp4", "kind": "video"}]


def test_stream_worker_forwards_tool_progress(qapp):
    """智能体自己调 generate_video 时，进度也要推成信号（工具在 worker 线程里跑）。"""
    from paper_agent.services.agent_service import _StreamWorker, _VideoProgress

    client = _RecordingVideoClient()
    bridge = _VideoProgress()

    def producer(emit) -> None:
        result = client.generate("一只橘猫", on_progress=bridge.report, seconds="5")
        assert result

    worker = _StreamWorker(producer=producer)
    worker.video_progress = bridge
    seen: list[str] = []
    worker.progress.connect(seen.append)
    worker.run()      # 直接跑，省掉线程：信号在同一线程里同步送达

    assert [json.loads(item)["percent"] for item in seen] == [10, 60, 100]
    assert client.reported == [10, 60, 100]


def test_progress_label_marks_segment(qapp):
    """长视频分段：文案要写「第 i/N 段」，否则连跑十几分钟用户不知道到哪了。"""
    from paper_agent.services.agent_service import _StreamWorker, _VideoProgress

    bridge = _VideoProgress()

    def producer(emit) -> None:
        bridge.label("第 2/3 段")
        bridge.report(55)
        bridge.label("")
        bridge.report(-1)

    worker = _StreamWorker(producer=producer)
    worker.video_progress = bridge
    seen: list[str] = []
    worker.progress.connect(seen.append)
    worker.run()

    first, second = (json.loads(item) for item in seen)
    assert first["percent"] == 55
    assert first["text"] == "第 2/3 段 · 视频生成中 55%…"
    assert second["percent"] == -1
    assert "排队" in second["text"], "还没拿到百分比时说「排队中」，别显示 -1%"
    assert "第" not in second["text"], "文案前缀清了就不该再带段号"


# ---------------------------------------------------------------- 主窗口接线
def _window(qapp, tmp_path, sessions):
    from paper_agent.core.config import AppConfig
    from paper_agent.core.session_store import SessionStore
    from paper_agent.ui.main_window import MainWindow

    window = MainWindow(AppConfig(), SessionStore(tmp_path / "sessions"))
    window.sessions = {session.id: session for session in sessions}
    return window


def test_main_window_routes_progress_to_message(qapp, tmp_path):
    """信号要落到那条消息上：并且消息 id 对不上时不能炸。"""
    message = Message(role="assistant", content="", status="streaming")
    session = ChatSession(title="做个视频", messages=[message])

    window = _window(qapp, tmp_path, [session])
    window.activate_session(session.id)
    widget = window.chat_view.widget_for(message.id)
    assert widget is not None, "消息先渲染出来，后面的进度才有地方落"

    window._on_agent_progress(message.id, _progress(55, "视频生成中 55%…"))
    assert widget.progress_bar.value() == 55
    assert "55%" in widget.status_label.text()

    # 消息已经不在界面上（换了会话 / 被删）时静默忽略
    window._on_agent_progress("不存在的消息", _progress(90, "视频生成中 90%…"))


def test_agent_service_exposes_progress_signal(qapp):
    """AgentService 要有 progress 信号，否则窗口连不上（接线回归）。"""
    from paper_agent.services.agent_service import AgentService

    service = AgentService()
    seen: list[tuple[str, str]] = []
    service.progress.connect(lambda mid, text: seen.append((mid, text)))
    service.progress.emit("m-1", _progress(30, "视频生成中 30%…"))

    assert seen and seen[0][0] == "m-1"
