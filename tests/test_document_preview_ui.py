"""聊天内预览的界面行为：展开/收起、按类型渲染、随文件变化实时更新。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from paper_agent.services.skills.document_skills import build_docx
from paper_agent.ui.chat.artifact_card import ArtifactCard
from paper_agent.ui.chat.document_preview import DocumentPreview

PAPER_MD = "# 第一章 绪论\n\n## 1.1 研究背景\n\n自动识别车牌在智能交通里非常重要。\n"


def _docx(tmp_path, name="论文.docx", markdown=PAPER_MD):
    path = tmp_path / name
    build_docx(markdown, path, style="thesis")
    return path


# ---------------------------------------------------------------- 卡片
def test_preview_button_only_when_enabled(qapp, tmp_path):
    path = _docx(tmp_path)
    plain = ArtifactCard("论文.docx", str(path))
    assert not plain.preview_button.isVisibleTo(plain), "未开启预览时不该出现预览按钮"

    card = ArtifactCard("论文.docx", str(path), previewable=True)
    assert card.preview_button.isVisibleTo(card)
    assert card.preview_button.toolTip() == "在对话里预览"


def test_toggle_preview_creates_and_hides(qapp, tmp_path):
    path = _docx(tmp_path)
    card = ArtifactCard("论文.docx", str(path), previewable=True)
    assert card.preview is None, "预览控件应延迟到展开时才创建"

    card.toggle_preview()
    assert card.preview_visible is True
    assert card.preview.path == str(path)
    assert card.preview_button.toolTip() == "收起预览"
    assert card.preview_button.icon_name() == "close"

    card.toggle_preview()
    assert card.preview_visible is False
    assert card.preview_button.icon_name() == "eye"


def test_preview_missing_file_is_refused(qapp, tmp_path):
    card = ArtifactCard("没了.docx", str(tmp_path / "没了.docx"), previewable=True)
    card.toggle_preview()
    assert card.preview_visible is False
    assert card.preview is None


# ---------------------------------------------------------------- 渲染类型
def test_preview_renders_docx_markdown(qapp, tmp_path):
    path = _docx(tmp_path)
    card = ArtifactCard("论文.docx", str(path), previewable=True)
    card.set_preview_visible(True)

    preview = card.preview
    assert preview.body.currentWidget() is preview.text_scroll
    assert "自动识别车牌" in preview.text_view.plain_text()
    assert "# 第一章 绪论" in preview.text_view.plain_text()
    assert "DOCX" in preview.header_label.text()


def test_preview_renders_image(qapp, sample_png):
    card = ArtifactCard("配图.png", str(sample_png), previewable=True)
    card.set_preview_visible(True)

    preview = card.preview
    assert preview.body.currentWidget() is preview.image_scroll
    pixmap = preview.image_label.pixmap()
    assert pixmap is not None and not pixmap.isNull()
    assert pixmap.width() <= 460


def test_preview_unavailable_shows_hint(qapp, tmp_path):
    path = tmp_path / "模型.bin"
    path.write_bytes(b"\x00\x01")
    card = ArtifactCard("模型.bin", str(path), previewable=True)
    card.set_preview_visible(True)

    preview = card.preview
    assert preview.body.currentWidget() is preview.hint_label
    assert "暂不支持预览" in preview.hint_label.text()


# ---------------------------------------------------------------- 实时更新
def test_preview_follows_file_changes(qapp, tmp_path):
    """模型边写边改同一份文档时，预览要能跟上。"""
    path = _docx(tmp_path)
    card = ArtifactCard("论文.docx", str(path), previewable=True)
    card.set_preview_visible(True)
    assert "自动识别车牌" in card.preview.text_view.plain_text()

    build_docx(PAPER_MD + "\n# 第二章 相关工作\n\n新增章节内容。\n", path, style="thesis")
    card.preview.refresh()                    # 定时器走的就是这个方法
    assert "新增章节内容" in card.preview.text_view.plain_text()


def test_preview_skips_reload_when_unchanged(qapp, tmp_path, monkeypatch):
    path = _docx(tmp_path)
    preview = DocumentPreview(str(path))
    calls: list = []
    monkeypatch.setattr(
        "paper_agent.ui.chat.document_preview.build_preview",
        lambda *a, **k: calls.append(1) or __import__(
            "paper_agent.services.document_preview", fromlist=["Preview"]
        ).Preview(kind="text", content="x"),
    )
    preview.refresh(force=True)
    preview.refresh()                          # 文件没变 → 不再重读
    assert len(calls) == 1

    monkeypatch.undo()
    path.write_text("变了", encoding="utf-8")
    preview.refresh()
    assert len(calls) == 1                     # 仍不重读（monkeypatch 已还原）


def test_preview_timer_runs_only_when_visible(qapp, tmp_path):
    path = _docx(tmp_path)
    card = ArtifactCard("论文.docx", str(path), previewable=True)
    card.set_preview_visible(True)
    card.show()
    qapp.processEvents()
    assert card.preview._timer.isActive(), "展开后要自动刷新"

    card.set_preview_visible(False)
    assert not card.preview._timer.isActive(), "收起后停止刷新"


# ---------------------------------------------------------------- 点击语义
def test_click_inside_preview_does_not_open_file(qapp, tmp_path, monkeypatch):
    opened: list = []
    monkeypatch.setattr(
        "paper_agent.ui.chat.artifact_card.QDesktopServices.openUrl",
        lambda url: opened.append(url.toLocalFile()),
    )
    path = _docx(tmp_path)
    card = ArtifactCard("论文.docx", str(path), previewable=True)
    card.resize(520, 420)
    card.show()
    card.set_preview_visible(True)
    qapp.processEvents()

    inside = card.preview.geometry().center()
    QTest.mouseClick(card, Qt.MouseButton.LeftButton, pos=inside)
    assert opened == [], "点预览区域（选文字/看图）不该打开系统程序"

    QTest.mouseClick(card, Qt.MouseButton.LeftButton, pos=QPoint(20, 12))
    assert [Path(item) for item in opened] == [path], "点卡片标题行应打开文件"


def test_message_widget_cards_are_previewable(qapp, tmp_path):
    from paper_agent.core.models import Message
    from paper_agent.services.skills.document_skills import build_docx as _build
    from paper_agent.ui.chat.message_widget import MessageWidget

    path = tmp_path / "论文.docx"
    _build("# 第一章\n\n正文。\n", path, style="thesis")
    message = Message.from_dict(
        {
            "role": "assistant",
            "content": "",
            "artifacts": [{"name": path.name, "path": str(path), "kind": "file"}],
        }
    )
    widget = MessageWidget(message)
    card = widget.artifact_cards()[0]
    assert isinstance(card, ArtifactCard)
    assert card.preview_button.isVisibleTo(card), "聊天里的产物卡片应可直接预览"
