"""编程模式下的附件：上传的文件要**落到工程目录里**，模型才找得到。

回归的坑：附件平时存在应用的 ``data/attachments``，而编程模式的工具
（``list_files`` / ``read_file`` / ``run_command``）都钉在**工程目录**内 ——
不复制进去的话，模型在工程目录里看不到用户传的文件，只能回「找不到这个文件」，
用户以为是模型能力问题。
"""

from __future__ import annotations

from pathlib import Path

from paper_agent.core.models import Attachment


def _attachment(directory: Path, name: str, data: bytes = b"PNG" * 50) -> Attachment:
    path = directory / name
    path.write_bytes(data)
    return Attachment(name=name, path=str(path), kind="image" if path.suffix == ".png" else "file")


def test_attachments_are_copied_into_workspace(tmp_path):
    from paper_agent.services.skills.code_workspace import import_attachments

    source = tmp_path / "att"
    source.mkdir()
    workspace = tmp_path / "project"

    copied = import_attachments(
        workspace, [_attachment(source, "设计稿.png"), _attachment(source, "需求.md")]
    )

    assert copied == ["设计稿.png", "需求.md"]
    assert (workspace / "设计稿.png").is_file(), "文件要真的在工程目录里"
    assert (workspace / "需求.md").read_bytes() == (source / "需求.md").read_bytes(), "内容要一致"


def test_import_is_idempotent(tmp_path):
    """同一批附件连着发两轮，不该在工程目录里堆一堆 -1 -2 副本。"""
    from paper_agent.services.skills.code_workspace import import_attachments

    source = tmp_path / "att"
    source.mkdir()
    workspace = tmp_path / "project"
    items = [_attachment(source, "图.png")]

    first = import_attachments(workspace, items)
    second = import_attachments(workspace, items)

    assert first == second == ["图.png"]
    assert len(list(workspace.iterdir())) == 1


def test_existing_project_file_is_not_overwritten(tmp_path):
    """工程目录里已经有同名文件（用户自己的）：加后缀，绝不覆盖。"""
    from paper_agent.services.skills.code_workspace import import_attachments

    source = tmp_path / "att"
    source.mkdir()
    workspace = tmp_path / "project"
    workspace.mkdir()
    mine = workspace / "index.html"
    mine.write_text("<h1>我的工程</h1>", encoding="utf-8")

    copied = import_attachments(
        workspace, [_attachment(source, "index.html", "<p>上传的</p>".encode())]
    )

    assert copied == ["index-1.html"], f"要另存一份，不覆盖：{copied}"
    assert mine.read_text(encoding="utf-8") == "<h1>我的工程</h1>"


def test_workspace_index_shows_uploaded_files(tmp_path):
    """注入给模型的「工程目录现有文件」清单里必须带上上传的文件。

    顺序很重要：先复制、再扫清单。反了的话模型这一轮仍不知道文件存在，
    只能等它自己调 list_files 才发现。
    """
    from paper_agent.services.skills.code_workspace import file_index, import_attachments

    source = tmp_path / "att"
    source.mkdir()
    workspace = tmp_path / "project"
    import_attachments(workspace, [_attachment(source, "参考图.png")])

    lines, total = file_index(workspace)

    assert total == 1
    assert any("参考图.png" in line for line in lines)


def test_model_can_read_uploaded_file(tmp_path):
    """端到端：模型拿文件名调 read_file 真的读得到（相对路径按工程目录算）。"""
    from paper_agent.services.skills.code_workspace import ReadCodeFileTool, import_attachments

    source = tmp_path / "att"
    source.mkdir()
    workspace = tmp_path / "project"
    spec = source / "需求.md"
    spec.write_text("做一个登录页", encoding="utf-8")
    import_attachments(workspace, [Attachment(name=spec.name, path=str(spec), kind="file")])

    result = ReadCodeFileTool(workspace).run(path="需求.md")

    assert result.success is True
    assert "做一个登录页" in result.content


def test_code_mode_prompt_says_attachments_are_in_workspace(tmp_path):
    """要明说「附件已复制到工程目录」—— 不然模型会拿着名字去磁盘别处找。"""
    from paper_agent.services.agent_service import build_chat_messages

    attachment = _attachment(tmp_path, "需求.md", b"x")
    messages = build_chat_messages([], "按需求做", [attachment], mode="code")
    text = "\n".join(str(item.get("content") or "") for item in messages)

    assert "需求.md" in text
    assert "复制" in text and "工程目录" in text


def test_doc_mode_does_not_mention_copying(tmp_path):
    """文档模式不做这次复制（它有自己的 session_files 通路），别乱说。"""
    from paper_agent.services.agent_service import build_chat_messages

    attachment = _attachment(tmp_path, "需求.md", b"x")
    messages = build_chat_messages([], "写进论文", [attachment], mode="doc")
    text = "\n".join(str(item.get("content") or "") for item in messages)

    assert "需求.md" in text
    assert "复制到" not in text
