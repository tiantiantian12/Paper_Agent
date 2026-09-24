"""聊天正文里的地址要能点开。

回归的坑：模型写 ``**http://127.0.0.1:8000**`` 或 ``http://x:8000/api，可直接调试`` 时，
地址把 ``**`` 和中文标点一起吞进 href，链接指向一个打不开的地址 —— 看着是链接，
点了没反应。
"""

from __future__ import annotations

from paper_agent.ui.chat.markdown_renderer import MarkdownRenderer, split_url


def render(text: str) -> str:
    return MarkdownRenderer().render(text)


def test_bold_wrapped_url_keeps_clean_href():
    html = render("浏览器打开 **http://127.0.0.1:8000** 即可")

    assert 'href="http://127.0.0.1:8000"' in html
    assert "**" not in html, "加粗标记不该被吃进地址"


def test_url_stops_before_chinese_punctuation():
    html = render("地址：http://127.0.0.1:8000/api/docs，可直接调试")

    assert 'href="http://127.0.0.1:8000/api/docs"' in html
    assert "可直接调试" not in html.split("</a>")[0]


def test_url_stops_before_fullwidth_bracket():
    html = render("访问 http://127.0.0.1:8000/（前端首页）。")

    assert 'href="http://127.0.0.1:8000/"' in html
    assert "（前端首页）" in html


def test_host_port_without_scheme_is_clickable():
    html = render("前端在 localhost:5173 上跑起来了")

    assert '<a href="http://localhost:5173"' in html
    assert ">localhost:5173</a>" in html


def test_markdown_link_is_not_wrapped_twice():
    """自建链接的 href 不能被裸地址规则再套一层 <a>。"""
    html = render("见 [本地文档](http://127.0.0.1:8000/docs)")

    assert html.count("<a ") == 1
    assert 'href="http://127.0.0.1:8000/docs"' in html


def test_code_span_is_not_linkified():
    html = render("`http://127.0.0.1:8000`")

    assert "<a " not in html
    assert "<code" in html


def test_split_url_keeps_path_and_strips_punctuation():
    assert split_url("http://127.0.0.1:8000/api/docs") == (
        "http://127.0.0.1:8000/api/docs",
        "",
    )
    assert split_url("http://127.0.0.1:8000/,") == ("http://127.0.0.1:8000/", ",")
    assert split_url("http://127.0.0.1:8000**") == ("http://127.0.0.1:8000", "**")
