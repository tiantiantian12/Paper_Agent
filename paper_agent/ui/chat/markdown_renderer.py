"""轻量 Markdown 渲染器（专为 Qt 富文本引擎裁剪）。

支持：标题、段落、粗体/斜体/删除线、行内代码、围栏代码块（含基础高亮与
"复制"锚点）、有序/无序列表、任务列表、引用、表格、分割线、链接与图片。
不依赖第三方库，输出可被 ``QTextDocument`` 直接渲染的 HTML 片段。
"""

from __future__ import annotations

import html
import re
from typing import Iterable

from paper_agent.core.theme import theme_manager

# --------------------------------------------------------------------- 正则
RE_FENCE = re.compile(r"^\s*```+\s*([\w+#.-]*)\s*$")
RE_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
RE_HR = re.compile(r"^\s{0,3}([-*_])\s*(?:\1\s*){2,}$")
RE_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
RE_UL_ITEM = re.compile(r"^(\s*)[-*+]\s+(.*)$")
RE_OL_ITEM = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
RE_TASK_ITEM = re.compile(r"^\[([ xX])\]\s+(.*)$")
RE_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")
RE_CODE_SPAN = re.compile(r"`([^`]+)`")
RE_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
RE_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

# 裸链接：地址里不该出现空白、引号、反引号、星号（Markdown 的 ** 加粗标记）
# 以及中英文标点 —— 出现即说明地址结束。
_URL_TAIL = (
    r"[^\s<>\"'`*"           # 空白、尖括号、引号、反引号、Markdown 加粗星号
    r"\u3000-\u303f"         # 中文标点：。、，；：！？（）《》「」
    r"\uff01-\uff65"         # 全角标点：！（），：；？…
    r"\u2018-\u201f"         # 弯引号
    r")\]}]"                 # 英文右括号等收尾符号
)
# 两种写法合在一个正则里一次替换，否则先替换出来的 <a href="http://…"> 会被
# 第二条规则再匹配一次，套成 <a><a> 的嵌套废墟
RE_AUTOLINK = re.compile(
    rf"(?:https?://|www\.){_URL_TAIL}+"
    rf"|\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d{{2,5}}(?:/{_URL_TAIL}*)?"
)
# 地址末尾常跟着句子标点：`http://x:8000/。` 里的「。」不属于地址
URL_TRAILING = (
    "*_~,.;:!?'\"`)]}>"
    "，。；：！？、）】》”’"
    "（）《》「」【】"
)
# 行内代码 / 自建链接的占位符（先藏起来，免得被后面的规则二次加工）
RE_CODE_SLOT = re.compile(r"\x00CODE(\d+)\x00")
RE_LINK_SLOT = re.compile(r"\x00LINK(\d+)\x00")
RE_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
RE_ITALIC = re.compile(r"(?<![\w*])([*_])(?=\S)(.+?)(?<=\S)\1(?![\w*])")
RE_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
RE_HTML_TAG = re.compile(r"<[^>]+>")

KEYWORDS: dict[str, set[str]] = {
    "python": {
        "def", "class", "return", "if", "elif", "else", "for", "while", "in", "is", "not",
        "and", "or", "import", "from", "as", "with", "try", "except", "finally", "raise",
        "lambda", "None", "True", "False", "self", "pass", "yield", "global", "assert",
        "del", "break", "continue", "async", "await", "match", "case",
    },
    "cpp": {
        "int", "float", "double", "char", "bool", "void", "auto", "const", "class", "struct",
        "public", "private", "protected", "return", "if", "else", "for", "while", "switch",
        "case", "break", "continue", "new", "delete", "template", "typename", "namespace",
        "using", "include", "define", "nullptr", "true", "false", "this",
    },
    "js": {
        "function", "const", "let", "var", "return", "if", "else", "for", "while", "class",
        "import", "export", "from", "default", "new", "this", "async", "await", "try",
        "catch", "finally", "throw", "typeof", "null", "undefined", "true", "false",
    },
    "json": {"true", "false", "null"},
    "sql": {
        "select", "from", "where", "insert", "into", "values", "update", "set", "delete",
        "join", "left", "right", "inner", "outer", "on", "group", "by", "order", "limit",
        "create", "table", "as", "and", "or", "not", "null", "distinct",
    },
    "bash": {"if", "then", "else", "fi", "for", "in", "do", "done", "while", "case", "esac", "echo"},
}
KEYWORDS["c"] = KEYWORDS["cpp"] | {"printf", "malloc", "sizeof"}
KEYWORDS["java"] = KEYWORDS["cpp"] | {"public", "static", "final", "package", "import", "extends"}
KEYWORDS["typescript"] = KEYWORDS["js"] | {"interface", "type", "enum", "implements"}
KEYWORDS["py"] = KEYWORDS["python"]
KEYWORDS["shell"] = KEYWORDS["bash"]
KEYWORDS["sh"] = KEYWORDS["bash"]

TOKEN_RE = re.compile(
    r"""
    (?P<comment>\#[^\n]*|//[^\n]*|/\*.*?\*/)
  | (?P<string>\"\"\"[\s\S]*?\"\"\"|'''[\s\S]*?'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
  | (?P<number>\b\d+(?:\.\d+)?\b)
  | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)


def _css(key: str) -> str:
    """取某类元素的 CSS，顺便把双引号换成单引号。

    ``font-family:"Cascadia Code", "Consolas"`` 里的双引号会**提前结束**
    ``style="…"`` 这个 HTML 属性，后面的声明（背景色 / 边框 / 内边距）会被整段丢掉 ——
    代码块一直「没有底色」就是这么来的。CSS 里单双引号等价，换掉即可。
    """
    return theme_manager.markdown_css.get(key, "").replace('"', "'")


def split_url(raw: str) -> tuple[str, str]:
    """拆开地址与紧跟其后的标点，返回 ``(地址, 标点)``。

    ``地址：http://127.0.0.1:8000/api，可直接调试`` 里，「，可直接调试」不是
    地址的一部分；``**http://x:8000**`` 里的 ``**`` 是加粗标记，也不该吃进 href，
    否则链接会指到一个打不开的地址（点了没反应）。
    """
    url = raw
    suffix = ""
    while url and url[-1] in URL_TRAILING:
        suffix = url[-1] + suffix
        url = url[:-1]
    return url, suffix


# Qt 的富文本引擎不支持 word-wrap/overflow-wrap，超长连续串（路径、URL、
# base64、无空格的中文长句）会把文档撑得比视口还宽，右侧被直接裁掉。
# 这里给长串插入零宽空格，让它在任意位置可断行；复制代码用的是原文，
# 不受影响。
ZWSP = "\u200b"
SOFT_WRAP_LIMIT = 28


def _linkify(match: re.Match) -> str:
    """把裸地址变成可点的 <a>；省略协议时补上 http:// 才能被浏览器打开。"""
    url, suffix = split_url(match.group(0))
    if not url:
        return match.group(0)
    href = url if "://" in url else f"http://{url}"
    return f'<a href="{href}" style="{_css("a")}">{url}</a>{suffix}'


def soften_long_tokens(text: str, limit: int = SOFT_WRAP_LIMIT) -> str:
    """给超长连续串插入零宽空格，使富文本能在此换行。"""
    if not text:
        return text
    out: list[str] = []
    run = 0
    for char in text:
        if char.isspace():
            run = 0
            out.append(char)
            continue
        run += 1
        out.append(char)
        if run % limit == 0:
            out.append(ZWSP)
    return "".join(out)


class MarkdownRenderer:
    """把 Markdown 文本渲染为 QTextDocument 可用的 HTML。"""

    def __init__(self) -> None:
        self.code_blocks: list[str] = []

    # ------------------------------------------------------------------ 入口
    def render(self, text: str) -> str:
        self.code_blocks = []
        if not text:
            return ""
        body = self._render_blocks(text.replace("\r\n", "\n").split("\n"))
        return f'<div style="{_css("body")}">{body}</div>'

    # ------------------------------------------------------------------ 块级
    def _render_blocks(self, lines: list[str]) -> str:
        out: list[str] = []
        index = 0
        total = len(lines)

        while index < total:
            line = lines[index]

            # 空行
            if not line.strip():
                index += 1
                continue

            # 围栏代码块
            fence = RE_FENCE.match(line)
            if fence:
                lang = fence.group(1)
                index += 1
                buffer: list[str] = []
                while index < total and not RE_FENCE.match(lines[index]):
                    buffer.append(lines[index])
                    index += 1
                index += 1
                out.append(self._code_block("\n".join(buffer), lang))
                continue

            # 标题
            heading = RE_HEADING.match(line)
            if heading:
                level = min(len(heading.group(1)), 4)
                out.append(
                    f'<h{level} style="{_css(f"h{level}")}">'
                    f"{self._inline(heading.group(2).strip())}</h{level}>"
                )
                index += 1
                continue

            # 分割线
            if RE_HR.match(line):
                out.append(f'<hr style="{_css("hr")}"/>')
                index += 1
                continue

            # 引用
            if RE_QUOTE.match(line):
                buffer = []
                while index < total and RE_QUOTE.match(lines[index]):
                    buffer.append(RE_QUOTE.match(lines[index]).group(1))  # type: ignore[union-attr]
                    index += 1
                inner = self._render_blocks(buffer)
                out.append(f'<blockquote style="{_css("blockquote")}">{inner}</blockquote>')
                continue

            # 表格
            if line.strip().startswith("|") and index + 1 < total and RE_TABLE_SEP.match(lines[index + 1]):
                table, index = self._table(lines, index)
                out.append(table)
                continue

            # 列表
            if RE_UL_ITEM.match(line) or RE_OL_ITEM.match(line):
                block, index = self._list_block(lines, index)
                out.append(block)
                continue

            # 段落
            buffer = []
            while index < total and lines[index].strip() and not self._is_block_start(lines[index]):
                buffer.append(lines[index])
                index += 1
            if buffer:
                paragraph = self._inline("\n".join(buffer)).replace("\n", "<br/>")
                out.append(f'<p style="{_css("p")}">{paragraph}</p>')

        return "".join(out)

    @staticmethod
    def _is_block_start(line: str) -> bool:
        return bool(
            RE_FENCE.match(line)
            or RE_HEADING.match(line)
            or RE_HR.match(line)
            or RE_QUOTE.match(line)
            or RE_UL_ITEM.match(line)
            or RE_OL_ITEM.match(line)
        )

    # ------------------------------------------------------------------ 列表
    def _list_block(self, lines: list[str], start: int) -> tuple[str, int]:
        """渲染（支持嵌套的）列表，返回 HTML 与新的行号。"""
        index = start
        total = len(lines)
        items: list[tuple[int, str, bool]] = []  # (indent, html, ordered)
        ordered = RE_OL_ITEM.match(lines[start]) is not None

        while index < total:
            line = lines[index]
            if not line.strip():
                # 空行后若仍为同类型列表项则继续，否则结束
                if index + 1 < total and (
                    RE_UL_ITEM.match(lines[index + 1]) or RE_OL_ITEM.match(lines[index + 1])
                ):
                    index += 1
                    continue
                break

            ul = RE_UL_ITEM.match(line)
            ol = RE_OL_ITEM.match(line)
            if ul:
                indent = len(ul.group(1).replace("\t", "  ")) // 2
                content = ul.group(2)
                items.append((indent, self._list_item_html(content), False))
                index += 1
                continue
            if ol and ordered:
                indent = len(ol.group(1).replace("\t", "  ")) // 2
                items.append((indent, self._list_item_html(ol.group(3)), True))
                index += 1
                continue
            break

        html_text = self._build_list(items, ordered, 0, 0)[0]
        return html_text, index

    def _list_item_html(self, content: str) -> str:
        task = RE_TASK_ITEM.match(content.strip())
        if task:
            checked = task.group(1).lower() == "x"
            mark = "☑" if checked else "☐"
            rest = self._inline(task.group(2))
            style = f'{_css("muted")}' if checked else ""
            return f'{mark} <span style="{style}">{rest}</span>'
        return self._inline(content)

    def _build_list(
        self, items: list[tuple[int, str, bool]], ordered: bool, level: int, index: int
    ) -> tuple[str, int]:
        """按缩进层级递归构造 <ul>/<ol>。"""
        tag = "ol" if ordered else "ul"
        parts: list[str] = []
        while index < len(items):
            indent, content, _ = items[index]
            if indent > level:
                child, index = self._build_list(items, ordered, indent, index)
                # 子列表挂到上一个 <li> 内部
                if parts and parts[-1].endswith("</li>"):
                    parts[-1] = parts[-1][: -len("</li>")] + child + "</li>"
                else:
                    parts.append(child)
                continue
            if indent < level:
                break
            parts.append(f'<li style="{_css("li")}">{content}</li>')
            index += 1
        inner = "".join(parts)
        return f'<{tag} style="{_css(tag)}">{inner}</{tag}>', index

    # ------------------------------------------------------------------ 表格
    def _table(self, lines: list[str], start: int) -> tuple[str, int]:
        header = [c.strip() for c in lines[start].strip().strip("|").split("|")]
        index = start + 2  # 跳过分隔行
        rows: list[list[str]] = []
        while index < len(lines) and lines[index].strip().startswith("|"):
            rows.append([c.strip() for c in lines[index].strip().strip("|").split("|")])
            index += 1

        head_html = "".join(
            f'<th style="{_css("th")}">{self._inline(soften_long_tokens(c))}</th>' for c in header
        )
        body_html = ""
        for row in rows:
            cells = "".join(
                f'<td style="{_css("td")}">{self._inline(soften_long_tokens(c))}</td>' for c in row
            )
            body_html += f"<tr>{cells}</tr>"

        # width="100%"：让表格按可用宽度自适应，而不是按内容宽度撑出去被裁掉
        table = (
            f'<table width="100%" style="{_css("table")}" cellspacing="0" cellpadding="0" border="0">'
            f"<thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table>"
        )
        return f'<div style="{_css("p")}">{table}</div>', index

    # ------------------------------------------------------------------ 代码
    def _code_block(self, code: str, lang: str) -> str:
        index = len(self.code_blocks)
        self.code_blocks.append(code)

        label = (lang or "text").lower()
        # 长行先插零宽空格再高亮：pre-wrap 只按空格断行，不断长串
        highlighted = self._highlight(soften_long_tokens(code), label)
        copy_link = (
            f'<a href="app://copy-code/{index}" style="{_css("copy_link")}">复制代码</a>'
        )
        header = (
            f'<div style="{_css("code_lang")}margin-bottom:2px;">'
            f"{html.escape(label)} &nbsp;&nbsp; {copy_link}</div>"
        )
        # 外面套一层单格表格当「代码卡片」：Qt 富文本不支持块级元素的 border /
        # padding（写了也不生效，代码会贴着左边缘、没有底色框），
        # 而表格的边框与单元格内边距是真支持的。
        return (
            f'<table width="100%" cellspacing="0" cellpadding="0" border="0" '
            f'style="{_css("code_card")}"><tr><td style="{_css("code_cell")}">'
            f"{header}<pre style=\"{_css('pre')}\">{highlighted}</pre>"
            f"</td></tr></table>"
        )

    def _highlight(self, code: str, lang: str) -> str:
        keywords = KEYWORDS.get(lang, KEYWORDS["python"])
        escaped = html.escape(code)
        result: list[str] = []
        position = 0

        for match in TOKEN_RE.finditer(escaped):
            result.append(escaped[position : match.start()])
            kind = match.lastgroup
            text = match.group()
            if kind == "comment":
                result.append(f'<span style="{_css("comment")}">{text}</span>')
            elif kind == "string":
                result.append(f'<span style="{_css("string")}">{text}</span>')
            elif kind == "number":
                result.append(f'<span style="{_css("number")}">{text}</span>')
            elif kind == "word":
                if text in keywords:
                    result.append(f'<span style="{_css("keyword")}">{text}</span>')
                else:
                    tail = escaped[match.end() : match.end() + 1]
                    if tail == "(":
                        result.append(f'<span style="{_css("function")}">{text}</span>')
                    else:
                        result.append(text)
            else:
                result.append(text)
            position = match.end()

        result.append(escaped[position:])
        return "".join(result)

    # ------------------------------------------------------------------ 行内
    def _inline(self, text: str) -> str:
        if not text:
            return ""

        # 1. 转义
        escaped = html.escape(text, quote=False)

        # 2. 行内代码先占位，避免被其它规则处理
        code_spans: list[str] = []

        def _stash_code(match: re.Match) -> str:
            code_spans.append(match.group(1))
            return f"\x00CODE{len(code_spans) - 1}\x00"

        escaped = RE_CODE_SPAN.sub(_stash_code, escaped)

        # 3. 图片 / 链接
        anchors: list[str] = []

        def _stash_anchor(markup: str) -> str:
            anchors.append(markup)
            return f"\x00LINK{len(anchors) - 1}\x00"

        escaped = RE_IMAGE.sub(
            lambda m: _stash_anchor(
                f'<img src="{html.escape(m.group(2), quote=True)}" style="{_css("img")}"/>'
            ),
            escaped,
        )
        escaped = RE_LINK.sub(
            lambda m: _stash_anchor(
                f'<a href="{html.escape(m.group(2), quote=True)}" style="{_css("a")}">'
                f"{m.group(1)}</a>"
            ),
            escaped,
        )
        # 裸地址（含省略协议的 localhost:端口）自动转成可点链接
        escaped = RE_AUTOLINK.sub(_linkify, escaped)
        # 还原上面藏起来的图片 / 链接：必须放在裸地址替换之后，否则 href 会被再套一层
        escaped = RE_LINK_SLOT.sub(
            lambda m: anchors[int(m.group(1))] if int(m.group(1)) < len(anchors) else m.group(0),
            escaped,
        )

        # 4. 强调
        escaped = RE_BOLD.sub(lambda m: f'<b style="{_css("strong")}">{m.group(2)}</b>', escaped)
        escaped = RE_STRIKE.sub(lambda m: f'<s style="{_css("del")}">{m.group(1)}</s>', escaped)
        escaped = RE_ITALIC.sub(lambda m: f'<i style="{_css("em")}">{m.group(2)}</i>', escaped)

        # 5. 还原行内代码
        def _restore(match: re.Match) -> str:
            index = int(match.group(1))
            if index >= len(code_spans):
                return match.group(0)
            return f'<code style="{_css("code_inline")}">{html.escape(code_spans[index], quote=False)}</code>'

        escaped = RE_CODE_SLOT.sub(_restore, escaped)
        return escaped


def render_markdown(text: str, renderer: MarkdownRenderer | None = None) -> str:
    """便捷函数：渲染 Markdown 文本。"""
    return (renderer or MarkdownRenderer()).render(text)


def strip_markdown(text: str) -> str:
    """去掉 Markdown 标记，用于复制为纯文本。"""
    plain = RE_CODE_SPAN.sub(r"\1", text)
    plain = RE_IMAGE.sub(r"\1", plain)
    plain = RE_LINK.sub(r"\1 (\2)", plain)
    plain = RE_HTML_TAG.sub("", plain)
    for marker in ("**", "__", "~~", "`", "*", "#", ">"):
        plain = plain.replace(marker, "")
    return plain
