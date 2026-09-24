"""窄栏布局回归：打开论文结构面板 / 窗口变窄时，内容要被压缩而不是溢出被裁。

历史问题：消息列内部控件（附件条、产物卡片）的最小宽度会撑宽滚动容器，
而横向滚动条是关掉的，于是用户气泡和模型输出右侧被直接裁掉（看起来像被
结构面板盖住），同时中间栏也因为输入区的最小宽度压不下去。
"""

from __future__ import annotations

import math

import pytest

from paper_agent.core.models import Attachment, Message

NARROW = 420


def _long_name(index: int) -> str:
    return f"实验数据-第{index}组-对比结果-最终确认版本-{index}.csv"


def _user_message() -> Message:
    message = Message(role="user", content="帮我按大纲写第 3 章，要求表格完整、图用我上传的。" * 4)
    message.attachments = [
        Attachment(name=_long_name(index), path=f"C:/tmp/data{index}.csv", kind="file", size=10240)
        for index in range(1, 4)
    ]
    return message


def test_message_column_never_wider_than_pane(qapp):
    """窄栏下消息列必须跟着可视宽度走，否则右侧内容被静默裁掉。"""
    from paper_agent.ui.chat.chat_view import ChatView

    view = ChatView()
    view.resize(NARROW, 640)
    view.show()
    widget = view.add_message(_user_message())
    qapp.processEvents()

    viewport = view.scroll_area.viewport().width()
    assert view.viewport.width() <= viewport + 2, "滚动容器不该被内容撑到超出可视宽度"
    assert view.column.width() <= viewport + 2
    assert widget.bubble.width() <= view.column.width()


def test_attachments_do_not_pin_bubble_width(qapp):
    """三个长文件名附件不该把气泡的最小宽度顶到几百像素。"""
    from paper_agent.ui.chat.chat_view import ChatView

    view = ChatView()
    view.resize(NARROW, 640)
    view.show()
    widget = view.add_message(_user_message())
    qapp.processEvents()

    assert widget.bubble.minimumSizeHint().width() <= NARROW - 48
    assert widget.attachment_host.minimumSizeHint().width() <= NARROW - 48


def test_user_bubble_follows_content_width(qapp):
    """气泡宽度按内容走：短消息窄、长消息宽，但仍可被压缩。"""
    from paper_agent.ui.chat.message_widget import MessageWidget

    short = MessageWidget(Message(role="user", content="你好"))
    long = MessageWidget(Message(role="user", content="需求：" * 60))
    for widget in (short, long):
        widget.show()
    qapp.processEvents()

    assert short.sizeHint().width() < long.sizeHint().width()
    long.resize(NARROW - 48, long.sizeHint().height())
    qapp.processEvents()
    assert long.bubble.width() <= NARROW - 48


def test_image_card_preview_shrinks_with_card(qapp, sample_png):
    """图片卡的预览图要跟着卡片收窄，而不是固定 420 把消息列顶宽。"""
    from paper_agent.ui.chat.chat_view import ChatView

    view = ChatView()
    view.resize(NARROW, 640)
    view.show()
    widget = view.add_message(Message(role="assistant", content="图片已生成", status="done"))
    widget.add_artifact("消融实验对比图-最终版.png", str(sample_png), "image")
    qapp.processEvents()

    card = widget.artifact_cards()[0]
    assert card.width() <= view.column.width()
    assert card.preview.width() <= card.width()
    assert card.minimumSizeHint().width() <= card.width(), "卡片最小宽度不该超过实际宽度"


def test_elide_label_elides_and_keeps_tooltip(qapp):
    from PySide6.QtWidgets import QSizePolicy

    from paper_agent.ui.widgets.elide_label import ElideLabel

    full = "毕业论文-第3章-消融实验与对比分析-初稿-终版.docx"
    label = ElideLabel(full)
    label.resize(70, 20)
    label.show()
    qapp.processEvents()

    assert label.text() != full and "…" in label.text()
    assert label.toolTip() == full
    # 压缩靠「最小宽度放开」，而不是 Ignored 策略
    # （用 Ignored 时，只要行里有 addStretch，控件会被压成 0 宽，文字直接消失）
    assert label.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Preferred
    assert label.minimumSizeHint().width() == 0
    assert label.full_text() == full


@pytest.mark.parametrize("name", ["毕业论文-第3章-消融实验与对比分析-终版.pptx", "配图.png"])
@pytest.mark.parametrize("width", [640, NARROW])
def test_card_keeps_file_name_visible(qapp, sample_png, name, width):
    """产物卡片的名字行带 addStretch：文件名不能被压缩策略挤成 0 宽。"""
    from paper_agent.ui.chat.chat_view import ChatView

    path = str(sample_png) if name.endswith(".png") else "C:/tmp/论文.pptx"
    view = ChatView()
    view.resize(width, 640)
    view.show()
    widget = view.add_message(Message(role="assistant", content="已生成", status="done"))
    widget.add_artifact(name, path)
    qapp.processEvents()

    card = widget.artifact_cards()[0]
    assert card.name_label.full_text() == name
    assert card.name_label.text(), "文件名不该是空字符串"
    assert card.name_label.width() >= 60, "文件名要真的占住位置"
    assert card.width() <= view.column.width()



def test_rich_text_height_keeps_room_for_scrollbar(qapp, sample_png):
    """宽内容（超宽图片）出现横向滚动条时，固定高度要给它留位置，别裁掉最后一行。

    注意：长代码行不再触发横向滚动条 —— 代码块现在是「卡片表格」，
    ``<pre>`` 在单元格里会换行（见 test_code_block_style）。真正撑宽文档的是
    图片这类不可折行的内容。
    """
    from PySide6.QtGui import QColor, QPixmap

    from paper_agent.ui.chat.rich_text import RichTextLabel

    wide = sample_png.parent / "wide.png"
    pixmap = QPixmap(900, 120)
    pixmap.fill(QColor("#3366ff"))
    assert pixmap.save(str(wide), "PNG")

    label = RichTextLabel()
    label.resize(320, 200)
    label.show()
    label.set_markdown(f"![宽图]({wide.as_posix()})")
    qapp.processEvents()

    bar = label.horizontalScrollBar()
    expected = math.ceil(label.document().size().height()) + 2
    assert bar.isVisible(), "超宽图片应当出现横向滚动条"
    expected += bar.sizeHint().height()
    assert label.height() >= expected - 1


def test_composer_can_shrink_in_narrow_pane(qapp):
    """输入区的最小宽度会变成聊天栏的地板：窄栏下必须能收窄。"""
    from paper_agent.core.config import AppConfig
    from paper_agent.ui.composer.composer import Composer

    composer = Composer(AppConfig())
    composer.resize(1000, 160)
    composer.show()
    qapp.processEvents()
    wide_floor = composer.minimumSizeHint().width()

    composer.resize(470, 160)
    qapp.processEvents()

    assert composer._compact is True
    assert composer.model_button._text_width == Composer.COMPACT_MODEL_TEXT_WIDTH
    assert composer.minimumSizeHint().width() < wide_floor
    assert composer.minimumSizeHint().width() <= 470


def test_outline_panel_opens_with_usable_width(qapp, tmp_path):
    """打开结构面板时给它一个可用宽度，并把聊天栏留在最小宽度之上。"""
    from paper_agent.core.config import AppConfig
    from paper_agent.core.constants import OUTLINE_PANEL_WIDTH
    from paper_agent.core.session_store import SessionStore
    from paper_agent.ui.main_window import MainWindow

    window = MainWindow(AppConfig(), SessionStore(tmp_path / "sessions"))
    window.resize(1280, 860)
    window.show()
    qapp.processEvents()
    window._set_outline_visible(False)
    qapp.processEvents()

    chat = window.splitter.widget(1)
    window._set_outline_visible(True)
    qapp.processEvents()

    assert window.outline_panel.isVisible()
    assert window.outline_panel.width() >= OUTLINE_PANEL_WIDTH
    assert chat.width() >= chat.minimumSizeHint().width() - 1, "不能挤爆聊天栏"
    assert sum(window.splitter.sizes()) <= window.splitter.width() + 1
    opened_width = chat.width()

    window._set_outline_visible(False)
    qapp.processEvents()
    assert not window.outline_panel.isVisible()
    assert chat.width() > opened_width, "收起结构面板后宽度要还给聊天区"


@pytest.mark.parametrize("size", [(1280, 860), (1080, 760)])
def test_splitter_never_overflows_window(qapp, tmp_path, size):
    """三栏宽度之和不能超过窗口，否则最后一栏会被推到窗口外。"""
    from paper_agent.core.config import AppConfig
    from paper_agent.core.session_store import SessionStore
    from paper_agent.ui.main_window import MainWindow

    window = MainWindow(AppConfig(), SessionStore(tmp_path / "sessions"))
    window.resize(*size)
    window.show()
    qapp.processEvents()
    window._set_outline_visible(True)
    qapp.processEvents()
    window.splitter.setSizes([268, 300, 1200])
    qapp.processEvents()

    assert sum(window.splitter.sizes()) <= window.splitter.width() + 1
    assert window.splitter.widget(1).width() > 0
