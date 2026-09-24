"""论文空间与文件生命周期：清单、删除、会话清理、孤儿文件清理。"""

from __future__ import annotations

import time

from PySide6.QtWidgets import QMessageBox

from paper_agent.core.models import Attachment, ChatSession, Message
from paper_agent.ui.dialogs.paper_space_dialog import PaperSpaceDialog
from paper_agent.utils import files as files_util


def _session(managed_dirs, name="论文.docx", upload="参考.pdf") -> tuple[ChatSession, object, object]:
    attachments_dir, artifacts_dir = managed_dirs
    artifact = artifacts_dir / name
    artifact.write_bytes(b"artifact")
    uploaded = attachments_dir / upload
    uploaded.write_bytes(b"upload")

    now = time.time()
    session = ChatSession(title="会话")
    session.add_message(
        Message(
            role="user",
            content="参考资料",
            attachments=[
                Attachment(name=uploaded.name, path=str(uploaded), kind="file", created_at=now - 10)
            ],
        )
    )
    session.add_message(
        Message(
            role="assistant",
            content="",
            artifacts=[
                Attachment(name=artifact.name, path=str(artifact), kind="file", created_at=now)
            ],
        )
    )
    return session, artifact, uploaded


# ---------------------------------------------------------------- 清单
def test_paper_files_marks_source_and_order(managed_dirs):
    session, artifact, uploaded = _session(managed_dirs)
    files = session.paper_files()
    assert [item.name for item in files] == [artifact.name, uploaded.name]
    assert files[0].source == "model" and files[1].source == "user"
    assert files[0].is_user_upload is False and files[1].is_user_upload is True


def test_legacy_attachment_falls_back_to_message_time():
    """旧会话没有附件时间字段，读取时要回退到消息时间，否则论文空间显示不出时间。"""
    data = {
        "role": "user",
        "content": "旧消息",
        "created_at": 1700000000.0,
        "attachments": [{"name": "旧.pdf", "path": "C:/x/旧.pdf", "kind": "file"}],
    }
    message = Message.from_dict(data)
    assert message.attachments[0].created_at == 1700000000.0


# ---------------------------------------------------------------- 删除
def test_remove_paper_file(managed_dirs):
    session, artifact, _uploaded = _session(managed_dirs)
    record = session.paper_files()[0]
    assert session.remove_paper_file(record) is True
    assert all(item.path != str(artifact) for item in session.paper_files())
    assert session.remove_paper_file(record) is False, "重复删除应返回 False"


def test_delete_managed_file_only_touches_app_dirs(managed_dirs, tmp_path):
    _attachments, artifacts = managed_dirs
    managed = artifacts / "产物.docx"
    managed.write_bytes(b"x")
    deleted, detail = files_util.delete_managed_file(managed)
    assert deleted and not managed.exists() and detail == "已删除"

    external = tmp_path / "用户自己的文件.docx"
    external.write_bytes(b"y")
    deleted, detail = files_util.delete_managed_file(external)
    assert not deleted and external.exists(), "外部文件只能解除引用，绝不能删除"
    assert "不在应用数据目录内" in detail

    missing = artifacts / "已不存在.docx"
    deleted, detail = files_util.delete_managed_file(missing)
    assert not deleted and "已不存在" in detail


def test_paper_space_dialog_delete_flow(qapp, managed_dirs):
    session, artifact, _uploaded = _session(managed_dirs)
    dialog = PaperSpaceDialog(session)
    dialog._confirm_delete = lambda record: True          # 跳过确认框

    removed: list = []
    dialog.files_changed.connect(removed.extend)
    dialog._on_delete(str(artifact))

    assert not artifact.exists(), "应用数据目录内的文件应真的删掉"
    assert dialog._files == [] or all(item.path != str(artifact) for item in dialog._files)
    assert removed == [str(artifact)], "删除后要通知主窗口同步"
    assert "已移除" in dialog.status_label.text()


def test_paper_space_dialog_cancel_keeps_file(qapp, managed_dirs):
    session, artifact, _uploaded = _session(managed_dirs)
    dialog = PaperSpaceDialog(session)
    dialog._confirm_delete = lambda record: False
    dialog._on_delete(str(artifact))
    assert artifact.exists(), "取消删除不该动文件"
    assert len(session.paper_files()) == 2


# ---------------------------------------------------------------- 清理
def _window(qapp, tmp_path, sessions):
    from paper_agent.core.config import AppConfig
    from paper_agent.core.session_store import SessionStore
    from paper_agent.ui.main_window import MainWindow

    window = MainWindow(AppConfig(), SessionStore(tmp_path / "sessions"))
    window.sessions = {session.id: session for session in sessions}
    if sessions:
        window.current = sessions[0]
    return window


def test_delete_session_removes_only_its_files(qapp, tmp_path, managed_dirs):
    attachments_dir, artifacts_dir = managed_dirs
    shared = artifacts_dir / "共享.docx"
    shared.write_bytes(b"shared")
    shared_session = ChatSession(title="另一个会话")
    shared_session.add_message(
        Message(
            role="assistant",
            content="",
            artifacts=[Attachment(name=shared.name, path=str(shared), kind="file")],
        )
    )

    session, artifact, uploaded = _session(managed_dirs)
    window = _window(qapp, tmp_path, [session, shared_session])

    removed = window._cleanup_session_files(session)
    assert removed == 2
    assert not artifact.exists() and not uploaded.exists()
    assert shared.exists(), "别的会话还在引用的文件不能删"


def test_cleanup_orphan_files(qapp, tmp_path, managed_dirs, monkeypatch):
    _attachments, artifacts_dir = managed_dirs
    orphan = artifacts_dir / "没人引用.docx"
    orphan.write_bytes(b"x")
    session, artifact, _uploaded = _session(managed_dirs)
    window = _window(qapp, tmp_path, [session])

    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    window._cleanup_orphan_files()

    assert not orphan.exists(), "无引用文件应被清理"
    assert artifact.exists(), "仍被会话引用的文件要保留"


def test_cleanup_orphan_files_cancelled(qapp, tmp_path, managed_dirs, monkeypatch):
    _attachments, artifacts_dir = managed_dirs
    orphan = artifacts_dir / "没人引用.docx"
    orphan.write_bytes(b"x")
    window = _window(qapp, tmp_path, [])

    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    )
    window._cleanup_orphan_files()
    assert orphan.exists()
