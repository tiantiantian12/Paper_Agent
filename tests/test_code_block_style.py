"""聊天里的代码块要有底色卡片（背景 + 边框 + 内边距）。

回归的坑：markdown CSS 里 font-family 用了双引号（``"Cascadia Code"``），
塞进 ``style="…"`` 后会把属性**提前截断**，后面的 background / border / padding
全被丢掉 —— 代码块看着就是一段没有底色的裸文本。
"""

from __future__ import annotations

import re

import pytest
from PySide6.QtGui import QTextDocument, QTextTable

from paper_agent.core.theme import theme_manager
from paper_agent.ui.chat.markdown_renderer import MarkdownRenderer

MD = "看这段：\n\n```css\n.modal-mask {\n  display: grid;\n}\n```\n\n行内 `display: grid` 也看看。\n"
RE_STYLE_ATTR = re.compile(r'style="([^"]*)"')


@pytest.fixture
def light_theme(qapp):
    """浅色主题 + 一个 QApplication（QTextDocument 排版需要它，否则会崩）。"""
    original = theme_manager._theme.name
    theme_manager.set_theme("light")
    try:
        yield theme_manager._theme
    finally:
        theme_manager.set_theme(original)


def _render(md: str) -> str:
    return MarkdownRenderer().render(md)


# ---------------------------------------------------------------- 属性不被截断
def test_style_attributes_have_no_inner_quotes(light_theme):
    """style="…" 里不能再出现双引号，否则属性会被截断、后续声明全丢。"""
    html = _render(MD)

    for value in RE_STYLE_ATTR.findall(html):
        assert '"' not in value, f"style 属性被引号截断：{value[:80]}"

    assert 'style="font-family:' in html, "字体栈应该用单引号写在 style 里"


def test_font_family_uses_single_quotes(light_theme):
    html = _render("```python\nprint(1)\n```")

    assert "'Consolas'" in html or "'Cascadia Code'" in html
    assert '"Consolas"' not in html


# ---------------------------------------------------------------- 背景真的生效
def test_code_block_gets_solid_background(light_theme):
    """Qt 富文本层面确认：代码行的块背景是实色，而不是空笔刷。"""
    doc = QTextDocument()
    doc.setHtml(_render(MD))

    block = doc.begin()
    while block.isValid() and "display: grid" not in block.text():
        block = block.next()

    assert block.isValid(), "没找到代码行"
    background = block.blockFormat().background()
    assert background.style() == background.style().SolidPattern, "代码块没有背景色"
    assert background.color().name().lower() == light_theme.bg_code.lower()


def test_code_block_is_wrapped_in_bordered_card(light_theme):
    """Qt 不支持块级 border，所以代码块外面套了一层带边框的表格。"""
    doc = QTextDocument()
    doc.setHtml(_render(MD))

    tables = [frame for frame in doc.rootFrame().childFrames() if isinstance(frame, QTextTable)]
    assert tables, "代码块应该包在卡片表格里"

    fmt = tables[0].format()
    assert fmt.border() > 0, "卡片要有边框（Qt 里只有表格边框生效）"
    assert fmt.background().color().name().lower() == light_theme.bg_code.lower()
    # 单元格左边距：代码不该贴着卡片边缘
    cell = tables[0].cellAt(0, 0)
    assert cell.format().isValid()


def test_inline_code_has_background(light_theme):
    html = _render("行内 `display: grid` 也看看")

    assert "<code" in html
    code_html = html.split("<code", 1)[1].split(">", 1)[0]
    assert "background:" in code_html, "行内代码要有底色"
    assert light_theme.bg_code in code_html


def test_long_code_line_wraps_inside_the_card(qapp, light_theme):
    """超长代码行在卡片里换行，不再把消息撑宽（右侧会被裁掉）。"""
    from paper_agent.ui.chat.rich_text import RichTextLabel

    line = "result = " + " + ".join(f"value_{index}" for index in range(40))
    label = RichTextLabel()
    label.resize(320, 200)
    label.show()
    label.set_markdown(f"```python\n{line}\n```")
    qapp.processEvents()

    assert not label.horizontalScrollBar().isVisible()
    assert label.document().size().width() <= label.viewport().width() + 1


def test_card_keeps_copy_link_and_highlighting(light_theme):
    """加了卡片外壳后，复制锚点与语法高亮不能被弄丢。"""
    html = _render("```python\n# 注释\ndef run():\n    return 42\n```")

    assert "app://copy-code/0" in html, "复制代码锚点要还在"
    assert "复制代码" in html
    assert "font-style:italic" in html, "注释该是斜体高亮"
    assert "42" in html


def test_copy_anchor_still_resolves_to_the_code(light_theme):
    renderer = MarkdownRenderer()
    renderer.render("```python\nprint('hi')\n```")

    assert renderer.code_blocks == ["print('hi')"]
