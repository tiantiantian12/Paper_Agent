"""给模型的上下文：会话工作区注入、产物列举按会话收口、工具接线。"""

from __future__ import annotations

import time

from paper_agent.core.models import Attachment, ChatSession, Message
from paper_agent.services import paper_outline as po
from paper_agent.services.agent_service import build_chat_messages
from paper_agent.services.skills.document_skills import (
    CreateDocxTool,
    ListArtifactsTool,
    build_default_registry,
)


def _session(tmp_path, name="毕业论文.docx"):
    path = tmp_path / name
    path.write_bytes(b"x")
    session = ChatSession(title="会话")
    session.add_message(
        Message(
            role="assistant",
            content="",
            artifacts=[Attachment(name=name, path=str(path), kind="file", created_at=time.time())],
        )
    )
    return session, path


def test_workspace_injected_into_messages(tmp_path):
    session, path = _session(tmp_path)
    snapshot = po.workspace_snapshot(session)
    messages = build_chat_messages([], "继续写第二章", [], workspace=snapshot)

    system_text = "\n".join(m["content"] for m in messages if m["role"] == "system")
    assert "本会话工作区已有以下文件" in system_text
    assert path.name in system_text and str(path) in system_text
    assert "不要再去磁盘搜索" in system_text


def test_workspace_omitted_when_empty():
    messages = build_chat_messages([], "你好", [], workspace=[])
    system_text = "\n".join(m["content"] for m in messages if m["role"] == "system")
    assert "本会话工作区" not in system_text


def test_attachment_note_still_added(tmp_path):
    reference = tmp_path / "参考.txt"
    reference.write_text("参考文献内容", encoding="utf-8")
    attachment = Attachment(name=reference.name, path=str(reference), kind="file")
    messages = build_chat_messages([], "看看这个", [attachment])

    system_text = "\n".join(m["content"] for m in messages if m["role"] == "system")
    assert "用户附带了以下参考资料" in system_text
    assert "参考文献内容" in system_text


def test_list_artifacts_is_session_scoped(tmp_path):
    session, path = _session(tmp_path)
    tool = ListArtifactsTool(po.workspace_snapshot(session))
    result = tool.run()

    assert "本会话工作区文件（共 1 个）" in result.content
    assert path.name in result.content
    assert "别的会话的论文.docx" not in result.content


def test_list_artifacts_falls_back_to_directory(managed_dirs):
    _attachments, artifacts_dir = managed_dirs
    (artifacts_dir / "历史产物.docx").write_bytes(b"x")
    result = ListArtifactsTool().run()
    assert "历史产物.docx" in result.content


def test_registry_wires_session_files_into_create_tools(managed_dirs):
    """同一会话里重复生成同名文档：复用同一路径，不再堆 -1/-2 副本。"""
    _attachments, artifacts_dir = managed_dirs
    session, path = _session(artifacts_dir, "论文.docx")
    session_files = po.workspace_snapshot(session)

    registry = build_default_registry(None, session_files)
    tool = registry.get("create_docx")
    assert isinstance(tool, CreateDocxTool)

    result = tool.run(filename="论文.docx", markdown="# 第一章 绪论\n\n正文")
    assert result.artifact_paths == [str(path)], "本会话同名文档应原地更新"
    assert len(list(artifacts_dir.glob("论文*.docx"))) == 1

    assert [item["name"] for item in po.workspace_snapshot(session)] == ["论文.docx"]


def test_system_prompt_guides_document_tools():
    """提示词必须引导模型用工具产出文档，否则论文排版/目录页码都不会生效。"""
    from paper_agent.services.agent_service import SYSTEM_PROMPT
    from paper_agent.services.skills.code_sandbox import ExecutePythonTool

    assert "doc_type=thesis" in SYSTEM_PROMPT
    assert "![图" in SYSTEM_PROMPT                    # 配图写法
    assert "# 目录" in SYSTEM_PROMPT                  # 目录交给系统生成
    assert "execute_python" in SYSTEM_PROMPT
    assert "不要" in ExecutePythonTool.description and "create_docx" in ExecutePythonTool.description


def test_create_tool_without_session_context_keeps_suffixing(managed_dirs):
    attachments_dir, artifacts_dir = managed_dirs
    _session(artifacts_dir, "论文.docx")
    tool = CreateDocxTool()                        # 没有会话上下文
    result = tool.run(filename="论文.docx", markdown="# 第一章 绪论\n\n正文")
    assert result.artifact_paths[0].endswith("论文-1.docx")
