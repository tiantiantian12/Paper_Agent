"""代码工作区是**会话级**的：不同会话不共用同一份工程文件。

回归的坑：工作区原先只有一份（全局 `workspace/`），A 会话的前端项目和
B 会话的脚本混在一起，右侧目录树与模型看到的文件清单也是同一份。
"""

from __future__ import annotations

import json

import pytest

from paper_agent.core.models import ChatSession, Message, ToolRun
from paper_agent.services.skills import code_workspace
from paper_agent.services.skills.code_workspace import code_root


@pytest.fixture
def temp_workspace_root(tmp_path, monkeypatch):
    """把「默认工作区」指到临时目录。"""
    base = tmp_path / "workspace"
    base.mkdir()
    monkeypatch.setattr(code_workspace, "default_root", lambda: base)
    monkeypatch.setattr(code_workspace, "configured_root", lambda: "")
    monkeypatch.delenv("PAPERAGENT_CODE_ROOT", raising=False)
    return base


# ---------------------------------------------------------------- 路径规则
def test_each_session_gets_its_own_directory(temp_workspace_root):
    """工程目录默认是工作区里的 generated：模型生成的一切都写在这一层。"""
    from paper_agent.services import session_workspace

    first = code_root("session-a")
    second = code_root("session-b")

    assert first == temp_workspace_root / "session-a" / session_workspace.GENERATED_DIR_NAME
    assert second == temp_workspace_root / "session-b" / session_workspace.GENERATED_DIR_NAME
    assert first != second
    assert first.is_dir() and second.is_dir()


def test_legacy_session_keeps_its_root_directory(temp_workspace_root):
    """改版前把代码写在工作区根目录的老会话：继续用根目录，别让文件从面板消失。"""
    old = temp_workspace_root / "session-old"
    old.mkdir(parents=True)
    (old / "main.py").write_text("print(1)", encoding="utf-8")

    assert code_root("session-old") == old


def test_session_can_point_at_its_own_folder(temp_workspace_root, tmp_path):
    chosen = tmp_path / "my-frontend"

    root = code_root("session-a", str(chosen))

    assert root == chosen
    assert root.is_dir()


def test_env_override_still_wins(temp_workspace_root, tmp_path, monkeypatch):
    override = tmp_path / "override"
    monkeypatch.setenv("PAPERAGENT_CODE_ROOT", str(override))

    assert code_root("session-a", str(tmp_path / "x")) == override


def test_no_session_falls_back_to_shared_directory(temp_workspace_root):
    assert code_root() == temp_workspace_root


# ---------------------------------------------------------------- 数据模型
def test_session_code_root_survives_serialisation():
    session = ChatSession(title="工程", mode="code", code_root="D:/proj/frontend")

    revived = ChatSession.from_dict(json.loads(json.dumps(session.to_dict())))

    assert revived.code_root == "D:/proj/frontend"
    assert ChatSession.from_dict({"title": "老会话"}).code_root == ""


# ---------------------------------------------------------------- 界面接线
def _window(qapp, tmp_path, sessions, monkeypatch):
    from paper_agent.core.config import AppConfig
    from paper_agent.core.session_store import SessionStore
    from paper_agent.ui import main_window as mw

    window = mw.MainWindow(AppConfig(), SessionStore(tmp_path / "sessions"))
    window.sessions = {session.id: session for session in sessions}
    return window


def test_switching_session_switches_workspace(qapp, tmp_path, temp_workspace_root, monkeypatch):
    from paper_agent.core.config import MODE_CODE

    first = ChatSession(title="前端", mode=MODE_CODE)
    second = ChatSession(title="脚本", mode=MODE_CODE)
    window = _window(qapp, tmp_path, [first, second], monkeypatch)

    window.activate_session(first.id)
    assert window._session_workspace() == code_root(first.id)

    window.activate_session(second.id)
    workspace = window._session_workspace()
    assert workspace == code_root(second.id)
    assert workspace != code_root(first.id)


def test_submit_sends_each_session_its_own_file_list(
    qapp, tmp_path, temp_workspace_root, monkeypatch
):
    """A 会话写的文件不会出现在 B 会话的文件清单里。"""
    import paper_agent.ui.main_window as mw
    from paper_agent.core.config import MODE_CODE

    first = ChatSession(title="前端", mode=MODE_CODE)
    second = ChatSession(title="脚本", mode=MODE_CODE)
    window = _window(qapp, tmp_path, [first, second], monkeypatch)

    (temp_workspace_root / first.id / "generated").mkdir(parents=True, exist_ok=True)
    (temp_workspace_root / first.id / "generated" / "index.html").write_text("<html>", encoding="utf-8")
    (temp_workspace_root / second.id / "generated").mkdir(parents=True, exist_ok=True)
    (temp_workspace_root / second.id / "generated" / "crawler.py").write_text("print(1)", encoding="utf-8")

    captured: dict = {}
    original = mw.build_chat_messages

    def fake(*args, **kwargs):
        captured.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(mw, "build_chat_messages", fake)
    monkeypatch.setattr(window.agent, "start", lambda *args, **kwargs: None)

    window.activate_session(first.id)
    window._on_submit("改一下首页", [], first)
    assert any("index.html" in line for line in captured["code_files"])

    window.activate_session(second.id)
    window._on_submit("爬点数据", [], second)
    listing = captured["code_files"]
    assert any("crawler.py" in line for line in listing)
    assert not any("index.html" in line for line in listing), "别的会话的文件不该出现"


def test_picking_a_folder_is_remembered_per_session(
    qapp, tmp_path, temp_workspace_root, monkeypatch
):
    from paper_agent.core.config import MODE_CODE
    from paper_agent.ui import main_window as mw

    first = ChatSession(title="前端", mode=MODE_CODE)
    second = ChatSession(title="脚本", mode=MODE_CODE)
    window = _window(qapp, tmp_path, [first, second], monkeypatch)

    chosen = tmp_path / "my-app"
    chosen.mkdir()
    monkeypatch.setattr(
        mw.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(chosen))
    )

    window.activate_session(first.id)
    window._pick_workspace()

    assert first.code_root == str(chosen)
    assert second.code_root == "", "别的会话不该跟着变"
    assert window.config.code_root == "", "不再写进全局配置"


# ---------------------------------------------------------------- 目录布局可见性
def test_workspace_panel_shows_upload_and_generated(
    qapp, tmp_path, temp_workspace_root, monkeypatch
):
    """切到编程模式，面板里要能看到 `upload/` 与 `generated/` 两个固定目录。

    不建出来的话，它们只在真有文件时才出现 —— 用户会以为「生成的产物没进
    generated」（真实的困惑：截图里只看得到模型写的 quicksort.py）。
    """
    from paper_agent.core.config import MODE_CODE
    from paper_agent.services import session_workspace

    session = ChatSession(title="脚本", mode=MODE_CODE)
    window = _window(qapp, tmp_path, [session], monkeypatch)

    window.activate_session(session.id)

    assert session_workspace.upload_dir(session.id).is_dir()
    assert session_workspace.generated_dir(session.id).is_dir()


def test_custom_workspace_is_not_polluted_with_our_dirs(
    qapp, tmp_path, temp_workspace_root, monkeypatch
):
    """用户自己选的工程目录里不许塞 upload / generated —— 那是他的项目。"""
    from paper_agent.core.config import MODE_CODE

    chosen = tmp_path / "my-project"
    chosen.mkdir()
    session = ChatSession(title="我的项目", mode=MODE_CODE, code_root=str(chosen))
    window = _window(qapp, tmp_path, [session], monkeypatch)

    window.activate_session(session.id)

    assert list(chosen.iterdir()) == []


# ---------------------------------------------------------------- 老数据
def _worked_on_code(title: str, tool: str = "write_file") -> ChatSession:
    """造一个「真在工程目录里写过东西」的编程会话（只有这类才可能属于旧布局）。"""
    session = ChatSession(title=title, mode="code")
    session.messages.append(
        Message(role="assistant", tool_runs=[ToolRun(name=tool, args="{}", result="")])
    )
    return session


def _only_doc_tools(title: str) -> ChatSession:
    """只是**切到**编程模式、实际干的是文档 / 视频那类活的会话。

    用户实际遇到的就是这种：用 generate_video / create_docx / list_files，
    从没写过工程文件，却被误钉到共享目录 —— 于是上传的附件落到了 `workspace/` 根目录。
    """
    session = ChatSession(title=title, mode="code")
    session.messages.append(
        Message(
            role="assistant",
            tool_runs=[
                ToolRun(name="generate_video"),
                ToolRun(name="create_docx"),
                ToolRun(name="list_files"),
            ],
        )
    )
    return session


def test_legacy_code_session_is_pinned_to_old_workspace(
    qapp, tmp_path, temp_workspace_root, monkeypatch
):
    """改版前的会话：文件还在共享目录里，要给它钉住，别让它突然空掉。"""
    import paper_agent.ui.main_window as mw
    from paper_agent.core.config import MODE_CODE

    legacy = temp_workspace_root
    (legacy / "index.html").write_text("<html>", encoding="utf-8")

    old_code = _worked_on_code("老工程会话")
    doc_session = ChatSession(title="论文", mode="doc")
    window = _window(qapp, tmp_path, [old_code, doc_session], monkeypatch)

    window._pin_legacy_workspaces([old_code, doc_session])

    assert old_code.code_root == str(legacy)
    assert doc_session.code_root == "", "文档会话不该被钉"


def test_legacy_pinning_skipped_when_shared_workspace_is_empty(
    qapp, tmp_path, temp_workspace_root, monkeypatch
):
    import paper_agent.ui.main_window as mw
    from paper_agent.core.config import MODE_CODE

    session = _worked_on_code("工程")
    window = _window(qapp, tmp_path, [session], monkeypatch)

    window._pin_legacy_workspaces([session])

    assert session.code_root == "", "共享目录空着就没什么可钉的"


def test_legacy_pinning_covers_every_old_code_session(
    qapp, tmp_path, temp_workspace_root, monkeypatch
):
    """老的编程会话都指回共享目录：老文件分不清是谁写的，谁都不该丢东西。"""
    import paper_agent.ui.main_window as mw
    from paper_agent.core.config import MODE_CODE

    legacy = temp_workspace_root
    (legacy / "index.html").write_text("<html>", encoding="utf-8")

    older = _worked_on_code("旧的")
    newer = _worked_on_code("新的", tool="run_command")
    window = _window(qapp, tmp_path, [older, newer], monkeypatch)

    window._pin_legacy_workspaces([older, newer])

    assert older.code_root == str(legacy)
    assert newer.code_root == str(legacy)


def test_new_code_session_gets_its_own_workspace(
    qapp, tmp_path, temp_workspace_root, monkeypatch
):
    """钉住的只是老会话：新会话照样各自独立。"""
    import paper_agent.ui.main_window as mw
    from paper_agent.core.config import MODE_CODE

    legacy = temp_workspace_root
    (legacy / "index.html").write_text("<html>", encoding="utf-8")

    old = _worked_on_code("老的")
    window = _window(qapp, tmp_path, [old], monkeypatch)
    window._pin_legacy_workspaces([old])

    fresh = ChatSession(title="新的", mode=MODE_CODE)
    window.sessions[fresh.id] = fresh

    assert old.code_root == str(legacy)
    assert window._session_workspace(fresh) == code_root(fresh.id)
    assert window._session_workspace(fresh) != window._session_workspace(old)


def test_session_that_never_wrote_code_is_not_pinned(
    qapp, tmp_path, temp_workspace_root, monkeypatch
):
    """只是切到编程模式、没写过工程文件的会话：不能被钉到共享目录。

    否则用户上传的附件会跟着落到 `workspace/` 根目录（别人工程的旁边），
    而不是本会话自己的目录 —— 这是用户实际的反馈。
    """
    import paper_agent.ui.main_window as mw

    legacy = temp_workspace_root
    (legacy / "index.html").write_text("<html>", encoding="utf-8")   # 别人的老文件

    session = _only_doc_tools("视频创作（切到了编程模式）")
    window = _window(qapp, tmp_path, [session], monkeypatch)

    window._pin_legacy_workspaces([session])
    window._ensure_session_workspace(session, "code")

    assert session.code_root == str(code_root(session.id)), "应该用它自己的会话目录"
    assert window._session_workspace(session) == code_root(session.id)


def test_mis_pinned_session_is_released(qapp, tmp_path, temp_workspace_root, monkeypatch):
    """已经被误钉到共享目录的会话：重启时要解绑回自己的目录。

    兼容性不能反过来伤害新会话：解绑只针对「没写过工程文件、自己的目录还空着」的。
    """
    import paper_agent.ui.main_window as mw

    legacy = temp_workspace_root
    (legacy / "index.html").write_text("<html>", encoding="utf-8")

    mis_pinned = _only_doc_tools("被误钉的")
    mis_pinned.code_root = str(legacy)
    window = _window(qapp, tmp_path, [mis_pinned], monkeypatch)

    window._pin_legacy_workspaces([mis_pinned])

    assert mis_pinned.code_root == "", "解绑，回到自己的目录"


def test_genuine_legacy_session_stays_pinned(qapp, tmp_path, temp_workspace_root, monkeypatch):
    """真写过工程文件的老会话：不能被解绑，不然它的文件会「突然消失」。"""
    import paper_agent.ui.main_window as mw

    legacy = temp_workspace_root
    (legacy / "index.html").write_text("<html>", encoding="utf-8")

    legacy_session = _worked_on_code("老工程")
    legacy_session.code_root = str(legacy)
    window = _window(qapp, tmp_path, [legacy_session], monkeypatch)

    window._pin_legacy_workspaces([legacy_session])

    assert legacy_session.code_root == str(legacy)


# ---------------------------------------------------------------- 删除会话
def test_deleting_session_removes_its_own_workspace(
    qapp, tmp_path, temp_workspace_root, monkeypatch
):
    """会话没了，它的专属工作区目录也该一起走，否则每用一次就沉淀一个空目录。"""
    from PySide6.QtWidgets import QMessageBox

    from paper_agent.core.config import MODE_CODE

    session = ChatSession(title="工程", mode=MODE_CODE)
    window = _window(qapp, tmp_path, [session], monkeypatch)
    window.current = session

    own = window._session_workspace(session)
    assert own.is_dir()
    (own / "app.py").write_text("print(1)", encoding="utf-8")

    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    window.delete_session(session.id)

    assert not own.exists(), "会话的专属工作区要跟着会话一起删掉"


def test_deleting_session_keeps_user_chosen_folder(
    qapp, tmp_path, temp_workspace_root, monkeypatch
):
    """用户自己选的目录是真实工程，删会话不能碰它。"""
    from PySide6.QtWidgets import QMessageBox

    from paper_agent.core.config import MODE_CODE

    chosen = tmp_path / "my-app"
    chosen.mkdir()
    (chosen / "app.py").write_text("print(1)", encoding="utf-8")

    session = ChatSession(title="工程", mode=MODE_CODE, code_root=str(chosen))
    window = _window(qapp, tmp_path, [session], monkeypatch)
    window.current = session

    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    window.delete_session(session.id)

    assert chosen.is_dir() and (chosen / "app.py").exists(), "用户选的目录不能删"


def test_deleting_session_keeps_workspace_others_still_point_at(
    qapp, tmp_path, temp_workspace_root, monkeypatch
):
    from PySide6.QtWidgets import QMessageBox

    from paper_agent.core.config import MODE_CODE

    shared = temp_workspace_root / "shared-app"
    shared.mkdir(parents=True)
    first = ChatSession(title="A", mode=MODE_CODE, code_root=str(shared))
    second = ChatSession(title="B", mode=MODE_CODE, code_root=str(shared))
    # B 自己那个目录名恰好是 A 的 id（模拟被别的会话指着的目录）
    second.code_root = str(temp_workspace_root / first.id)

    window = _window(qapp, tmp_path, [first, second], monkeypatch)
    window.current = first
    occupied = temp_workspace_root / first.id
    occupied.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    window.delete_session(first.id)

    assert occupied.is_dir(), "还有别的会话指着的目录不能删"
    assert shared.is_dir(), "共享目录更不能删"
