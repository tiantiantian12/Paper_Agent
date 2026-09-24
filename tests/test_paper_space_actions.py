"""论文空间的文件卡片：复制绝对路径 / 复制文件，不再提供行内预览。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication

from paper_agent.core.models import Attachment, ChatSession, Message
from paper_agent.ui.chat.artifact_card import ArtifactCard


def test_paper_space_card_has_copy_actions_without_preview(qapp, tmp_path):
    path = tmp_path / "论文.docx"
    path.write_bytes(b"x")

    card = ArtifactCard(
        path.name, str(path), removable=True, previewable=False, copyable=True
    )
    assert not card.preview_button.isVisibleTo(card), "论文空间不再提供行内预览"
    assert card.copy_path_button.isVisibleTo(card)
    assert card.copy_file_button.isVisibleTo(card)
    assert card.copy_path_button.toolTip() == "复制绝对路径"


def test_copy_path_puts_absolute_path_on_clipboard(qapp, tmp_path):
    path = tmp_path / "论文.docx"
    path.write_bytes(b"x")
    card = ArtifactCard(path.name, str(path), copyable=True)

    card._copy_path()
    assert QApplication.clipboard().text() == str(path)


def test_copy_file_puts_file_url_on_clipboard(qapp, tmp_path):
    path = tmp_path / "答辩.pptx"
    path.write_bytes(b"x")
    card = ArtifactCard(path.name, str(path), copyable=True)

    try:
        card._copy_file()
        urls = QApplication.clipboard().mimeData().urls()
        assert [Path(url.toLocalFile()) for url in urls] == [path], "剪贴板里应是文件本体"
    finally:
        # 收尾必须把剪贴板清掉：放进剪贴板的是**文件 URL**，Qt 退出时还要释放那份
        # QMimeData，实测会让解释器在退出阶段直接访问违例（进程退出码 -1073741819，
        # 用例本身全绿、但 pytest 的汇总行都来不及输出）。清一下就没事了。
        QApplication.clipboard().clear()


def test_copy_missing_file_is_refused(qapp, tmp_path):
    card = ArtifactCard("没了.docx", str(tmp_path / "没了.docx"), copyable=True)
    from paper_agent.utils.clipboard import copy_files

    assert copy_files([str(tmp_path / "没了.docx")]) == 0


def test_paper_space_dialog_uses_copyable_cards(qapp, tmp_path):
    from paper_agent.ui.dialogs.paper_space_dialog import PaperSpaceDialog

    path = tmp_path / "论文.docx"
    path.write_bytes(b"x")
    session = ChatSession(title="测试对话")
    message = Message(role="assistant", content="已生成")
    message.artifacts.append(
        Attachment(name=path.name, path=str(path), kind="file", size=1)
    )
    session.add_message(message)

    dialog = PaperSpaceDialog(session)
    card = dialog.list_host.findChild(ArtifactCard)
    assert card is not None
    assert not card.preview_button.isVisibleTo(card)
    assert card.copy_path_button.isVisibleTo(card)
    assert card.copy_file_button.isVisibleTo(card)
