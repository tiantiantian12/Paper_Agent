"""上传的文件要落进**本会话**工作区的 ``upload``，和生成的文件（``generated``）同一棵树。

以前附件一律进 ``data/attachments``（所有会话混在一起），而产物在 ``data/artifacts`` ——
同一个会话的文件分散在两处，模型按名字找文件时只能扫其中一处。
"""

from __future__ import annotations


def test_upload_lands_in_the_session_workspace(qapp, tmp_path):
    """端到端：输入区加附件 → 文件落在本会话的 upload 目录里。"""
    from paper_agent.core.config import AppConfig
    from paper_agent.services import session_workspace
    from paper_agent.ui.composer.composer import Composer

    source = tmp_path / "开题报告.pdf"
    source.write_bytes(b"%PDF-fake")
    upload = session_workspace.upload_dir("sess-a", create=False)

    composer = Composer(AppConfig())
    composer.set_upload_dir(upload)
    composer.add_files([str(source)])

    stored = [item.path for item in composer.attachment_bar.items()]
    assert stored and stored[0] == str(upload / "开题报告.pdf"), stored
    assert (upload / "开题报告.pdf").is_file()


def test_switching_session_switches_the_upload_dir(qapp, tmp_path):
    """切会话后上传的文件进**新会话**的目录，别还写在上一个会话里。"""
    from paper_agent.core.config import AppConfig
    from paper_agent.services import session_workspace
    from paper_agent.ui.composer.composer import Composer

    composer = Composer(AppConfig())
    composer.set_upload_dir(session_workspace.upload_dir("sess-a", create=False))
    composer.set_upload_dir(session_workspace.upload_dir("sess-b", create=False))

    source = tmp_path / "数据.csv"
    source.write_text("a,b\n1,2", encoding="utf-8")
    composer.add_files([str(source)])

    stored = [item.path for item in composer.attachment_bar.items()]
    assert stored[0] == str(session_workspace.upload_dir("sess-b") / "数据.csv")
    assert not (session_workspace.upload_dir("sess-a") / "数据.csv").exists()


def test_without_upload_dir_it_falls_back_to_attachments(qapp, tmp_path, monkeypatch):
    """没设过上传目录（例如界面外的调用）时退回附件目录，文件不能丢。"""
    from paper_agent.core.config import AppConfig
    from paper_agent.ui.composer.composer import Composer
    from paper_agent.utils import files as files_utils

    attachments = tmp_path / "attachments"
    monkeypatch.setattr(files_utils, "ATTACHMENTS_DIR", attachments)
    source = tmp_path / "资料.txt"
    source.write_text("hello", encoding="utf-8")

    composer = Composer(AppConfig())
    composer.set_upload_dir(None)
    composer.add_files([str(source)])

    stored = [item.path for item in composer.attachment_bar.items()]
    assert stored and stored[0] == str(attachments / "资料.txt"), stored
