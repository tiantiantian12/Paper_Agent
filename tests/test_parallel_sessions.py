"""多会话并发：A 会话在出视频时，B 会话照样能问问题。

以前整个窗口只有一个 worker 线程：一边跑长任务，另一边连提交都被「正在生成中」挡回来。
现在每个会话各一个任务、互不干扰，总并发用 ``max_parallel`` 兜住（免费档的视频 / 生图
很容易被限流，一起猛跑只会一起卡）。

这里盯住四件事：
1. 两个会话真的能**同时**跑（不是排队）；
2. 同一个会话同时只跑一个；
3. 超出并发上限时给一句能看懂的提示，而不是默默失败；
4. 停止只停「这一个」任务，不牵连别的会话。
"""

from __future__ import annotations

import threading
import time

from paper_agent.core.config import AppConfig
from paper_agent.core.models import ChatSession, Message
from paper_agent.core.session_store import SessionStore
from paper_agent.services.agent_service import AgentService, _StreamWorker
from paper_agent.ui.main_window import MainWindow


def _wait_for(qapp, predicate, timeout: float = 5.0) -> bool:
    """等一个条件成立（其间持续处理 Qt 事件，免得信号堆在队列里）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    qapp.processEvents()
    return predicate()


def _parallel_service(monkeypatch, gate: threading.Event, started: list[str]) -> AgentService:
    """一个能「卡住」的服务：producer 会停在 gate 上，方便观察并发。"""
    service = AgentService(max_parallel=2)

    def fake_build(messages, model, *args, **kwargs):
        name = messages[-1]["content"]

        def producer(emit):
            started.append(name)
            emit("token", f"{name}:1")
            gate.wait(5)
            emit("token", f"{name}:2")

        return _StreamWorker(producer=producer)

    monkeypatch.setattr(service, "_build_worker", fake_build)
    return service


# ---------------------------------------------------------------- 服务层
def test_two_sessions_run_at_the_same_time(qapp, monkeypatch):
    gate = threading.Event()
    started: list[str] = []
    service = _parallel_service(monkeypatch, gate, started)
    tokens: list[tuple[str, str]] = []
    service.token.connect(lambda mid, text: tokens.append((mid, text)))
    finished: list[str] = []
    service.finished.connect(finished.append)

    service.start("m-1", [{"role": "user", "content": "论文"}], session_id="s-1")
    service.start("m-2", [{"role": "user", "content": "视频"}], session_id="s-2")

    assert service.running_count == 2
    assert service.is_session_running("s-1") and service.is_session_running("s-2")
    assert _wait_for(qapp, lambda: len(started) == 2), f"两个都该起跑：{started}"

    gate.set()      # 放行：两个都跑到终点
    assert _wait_for(qapp, lambda: service.running_count == 0), "都该收尾"
    assert _wait_for(qapp, lambda: sorted(finished) == ["m-1", "m-2"]), finished
    # 各条流只带自己的消息 ID（不能串台）
    assert _wait_for(
        qapp, lambda: ("m-1", "论文:1") in tokens and ("m-2", "视频:1") in tokens
    ), tokens
    assert service.is_running is False


def test_capacity_and_single_flight_helpers(qapp, monkeypatch):
    gate = threading.Event()
    service = _parallel_service(monkeypatch, gate, [])

    assert service.max_parallel == 2 and service.has_capacity
    service.start("m-1", [{"role": "user", "content": "A"}], session_id="s-1")
    service.start("m-2", [{"role": "user", "content": "B"}], session_id="s-2")

    assert service.running_count == 2
    assert service.has_capacity is False, "到上限了"
    assert service.is_running is True
    assert service.streaming_message_id("s-1") == "m-1"
    assert service.streaming_message_id("s-3") == ""
    assert service.running_sessions() == {"s-1", "s-2"}
    # 同一条消息不会起两次（重复提交的保护）
    service.start("m-1", [{"role": "user", "content": "A"}], session_id="s-1")
    assert service.running_count == 2

    gate.set()
    assert _wait_for(qapp, lambda: service.running_count == 0)


def test_real_worker_runs_through_mock_mode(qapp):
    """真起一个 worker（示例引擎，不联网）：把参数接线走一遍，能跑完并收尾。

    上面那些用例把 ``_build_worker`` 换掉了，接参数的顺序就只能靠这里兜住。
    """
    service = AgentService(max_parallel=1)
    tokens: list[str] = []
    done: list[str] = []
    service.token.connect(lambda _mid, text: tokens.append(text))
    service.finished.connect(done.append)

    service.start(
        "m-1", [{"role": "user", "content": "你好"}], model="内置模型", session_id="s-1"
    )

    assert service.running_count == 1
    assert _wait_for(qapp, lambda: done == ["m-1"], timeout=15), "该跑完并回报"
    assert "".join(tokens), "示例引擎也要有正文输出"
    assert service.running_count == 0 and service.has_capacity


def test_stop_only_targets_one_run(qapp, monkeypatch, fake_run):
    """停止只停指定的那一个：别的会话继续跑。"""
    service = AgentService(max_parallel=2)
    first = fake_run(service, "s-1", "m-1")
    second = fake_run(service, "s-2", "m-2")

    service.stop("m-1")

    assert first.worker.aborted is True and first.thread.quit_calls == 1
    assert second.worker.aborted is False, "别的会话不能被牵连"
    assert service.is_session_running("s-2") is True

    service.stop()
    assert second.worker.aborted is True, "不给 id 就全停"


# ---------------------------------------------------------------- 界面层
def _window(qapp, tmp_path, sessions) -> MainWindow:
    window = MainWindow(AppConfig(), SessionStore(tmp_path / "sessions"))
    window.sessions = {session.id: session for session in sessions}
    if sessions:
        window.current = sessions[0]
    return window


def _session(title: str) -> ChatSession:
    session = ChatSession(title=title)
    session.add_message(Message(role="user", content="先聊一句"))
    session.add_message(Message(role="assistant", content="好", status="done"))
    return session


def _stub_start(window, monkeypatch) -> list[tuple[str, str]]:
    """把 agent.start 换成只记参数（不真起线程）。"""
    calls: list[tuple[str, str]] = []

    def fake_start(message_id, **kwargs):
        calls.append((message_id, kwargs.get("session_id", "")))

    monkeypatch.setattr(window.agent, "start", fake_start)
    return calls


def test_other_session_can_submit_while_one_runs(qapp, tmp_path, monkeypatch, fake_run):
    """核心场景：A 会话在跑（比如出视频），B 会话照样能提交。"""
    running = _session("出视频的会话")
    other = _session("另一个会话")
    window = _window(qapp, tmp_path, [running, other])
    window.activate_session(running.id)
    fake_run(window.agent, running.id, "m-running")
    calls = _stub_start(window, monkeypatch)

    window.activate_session(other.id)
    window._on_submit("顺便问个问题", [], other)

    assert len(calls) == 1, "另一个会话不该被挡"
    assert calls[0][1] == other.id, "要带上会话 id（进度与停止都靠它定位）"
    assert other.messages[-1].status == "streaming"
    assert window.composer.is_streaming, "当前会话在生成：发送键变停止"
    assert window.agent.is_session_running(running.id), "原来那个还在跑"


def test_same_session_cannot_submit_twice(qapp, tmp_path, monkeypatch, fake_run):
    """同一个会话同时只跑一个：连发会被挡住并给出提示。"""
    from paper_agent.core.signals import signals as app_signals

    session = _session("正在跑的会话")
    window = _window(qapp, tmp_path, [session])
    window.activate_session(session.id)
    fake_run(window.agent, session.id, "m-running")
    calls = _stub_start(window, monkeypatch)

    toasts: list[str] = []
    app_signals.toast_requested.connect(toasts.append)
    before = len(session.messages)
    window._on_submit("再问一句", [], session)

    assert calls == [], "同一个会话不该并发"
    assert len(session.messages) == before, "不该冒出半条消息"
    assert any("这个会话正在生成中" in text for text in toasts), toasts


def test_capacity_limit_gives_actionable_toast(qapp, tmp_path, monkeypatch, fake_run):
    """到并发上限时，提示要说清「几个在跑、上限多少、怎么调大」。"""
    from paper_agent.core.signals import signals as app_signals

    first = _session("会话一")
    second = _session("会话二")
    third = _session("会话三")
    window = _window(qapp, tmp_path, [first, second, third])
    window.activate_session(first.id)
    window.agent._max_parallel = 2                    # noqa: SLF001 - 固定上限便于断言
    fake_run(window.agent, first.id, "m-1")
    fake_run(window.agent, second.id, "m-2")
    calls = _stub_start(window, monkeypatch)

    toasts: list[str] = []
    app_signals.toast_requested.connect(toasts.append)
    window.activate_session(third.id)
    window._on_submit("还能发吗", [], third)

    assert calls == [], "到上限就不该再起"
    assert any("上限 2 个" in text for text in toasts), toasts


def test_finishing_one_session_keeps_other_streaming(qapp, tmp_path, fake_run):
    """A 会话结束时，不能把 B 会话的「停止生成」状态清掉。"""
    first = _session("先结束的")
    second = _session("还在跑的")
    window = _window(qapp, tmp_path, [first, second])
    window.activate_session(second.id)
    fake_run(window.agent, first.id, "m-1")
    fake_run(window.agent, second.id, "m-2")
    window.composer.set_streaming(True)

    # 真实顺序：agent 先把任务摘掉，再发 finished（见 AgentService._finish）
    window.agent._runs.pop("m-1", None)     # noqa: SLF001
    window._on_agent_finished("m-1")        # 先结束的那个（非当前会话）

    assert window.composer.is_streaming, "当前会话还在跑，状态要留着"
    assert window.agent.is_session_running(second.id)

    window.agent._runs.pop("m-2", None)     # noqa: SLF001
    window._on_agent_finished("m-2")
    assert not window.composer.is_streaming, "当前会话结束才回正"
