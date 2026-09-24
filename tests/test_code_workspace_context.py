"""编程模式：模型要**默认就认工作区里的文件**。

回归的坑：以前编程模式只写了一句「用 list_files 了解现状」，模型既不知道这里
有什么，也不知道「用户没给路径时该动哪个文件」—— 于是要么先浪费一轮工具调用，
要么跑去全盘搜索 / 猜别的目录。
"""

from __future__ import annotations

from paper_agent.core.config import MODE_CODE, MODE_DOC
from paper_agent.services.agent_service import (
    CODE_SYSTEM_PROMPT,
    build_chat_messages,
)
from paper_agent.services.skills.code_workspace import file_index


def _tree(tmp_path) -> None:
    (tmp_path / "css").mkdir()
    (tmp_path / "css" / "style.css").write_text("body{}", encoding="utf-8")
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")


# ---------------------------------------------------------------- 清单
def test_file_index_lists_relative_paths(tmp_path):
    _tree(tmp_path)

    lines, total = file_index(tmp_path)

    assert total == 2
    assert any("css/style.css" in line for line in lines), lines
    assert any("index.html" in line for line in lines)
    assert all(not line.startswith("- /") for line in lines), "应该是相对路径"


def test_file_index_caps_long_listings(tmp_path):
    for index in range(30):
        (tmp_path / f"file_{index}.txt").write_text("x" * 200, encoding="utf-8")

    lines, total = file_index(tmp_path, max_files=5, max_chars=400)

    assert total == 30
    assert len(lines) <= 6                       # 5 条 + 「还有 N 个」提示
    assert any("还有" in line for line in lines), "超上限要说明还有多少没列"


# ---------------------------------------------------------------- 注入
def test_code_mode_injects_file_listing(tmp_path):
    _tree(tmp_path)
    lines, _total = file_index(tmp_path)

    messages = build_chat_messages(
        [], "改一下 index.html", mode=MODE_CODE, code_files=lines, code_root=str(tmp_path)
    )
    joined = "\n".join(str(item.get("content", "")) for item in messages)

    assert "index.html" in joined
    assert "默认就在这里找" in joined or "一律按这里的同名文件理解" in joined
    assert str(tmp_path) in joined


def test_code_mode_without_listing_injects_nothing():
    messages = build_chat_messages([], "写段代码", mode=MODE_CODE)

    assert len(messages) == 2, "没有清单时不要凭空加消息"
    assert messages[0]["content"] == CODE_SYSTEM_PROMPT


def test_doc_mode_ignores_code_listing(tmp_path):
    _tree(tmp_path)
    lines, _total = file_index(tmp_path)

    messages = build_chat_messages(
        [], "写篇论文", mode=MODE_DOC, code_files=lines, code_root=str(tmp_path)
    )

    assert "index.html" not in "\n".join(str(item.get("content", "")) for item in messages)


def test_submit_in_code_mode_passes_the_listing(qapp, tmp_path, monkeypatch):
    """真实提交路径：编程模式要把工作区清单塞进请求。"""
    import paper_agent.ui.main_window as main_window
    from paper_agent.core.config import AppConfig
    from paper_agent.core.models import ChatSession
    from paper_agent.core.session_store import SessionStore

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "index.html").write_text("<html></html>", encoding="utf-8")

    window = main_window.MainWindow(AppConfig(), SessionStore(tmp_path / "sessions"))
    session = ChatSession(title="工程", mode=MODE_CODE)
    window.sessions = {session.id: session}

    captured: dict = {}
    original = main_window.build_chat_messages

    def fake(*args, **kwargs):
        captured.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(main_window, "build_chat_messages", fake)
    monkeypatch.setattr(main_window, "code_root", lambda *args, **kwargs: workspace)
    monkeypatch.setattr(window.agent, "start", lambda *args, **kwargs: None)

    window.activate_session(session.id)
    window._on_submit("改一下首页", [], session)

    assert captured.get("mode") == MODE_CODE
    assert captured.get("code_root") == str(workspace)
    assert any("index.html" in line for line in captured.get("code_files") or [])


# ---------------------------------------------------------------- 提示词规则
def test_prompt_states_workspace_default():
    assert "路径默认规则" in CODE_SYSTEM_PROMPT
    assert "一律按**工程目录内**的同名文件理解" in CODE_SYSTEM_PROMPT
    assert "不要用 execute_python 全盘扫描" in CODE_SYSTEM_PROMPT
    assert "工程目录里没有这个文件" in CODE_SYSTEM_PROMPT
    assert "不要擅自到工程目录之外" in CODE_SYSTEM_PROMPT
