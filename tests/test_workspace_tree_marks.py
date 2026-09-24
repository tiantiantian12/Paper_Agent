"""代码工作区目录树：展开 / 折叠符号要画在文件夹图标的**左边**。

回归的坑一：工作区模式下图标槽位被文件夹 / 文件图标占满，展开折叠符号被覆盖掉，
目录看起来是「不能展开的」。
回归的坑二：符号曾经画在图标右边（拼在目录名前面），和常见文件树的观感相反。
"""

from __future__ import annotations

from PySide6.QtGui import QIcon

from paper_agent.ui.outline.outline_panel import (
    CHEVRON_SIZE,
    COMPOSITE_WIDTH,
    OutlinePanel,
)


def _panel(qapp):
    panel = OutlinePanel()
    panel.set_workspace_mode(True)
    return panel


def _nodes() -> list[dict]:
    return [
        {
            "name": "src",
            "path": "D:/ws/src",
            "is_dir": True,
            "size": 0,
            "children": [
                {
                    "name": "main.py",
                    "path": "D:/ws/src/main.py",
                    "is_dir": False,
                    "size": 120,
                    "children": [],
                }
            ],
        },
        {"name": "empty", "path": "D:/ws/empty", "is_dir": True, "size": 0, "children": []},
        {"name": "README.md", "path": "D:/ws/README.md", "is_dir": False, "size": 40, "children": []},
    ]


def _left_alpha(icon: QIcon, width: int = COMPOSITE_WIDTH, height: int = CHEVRON_SIZE) -> int:
    """图标最左边那一段（箭头该在的位置）的不透明像素总和。"""
    image = icon.pixmap(width, height).toImage()
    return sum(
        image.pixelColor(x, y).alpha()
        for x in range(CHEVRON_SIZE)
        for y in range(height)
    )


def test_expandable_folder_draws_arrow_on_the_left(qapp):
    panel = _panel(qapp)
    panel.set_workspace("D:/ws", _nodes(), files=2, dirs=2)
    src = panel.tree.topLevelItem(0)

    assert src.text(0) == "src/", "符号画在图标里，文字保持原名"
    assert _left_alpha(src.icon(0)) > 0, "可展开的目录左边要有箭头"


def test_leaf_items_leave_the_arrow_slot_empty(qapp):
    """文件 / 空目录不能显示箭头，但要占同样的宽度（文字左边界才对齐）。"""
    panel = _panel(qapp)
    panel.set_workspace("D:/ws", _nodes(), files=2, dirs=2)
    tree = panel.tree

    assert tree.topLevelItem(1).text(0) == "empty/"
    assert _left_alpha(tree.topLevelItem(1).icon(0)) == 0
    assert tree.topLevelItem(2).text(0).startswith("README.md")
    assert _left_alpha(tree.topLevelItem(2).icon(0)) == 0


def test_icon_slot_is_wide_enough_for_arrow_and_icon(qapp):
    panel = _panel(qapp)
    panel.set_workspace("D:/ws", _nodes(), files=2, dirs=2)

    assert panel.tree.iconSize().width() == COMPOSITE_WIDTH


def test_doc_mode_keeps_narrow_icons(qapp):
    """切回论文结构后，图标位要收回成单个箭头的宽度。"""
    panel = _panel(qapp)
    panel.set_workspace("D:/ws", _nodes(), files=2, dirs=2)

    panel.set_workspace_mode(False)
    panel._refresh_chevrons()

    assert panel.tree.iconSize().width() == CHEVRON_SIZE


def test_arrow_flips_when_toggled(qapp):
    panel = _panel(qapp)
    panel.set_workspace("D:/ws", _nodes(), files=2, dirs=2)
    src = panel.tree.topLevelItem(0)

    collapsed = src.icon(0).pixmap(COMPOSITE_WIDTH, CHEVRON_SIZE).toImage()
    src.setExpanded(True)
    expanded = src.icon(0).pixmap(COMPOSITE_WIDTH, CHEVRON_SIZE).toImage()

    assert collapsed != expanded, "展开 / 折叠的箭头要不一样"


def test_clicking_folder_row_toggles(qapp):
    panel = _panel(qapp)
    panel.set_workspace("D:/ws", _nodes(), files=2, dirs=2)
    src = panel.tree.topLevelItem(0)

    panel._on_item_clicked(src, 0)
    assert src.isExpanded()
    assert _left_alpha(src.icon(0)) > 0

    panel._on_item_clicked(src, 0)
    assert not src.isExpanded()


def test_marks_survive_tree_rebuild(qapp):
    """目录树每轮对话都会重建：重建后文字不能叠加，图标也不能被冲掉。"""
    panel = _panel(qapp)
    panel.set_workspace("D:/ws", _nodes(), files=2, dirs=2)
    panel.set_workspace("D:/ws", _nodes(), files=2, dirs=2)
    src = panel.tree.topLevelItem(0)

    assert src.text(0) == "src/"
    assert "▶ ▶" not in src.text(0)
    assert _left_alpha(src.icon(0)) > 0
