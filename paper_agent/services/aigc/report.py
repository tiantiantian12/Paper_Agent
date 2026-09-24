"""AIGC 检测报告导出：第 1 页结论，第 2 页起是带颜色标注的原文。

- AI 生成的句子标红
- 疑似 AI 的句子标黄（琥珀色，保证白底可读）
- 判定为人工的句子标灰
- 标题 / 参考文献不判定，用中性色照原样保留，方便对着原文找位置

原文部分严格按源文档的段落结构渲染：段落不拆行、顺序不变，只把每句话换成
对应颜色，所以打开 PDF 就能直接和原稿对照修改。
"""

from __future__ import annotations

import html
import time
from pathlib import Path

from paper_agent.services.aigc.analyzer import (
    KIND_BODY,
    KIND_HEADING,
    KIND_REFERENCE,
    LEVEL_AI,
    LEVEL_COLORS,
    LEVEL_HUMAN,
    LEVEL_LABELS,
    LEVEL_ORDER,
    LEVEL_SUSPECT,
    SENSITIVITY_PRESETS,
    AigcReport,
    split_sentence_spans,
)

_FONT_NAME = "PaperAgentCJK"
# 标题 / 参考文献 / 未判定片段的中性色，保证与判定色区分得开
NEUTRAL_COLOR = "#3F3F3C"
SKIPPED_COLOR = "#9A9A93"


def _register_font() -> str:
    """注册中文字体；找不到时退回 reportlab 内置字体。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if _FONT_NAME in pdfmetrics.getRegisteredFontNames():
        return _FONT_NAME
    candidates = (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont(_FONT_NAME, str(candidate), subfontIndex=0))
            return _FONT_NAME
        except Exception:      # noqa: BLE001 - 字体异常时退回内置字体
            continue
    return "Helvetica"


def _escape(text: str) -> str:
    return html.escape(text or "")


def _inline(text: str) -> str:
    """转义并保留换行：reportlab 的 Paragraph 只认 ``<br/>``。

    短段落合并进上一段后，换行符留在文本里 —— 不转成 ``<br/>`` 的话几行会被
    挤成一整段，原文的断行就丢了。
    """
    return _escape(text).replace("\n", "<br/>")


def _paragraph_markup(block, block_index: int, verdicts: dict) -> str:
    """把一段正文拼回原样，只给每句套上对应颜色。

    ``verdicts`` 以 ``(段落下标, 句子起始下标)`` 为键。段落里没被判定过的片段
    （过短的碎片等）用中性灰，保证整段文字一字不落地出现在报告里。
    """
    chunks: list[str] = []
    cursor = 0
    for start, end, sentence in split_sentence_spans(block.text):
        gap = block.text[cursor:start]
        if gap:
            chunks.append(_inline(gap))
        verdict = verdicts.get((block_index, start))
        color = LEVEL_COLORS.get(verdict.level, SKIPPED_COLOR) if verdict else SKIPPED_COLOR
        chunks.append(f'<font color="{color}">{_inline(sentence)}</font>')
        cursor = end
    tail = block.text[cursor:]
    if tail:
        chunks.append(_inline(tail))
    if not chunks:
        chunks.append(_inline(block.text))
    return "".join(chunks)


def _layout_flowables(report: AigcReport, prose_style, heading_style, skipped_style) -> list:
    """按原文段落流生成 flowable 列表：正文着色，标题 / 文献中性保留。"""
    from reportlab.platypus import Paragraph, Spacer

    # 判定结果按「段落下标 + 句子起点」索引，段落拼回时才能对上号
    verdicts = {
        (item.paragraph, item.start): item for item in report.sentences if item.start >= 0
    }

    flowables: list = []
    if not report.layout:
        # 没有段落流（旧数据 / 手工构造的报告）：退回「一句一行」的简明展示
        for item in report.sentences:
            color = LEVEL_COLORS.get(item.level, LEVEL_COLORS[LEVEL_HUMAN])
            flowables.append(
                Paragraph(f'<font color="{color}">{_escape(item.text)}</font>', prose_style)
            )
        return flowables

    for index, block in enumerate(report.layout):
        if block.kind == KIND_BODY:
            flowables.append(Paragraph(_paragraph_markup(block, index, verdicts), prose_style))
        elif block.kind == KIND_HEADING:
            flowables.append(Paragraph(_inline(block.text), heading_style))
        elif block.kind == KIND_REFERENCE:
            flowables.append(Paragraph(_inline(block.text), skipped_style))
        else:      # pragma: no cover - 未知类型按正文处理
            flowables.append(Paragraph(_inline(block.text), prose_style))
        flowables.append(Spacer(1, 2))
    return flowables


def export_report_pdf(report: AigcReport, path: str | Path) -> Path:
    """导出检测报告到 ``path``，返回实际写入的路径。"""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    font = _register_font()

    title_style = ParagraphStyle("aigcTitle", fontName=font, fontSize=20, leading=26,
                                 textColor=colors.HexColor("#1B1B19"))
    meta_style = ParagraphStyle("aigcMeta", fontName=font, fontSize=10, leading=16,
                                textColor=colors.HexColor("#5F5F58"))
    section_style = ParagraphStyle("aigcSection", fontName=font, fontSize=13, leading=19,
                                   textColor=colors.HexColor("#1B1B19"), spaceBefore=10)
    body_style = ParagraphStyle("aigcBody", fontName=font, fontSize=10.5, leading=17,
                                alignment=TA_LEFT, spaceAfter=5)
    # 原文标注：正文首行缩进 2 字符、1.75 倍行距，尽量贴近论文的排版观感
    prose_style = ParagraphStyle("aigcProse", fontName=font, fontSize=11, leading=19,
                                 alignment=TA_LEFT, firstLineIndent=22, spaceAfter=8)
    heading_style = ParagraphStyle("aigcHeading", fontName=font, fontSize=12, leading=20,
                                   textColor=colors.HexColor(NEUTRAL_COLOR),
                                   spaceBefore=10, spaceAfter=6)
    skipped_style = ParagraphStyle("aigcSkipped", fontName=font, fontSize=10, leading=16,
                                   textColor=colors.HexColor(SKIPPED_COLOR),
                                   leftIndent=10, spaceAfter=4)

    story: list = [
        Paragraph("AIGC 检测报告", title_style),
        Spacer(1, 6),
    ]

    preset = SENSITIVITY_PRESETS.get(report.sensitivity, {})
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report.created_at))
    excluded = []
    if report.reference_paragraphs:
        excluded.append(f"参考文献 {report.reference_paragraphs} 段")
    if report.heading_paragraphs:
        excluded.append(f"章节标题 / 目录 {report.heading_paragraphs} 行")
    if report.uncounted_count:
        excluded.append(f"过短句 {report.uncounted_count} 句")
    excluded_text = f"　·　已排除：{'、'.join(excluded)}" if excluded else ""
    story.append(Paragraph(
        f"文件：{_escape(report.source or '文本')}<br/>"
        f"检测时间：{stamp}　·　灵敏度：{_escape(preset.get('label', '标准'))}　·　"
        f"引擎：{_escape(report.engine)}<br/>"
        f"段落 {report.paragraph_count} 个　·　句子 {report.sentence_count} 句　·　"
        f"检测字数 {report.char_count} 字　·　平均每句倾向 {report.average_score:.2f}"
        f"{excluded_text}",
        meta_style,
    ))
    story.append(Spacer(1, 12))

    # ---------------------------------------------------------------- 结论区
    risk_color = {
        "low": "#1A7F4B",
        "medium": "#B26A00",
        "high": "#C0392B",
        "critical": "#C0392B",
    }.get(report.risk, "#1B1B19")
    story.append(Paragraph(
        f'<font size="30" color="{risk_color}"><b>{report.aigc_rate:.1f}%</b></font>'
        f'<font size="13" color="{risk_color}">　AIGC 率 · {_escape(report.risk_label)}</font>',
        body_style,
    ))
    story.append(Spacer(1, 10))

    counts = report.counts()
    char_ratio = report.char_ratio()
    score_ratio = report.score_ratio()
    rows = [["判定", "句子数", "字数占比", "对 AIGC 率的贡献"]]
    for level in LEVEL_ORDER:
        rows.append([
            LEVEL_LABELS[level],
            str(counts.get(level, 0)),
            f"{char_ratio.get(level, 0.0):.1f}%",
            f"{score_ratio.get(level, 0.0):.1f}%",
        ])
    table = Table(rows, colWidths=[70, 60, 80, 120], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#5F5F58")),
        ("TEXTCOLOR", (0, 1), (0, 1), colors.HexColor(LEVEL_COLORS[LEVEL_AI])),
        ("TEXTCOLOR", (0, 2), (0, 2), colors.HexColor(LEVEL_COLORS[LEVEL_SUSPECT])),
        ("TEXTCOLOR", (0, 3), (0, 3), colors.HexColor(LEVEL_COLORS[LEVEL_HUMAN])),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DCDCD7")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F4F4F2")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)
    story.append(Spacer(1, 10))

    threshold_text = ""
    if report.thresholds:
        threshold_text = f"（阈值 {report.thresholds[0]:.2f} / {report.thresholds[1]:.2f}）"
    legend = "　".join(
        f'<font color="{LEVEL_COLORS[level]}">■</font> {LEVEL_LABELS[level]}'
        + (threshold_text if level == LEVEL_AI else "")
        for level in LEVEL_ORDER
    )
    story.append(Paragraph(legend, meta_style))
    story.append(Paragraph(
        "AIGC 率 =（AI 生成句字数 × 1.0 + 疑似 AI 句字数 × 0.5）÷ 参与统计的总字数。"
        "它与左表的三档计数严格一致：AI 与疑似都是 0 句时，AIGC 率就是 0%。"
        "「平均每句倾向」是各句倾向分的加权平均，作为连续值参考。",
        meta_style,
    ))
    story.append(Paragraph(
        "说明：章节标题与目录行不参与检测；参考文献整段不参与检测；正文中的引文角标"
        "（如 [1]）在统计时已剔除；过短的句子统计特征不可靠，一律按人工标注且不进 AIGC 率。"
        "本结果为本地启发式引擎的统计估算，与知网 / 格子达等官方系统的口径不同，"
        "仅供参考，不能作为学术判定依据。",
        meta_style,
    ))
    story.append(PageBreak())

    # ---------------------------------------------------------------- 原文标注
    # 第 2 页起是「按原文排版 + 换颜色」的标注稿：段落结构、句序都跟源文档一致，
    # 拿着报告就能直接对着原文找位置，不必再一段段核对。
    story.append(Paragraph("原文标注", section_style))
    story.append(Paragraph(
        f"以下为「{_escape(report.source or '文本')}」的原文，仅按判定结果改色，未做任何删改。",
        meta_style,
    ))
    story.append(Spacer(1, 8))
    if not report.layout:
        story.append(Paragraph("（文档里没有可展示的内容）", body_style))
    else:
        story.extend(_layout_flowables(report, prose_style, heading_style, skipped_style))

    doc = SimpleDocTemplate(
        str(target), pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm, topMargin=1.8 * cm, bottomMargin=1.8 * cm,
        title=f"AIGC 检测报告 - {report.source or '文本'}",
        author="Paper Agent",
    )

    def _footer(canvas, document) -> None:
        canvas.saveState()
        canvas.setFont(font, 9)
        canvas.setFillColor("#8E8E86")
        canvas.drawCentredString(A4[0] / 2, 1.1 * cm, f"第 {document.page} 页")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return target
