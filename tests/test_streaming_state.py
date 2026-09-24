"""输入区的「正在生成」状态必须跟着真实状态回正。

回归的坑：模型输出完了，发送键还是「停止」（红色）—— 收尾链路漏了一环
（消息被提前删掉、生成属于别的会话、停止后线程已结束却没回报），
界面就一直停在生成中。
"""

from __future__ import annotations

from paper_agent.core.models import ChatSession, Message


def _window(qapp, tmp_path, sessions):
    from paper_agent.core.config import AppConfig
    from paper_agent.core.session_store import SessionStore
    from paper_agent.ui.main_window import MainWindow

    window = MainWindow(AppConfig(), SessionStore(tmp_path / "sessions"))
    window.sessions = {session.id: session for session in sessions}
    if sessions:
        window.current = sessions[0]
    return window


def _active_session() -> ChatSession:
    session = ChatSession(title="正在生成")
    session.add_message(Message(role="user", content="写一段"))
    session.add_message(Message(role="assistant", content="", status="streaming"))
    return session


def test_finish_resets_composer_even_when_message_is_gone(qapp, tmp_path):
    """消息被提前删掉（自动重试会先删再重发）时也要回正。"""
    window = _window(qapp, tmp_path, [_active_session()])
    window.composer.set_streaming(True)

    window._after_generation("已经不存在的消息 id", "done")

    assert not window.composer.is_streaming


def test_finish_resets_composer_for_background_session(qapp, tmp_path):
    """生成属于别的会话时，界面同样不能停在生成中。"""
    session = _active_session()
    window = _window(qapp, tmp_path, [session])
    other = ChatSession(title="另一个")
    window.sessions[other.id] = other
    window.composer.set_streaming(True)

    window._after_generation(session.messages[-1].id, "done")

    assert not window.composer.is_streaming


def test_stream_guard_clears_stuck_state(qapp, tmp_path):
    """收尾信号彻底丢了：兜底计时器要把状态掰回来。"""
    window = _window(qapp, tmp_path, [_active_session()])
    window.composer.set_streaming(True)

    window._tick_stream_guard()

    assert not window.composer.is_streaming


def test_stream_guard_keeps_state_while_generating(qapp, tmp_path, fake_run):
    """真的还在生成时，兜底不能把状态清掉。"""
    session = _active_session()
    window = _window(qapp, tmp_path, [session])
    window.activate_session(session.id)
    window.composer.set_streaming(True)
    fake_run(window.agent, session.id, session.messages[-1].id)

    window._tick_stream_guard()

    assert window.composer.is_streaming


def test_stopping_normalizes_ui_when_thread_already_ended(qapp, tmp_path):
    """点了停止：线程其实已经结束但没回报 —— 界面必须回正。"""
    session = _active_session()
    window = _window(qapp, tmp_path, [session])
    window.activate_session(session.id)
    window.composer.set_streaming(True)
    # 界面上还显示「正在生成」（消息 status=streaming），但 agent 那边已经没任务了

    window._stop_timer.start()
    window._tick_stopping()

    assert not window.composer.is_streaming
    assert not window._stop_timer.isActive()


def test_submit_then_finish_round_trip(qapp, tmp_path, monkeypatch):
    """完整走一遍：提交 → 变「停止」→ 收到完成信号 → 变回「发送」。"""
    session = ChatSession(title="对话")
    window = _window(qapp, tmp_path, [session])
    window.activate_session(session.id)

    started: dict = {}
    monkeypatch.setattr(
        window.agent,
        "start",
        lambda message_id, **kwargs: started.setdefault("id", message_id),
    )

    window._on_submit("写一段", [], session)
    assert window.composer.is_streaming, "提交后发送键要变成停止"

    window._on_agent_finished(started["id"])
    assert not window.composer.is_streaming
    assert window._streaming_id(session.id) == ""


def test_composer_exposes_streaming_state(qapp):
    from paper_agent.core.config import AppConfig
    from paper_agent.ui.composer.composer import Composer

    composer = Composer(AppConfig())
    composer.set_streaming(True)
    assert composer.is_streaming
    composer.set_streaming(False)
    assert not composer.is_streaming
