"""助手称谓与「生成中」文案要跟着工作模式走。

文档模式：论文助手 · 正在写作…
编程模式：编程助手 · 正在创作…
"""

from __future__ import annotations

from paper_agent.core.config import MODE_CODE, MODE_DOC, mode_text
from paper_agent.core.models import Message


def test_mode_text_falls_back_for_unknown_mode():
    assert mode_text(MODE_DOC, "assistant") == "论文助手"
    assert mode_text(MODE_CODE, "assistant") == "编程助手"
    assert mode_text("whatever", "assistant") == "论文助手"


def test_message_widget_follows_mode(qapp):
    from paper_agent.ui.chat.message_widget import MessageWidget

    widget = MessageWidget(Message(role="assistant", content="", status="streaming"))
    assert widget.name_label.text() == "论文助手"
    assert widget.status_label.text().startswith("正在写作")

    widget.set_mode(MODE_CODE)
    assert widget.name_label.text() == "编程助手"
    assert widget.status_label.text().startswith("正在创作")

    widget.set_mode(MODE_DOC)
    assert widget.name_label.text() == "论文助手"


def test_chat_view_applies_mode_to_existing_messages(qapp):
    from paper_agent.ui.chat.chat_view import ChatView

    view = ChatView()
    widget = view.add_message(Message(role="assistant", content="已生成", status="done"))
    assert widget.name_label.text() == "论文助手"

    view.set_mode(MODE_CODE)
    assert widget.name_label.text() == "编程助手"

    # 切换模式之后新增的消息也要用新文案
    fresh = view.add_message(Message(role="assistant", content="你好", status="done"))
    assert fresh.name_label.text() == "编程助手"


def test_chat_view_starts_in_given_mode(qapp):
    from paper_agent.ui.chat.chat_view import ChatView

    view = ChatView(mode=MODE_CODE)
    widget = view.add_message(Message(role="assistant", content="", status="streaming"))
    assert widget.name_label.text() == "编程助手"
    assert widget.status_label.text().startswith("正在创作")
