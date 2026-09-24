"""终端任务卡片：聊天里要能看见「终端正在跑什么」。

编程模式下模型跑命令 / 执行代码时，界面上原本只有折叠的推理区里一行
「调用工具：run_command」——用户既不知道在跑什么，也不知道还在不在跑。
"""

from __future__ import annotations

import json

from paper_agent.core.models import Message
from paper_agent.ui.chat import task_card as task_card_module
from paper_agent.ui.chat.task_card import (
    TerminalTaskCard,
    command_of,
    is_terminal_tool,
    task_title,
)


def _assistant_widget():
    from paper_agent.ui.chat.message_widget import MessageWidget

    return MessageWidget(Message(role="assistant", content="", status="streaming"))


# ---------------------------------------------------------------- 识别与取值
def test_terminal_tools_are_recognised():
    assert is_terminal_tool("run_command")
    assert is_terminal_tool("execute_python")
    assert is_terminal_tool("start_service")
    assert not is_terminal_tool("create_docx")


def test_command_extraction_covers_command_and_code():
    assert command_of("run_command", {"command": "pytest -q"}) == "pytest -q"
    assert command_of("execute_python", {"code": "print(1)"}) == "print(1)"
    assert command_of("run_command", {"timeout": 30}) == ""
    assert task_title("start_service") == "后台服务"
    assert task_title("unknown_tool") == "unknown_tool"


# ---------------------------------------------------------------- 卡片状态
def test_card_running_state_shows_command(qapp):
    card = TerminalTaskCard("python -m http.server 8000", "run_command")

    assert card.is_running()
    assert card.state == "running"
    assert card.meta_text().startswith("运行中")
    assert card.command_label.full_text() == "python -m http.server 8000"
    assert card.command == "python -m http.server 8000"


def test_finished_card_reports_success_and_elapsed(qapp):
    card = TerminalTaskCard("pytest", "run_command")
    card.finish(True, output="3 passed in 0.4s")

    assert card.state == "done"
    assert "已完成" in card.meta_text()
    assert card.output_text() == "3 passed in 0.4s"
    assert not card.expanded, "成功的输出先收起来，需要时再点开"


def test_failed_card_expands_output(qapp):
    card = TerminalTaskCard("python app.py", "run_command")
    card.finish(False, output="Traceback: ModuleNotFoundError: flask")

    assert card.state == "failed"
    assert "失败" in card.meta_text()
    assert card.expanded, "失败要能直接看到报错"


def test_output_can_be_toggled(qapp):
    card = TerminalTaskCard("pytest", "run_command")
    card.finish(True, output="3 passed")

    card.toggle()
    assert card.expanded
    card.toggle()
    assert not card.expanded


def test_background_service_keeps_running_label_and_url(qapp):
    card = TerminalTaskCard("npm run dev", "start_service")
    card.finish(True, output="Local: http://localhost:5173", url="http://localhost:5173")

    assert card.state == "done"
    assert card.meta_text().startswith("后台运行中"), "后台服务不该显示「已完成」"
    assert card.url == "http://localhost:5173"
    assert card.url_label.full_text() == "http://localhost:5173"


def test_open_button_opens_browser(qapp, monkeypatch):
    opened: list[str] = []

    class _FakeDesktop:
        @staticmethod
        def openUrl(url) -> None:      # noqa: N802 - 对齐 Qt 命名
            opened.append(url.toString())

    monkeypatch.setattr(task_card_module, "QDesktopServices", _FakeDesktop)
    card = TerminalTaskCard("npm run dev", "start_service")
    card.finish(True, url="http://localhost:5173")

    card.open_button.click()

    assert opened == ["http://localhost:5173"]


def test_aborted_card_stops_spinning(qapp):
    card = TerminalTaskCard("long_task.py", "run_command")
    card.stop()

    assert card.state == "aborted"
    assert not card.is_running()
    assert "已中断" in card.meta_text()


# ---------------------------------------------------------------- 消息里落地
def test_message_widget_inserts_and_finishes_task_card(qapp):
    widget = _assistant_widget()

    card = widget.begin_terminal_task("run_command", "pytest -q", "run_command|{}")
    assert widget.task_cards() == [card]
    assert card.is_running()

    widget.finish_terminal_task("run_command|{}", success=True, output="3 passed")
    assert card.state == "done"
    assert card.output_text() == "3 passed"


def test_message_widget_stops_running_cards_on_stream_end(qapp):
    widget = _assistant_widget()
    card = widget.begin_terminal_task("run_command", "sleep 100", "k")

    widget.finish_stream("aborted", "已停止生成")

    assert not card.is_running(), "本轮结束了，卡片不能一直转圈"


def test_message_widget_clears_cards_on_restart_of_stream(qapp):
    widget = _assistant_widget()
    widget.begin_terminal_task("run_command", "pytest", "k")

    widget.begin_stream()

    assert widget.task_cards() == []


# ---------------------------------------------------------------- 事件接线
def test_tool_event_creates_and_closes_card(qapp):
    from paper_agent.ui.main_window import MainWindow

    widget = _assistant_widget()
    args = json.dumps({"command": "python -m http.server 8000"}, ensure_ascii=False)

    MainWindow._update_terminal_task(
        widget, "run_command", args, "running", {"name": "run_command", "args": args}
    )
    cards = widget.task_cards()
    assert len(cards) == 1
    assert cards[0].command == "python -m http.server 8000"

    MainWindow._update_terminal_task(
        widget,
        "run_command",
        args,
        "done",
        {"name": "run_command", "args": args, "result": "Serving HTTP on 0.0.0.0 port 8000"},
    )
    assert cards[0].state == "done"
    assert "Serving HTTP" in cards[0].output_text()


def test_non_terminal_tool_makes_no_card(qapp):
    from paper_agent.ui.main_window import MainWindow

    widget = _assistant_widget()

    MainWindow._update_terminal_task(widget, "create_docx", "{}", "running", {})

    assert widget.task_cards() == []


def test_two_runs_of_same_tool_report_to_their_own_card(qapp):
    """同一个工具连续调两次：结果不能写串到上一张卡片上。"""
    from paper_agent.ui.main_window import MainWindow

    widget = _assistant_widget()
    first = json.dumps({"command": "pytest tests/a.py"})
    second = json.dumps({"command": "pytest tests/b.py"})

    MainWindow._update_terminal_task(widget, "run_command", first, "running", {})
    MainWindow._update_terminal_task(widget, "run_command", second, "running", {})
    MainWindow._update_terminal_task(
        widget, "run_command", first, "done", {"result": "a passed"}
    )
    MainWindow._update_terminal_task(
        widget, "run_command", second, "failed", {"result": "b failed"}
    )

    cards = widget.task_cards()
    assert [card.command for card in cards] == ["pytest tests/a.py", "pytest tests/b.py"]
    assert [card.state for card in cards] == ["done", "failed"]
    assert [card.output_text() for card in cards] == ["a passed", "b failed"]
