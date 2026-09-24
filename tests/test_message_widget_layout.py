"""消息控件：流式写入的正文与产物卡片必须真的显示出来。

回归的坑：正文/产物容器在「新建消息时还是空的」这一刻被隐藏过，之后流式写入的
内容全落在隐藏容器里 —— 界面表现为「模型什么都没输出」，重启重建控件才恢复。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication

from paper_agent.core.models import Message
from paper_agent.ui.chat.message_widget import MessageWidget


def _widget(qapp: QApplication, content: str = "") -> MessageWidget:
    message = Message(role="assistant", content=content, status="streaming")
    widget = MessageWidget(message)
    widget.begin_stream()
    return widget


def test_content_area_stays_visible_while_streaming(qapp, tmp_path):
    widget = _widget(qapp)
    assert not widget.content_host.isHidden(), "空消息时正文容器也不该被隐藏"

    widget.append_delta("第一段正文。")
    widget._flush()
    assert not widget.content_host.isHidden(), "流式正文必须可见"
    assert widget._text_labels[0].toPlainText().strip() == "第一段正文。"


def test_artifact_card_appears_and_stays_visible(qapp, tmp_path):
    sample = tmp_path / "答辩PPT.pptx"
    sample.write_bytes(b"x")

    widget = _widget(qapp)
    widget.add_artifact(sample.name, str(sample), "file")
    widget.append_delta("生成完毕。")
    widget._flush()

    assert not widget.content_host.isHidden()
    cards = widget.artifact_cards()
    assert len(cards) == 1, "产物卡片要插进正文流"
    assert not cards[0].isHidden(), "产物卡片必须可见（曾经整块被隐藏）"


def test_sync_artifacts_fills_missing_cards(qapp, tmp_path):
    """数据里有产物、界面漏渲染时，生成结束的对账要把卡片补上。"""
    sample = tmp_path / "漏掉的PPT.pptx"
    sample.write_bytes(b"x")

    widget = _widget(qapp)
    widget._message.add_artifact(sample.name, str(sample), "file")   # 只写数据，不插控件
    assert widget.artifact_cards() == []

    widget.sync_artifacts()
    assert len(widget.artifact_cards()) == 1
    assert not widget.artifact_cards()[0].isHidden()


def _layout_signature(widget: MessageWidget) -> list[str]:
    """布局指纹：控件类型 + 正文内容，用于比较「流式实时」与「重开软件重建」。"""
    signature: list[str] = []
    for index in range(widget.content_layout.count()):
        item = widget.content_layout.itemAt(index).widget()
        name = type(item).__name__
        text = item.toPlainText().strip() if hasattr(item, "toPlainText") else ""
        signature.append(f"{name}:{text}")
    return signature


def test_reload_keeps_same_card_position(qapp, tmp_path):
    """重开软件后卡片的顺序必须和生成时看到的一致（不再堆到消息最下面）。"""
    sample = tmp_path / "答辩PPT.pptx"
    sample.write_bytes(b"x")

    live = _widget(qapp)
    live.append_delta("先写了一段说明。")
    live.add_artifact(sample.name, str(sample), "file")
    live.append_delta("生成完之后又写了一段总结。")
    live._flush()

    revived = MessageWidget(live.message)          # 模拟重启后按数据重建
    assert _layout_signature(revived) == _layout_signature(live)
    assert _layout_signature(revived) == [
        "RichTextLabel:先写了一段说明。",
        "ArtifactCard:",
        "RichTextLabel:生成完之后又写了一段总结。",
    ]


def test_legacy_artifacts_without_anchor_go_last(qapp, tmp_path):
    """老数据没有记录位置：正文在前、卡片在后（保持原有渲染）。"""
    sample = tmp_path / "旧文件.docx"
    sample.write_bytes(b"x")
    message = Message(role="assistant", content="正文内容。", status="done")
    message.add_artifact(sample.name, str(sample), "file")      # 不传 anchor → -1

    widget = MessageWidget(message)
    assert _layout_signature(widget) == ["RichTextLabel:正文内容。", "ArtifactCard:"]


def test_text_segments_render_in_generation_order(qapp, tmp_path):
    """「正文 → 产物 → 正文」的阅读顺序要与生成顺序一致。"""
    sample = tmp_path / "图.png"
    sample.write_bytes(b"x")
    widget = _widget(qapp)

    widget.append_delta("图片之前的话。")
    widget.add_artifact(sample.name, str(sample), "image")
    widget.append_delta("图片之后的话。")
    widget._flush()

    order = [
        type(widget.content_layout.itemAt(index).widget()).__name__
        for index in range(widget.content_layout.count())
    ]
    assert order[:3] == ["RichTextLabel", "ImageCard", "RichTextLabel"], order
    assert widget._text_labels[0].toPlainText().strip() == "图片之前的话。"
    assert widget._text_labels[1].toPlainText().strip() == "图片之后的话。"
