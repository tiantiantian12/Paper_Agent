"""工作模式：文档模式（论文/PPT）与编程模式（配环境/写代码/跑代码）。"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt

from paper_agent.core.config import MODE_CODE, MODE_DOC, AppConfig, CHAT_MODES
from paper_agent.services.agent_service import (
    CODE_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_chat_messages,
)
from paper_agent.services.skills import build_default_registry
from paper_agent.services.skills.code_sandbox import static_check
from paper_agent.services.skills.code_workspace import (
    ListFilesTool,
    ReadCodeFileTool,
    RunCommandTool,
    WriteFileTool,
    is_inside,
    resolve,
)


# ---------------------------------------------------------------- 提示词
def test_code_mode_uses_engineering_prompt():
    messages = build_chat_messages([], "写个接口", mode=MODE_CODE)
    assert messages[0]["content"] == CODE_SYSTEM_PROMPT
    assert "编程模式" in messages[0]["content"]


def test_doc_mode_keeps_thesis_prompt():
    messages = build_chat_messages([], "写篇论文", mode=MODE_DOC)
    assert messages[0]["content"] == SYSTEM_PROMPT


def test_code_mode_skips_document_only_hints():
    """工作区文件清单与「深度写作」都是文档模式的提示，编程模式下不注入。"""
    messages = build_chat_messages(
        [],
        "跑个测试",
        workspace=[{"name": "论文.docx", "path": "x.docx", "source": "model", "size": 1}],
        deep=True,
        mode=MODE_CODE,
    )
    joined = "\n".join(str(item.get("content", "")) for item in messages)
    assert "本会话工作区已有以下文件" not in joined
    assert "深度写作" not in joined


# ---------------------------------------------------------------- 工具集
def test_code_mode_registry_has_engineering_tools_only():
    registry = build_default_registry(mode=MODE_CODE)
    names = set(registry.names())
    assert {"write_file", "read_file", "list_files", "run_command", "execute_python"} <= names
    assert "create_docx" not in names, "编程模式不该挂文档工具"


def test_code_mode_registry_also_has_image_tool():
    """回归：编程模式漏挂文生图工具，模型会一口咬定「我没有图像生成能力」。

    用户当时就在编程模式里说「生成一张图」，模型翻遍工具列表没有 generate_image，
    于是回答自己没有生图能力——明明文生图是配好的。
    """
    calls: list[str] = []
    registry = build_default_registry(
        image_generator=lambda prompt: calls.append(prompt) or [],
        mode=MODE_CODE,
    )
    names = set(registry.names())
    assert "generate_image" in names, "编程模式也要能出图"
    assert "create_docx" not in names, "但文档工具仍然不该挂"

    tool = registry.get("generate_image")
    tool.run(prompt="一只猫")
    assert calls == ["一只猫"], "工具要真的调到配置好的文生图生成器"


def test_code_mode_registry_drops_image_tool_without_generator():
    """没配文生图 Key 时不挂这个工具，免得模型调了才报错。"""
    assert "generate_image" not in set(build_default_registry(mode=MODE_CODE).names())


def test_doc_mode_registry_keeps_document_tools():
    registry = build_default_registry(mode=MODE_DOC)
    names = set(registry.names())
    assert {"create_docx", "create_pptx", "create_pdf", "create_csv", "create_xlsx"} <= names
    assert "write_file" not in names


# ---------------------------------------------------------------- 沙箱策略
def test_code_mode_allows_subprocess_but_doc_mode_blocks_it():
    code = "import subprocess\nsubprocess.run(['echo', 'hi'])"
    assert static_check(code) != [], "文档模式仍要拦住子进程"
    assert static_check(code, relaxed=True) == [], "编程模式要能装依赖 / 跑构建"


def test_workspace_write_is_allowed_outside_is_not(tmp_path):
    tool = WriteFileTool(tmp_path)
    inside = tool.run(path="src/main.py", content="print(1)\n")
    assert inside.success and (tmp_path / "src" / "main.py").exists()

    outside = tool.run(path=str(tmp_path.parent / "逃逸.py"), content="x")
    assert not outside.success and "只能写到工程目录内" in outside.error


def test_read_and_list_inside_workspace(tmp_path):
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    tool = WriteFileTool(tmp_path)
    tool.run(path="pkg/mod.py", content="x = 1\n")

    listed = ListFilesTool(tmp_path).run()
    assert "README.md" in listed.content and "pkg/" in listed.content

    read = ReadCodeFileTool(tmp_path).run(path="pkg/mod.py")
    assert read.success and "x = 1" in read.content

    missing = ReadCodeFileTool(tmp_path).run(path="nope.py")
    assert not missing.success


def test_run_command_blocks_destructive_ones(tmp_path):
    tool = RunCommandTool(tmp_path)
    blocked = tool.run(command="format c: /q")
    assert not blocked.success and "破坏性" in blocked.error


def test_run_command_runs_inside_workspace(tmp_path):
    tool = RunCommandTool(tmp_path)
    result = tool.run(command=f'"{sys.executable}" -c "print(42)"', timeout=60)
    assert result.success, result.error
    assert "42" in result.content


def test_resolve_and_is_inside(tmp_path):
    assert is_inside(resolve("a/b.py", root=tmp_path), tmp_path)
    assert not is_inside(resolve("../outside.py", root=tmp_path), tmp_path)


# ---------------------------------------------------------------- 配置
def test_mode_is_persisted_and_defaults_to_doc():
    config = AppConfig()
    original = config.mode
    try:
        config.mode = MODE_CODE
        assert AppConfig().mode == MODE_CODE
        config.mode = "不存在的模式"
        assert AppConfig().mode == MODE_DOC, "非法值回落到文档模式"
    finally:
        config.mode = original


def test_chat_modes_expose_the_two_modes():
    assert [item["id"] for item in CHAT_MODES] == [MODE_DOC, MODE_CODE]


# ---------------------------------------------------------------- 工作区目录
def test_build_tree_skips_heavy_dirs_and_sorts(tmp_path):
    from paper_agent.services.skills.code_workspace import build_tree

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")

    nodes, files, dirs = build_tree(tmp_path)
    assert [node["name"] for node in nodes] == ["src", "README.md"], "目录在前、按名排序"
    assert "node_modules" not in str(nodes), "依赖目录要跳过"
    assert (files, dirs) == (2, 1), "main.py 与 README.md 都算文件"
    assert nodes[0]["children"][0]["name"] == "main.py"


def test_code_root_setting_overrides_default(tmp_path):
    from paper_agent.services.skills import code_workspace
    from paper_agent.services.skills.code_workspace import code_root, default_root

    config = AppConfig()
    original = config.code_root
    os.environ.pop("PAPERAGENT_CODE_ROOT", None)
    try:
        config.code_root = ""
        assert code_root() == default_root()

        chosen = tmp_path / "my_project"
        chosen.mkdir()
        config.code_root = str(chosen)
        assert code_root() == chosen

        os.environ["PAPERAGENT_CODE_ROOT"] = str(tmp_path)
        assert code_root() == tmp_path, "环境变量优先级最高"
    finally:
        os.environ.pop("PAPERAGENT_CODE_ROOT", None)
        config.code_root = original


def test_outline_panel_switches_to_workspace_view(qapp, tmp_path):
    from paper_agent.ui.outline.outline_panel import OutlinePanel

    (tmp_path / "app.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "pkg").mkdir()

    panel = OutlinePanel()
    panel.set_workspace_mode(True)
    assert panel.title_label.text() == "代码工作区"
    assert panel.workspace_row.isVisibleTo(panel) or not panel.workspace_row.isHidden()

    from paper_agent.services.skills.code_workspace import build_tree

    nodes, files, dirs = build_tree(tmp_path)
    panel.set_workspace(str(tmp_path), nodes, files, dirs)
    assert panel.workspace_label.text() == tmp_path.name, "标签只显示目录名"
    assert panel.workspace_label.toolTip() == str(tmp_path), "绝对路径放在悬停提示里"
    assert panel.tree.topLevelItemCount() == 2
    assert "1 个文件" in panel.stats_label.text()

    activated: list[str] = []
    panel.file_activated.connect(activated.append)
    file_item = next(
        panel.tree.topLevelItem(index)
        for index in range(panel.tree.topLevelItemCount())
        if not panel.tree.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole + 1)
    )
    panel._on_item_double_clicked(file_item, 0)
    assert activated == [file_item.data(0, Qt.ItemDataRole.UserRole)], "双击文件要能打开"

    panel.set_workspace_mode(False)
    assert panel.title_label.text() == "论文结构"


# ---------------------------------------------------------------- 输入区控件
def test_composer_mode_button_switches_and_persists(qapp):
    from paper_agent.ui.composer.composer import Composer

    config = AppConfig()
    original = config.mode
    try:
        config.mode = MODE_DOC
        composer = Composer(config)
        assert composer.mode_button.current_mode == MODE_DOC
        assert "文档" in composer.mode_button.toolTip()

        changed: list[str] = []
        composer.mode_changed.connect(changed.append)

        action = next(
            item for item in composer.mode_button.styled_menu.actions()
            if item.data() == MODE_CODE
        )
        action.trigger()

        assert changed == [MODE_CODE], "切换模式要发出信号"
        assert config.mode == MODE_CODE, "模式要持久化配置"
        assert composer.mode_button.current_mode == MODE_CODE
        assert "编程" in composer.mode_button.toolTip()
        assert "编程需求" in composer.editor.placeholderText(), "输入提示语要跟着模式变"
    finally:
        config.mode = original
