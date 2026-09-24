"""从真实文件里解析论文结构（章节层级 + 字数）。

「论文结构」面板用它展示**所选论文的真实章节**，而不是写死的样例。
支持模型生成的 ``.docx`` / ``.pdf``，也支持用户上传的同格式文件。
"""

from __future__ import annotations

import re
from pathlib import Path

from paper_agent.core.models import ChatSession, OutlineNode, PaperFile
from paper_agent.utils.files import file_size, human_readable_size
from paper_agent.utils.text import format_datetime

PAPER_SUFFIXES = {".docx", ".pdf"}
MAX_TITLE_LENGTH = 60
CACHE_LIMIT = 12

# Word 内置标题样式：Heading 1 / 标题 1
_STYLE_HEADING = re.compile(r"(?:heading|标题)\s*([1-9])", re.IGNORECASE)
# 中文论文常见章节写法
_CHAPTER = re.compile(r"^第\s*[一二三四五六七八九十百零〇\d]+\s*[章节篇]")
_ENUMERATED = re.compile(r"^[（(]?[一二三四五六七八九十]+[）)、.．]\s*(\S.*)$")
# 多级编号：1.1 / 2.3.1；限制 1-2 位数字，避免把 "2026 年" 之类当标题
_NUMBERED = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})[ \t、.．:：]+(\S.{0,40})$")
# 明显不是标题的数字开头（1.5 倍、2026 年、30% 等）
_NOT_HEADING = re.compile(
    r"^\d+(?:\.\d+)?\s*(?:年|月|日|倍|个|次|人|元|万|亿|%|％|条|页|章|节|分|秒)"
)
# Word 目录（TOC）样式：toc 1 / 目录 1 / TOC 2 / Contents
_TOC_STYLE = re.compile(r"^(?:toc|目录|目次|contents)", re.IGNORECASE)
# 目录条目特征：以制表符 / 点线 / 多个空格结尾并跟页码
_TOC_LINE = re.compile(r"(?:[\t.·…]{1,}|\s{2,})\s*\d{1,4}\s*$")
# 目录 / 目次这类导航标题：不是论文章节
_NAVIGATION_TITLES = {"目录", "目次", "contents", "table of contents"}
_TAIL_PUNCTUATION = ("。", "；", "，", "、", ".", ",", ";", "?", "？", "!", "！")
# 论文里常见的无编号章节（PDF 抽不出字号，只能按整行匹配）
_SECTION_HEADINGS = {
    "摘要", "中文摘要", "英文摘要", "abstract", "目录", "引言", "绪论",
    "结论", "结束语", "总结与展望", "参考文献", "致谢", "附录",
}

# 解析结果缓存：key = (路径, mtime)
_CACHE: dict[tuple[str, float], list[dict]] = {}


def paper_candidates(session: ChatSession) -> list[PaperFile]:
    """本会话里可作为「论文」的文件：``.docx`` / ``.pdf``，按时间倒序。"""
    return [
        item
        for item in session.paper_files()
        if Path(item.path).suffix.lower() in PAPER_SUFFIXES
    ]


def extract_outline(path: str | Path) -> list[OutlineNode]:
    """解析真实章节结构；文件缺失或解析不出章节时返回空列表。"""
    target = Path(path)
    if not target.exists():
        return []
    suffix = target.suffix.lower()
    if suffix not in PAPER_SUFFIXES:
        return []

    try:
        key = (str(target), target.stat().st_mtime)
    except OSError:
        return []

    if key not in _CACHE:
        try:
            if suffix == ".docx":
                nodes = _docx_outline(target)
            else:
                nodes = _pdf_outline(target)
        except Exception:      # noqa: BLE001 - 解析失败按「无结构」处理
            # 不写缓存：文件可能正被写入 / 临时锁定，缓存空结果会让面板一直空着
            return []
        _CACHE[key] = [node.to_dict() for node in nodes]
        for stale in list(_CACHE)[: max(0, len(_CACHE) - CACHE_LIMIT)]:
            _CACHE.pop(stale, None)
    # 返回副本，避免面板 / 会话持有同一批对象
    return [OutlineNode.from_dict(item) for item in _CACHE[key]]


def source_label(item: PaperFile) -> str:
    """下拉框里的展示名：文件名 + 来源。"""
    return f"{item.name} · {'模型生成' if item.source == 'model' else '我上传'}"


def extract_markdown_outline(text: str) -> list[OutlineNode]:
    """从正在流式输出的 Markdown 里提取章节结构（作文尚未落盘时的实时预览）。

    只认 ``# / ## / ###`` 标题，跳过代码块围栏；最后一个章节标记为「进行中」。
    """
    if not text or "#" not in text:
        return []

    builder = _OutlineBuilder()
    in_fence = False
    for raw in text.split("\n"):
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            builder.add_heading(heading.group(2).strip(), min(len(heading.group(1)), 3))
        else:
            builder.add_text(stripped)

    nodes = builder.build()
    if not nodes:
        return []
    cursor = nodes[-1]
    while cursor.children:
        cursor = cursor.children[-1]
    cursor.state = "doing"                 # 正在写到这里
    return nodes


def outline_signature(nodes: list[OutlineNode]) -> tuple:
    """结构的指纹（含所有层级的标题、字数与状态），用于判断内容是否真的变了。

    状态也要算进来：否则「最后一节 进行中 → 已完成」这种变化检测不到，
    面板会停在旧标记上。
    """
    flat: list[tuple[str, int, str]] = []

    def walk(items: list[OutlineNode]) -> None:
        for node in items:
            flat.append((node.title, node.words, node.state))
            walk(node.children)

    walk(nodes)
    return tuple(flat)


# ---------------------------------------------------------------- 会话工作区
def workspace_snapshot(session: ChatSession) -> list[dict]:
    """本会话工作区文件清单（给模型的纯数据，按时间倒序）。"""
    return [
        {
            "name": item.name,
            "path": item.path,
            "source": item.source,
            "size": file_size(item.path),
            "created_at": item.created_at,
        }
        for item in session.paper_files()
    ]


def format_workspace_file(item: dict) -> str:
    """工作区文件的一行描述：文件名 + 来源 + 时间 + 大小 + 完整路径。"""
    label = "模型生成" if item.get("source") == "model" else "我上传"
    parts = [label]
    stamp = format_datetime(item.get("created_at", 0))
    if stamp:
        parts.append(stamp)
    size = item.get("size") or 0
    if size:
        parts.append(human_readable_size(size))
    return f"- {item.get('name') or '文件'}（{' · '.join(parts)}）路径：{item.get('path') or ''}"


# ---------------------------------------------------------------- 树构建
class _OutlineBuilder:
    """按出现顺序喂入标题与正文，产出带字数的章节树。"""

    def __init__(self) -> None:
        self._roots: list[OutlineNode] = []
        self._stack: list[tuple[int, OutlineNode]] = []
        self._current: OutlineNode | None = None

    def add_heading(self, title: str, level: int) -> None:
        node = OutlineNode(title=title, state="todo")
        while self._stack and self._stack[-1][0] >= level:
            self._stack.pop()
        if self._stack:
            self._stack[-1][1].children.append(node)
        else:
            self._roots.append(node)
        self._stack.append((level, node))
        self._current = node

    def add_text(self, text: str) -> None:
        if self._current is not None:
            self._current.words += _count_words(text)

    def build(self) -> list[OutlineNode]:
        for node in self._roots:
            _finalize(node)
        return self._roots


def _count_words(text: str) -> int:
    """中文字数按字符计（忽略空白）。"""
    return len(re.sub(r"\s+", "", text or ""))


def _finalize(node: OutlineNode) -> str:
    """自底向上推断状态：有正文或子节点全部完成 → 已完成。"""
    for child in node.children:
        _finalize(child)
    if node.children:
        states = {child.state for child in node.children}
        if node.words or states == {"done"}:
            node.state = "done"
        elif "done" in states:
            node.state = "doing"
        else:
            node.state = "todo"
    else:
        node.state = "done" if node.words else "todo"
    return node.state


def _clean_structure(nodes: list[OutlineNode]) -> list[OutlineNode]:
    """去掉 Word 的「目录」和文档标题：它们是导航信息，不是论文章节。"""
    kept: list[OutlineNode] = []
    for node in nodes:
        # 「目录」是导航信息（Word 目录域/条目都在这下面），一律不算章节
        if node.title.strip().lower() in _NAVIGATION_TITLES:
            continue
        node.children = _clean_structure(node.children)
        kept.append(node)

    # 文档标题（Word 里常被设成 Heading 1）：无正文、无子节、名字不是章节也不是
    # 「摘要/目录」这类固定节，且紧跟一个真正的章节 → 视为标题去掉
    if len(kept) >= 2 and not kept[0].words and not kept[0].children:
        title = kept[0].title.strip()
        plain = title.lower() not in _SECTION_HEADINGS and not (
            _CHAPTER.match(title)
            or _NOT_HEADING.match(title)
            or _NUMBERED.match(title)
            or _ENUMERATED.match(title)
        )
        if plain and _text_heading_level(kept[1].title.strip(), False):
            kept = kept[1:]
    return kept


def _text_heading_level(text: str, chapter_seen: bool) -> int:
    """按文本特征判断标题层级；0 表示不是标题。"""
    if not text or len(text) > MAX_TITLE_LENGTH:
        return 0
    if text.endswith(_TAIL_PUNCTUATION):
        return 0
    if _CHAPTER.match(text):
        return 1
    if text.strip().lower() in _SECTION_HEADINGS:
        return 1
    if _NOT_HEADING.match(text):
        return 0
    numbered = _NUMBERED.match(text)
    if numbered:
        return min(numbered.group(1).count(".") + 1, 3)
    if _ENUMERATED.match(text):
        return 2 if chapter_seen else 1
    return 0


# ---------------------------------------------------------------- docx
def _docx_outline(path: Path) -> list[OutlineNode]:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(str(path))
    builder = _OutlineBuilder()
    chapter_seen = False

    for element in document.element.body.iterchildren():
        if element.tag == qn("w:p"):
            paragraph = Paragraph(element, document)
            text = paragraph.text.strip()
            if not text or _is_toc_entry(paragraph.style, text):
                continue          # 目录条目既不是标题，也不计入正文字数
            level = _heading_level(paragraph, text, chapter_seen)
            if level:
                builder.add_heading(text, level)
                chapter_seen = chapter_seen or level == 1
            else:
                builder.add_text(text)
        elif element.tag == qn("w:tbl"):
            table = Table(element, document)
            builder.add_text(" ".join(cell.text for row in table.rows for cell in row.cells))

    return _clean_structure(builder.build())


def _is_toc_entry(style, text: str) -> bool:
    """是否是 Word 自动目录里的一行（样式 ``toc N`` 或「标题+页码」写法）。"""
    name = (getattr(style, "name", "") or "").strip()
    return bool(_TOC_STYLE.match(name)) or bool(_TOC_LINE.search(text))


def _heading_level(paragraph, text: str, chapter_seen: bool) -> int:
    """优先用 Word 样式判断层级（生成时用的是内置 Heading N）。"""
    name = (getattr(paragraph.style, "name", "") or "").strip()
    match = _STYLE_HEADING.search(name)
    if match:
        return min(int(match.group(1)), 3)
    return _text_heading_level(text, chapter_seen)


# ---------------------------------------------------------------- pdf
def _pdf_outline(path: Path) -> list[OutlineNode]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    builder = _OutlineBuilder()
    chapter_seen = False

    for page in reader.pages:
        text = page.extract_text() or ""
        for raw in text.split("\n"):
            line = raw.strip()
            if not line or _TOC_LINE.search(line):
                continue          # PDF 目录页同样跳过（标题….页码）
            level = _text_heading_level(line, chapter_seen)
            if level:
                builder.add_heading(line, level)
                chapter_seen = chapter_seen or level == 1
            else:
                builder.add_text(line)

    return _clean_structure(builder.build())
