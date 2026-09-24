"""论文结构面板：默认折叠、展开箭头、点击交互、刷新保持、与主窗口联动。"""

from __future__ import annotations

import time

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from paper_agent.core.models import Attachment, ChatSession, Message
from paper_agent.services.paper_outline import extract_outline
from paper_agent.services.skills.document_skills import build_docx
from paper_agent.ui.outline.outline_panel import OutlinePanel

PAPER_MD = """# 第一章 绪论

## 1.1 研究背景

背景正文。

## 1.2 研究意义

意义正文。

# 第二章 相关工作

## 2.1 国内现状

现状正文。

# 参考文献

[1] 张三. 研究[J]. 2025.
"""


def _paper(tmp_path, name="论文.docx"):
    path = tmp_path / name
    build_docx(PAPER_MD, path, style="thesis")
    return path


def _top_items(panel):
    return [panel.tree.topLevelItem(i) for i in range(panel.tree.topLevelItemCount())]


def test_default_collapsed(qapp, tmp_path):
    panel = OutlinePanel()
    panel.set_outline(extract_outline(_paper(tmp_path)))
    assert panel.tree.topLevelItemCount() == 3
    assert not any(item.isExpanded() for item in _top_items(panel))
    assert panel._all_expanded() is False


def test_chevron_only_on_expandable_rows(qapp, tmp_path):
    panel = OutlinePanel()
    panel.set_outline(extract_outline(_paper(tmp_path)))
    blank_key = panel._blank_icon.cacheKey()
    for item in _top_items(panel):
        assert (item.icon(0).cacheKey() == blank_key) is (not item.childCount())


def test_chevron_switches_with_state(qapp, tmp_path):
    panel = OutlinePanel()
    panel.set_outline(extract_outline(_paper(tmp_path)))
    item = next(i for i in _top_items(panel) if i.childCount())
    collapsed = item.icon(0).cacheKey()
    item.setExpanded(True)
    expanded = item.icon(0).cacheKey()
    assert collapsed != expanded
    item.setExpanded(False)
    assert item.icon(0).cacheKey() == collapsed


def test_click_arrow_toggles_but_click_text_does_not(qapp, tmp_path):
    panel = OutlinePanel()
    panel.set_outline(extract_outline(_paper(tmp_path)))
    panel.resize(300, 400)
    panel.show()
    qapp.processEvents()

    item = next(i for i in _top_items(panel) if i.childCount())
    rect = panel.tree.visualItemRect(item)
    arrow = QPoint(rect.x() + 6, rect.center().y())
    QTest.mouseClick(panel.tree.viewport(), Qt.MouseButton.LeftButton, pos=arrow)
    assert item.isExpanded()
    QTest.mouseClick(panel.tree.viewport(), Qt.MouseButton.LeftButton, pos=arrow)
    assert not item.isExpanded()

    text_area = QPoint(rect.x() + panel.tree.indentation() + 40, rect.center().y())
    QTest.mouseClick(panel.tree.viewport(), Qt.MouseButton.LeftButton, pos=text_area)
    assert not item.isExpanded(), "点标题文字不该展开"


def test_expand_all_button(qapp, tmp_path):
    panel = OutlinePanel()
    panel.set_outline(extract_outline(_paper(tmp_path)))
    assert panel.expand_button.isVisibleTo(panel)
    assert panel.expand_button.icon_name() == "chevron-right"
    assert panel.expand_button.toolTip() == "展开全部"

    panel.expand_button.click()
    assert panel._all_expanded() and panel.expand_button.icon_name() == "chevron-down"
    assert panel.expand_button.toolTip() == "折叠全部"

    panel.expand_button.click()
    assert not panel._all_expanded() and panel.expand_button.icon_name() == "chevron-right"


def test_expansion_survives_refresh(qapp, tmp_path):
    """实时刷新会频繁重建树，用户展开的层级不能被收回。"""
    path = _paper(tmp_path)
    nodes = extract_outline(path)
    panel = OutlinePanel()
    panel.set_outline(nodes)

    target = next(i for i in _top_items(panel) if i.childCount())
    title = target.data(0, Qt.ItemDataRole.UserRole)
    target.setExpanded(True)

    panel.set_outline(extract_outline(path))          # 模拟一次刷新
    restored = next(
        item
        for item in _top_items(panel)
        if item.data(0, Qt.ItemDataRole.UserRole) == title
    )
    assert restored.isExpanded()
    assert not next(
        item
        for item in _top_items(panel)
        if item is not restored and item.childCount()
    ).isExpanded(), "其它章节应保持折叠"


def test_button_hidden_without_children(qapp):
    from paper_agent.core.models import OutlineNode

    panel = OutlinePanel()
    panel.set_outline([OutlineNode("只有一章", 100, "done")])
    assert not panel.expand_button.isVisibleTo(panel)


def test_empty_outline(qapp):
    panel = OutlinePanel()
    panel.set_outline([], "没有结构")
    assert panel.tree.topLevelItemCount() == 0
    assert panel.empty_label.text() == "没有结构"


def test_writing_hint_in_stats(qapp, tmp_path):
    panel = OutlinePanel()
    panel.set_outline(extract_outline(_paper(tmp_path)), writing=True)
    assert "正在写入" in panel.stats_label.text()
    panel.set_outline(extract_outline(_paper(tmp_path)))
    assert "正在写入" not in panel.stats_label.text()


# ---------------------------------------------------------------- 主窗口联动
def _window(qapp, tmp_path, session_files):
    from paper_agent.core.config import AppConfig
    from paper_agent.core.session_store import SessionStore
    from paper_agent.ui.main_window import MainWindow

    session = ChatSession(title="论文")
    now = time.time()
    session.add_message(
        Message(
            role="assistant",
            content="",
            artifacts=[
                # 列表越靠后越新，便于断言「默认展示最近生成的那篇」
                Attachment(name=path.name, path=str(path), kind="file", created_at=now + index)
                for index, path in enumerate(session_files)
            ],
        )
    )
    store = SessionStore(tmp_path / "sessions")
    window = MainWindow(AppConfig(), store)
    window.sessions[session.id] = session
    window.current = session
    return window, session


def test_window_picks_newest_paper_by_default(qapp, tmp_path):
    first = _paper(tmp_path, "第一篇.docx")
    second = _paper(tmp_path, "第二篇.docx")
    window, session = _window(qapp, tmp_path, [first, second])
    window._refresh_outline(session)

    combo = window.outline_panel.paper_combo
    assert combo.count() == 2
    assert combo.currentData() == str(second), "默认展示最近生成的那篇"
    assert [n.title for n in session.outline] == [n.title for n in extract_outline(second)]


def test_window_keeps_manual_selection(qapp, tmp_path):
    first = _paper(tmp_path, "第一篇.docx")
    second = _paper(tmp_path, "第二篇.docx")
    window, session = _window(qapp, tmp_path, [first, second])
    window._refresh_outline(session)

    combo = window.outline_panel.paper_combo
    combo.setCurrentIndex(combo.findData(str(first)))
    assert session.outline_source == str(first)

    window._refresh_outline(session)                  # 没有新论文时不该抢走选中项
    assert session.outline_source == str(first)
    assert window.outline_panel.paper_combo.currentData() == str(first)


def test_window_empty_session_state(qapp, tmp_path):
    window, session = _window(qapp, tmp_path, [])
    window._refresh_outline(session)
    assert not window.outline_panel.paper_combo.isVisibleTo(window.outline_panel)
    assert "还没有论文文件" in window.outline_panel.empty_label.text()
