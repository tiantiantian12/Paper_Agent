"""工作模式是**会话级**的：论文会话写论文、工程会话写代码，切会话切模式。

回归的坑：模式原先存在全局配置里，切到任何会话都是同一个模式 —— 上午在编程会话
里跑代码，下午打开论文会话居然还是编程模式（右侧面板也还挂着代码工作区）。
"""

from __future__ import annotations

import json

from paper_agent.core.config import MODE_CODE, MODE_DOC, AppConfig
from paper_agent.core.models import ChatSession, Message


# ---------------------------------------------------------------- 数据模型
def test_new_session_defaults_to_doc_mode():
    assert ChatSession().mode == MODE_DOC


def test_session_mode_survives_serialisation():
    session = ChatSession(title="工程会话", mode=MODE_CODE)

    revived = ChatSession.from_dict(json.loads(json.dumps(session.to_dict())))

    assert revived.mode == MODE_CODE


def test_legacy_session_without_mode_falls_back_to_doc():
    """老会话文件没有 mode 字段：按文档模式，不能把论文会话变成编程会话。"""
    assert ChatSession.from_dict({"title": "老会话"}).mode == MODE_DOC
    assert ChatSession.from_dict({"title": "坏值", "mode": "??"}).mode == MODE_DOC


def test_legacy_code_session_is_inferred_from_history():
    """老会话里调过编程模式专属工具：重启后不该突然变回文档模式。"""
    session = ChatSession.from_dict(
        {
            "title": "老工程会话",
            "messages": [
                {
                    "role": "assistant",
                    "tool_runs": [{"name": "run_command", "args": '{"command": "pytest"}'}],
                }
            ],
        }
    )

    assert session.mode == MODE_CODE


def test_legacy_doc_session_is_not_misread_as_code():
    """文档模式也会调 execute_python（扫描磁盘之类），不能据此判定成编程会话。"""
    session = ChatSession.from_dict(
        {
            "title": "老论文会话",
            "messages": [
                {
                    "role": "assistant",
                    "tool_runs": [
                        {"name": "create_docx", "args": "{}"},
                        {"name": "execute_python", "args": '{"code": "print(1)"}'},
                    ],
                }
            ],
        }
    )

    assert session.mode == MODE_DOC


# ---------------------------------------------------------------- 右侧面板
def test_outline_refresh_does_not_hijack_workspace_view(qapp, tmp_path):
    """面板在工作区视图时，论文结构那套刷新不能把它冲掉。"""
    from paper_agent.services.skills.code_workspace import build_tree
    from paper_agent.ui.outline.outline_panel import OutlinePanel

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "app.py").write_text("print(1)", encoding="utf-8")

    panel = OutlinePanel()
    panel.set_workspace_mode(True)
    nodes, files, dirs = build_tree(workspace)
    panel.set_workspace(str(workspace), nodes, files, dirs)

    panel.set_outline([], "完成一次对话后，这里会显示论文的章节结构与进度。")

    assert panel.tree.isVisibleTo(panel), "目录树不该被论文空态顶掉"
    assert "完成一次对话后" not in panel.empty_label.text()
    assert panel.tree.topLevelItemCount() == 1


def test_live_refresh_updates_workspace_during_generation(
    qapp, tmp_path, monkeypatch, fake_run
):
    """编程模式生成中：实时刷新刷的是工作区（文件边写边长出来）。"""
    import paper_agent.ui.main_window as main_window

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "app.py").write_text("print(1)", encoding="utf-8")

    window = _window(qapp, tmp_path, [])
    session = ChatSession(
        title="工程", mode=MODE_CODE, messages=[Message(role="user", content="写代码")]
    )
    window.sessions[session.id] = session
    monkeypatch.setattr(main_window, "code_root", lambda *args, **kwargs: workspace)
    window.activate_session(session.id)
    assert window.outline_panel.tree.topLevelItemCount() == 1

    # 模拟「这个会话正在生成」
    fake_run(window.agent, session.id, session.messages[0].id)
    monkeypatch.setattr(window.outline_panel, "isVisible", lambda: True)

    window._tick_outline()
    assert window.outline_panel._workspace_mode is True
    assert window.outline_panel.tree.isVisibleTo(window.outline_panel)
    assert "完成一次对话后" not in window.outline_panel.empty_label.text()

    (workspace / "main.py").write_text("print(2)", encoding="utf-8")
    window._tick_outline()
    names = [
        window.outline_panel.tree.topLevelItem(index).text(0)
        for index in range(window.outline_panel.tree.topLevelItemCount())
    ]
    assert any("main.py" in name for name in names), names


# ---------------------------------------------------------------- 会话列表徽标
def test_code_session_shows_badge_in_sidebar(qapp):
    from paper_agent.ui.sidebar.session_item import SessionItemWidget

    code_item = SessionItemWidget(ChatSession(title="工程", mode=MODE_CODE))
    doc_item = SessionItemWidget(ChatSession(title="论文", mode=MODE_DOC))

    assert code_item.mode_label.isVisibleTo(code_item), "编程会话要标出来，不点开也看得见"
    assert not doc_item.mode_label.isVisibleTo(doc_item), "文档是默认，标出来反而是噪音"

    doc_item.update_session(ChatSession(title="改成了编程", mode=MODE_CODE))
    assert doc_item.mode_label.isVisibleTo(doc_item), "切模式后徽标要跟着更新"


# ---------------------------------------------------------------- 欢迎页
def test_welcome_page_matches_mode(qapp):
    from paper_agent.ui.chat.welcome_view import WelcomeView

    doc = WelcomeView(mode=MODE_DOC)
    assert doc.title_label.text() == "今天想写点什么？"
    assert _prompts(doc)

    code = WelcomeView(mode=MODE_CODE)
    assert code.title_label.text() == "想让代码做什么？"
    prompts = _prompts(code)
    assert any("FastAPI" in text for text in prompts), "编程模式要给工程类引导"
    assert not any("GB/T 7714" in text for text in prompts), "不该再出现论文选题"


def test_welcome_page_follows_mode_switch(qapp):
    from paper_agent.ui.chat.welcome_view import WelcomeView

    view = WelcomeView(mode=MODE_DOC)

    view.set_mode(MODE_CODE)

    assert view.title_label.text() == "想让代码做什么？"
    assert any("FastAPI" in text for text in _prompts(view))
    assert view.grid.count() == 6, "重建不能把旧卡片留下来"


def test_chat_view_forwards_mode_to_welcome(qapp):
    from paper_agent.ui.chat.chat_view import ChatView

    view = ChatView(mode=MODE_DOC)
    view.set_mode(MODE_CODE)

    assert view.welcome._mode == MODE_CODE


def _prompts(view) -> list[str]:
    cards: list[str] = []
    for index in range(view.grid.count()):
        widget = view.grid.itemAt(index).widget()
        if widget is not None:
            cards.append(widget.prompt())
    return cards


# ---------------------------------------------------------------- 界面
def _window(qapp, tmp_path, sessions):
    from paper_agent.core.session_store import SessionStore
    from paper_agent.ui.main_window import MainWindow

    window = MainWindow(AppConfig(), SessionStore(tmp_path / "sessions"))
    window.sessions = {session.id: session for session in sessions}
    return window


def test_switching_session_switches_mode(qapp, tmp_path):
    doc_session = ChatSession(title="论文", mode=MODE_DOC)
    code_session = ChatSession(title="工程", mode=MODE_CODE)
    window = _window(qapp, tmp_path, [doc_session, code_session])

    window.activate_session(doc_session.id)
    assert window.current_mode() == MODE_DOC
    assert window.composer.mode == MODE_DOC
    assert window.outline_panel._workspace_mode is False

    window.activate_session(code_session.id)
    assert window.current_mode() == MODE_CODE
    assert window.composer.mode == MODE_CODE, "输入区要跟着会话切"
    assert window.outline_panel._workspace_mode is True, "右侧面板要进代码工作区"

    window.activate_session(doc_session.id)
    assert window.composer.mode == MODE_DOC, "切回来还是论文会话自己的模式"
    assert window.outline_panel._workspace_mode is False


def test_new_session_starts_in_doc_mode(qapp, tmp_path):
    # 给这条会话一条消息：空会话会被 new_session 直接复用（既有行为）
    code_session = ChatSession(
        title="工程",
        mode=MODE_CODE,
        messages=[Message(role="user", content="把项目跑起来")],
    )
    window = _window(qapp, tmp_path, [code_session])

    window.activate_session(code_session.id)
    assert window.composer.mode == MODE_CODE

    window.new_session(silent=True)

    assert window.current.mode == MODE_DOC, "新建会话默认文档模式"
    assert window.composer.mode == MODE_DOC
    assert window.outline_panel._workspace_mode is False


def test_mode_switch_only_affects_current_session(qapp, tmp_path):
    first = ChatSession(title="A", mode=MODE_DOC)
    second = ChatSession(title="B", mode=MODE_DOC)
    window = _window(qapp, tmp_path, [first, second])

    window.activate_session(first.id)
    window._on_mode_changed(MODE_CODE)

    assert first.mode == MODE_CODE, "切换的模式要记在当前会话上"
    assert second.mode == MODE_DOC, "别的会话不受影响"

    window.activate_session(second.id)
    assert window.composer.mode == MODE_DOC


def test_mode_is_written_to_disk_immediately(qapp, tmp_path):
    session = ChatSession(title="A", mode=MODE_DOC)
    window = _window(qapp, tmp_path, [session])
    window.activate_session(session.id)

    window._on_mode_changed(MODE_CODE)

    saved = json.loads((tmp_path / "sessions" / f"{session.id}.json").read_text("utf-8"))
    assert saved["mode"] == MODE_CODE, "模式要立刻落盘，异常退出也不该丢"


def test_submit_uses_the_session_mode(qapp, tmp_path, monkeypatch):
    """生成请求用的是**目标会话**的模式，不是当前界面上的模式。"""
    import paper_agent.ui.main_window as main_window

    code_session = ChatSession(title="工程", mode=MODE_CODE)
    doc_session = ChatSession(title="论文", mode=MODE_DOC)
    window = _window(qapp, tmp_path, [code_session, doc_session])

    captured: dict = {}
    original = main_window.build_chat_messages

    def fake_build(*args, **kwargs):
        captured["mode"] = kwargs.get("mode")
        return original(*args, **kwargs)

    monkeypatch.setattr(main_window, "build_chat_messages", fake_build)
    monkeypatch.setattr(window.agent, "start", lambda *args, **kwargs: captured.setdefault("started", True))

    # 界面上现在是论文会话（文档模式），但请求要发给编程会话
    window.activate_session(doc_session.id)
    window._on_submit("继续修一下前端", [], code_session)

    assert captured.get("started") is True
    assert captured["mode"] == MODE_CODE


def test_messages_follow_their_session_mode(qapp, tmp_path):
    """助手称谓按会话模式走：文档会话显示「论文助手」。"""
    from paper_agent.ui.chat.chat_view import ChatView

    code_session = ChatSession(
        title="工程",
        mode=MODE_CODE,
        messages=[Message(role="assistant", content="已生成代码", status="done")],
    )
    window = _window(qapp, tmp_path, [code_session])
    window.activate_session(code_session.id)

    widget = window.chat_view.widget_for(code_session.messages[0].id)
    assert widget.name_label.text() == "编程助手"

    view = ChatView(mode=MODE_DOC)
    doc_widget = view.add_message(Message(role="assistant", content="正文", status="done"))
    assert doc_widget.name_label.text() == "论文助手"
