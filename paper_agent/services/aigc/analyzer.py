"""AIGC 检测引擎：文档抽取 → 分句 → 特征打分 → 结果聚合。

纯逻辑模块（不依赖 Qt），便于单测；界面层见 ``ui/dialogs/aigc_dialog.py``，
报告导出见同目录的 ``report.py``。

算法说明
--------
本模块是一个**离线启发式**检测器，思路与主流学术 AIGC 检测系统一致
（统计特征初筛 + 多维加权精判），但不依赖任何外部模型服务：

1. **可预测性（困惑度代理）**：用文档自身的字符 / 单词 bigram 语言模型估计
   每句的平均信息量（bits/token），再折算成它在全文中的相对位置。
   AI 倾向选择高概率词、复用常见搭配，句子信息量偏低；人类写作会插入
   低概率词与个性化表达，信息量偏高。
2. **搭配复用率**：句子里的二元组有多少在全文别处也出现过。AI 反复使用
   同一套搭配，人类写作的搭配更"一次性"。
3. **模板化密度**：列举式连接词与学术套话的命中密度（"首先/其次/综上/
   值得注意的是/本研究..."）。
4. **篇幅规整度**：句长是否落在 AI 常见的「舒适区」。
5. **突发性（Burstiness）**：句长相对全文的偏离程度。人类写作长短句交错，
   AI 输出的句长分布更均匀。

五个维度加权得到每句 0~1 的 AI 倾向分，再由全文的**语言冗余度**
（``1 - H(二元模型) / H(一元模型)``）与句长变异系数做一次**文档级校准**
（通篇可预测、句长整齐的文稿整体上调，反之整体下调）。

分句后还有一步**短句合并**（``merge_short_sentences``）：连续过短的句子会顺着
往后并，凑到判定长度再作为一个整体打分。短句要保护是因为它 token 太少、统计
特征不可靠，但「不可靠」不等于「安全」——AI 完全可以通篇用短句写作，只放过
短句就会整段漏检。合并到没有邻居可并时（比如整段就一句）才按人工处理。

最后按阈值分档并折算成 AIGC 率：

**AIGC 率 = (AI 句字数 × 1.0 + 疑似句字数 × 0.5) / 参与统计的总字数**

按档位折算（而不是直接对倾向分取平均）是为了让 AIGC 率与界面上的三档计数
严格一致 —— 「AI 0 句 / 疑似 0 句」就一定是 0%。各句倾向分的加权平均另存为
``average_score``，在报告里以「平均倾向」给出，用于交叉参考。

注意：这是启发式估算，与知网 / 格子达等官方系统的语料、模型与阈值都不相同，
结果仅供参考，不能作为学术判定依据。
"""

from __future__ import annotations

import math
import random
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ENGINE_NAME = "Paper Agent 本地启发式引擎"

# ---------------------------------------------------------------- 判定档位
LEVEL_AI = "ai"
LEVEL_SUSPECT = "suspect"
LEVEL_HUMAN = "human"
LEVEL_ORDER = (LEVEL_AI, LEVEL_SUSPECT, LEVEL_HUMAN)

LEVEL_LABELS = {
    LEVEL_AI: "AI 生成",
    LEVEL_SUSPECT: "疑似 AI",
    LEVEL_HUMAN: "人工撰写",
}

# AIGC 率按档位折算：AI 全额计入，疑似按一半计入，人工不计入。
#
# 早期版本直接拿「每句倾向分按字数加权平均」当 AIGC 率，结果和三档计数对不上：
# 一批句子的分数都卡在疑似阈值下方（比如 0.30~0.44）时，没有一句被标成 AI / 疑似，
# 平均值却仍有 30% 左右；而「可预测性」「突发性」两个维度用的是相对全文的位置，
# 句子处于平均位置就记 0.5，纯人工文稿的基线因此也被抬到 20~30%。
#
# 改成档位折算后，AIGC 率与界面上的三档计数严格一致：
# 「AI 0 句 / 疑似 0 句」就一定是 0%，红色加黄色的部分就是 AIGC 率。
# 连续分数仍然保留，在报告里以「平均倾向」的形式给出，供交叉参考。
LEVEL_WEIGHTS = {
    LEVEL_AI: 1.0,
    LEVEL_SUSPECT: 0.5,
    LEVEL_HUMAN: 0.0,
}

# 报告 / 界面共用的档位颜色（导出 PDF 时也用这一套，保证两处一致）
LEVEL_COLORS = {
    LEVEL_AI: "#C0392B",
    LEVEL_SUSPECT: "#B26A00",
    LEVEL_HUMAN: "#6B7280",
}

# ---------------------------------------------------------------- 灵敏度
# ai / suspect 为句子分数阈值：越高越不容易被标成 AI
SENSITIVITY_PRESETS: dict[str, dict] = {
    "lenient": {
        "label": "宽松",
        "ai": 0.72,
        "suspect": 0.55,
        "desc": "只标出高度可疑的句子，误判少",
    },
    "standard": {
        "label": "标准",
        "ai": 0.62,
        "suspect": 0.45,
        "desc": "兼顾召回与误判，推荐默认使用",
    },
    "strict": {
        "label": "严格",
        "ai": 0.53,
        "suspect": 0.37,
        "desc": "宁可多标，适合交稿前自查",
    },
}
DEFAULT_SENSITIVITY = "standard"

# 段落类型：参与检测的正文，与只留档不判定的标题 / 参考文献
KIND_BODY = "body"
KIND_HEADING = "heading"
KIND_REFERENCE = "reference"

# ---------------------------------------------------------------- 支持的文件
SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown", ".docx", ".pdf"}
DOCUMENT_FILTER = (
    "文档 (*.docx *.pdf *.txt *.md *.markdown);;"
    "Word 文档 (*.docx);;PDF (*.pdf);;文本 (*.txt *.md *.markdown);;所有文件 (*.*)"
)

# ---------------------------------------------------------------- 特征权重
WEIGHTS = {
    "pred": 0.30,     # 可预测性（相对全文）
    "tpl": 0.22,      # 模板化密度
    "repeat": 0.18,   # 搭配复用率
    "len": 0.15,      # 篇幅规整度
    "burst": 0.15,    # 突发性
}

# ---------------------------------------------------------------- 经验阈值
SURPRISE_NEUTRAL = 6.0          # 句子太短（不足两个 token）时的中性值
BACKOFF_K = 1.0                 # bigram 平滑：回退到一元分布的虚拟计数
MAX_SURPRISE_BITS = 16.0        # 单个 token 的信息量上限，避免生僻字主导结果
REUSE_FULL = 0.45               # 搭配复用率记满分的取值

TEMPLATE_FULL_HITS_PER_100 = 4.0   # 每百字命中多少个模板词记满分
COMFORT_LENGTH = 34                # AI 常见的句长中心（字数）
COMFORT_SPAN = 30                  # 偏离中心多少字降到 0 分

MIN_SENTENCE_CHARS = 10         # 短于这个长度的碎片（标题、图注、编号）不算句子
MIN_SENTENCE_TOKENS = 5
SHORT_SENTENCE_CHARS = 18       # 短于这个长度的句子统计特征不可靠：一律按人工处理且不进 AIGC 率

# 相对位置的软映射：z=0 → 0.5，z=-1 → 0.77，z=+1 → 0.23
Z_SLOPE = 1.2
Z_LIMIT = 4.0
# 突发性：句长偏离均值 |z| 越小越像 AI
BURST_PIVOT = 0.7
BURST_SLOPE = 1.5

SHUFFLE_SEED = 20240917         # 打乱基线的随机种子（固定 → 结果可复现）

# 文档级校准
REDUNDANCY_PIVOT = 0.22         # 语言冗余度中性点（越高越可预测）
REDUNDANCY_UP_SPAN = 0.15       # 高于中性点多少记满上调
REDUNDANCY_DOWN_SPAN = 0.15     # 低于中性点多少记满下调
CV_PIVOT = 0.45                 # 句长变异系数中性点（越小越整齐）
CV_PIVOT_SPAN = 0.25
BIAS_PREDICTABLE = 0.18         # 通篇可预测时的最大上调
BIAS_SURPRISING = -0.18         # 通篇「意外」时的最大下调
BIAS_UNIFORM = 0.08             # 句长过于整齐时的最大上调

# ---------------------------------------------------------------- 风险分级
RISK_STEPS = (
    (20.0, "low", "低风险"),
    (40.0, "medium", "需关注"),
    (60.0, "high", "风险较高"),
)
RISK_CRITICAL = ("critical", "高风险")


def risk_of(rate: float) -> tuple[str, str]:
    """按 AIGC 率给出风险等级 ``(key, label)``。"""
    for limit, key, label in RISK_STEPS:
        if rate < limit:
            return key, label
    return RISK_CRITICAL


# ---------------------------------------------------------------- 数据模型
@dataclass
class SentenceVerdict:
    """一个句子的判定结果。"""

    text: str = ""
    index: int = 0                  # 在全文句子序列中的序号（从 0 开始）
    paragraph: int = 0              # 所属段落在 layout 中的下标
    start: int = -1                 # 句子在所属段落文本中的起始下标
    end: int = -1                   # 结束下标（不含）；导出时据此把段落拼回原样
    score: float = 0.0              # AI 倾向分 0~1
    level: str = LEVEL_HUMAN        # ai / suspect / human
    counted: bool = True            # 是否计入 AIGC 率（短句与不可靠句子为 False）
    reasons: list[str] = field(default_factory=list)   # 命中的特征说明
    metrics: dict = field(default_factory=dict)        # 各维度原始值（便于排查）

    @property
    def length(self) -> int:
        return len(self.text)

    @property
    def level_label(self) -> str:
        return LEVEL_LABELS.get(self.level, LEVEL_LABELS[LEVEL_HUMAN])


@dataclass
class LayoutParagraph:
    """原文档的一个段落：正文参与检测，标题 / 参考文献只留档不判定。

    导出「带标注的原文」时按这个列表的顺序渲染，段落排版与原文一致。
    """

    text: str = ""
    kind: str = KIND_BODY          # body | heading | reference


@dataclass
class AigcReport:
    """一次检测的结果。"""

    source: str = ""                # 文件名
    path: str = ""                  # 原始文件路径
    sensitivity: str = DEFAULT_SENSITIVITY
    aigc_rate: float = 0.0          # AIGC 率（百分数）：AI / 疑似内容按档位折算后的字数占比
    average_score: float = 0.0      # 每句倾向分按字数加权平均，作为连续值参考
    risk: str = "low"               # low / medium / high / critical
    risk_label: str = "低风险"
    sentences: list[SentenceVerdict] = field(default_factory=list)
    layout: list[LayoutParagraph] = field(default_factory=list)   # 原文档段落流（含未检测的）
    paragraph_count: int = 0
    char_count: int = 0
    reference_paragraphs: int = 0   # 被当作参考文献排除掉的段落数
    heading_paragraphs: int = 0     # 被当作章节标题 / 目录行排除掉的段落数
    thresholds: tuple = ()          # (ai 阈值, 疑似阈值)
    created_at: float = field(default_factory=time.time)
    engine: str = ENGINE_NAME

    # ------------------------------------------------------------ 统计
    def counts(self) -> dict[str, int]:
        """各档位的句子数。"""
        result = {level: 0 for level in LEVEL_ORDER}
        for item in self.sentences:
            result[item.level] = result.get(item.level, 0) + 1
        return result

    def char_ratio(self) -> dict[str, float]:
        """各档位的字数占比（百分数）。"""
        return self._ratio(lambda item: item.length)

    def _counted(self) -> list[SentenceVerdict]:
        """真正参与 AIGC 率计算的句子（短句不参与）。"""
        return [item for item in self.sentences if item.counted]

    def score_ratio(self) -> dict[str, float]:
        """各档位对 AIGC 率的贡献（百分数）：三档相加即全文 AIGC 率。"""
        result = {level: 0.0 for level in LEVEL_ORDER}
        counted = self._counted()
        total = sum(item.length for item in counted)
        if total <= 0:
            return result
        for item in counted:
            weight = LEVEL_WEIGHTS.get(item.level, 0.0) * item.length
            result[item.level] = result.get(item.level, 0.0) + weight / total * 100
        return result

    def _ratio(self, weight: Callable[[SentenceVerdict], float]) -> dict[str, float]:
        result = {level: 0.0 for level in LEVEL_ORDER}
        total = sum(weight(item) for item in self.sentences)
        if total <= 0:
            return result
        for item in self.sentences:
            result[item.level] = result.get(item.level, 0.0) + weight(item) / total * 100
        return result

    @property
    def sentence_count(self) -> int:
        return len(self.sentences)

    @property
    def uncounted_count(self) -> int:
        """被排除在 AIGC 率之外的短句数量。"""
        return sum(1 for item in self.sentences if not item.counted)


# ---------------------------------------------------------------- 文本抽取
def extract_document_text(path: str | Path) -> str:
    """按扩展名抽取纯文本；不支持的类型抛 ValueError。"""
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix == ".docx":
        return _docx_text(target)
    if suffix == ".pdf":
        return _pdf_text(target)
    if suffix in SUPPORTED_SUFFIXES:
        return target.read_text(encoding="utf-8-sig", errors="ignore")
    raise ValueError(f"暂不支持检测 {suffix or '该类型'} 文件，请用 Word / PDF / 文本")


def _docx_text(path: Path) -> str:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(str(path))
    parts: list[str] = []
    for element in document.element.body.iterchildren():
        if element.tag == qn("w:p"):
            text = Paragraph(element, document).text.strip()
            if text:
                parts.append(text)
        elif element.tag == qn("w:tbl"):
            for row in Table(element, document).rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    parts.append(" ".join(cells))
    return "\n\n".join(parts)


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    pages: list[str] = []
    for page in PdfReader(str(path)).pages:
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(text)
    return "\n\n".join(pages)


# ---------------------------------------------------------------- 切分
_TOKEN_RE = re.compile(r"[A-Za-z]+|[\u4e00-\u9fff]|\d+")
# 正文里的引文角标：[1] / [2-5] / [1,3] / ［12］
_CITATION_RE = re.compile(r"[\[［]\s*\d+(?:\s*[-,–—、]\s*\d+)*\s*[\]］]")


def tokenize(text: str) -> list[str]:
    """切成 token：英文单词 / 单个汉字 / 数字串。"""
    return [match.group(0).lower() for match in _TOKEN_RE.finditer(text)]


def strip_citations(text: str) -> str:
    """去掉正文里的引文角标再统计。

    "[1]" 这类编号在同一篇里高频重复，会同时推高「搭配复用率」和「可预测性」，
    让引用密集的段落平白被判成 AI；它们不是作者写的词，不该计入语言特征。
    """
    return _CITATION_RE.sub("", text or "")


def _sentence_boundaries(text: str) -> list[tuple[int, int]]:
    """标出每个句子的 ``(起始下标, 结束下标)``（结束下标不含，含句末标点）。

    中英文混排：中文按 ``。！？；`` 断，英文按 ``. ! ?`` 断，但英文句点后面必须
    是空白或行尾才算（"0.08" 这类小数点不会被切开）。
    """
    spans: list[tuple[int, int]] = []
    start = 0
    length = len(text)
    for index, char in enumerate(text):
        if char in "。！？；":
            spans.append((start, index + 1))
            start = index + 1
        elif char in ".!?":
            following = text[index + 1] if index + 1 < length else ""
            if following == "" or following.isspace():
                spans.append((start, index + 1))
                start = index + 1
    if start < length:
        spans.append((start, length))
    return spans


def split_sentence_spans(text: str) -> list[tuple[int, int, str]]:
    """分句并保留每句在原文中的位置：``(起始下标, 结束下标, 句子文本)``。

    位置信息是给导出用的：只有知道每句在段落里的偏移，才能把段落按原样拼回去、
    只把颜色换掉，用户拿着报告就能直接在原文里定位。
    """
    result: list[tuple[int, int, str]] = []
    for start, end in _sentence_boundaries(text or ""):
        raw = text[start:end]
        stripped = raw.strip()
        if not stripped:
            continue
        lead = len(raw) - len(raw.lstrip())
        trail = len(raw.rstrip())
        result.append((start + lead, start + trail, stripped))
    return result


def split_sentences(text: str) -> list[str]:
    """中英文混排分句；标点保留在句尾。"""
    return [sentence for _start, _end, sentence in split_sentence_spans(text)]


def merge_short_sentences(
    spans: list[tuple[int, int, str]], min_chars: int = SHORT_SENTENCE_CHARS
) -> list[list[tuple[int, int, str]]]:
    """把同一段落里连续过短的句子并成一个检测单元。

    短句之所以要保护，是因为它 token 太少、统计特征不可靠；但「不可靠」不等于
    「安全」—— AI 完全可以通篇用短句写作（"该方法效果显著。实验数据充分。
    结果表明有效。"），单看每一句都不够判定长度，直接放过就会整段漏检。

    所以这里不丢弃短句，而是**顺着往后并**，凑到 ``min_chars`` 再作为一个整体
    判定；段落末尾凑不满的残留并入上一组。整段都凑不够长度时（比如一段就一句
    "本系统采用 B/S 架构。"），才退回按人工处理。

    Returns:
        分组后的 span 列表（每组内 span 连续），调用方据此取 ``(起点, 终点, 文本)``。
    """
    groups: list[list[tuple[int, int, str]]] = []
    current: list[tuple[int, int, str]] = []
    for span in spans:
        current.append(span)
        # 用原文跨度（含句间空格）判断是否够长，而不是拼接后的纯句子文本
        if span[1] - current[0][0] >= min_chars:
            groups.append(current)
            current = []
    if current:
        if groups:
            groups[-1].extend(current)     # 尾部残留并入上一组，避免丢字
        else:
            groups.append(current)         # 整段都很短，只能单独成组
    return groups


def split_paragraphs(text: str) -> list[str]:
    """按空行 / 单换行切段落。"""
    parts = (item.strip() for item in re.split(r"\n\s*\n|\n", text or ""))
    return [item for item in parts if item]


# 参考文献标题：单独成段的「参考文献 / References / Bibliography」，允许带编号
_REFERENCE_HEADING_RE = re.compile(
    r"^[\s\-\u2014]*([\[【(（]?\d{1,2}[\]】)）.、]|第[一二三四五六七八九十]+[章节部分])?\s*"
    r"(参考文献|references|bibliography|参考书目|引用文献)\s*[:：]?\s*$",
    re.IGNORECASE,
)
# 单条参考文献：以 [1] / 1. 开头
_REFERENCE_INDEX_RE = re.compile(r"^\s*[\[【(（]?\d{1,3}[\]】)）]\s*\S|^\s*\d{1,3}[.、]\s*\S")
# ---------------------------------------------------------------- 章节标题
# 带编号的标题：6.5.2 / 3.2 / 第三章 / 一、 / Chapter 3
# 点分形式限制首位最多两位数字，"2020.3.5" 这类日期不会被当成章节号
_HEADING_NUMBER_RE = re.compile(
    r"^\s*(?:"
    r"第\s*[0-9一二三四五六七八九十百零]+\s*[章节節篇]|"
    r"\d{1,2}(?:\.\d+)+|"
    r"[一二三四五六七八九十]+\s*[、.．]|"
    r"(?:chapter|section)\s*\d{1,3}"
    r")\s*",
    re.IGNORECASE,
)
# 目录行：标题与页码之间是引导点 / 制表符 / 全角空格
_TOC_LEADER_RE = re.compile(r"(?:\.{3,}|…{2,}|\t|\u3000)\s*\d{1,3}\s*$")
# 不带编号的常见标题（单独成段时才算）
_BARE_HEADING_RE = re.compile(
    r"^\s*(目录|contents|摘要|abstract|绪\s*论|引\s*言|前\s*言|结\s*论|结\s*语|结束语|"
    r"致\s*谢|附\s*录|研究背景|研究意义|研究内容|研究方法|技术路线|需求分析|总体设计|"
    r"详细设计|系统设计|系统实现|系统测试|本章小结|工作总结|总结与展望|创新点|参考文献)"
    r"\s*[:：]?\s*$",
    re.IGNORECASE,
)
# 标题正文不该以这些字开头：出现说明是正文（"3.2 节介绍了…"）
_HEADING_BODY_BLOCK = "节章中里所是为的了有"
HEADING_BODY_MAX = 30           # 去掉编号与页码后，标题正文最多多少字
HEADING_LINE_MAX = 60           # 整行超过这个长度一律不按标题处理

# 文献表之后的章节标题：遇到说明参考文献这一节结束了，后面照常检测
_REFERENCE_END_RE = re.compile(
    r"^\s*(致\s*谢|后\s*记|附\s*录|acknowledge?ments?|appendix)\s*[:：]?\s*$",
    re.IGNORECASE,
)
# 参考文献里才有的特征：文献类型标识、出版信息、年份、DOI / 链接
_REFERENCE_MARKER_RE = re.compile(
    r"\[[A-Z]{1,2}\]|https?://|\bdoi\b|\bDOI\b|(19|20)\d{2}|出版社|学报|杂志|期刊|"
    r"会议|论文集|学位论文|博士|硕士|专利|标准"
)


def is_reference_heading(text: str) -> bool:
    """这段话是不是「参考文献」那一节的标题。"""
    return bool(_REFERENCE_HEADING_RE.match(text or ""))


def is_reference_entry(text: str) -> bool:
    """这段话是不是一条参考文献条目（没有标题、直接列条目的文档也要能认出来）。

    只认「编号开头 + 含文献特征」的组合：正文里的编号列表（"1. 实验步骤"）
    不会带出版社 / 年份 / [J] 这类特征，不会被误伤。
    """
    stripped = text or ""
    if not stripped or len(stripped) > 400:
        return False
    return bool(_REFERENCE_INDEX_RE.match(stripped) and _REFERENCE_MARKER_RE.search(stripped))


def is_toc_line(text: str) -> bool:
    """目录行：标题与页码之间用引导点 / 制表符连起来。"""
    stripped = (text or "").strip()
    if not stripped or len(stripped) > 80:
        return False
    return bool(_TOC_LEADER_RE.search(stripped))


def is_heading(text: str) -> bool:
    """这一段是不是章节标题（含目录里的条目）。

    标题本身没有语言特征可言 —— 它既不是作者写的句子，也没有上下文，
    混进统计只会让"句长规整度""搭配复用"这些维度失真，所以整行跳过。
    """
    stripped = (text or "").strip()
    if not stripped or len(stripped) > HEADING_LINE_MAX:
        return False
    if is_toc_line(stripped):
        return True
    if _BARE_HEADING_RE.match(stripped):
        return True

    match = _HEADING_NUMBER_RE.match(stripped)
    if match is None:
        return False
    # 编号后面还挂着页码的目录条目：先把页码尾巴摘掉再判断
    body = _TOC_LEADER_RE.sub("", stripped[match.end():]).strip()
    if not body or len(body) > HEADING_BODY_MAX:
        return False
    if body[0] in _HEADING_BODY_BLOCK:
        return False
    return body[-1] not in "。！？；!?;，,、：:"


def classify_paragraphs(paragraphs: list[str]) -> list[LayoutParagraph]:
    """给每个段落打标记：``body`` 参与检测，``heading`` / ``reference`` 不参与。

    不参与检测的段落不会被丢掉 —— 它们留在 :attr:`AigcReport.layout` 里，
    导出「带标注的原文」时按原样（中性色）渲染，读者才能对着原文找位置。

    参考文献的识别带状态：命中「参考文献」标题后，其下内容整体跳过（直到
    「致谢 / 附录」这类结束标题），因为文献表通常一列到底；没有标题、直接
    罗列条目的文档则逐条判断。
    """
    layout: list[LayoutParagraph] = []
    in_references = False
    for text in paragraphs:
        if in_references and not _REFERENCE_END_RE.match(text):
            layout.append(LayoutParagraph(text=text, kind=KIND_REFERENCE))
            continue
        in_references = False
        if is_reference_heading(text) or is_reference_entry(text):
            in_references = is_reference_heading(text)
            layout.append(LayoutParagraph(text=text, kind=KIND_REFERENCE))
            continue
        if is_heading(text):
            layout.append(LayoutParagraph(text=text, kind=KIND_HEADING))
            continue
        layout.append(LayoutParagraph(text=text, kind=KIND_BODY))
    return _merge_short_paragraphs(layout)


def _merge_short_paragraphs(layout: list[LayoutParagraph]) -> list[LayoutParagraph]:
    """把连续过短的正文段落攒成一个检测单元（保留换行符）。

    一行一句的排版很常见（AI 的列表体喜欢这么写，PDF / Word 抽取也更容易把正文
    切成这样）。这时每个段落只有一个短句，**段内没有邻居可并**，合并短句的逻辑
    就使不上劲，通篇下来全是「片段过短、不计入」—— 整篇反而漏检。

    规则与句合并对称：连续短段落顺着往后攒，**攒够判定长度就自成一组**；攒不满的
    残段才并进上一段。这样既不会留下大量「过短」，也不会让一段长文一路吞下去
    把 AI 段和人工段搅在一起（那会把两端的分数互相稀释）。

    换行符保留在文本里，导出「带标注的原文」时仍然按原样断行。
    """
    merged: list[LayoutParagraph] = []
    pending: list[str] = []

    def flush() -> None:
        if not pending:
            return
        text = "\n".join(pending)
        pending.clear()
        if len(text) < SHORT_SENTENCE_CHARS and merged and merged[-1].kind == KIND_BODY:
            # 整个残段都不够判定长度：并进上一段，别单独留成「过短」
            previous = merged[-1]
            merged[-1] = LayoutParagraph(text=f"{previous.text}\n{text}", kind=KIND_BODY)
            return
        merged.append(LayoutParagraph(text=text, kind=KIND_BODY))

    for block in layout:
        if block.kind != KIND_BODY:
            flush()
            merged.append(block)
            continue
        if len(block.text) >= SHORT_SENTENCE_CHARS:
            flush()
            merged.append(block)
            continue
        pending.append(block.text)
        if sum(len(item) for item in pending) + len(pending) - 1 >= SHORT_SENTENCE_CHARS:
            flush()
    flush()
    return merged


def _usable(sentence: str, tokens: list[str]) -> bool:
    """过短的碎片（标题、编号、图注）不参与统计。"""
    return len(sentence.strip()) >= MIN_SENTENCE_CHARS and len(tokens) >= MIN_SENTENCE_TOKENS


# ---------------------------------------------------------------- 语言模型
def _build_model(token_lists: list[list[str]]) -> tuple[Counter, Counter, int, dict[str, float]]:
    """统计全文的一元 / 二元计数，返回 ``(unigram, bigram, vocab, 一元概率)``。"""
    unigram: Counter = Counter()
    bigram: Counter = Counter()
    for tokens in token_lists:
        unigram.update(tokens)
        bigram.update(zip(tokens, tokens[1:]))
    total = sum(unigram.values()) or 1
    probabilities = {token: count / total for token, count in unigram.items()}
    return unigram, bigram, max(len(unigram), 1), probabilities


def _surprise(
    tokens: list[str], bigram: Counter, unigram: Counter, probabilities: dict[str, float]
) -> float:
    """句子的平均信息量（bits/token）：越低越可预测。

    平滑是一元回退 ``P(b|a) = (n + k·P(b)) / (d + k)``，其中 ``n`` / ``d`` 都
    **扣掉了本次出现自己**（留一法）。这一步不能省：小语料上大量二元组只出现
    一次，不扣自己的话模型等于把句子背了下来，信息量全线塌到 1 bit 左右，
    反而失去区分度。没见过的搭配则退回一元概率。
    """
    if len(tokens) < 2:
        return SURPRISE_NEUTRAL
    total = 0.0
    for prev, cur in zip(tokens, tokens[1:]):
        numerator = max(bigram.get((prev, cur), 0) - 1, 0) + BACKOFF_K * probabilities.get(cur, 0.0)
        denominator = max(unigram.get(prev, 0) - 1, 0) + BACKOFF_K
        bits = -math.log2(max(numerator / denominator, 1e-9))
        total += min(bits, MAX_SURPRISE_BITS)
    return total / (len(tokens) - 1)


def _structure_baseline(token_lists: list[list[str]]) -> float:
    """把全文 token 打乱后重算一遍平均信息量，作为「没有任何搭配结构」的基线。

    留一法带来的惩罚跟语料规模有关（越长的文稿惩罚越小），直接拿一元熵当基线
    会让长篇一律显得"可预测"。打乱顺序后一元分布不变、二元结构被抹掉，用它当
    基线就把规模因素抵消掉了；固定随机种子保证同一份文档每次结果一致。
    """
    flat = [token for tokens in token_lists for token in tokens]
    if len(flat) < 4:
        return 0.0
    random.Random(SHUFFLE_SEED).shuffle(flat)
    chunks: list[list[str]] = []
    cursor = 0
    for tokens in token_lists:
        chunks.append(flat[cursor : cursor + len(tokens)])
        cursor += len(tokens)
    _unigram, bigram, _vocab, probabilities = _build_model(chunks)
    values = [
        _surprise(chunk, bigram, _unigram, probabilities)
        for chunk in chunks
        if len(chunk) >= 2
    ]
    return sum(values) / len(values) if values else 0.0


def _reuse_ratio(tokens: list[str], bigram: Counter) -> float:
    """句子里「在全文别处也出现过」的二元组占比：AI 更爱复用同一套搭配。"""
    if len(tokens) < 2:
        return 0.0
    pairs = list(zip(tokens, tokens[1:]))
    reused = sum(1 for pair in pairs if bigram.get(pair, 0) > 1)
    return reused / len(pairs)


# ---------------------------------------------------------------- 模板词
TEMPLATE_PHRASES = (
    "综上所述", "总而言之", "总的来看", "总的来说", "值得注意的是", "需要注意的是",
    "不难看出", "由此可见", "众所周知", "一般来说", "一般而言", "具体而言", "具体来看",
    "可以预见", "一方面", "另一方面", "在此基础上", "基于此", "在一定程度上",
    "首先", "其次", "再次", "最后", "综上", "此外", "另外", "同时", "因此", "然而",
    "而且", "并且", "不仅", "随着", "针对", "为了", "同时也要", "可以看出",
    "具有重要的", "发挥着", "起到了", "有效地", "显著提升", "进一步优化",
    "本研究", "本文", "该方法", "该系统", "该模型", "结果表明", "实验结果表明",
    "研究发现", "数据显示", "通过", "基于",
)

_BY_HEAD: dict[str, tuple[str, ...]] = {}


def _phrase_index() -> dict[str, tuple[str, ...]]:
    """按首字索引模板词，长的排在前面（优先匹配更具体的短语）。"""
    if _BY_HEAD:
        return _BY_HEAD
    grouped: dict[str, list[str]] = {}
    for phrase in TEMPLATE_PHRASES:
        grouped.setdefault(phrase[0], []).append(phrase)
    for head, items in grouped.items():
        _BY_HEAD[head] = tuple(sorted(items, key=len, reverse=True))
    return _BY_HEAD


def template_hits(text: str) -> list[str]:
    """列出句子中命中的模板化表达（按出现顺序，不重叠）。"""
    index = _phrase_index()
    hits: list[str] = []
    position = 0
    length = len(text)
    while position < length:
        matched = ""
        for phrase in index.get(text[position], ()):
            if text.startswith(phrase, position):
                matched = phrase
                break
        if matched:
            hits.append(matched)
            position += len(matched)
        else:
            position += 1
    return hits


# ---------------------------------------------------------------- 工具
def _clip(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def _length_stats(lengths: list[int]) -> tuple[float, float, float]:
    """句长的 (均值, 标准差, 变异系数)。"""
    if not lengths:
        return 0.0, 0.0, 0.0
    mean = sum(lengths) / len(lengths)
    if len(lengths) < 2:
        return mean, 0.0, 0.0
    variance = sum((item - mean) ** 2 for item in lengths) / (len(lengths) - 1)
    sd = math.sqrt(variance)
    return mean, sd, (sd / mean if mean else 0.0)


def _sigmoid(z: float, slope: float = Z_SLOPE) -> float:
    """把标准化偏离量映射到 0~1：z 越小（越"低于平均"）分数越高。"""
    clamped = _clamp(z, -Z_LIMIT, Z_LIMIT)
    return 1.0 / (1.0 + math.exp(slope * clamped))


def _document_bias(redundancy: float, cv: float) -> float:
    """文档级校准值：通篇可预测 / 句长整齐时上调，反之下调。"""
    predictable = _clip((redundancy - REDUNDANCY_PIVOT) / REDUNDANCY_UP_SPAN)
    surprising = _clip((REDUNDANCY_PIVOT - redundancy) / REDUNDANCY_DOWN_SPAN)
    uniform = _clip((CV_PIVOT - cv) / CV_PIVOT_SPAN)
    return BIAS_PREDICTABLE * predictable + BIAS_SURPRISING * surprising + BIAS_UNIFORM * uniform


def _reasons_of(subs: dict[str, float], metrics: dict) -> list[str]:
    """把得分高的维度翻译成人能看懂的理由。"""
    reasons: list[str] = []
    if subs["pred"] >= 0.6:
        reasons.append(f"用词可预测性高于全文平均（信息量 {metrics['surprise']:.1f} bits/token）")
    if subs["tpl"] >= 0.6:
        shown = "、".join(metrics["hits"][:3])
        reasons.append(f"模板化表达 {len(metrics['hits'])} 处（{shown}）")
    if subs["repeat"] >= 0.6:
        reasons.append(f"常见搭配复用率 {metrics['reuse']:.0%}")
    if subs["len"] >= 0.6:
        reasons.append(f"句长 {metrics['length']} 字，处于 AI 常见区间")
    if subs["burst"] >= 0.7:
        reasons.append("句长贴近全文平均，缺少长短句变化")
    return reasons


# ---------------------------------------------------------------- 主流程
ProgressFn = Callable[[str, int], None]


def analyze_file(
    path: str | Path,
    sensitivity: str = DEFAULT_SENSITIVITY,
    progress: ProgressFn | None = None,
) -> AigcReport:
    """检测一个文件（Word / PDF / 文本）。"""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"文件不存在：{target}")
    if progress:
        progress("正在读取文档", 5)
    text = extract_document_text(target)
    return analyze_text(text, source=target.name, path=str(target),
                        sensitivity=sensitivity, progress=progress)


def analyze_text(
    text: str,
    source: str = "",
    path: str = "",
    sensitivity: str = DEFAULT_SENSITIVITY,
    progress: ProgressFn | None = None,
) -> AigcReport:
    """检测一段文本，返回 :class:`AigcReport`。"""
    def tick(step: str, percent: int) -> None:
        if progress:
            progress(step, percent)

    preset = SENSITIVITY_PRESETS.get(sensitivity, SENSITIVITY_PRESETS[DEFAULT_SENSITIVITY])
    ai_threshold = float(preset["ai"])
    suspect_threshold = float(preset["suspect"])
    empty = AigcReport(
        source=source, path=path,
        sensitivity=sensitivity if sensitivity in SENSITIVITY_PRESETS else DEFAULT_SENSITIVITY,
        thresholds=(ai_threshold, suspect_threshold),
    )

    tick("正在解析段落", 12)
    layout = classify_paragraphs(split_paragraphs(text))
    empty.layout = layout
    empty.reference_paragraphs = sum(1 for item in layout if item.kind == KIND_REFERENCE)
    empty.heading_paragraphs = sum(1 for item in layout if item.kind == KIND_HEADING)
    if not any(item.kind == KIND_BODY for item in layout):
        return empty

    tick("正在切分句子", 26)
    # 只给正文段落分句；标题 / 参考文献仍然留在 layout 里，导出原文时照原样渲染。
    # 连续短句会先并成够长的检测单元，避免 AI 用短句写作时整段漏检。
    units: list[tuple[int, int, int, str]] = []
    for block_index, block in enumerate(layout):
        if block.kind != KIND_BODY:
            continue
        for group in merge_short_sentences(split_sentence_spans(block.text)):
            start, end = group[0][0], group[-1][1]
            units.append((block_index, start, end, block.text[start:end]))

    # 引文角标不参与统计：它们会在全文反复出现，白白推高「可预测 / 复用」两维
    token_lists = [tokenize(strip_citations(sentence)) for _b, _s, _e, sentence in units]
    pairs = [(item, tokens) for item, tokens in zip(units, token_lists) if _usable(item[3], tokens)]
    if not pairs:
        return empty
    units = [item for item, _tokens in pairs]
    token_lists = [tokens for _item, tokens in pairs]

    tick("正在构建语言模型", 46)
    unigram, bigram, _vocab, probabilities = _build_model(token_lists)

    tick("正在计算句子特征", 66)
    surprises = [_surprise(tokens, bigram, unigram, probabilities) for tokens in token_lists]
    lengths = [len(sentence) for _block, _start, _end, sentence in units]
    mean_length, sd_length, cv = _length_stats(lengths)
    mean_surprise, sd_surprise, _cv_surprise = _length_stats(surprises)
    # 语言冗余度：二元模型相对「打乱顺序」的基线压缩掉了多少信息量。
    # 通篇复用同一套说法时，二元模型能省下的信息更多，这个值就偏高。
    baseline = _structure_baseline(token_lists)
    redundancy = _clamp(1 - mean_surprise / baseline, -0.5, 1.0) if baseline > 0 else 0.0
    bias = _document_bias(redundancy, cv)

    tick("正在判定句子来源", 88)
    verdicts: list[SentenceVerdict] = []
    for index, ((paragraph, start, end, sentence), tokens) in enumerate(zip(units, token_lists)):
        surprise = surprises[index]
        length = lengths[index]
        hits = template_hits(sentence)
        reuse = _reuse_ratio(tokens, bigram)
        hits_per_100 = len(hits) * 100 / max(length, 1)
        deviation = abs(length - mean_length) / (sd_length + 1e-6)

        subs = {
            "pred": _sigmoid((surprise - mean_surprise) / (sd_surprise + 1e-6)),
            "burst": 1.0 - _sigmoid(deviation - BURST_PIVOT, BURST_SLOPE),
            "tpl": _clip(hits_per_100 / TEMPLATE_FULL_HITS_PER_100),
            "repeat": _clip(reuse / REUSE_FULL),
            "len": _clip(1 - abs(length - COMFORT_LENGTH) / COMFORT_SPAN),
        }
        raw = sum(WEIGHTS[key] * value for key, value in subs.items())
        score = _clamp(raw + bias, 0.02, 0.98)
        level = (
            LEVEL_AI if score >= ai_threshold
            else LEVEL_SUSPECT if score >= suspect_threshold
            else LEVEL_HUMAN
        )
        metrics = {
            "surprise": surprise,
            "length": length,
            "hits": hits,
            "reuse": reuse,
        }
        counted = True
        reasons = _reasons_of(subs, metrics)
        if length < SHORT_SENTENCE_CHARS:
            # 走到这里说明整段都凑不够判定长度（比如一段就一句"本系统采用 B/S 架构。"），
            # 前后没有可合并的邻居了：只有几个 token 谈不上统计意义，凭分数判成 AI
            # 十有八九是误判 —— 按人工处理，并且不进 AIGC 率，免得带偏整体结果。
            counted = False
            level = LEVEL_HUMAN
            score = min(score, max(suspect_threshold - 0.05, 0.0))
            reasons = [f"片段过短（{length} 字），统计特征不可靠，按人工处理且不计入 AIGC 率"]
        verdicts.append(
            SentenceVerdict(
                text=sentence,
                index=index,
                paragraph=paragraph,
                start=start,
                end=end,
                score=score,
                level=level,
                counted=counted,
                reasons=reasons,
                metrics=dict(subs) | {"surprise": surprise, "length": length},
            )
        )

    tick("正在汇总结果", 100)
    counted_verdicts = [item for item in verdicts if item.counted]
    total_length = sum(item.length for item in counted_verdicts) or 1
    # AIGC 率按档位折算，保证它与界面上的三档计数严格一致（AI 0 句 / 疑似 0 句 ⇒ 0%）
    rate = sum(
        LEVEL_WEIGHTS.get(item.level, 0.0) * item.length for item in counted_verdicts
    ) / total_length * 100
    average = sum(item.score * item.length for item in counted_verdicts) / total_length
    risk, risk_label = risk_of(rate)
    return AigcReport(
        source=source or "文本",
        path=path,
        sensitivity=sensitivity if sensitivity in SENSITIVITY_PRESETS else DEFAULT_SENSITIVITY,
        aigc_rate=round(rate, 1),
        average_score=round(average, 3),
        risk=risk,
        risk_label=risk_label,
        sentences=verdicts,
        layout=layout,
        paragraph_count=sum(1 for item in layout if item.kind == KIND_BODY),
        char_count=sum(item.length for item in counted_verdicts),
        reference_paragraphs=sum(1 for item in layout if item.kind == KIND_REFERENCE),
        heading_paragraphs=sum(1 for item in layout if item.kind == KIND_HEADING),
        thresholds=(ai_threshold, suspect_threshold),
    )
