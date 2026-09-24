"""AIGC 检测：分句、打分、聚合、PDF 导出与界面联动。"""

from __future__ import annotations

from pathlib import Path

from paper_agent.services.aigc import (
    LEVEL_AI,
    LEVEL_COLORS,
    LEVEL_HUMAN,
    LEVEL_LABELS,
    LEVEL_SUSPECT,
    analyze_file,
    analyze_text,
    export_report_pdf,
    risk_of,
)
from paper_agent.services.aigc import analyzer

AI_TEXT = (
    "综上所述，随着人工智能技术的不断发展，本研究针对当前制造业转型升级过程中存在"
    "的实际问题，提出了一种基于深度学习的智能优化方法。\n\n"
    "首先，本文对相关领域的国内外研究现状进行了系统梳理，明确了现有方法在精度与效率"
    "方面存在的不足。其次，在此基础上构建了融合多源数据的预测模型，并通过对比实验"
    "验证了所提方法的有效性。实验结果表明，该方法在准确率方面相较于传统方法提升了"
    "百分之十二点五，具有显著的优势。值得注意的是，该方法在处理大规模数据时依然保持"
    "了较好的稳定性，这对于实际工程应用具有重要的意义。\n\n"
    "随着信息技术的快速发展，数据分析在各行各业中的应用越来越广泛。一方面，本研究"
    "对相关理论基础进行了系统阐述，明确了数据分析在企业管理中的重要作用。另一方面，"
    "本文构建了基于机器学习的数据分析框架，该框架主要包括数据采集、数据预处理、"
    "特征提取与模型训练四个核心模块。在数据采集阶段，本研究采用分布式爬虫技术获取"
    "多源异构数据。在数据预处理阶段，本文提出了一种基于统计方法的异常值检测算法，"
    "有效地提升了数据质量。综上所述，本文所提出的框架具有良好的实用价值，为相关领域"
    "的研究提供了有益的参考。"
)

HUMAN_TEXT = (
    "我在车间蹲了两个月才想明白这件事。师傅姓王，干了二十多年钳工，他跟我说，机器"
    "好不好用，看的是换模时要不要骂人——这话糙，但比论文里那些指标实在。\n\n"
    "我一开始照着文献里那套做，结果第三次试切就把刀崩了，声音特别闷，像敲在湿木头上。"
    "后来把进给量降到零点零八，冷却液从乳化液换成半合成，才算稳住。数据不漂亮，样本"
    "也少，只有十七组，但我敢拿它去跟车间主任掰扯。\n\n"
    "回学校以后导师让我补一组对照，我拖了三周。不是懒，是那批料被仓库弄混了，标号"
    "贴错，等查清楚已经赶不上中期。图二那两条线看着分得开，其实我心里清楚，里面至少"
    "有三组是硬凑的，我在附录里都标了。你要问我这结论能不能用，我给你个保守的说法："
    "趋势可信，数值别直接抄。"
)


# ---------------------------------------------------------------- 切分
def test_split_sentences_handles_mixed_punctuation():
    sentences = analyzer.split_sentences("这是第一句。这是第二句！Is this the third one? 是的；还有第四句")
    assert len(sentences) == 5, "中英标点与分号都要断句"
    assert sentences[0] == "这是第一句。"
    assert sentences[2] == "Is this the third one?"


def test_split_sentences_keeps_decimal_point():
    sentences = analyzer.split_sentences("进给量降到 0.08 毫米后再试了一次。")
    assert len(sentences) == 1


def test_split_paragraphs_drops_blank_lines():
    paragraphs = analyzer.split_paragraphs("第一段\n\n\n第二段\n第三段")
    assert paragraphs == ["第一段", "第二段", "第三段"]


def test_tokenize_covers_chinese_and_latin():
    tokens = analyzer.tokenize("使用 ResNet50 处理 128 张图片")
    assert "resnet" in tokens and "50" in tokens and "128" in tokens
    assert "使" in tokens and "张" in tokens


def test_template_hits_does_not_overlap():
    hits = analyzer.template_hits("综上所述，本研究基于该方法")
    assert "综上所述" in hits
    assert "综上" not in hits, "长短语优先匹配，不应再切出短短语"


# ---------------------------------------------------------------- 参考文献
def test_reference_section_is_excluded():
    text = (
        "本文提出了一种融合多源数据的预测模型，并通过对比实验验证了所提方法的有效性。\n\n"
        "参考文献\n\n"
        "[1] 张三. 深度学习综述[J]. 计算机学报, 2020, 43(5): 100-110.\n"
        "[2] 李四. 制造业转型升级研究[M]. 北京: 机械工业出版社, 2019.\n"
    )
    report = analyze_text(text)
    assert report.reference_paragraphs == 3, "标题与其后所有文献条目都不检测"
    assert report.sentence_count == 1
    assert all("张三" not in item.text and "机械工业" not in item.text for item in report.sentences)


def test_reference_heading_variants():
    for line in ("参考文献", "参考文献：", "References", "Bibliography", "[1] 参考文献"):
        assert analyzer.is_reference_heading(line), line
    assert not analyzer.is_reference_heading("参考文献的著录格式说明如下")
    assert not analyzer.is_reference_heading("本文参考文献了已有的建模方法")


def test_reference_entries_without_heading():
    text = (
        "本研究提出了一种融合多源数据的预测模型，并通过对比实验验证了所提方法的有效性。\n\n"
        "[1] 张三. 深度学习综述[J]. 计算机学报, 2020.\n"
        "2. 李四. 制造业研究[M]. 机械工业出版社, 2019.\n"
        "1. 实验步骤如下：先采集数据，再做预处理，最后训练模型并评估效果。\n"
    )
    report = analyze_text(text)
    assert report.reference_paragraphs == 2, "只排除像文献条目的段落"
    assert any("实验步骤" in item.text for item in report.sentences), "正文编号列表不能误伤"


def test_strip_citations():
    assert analyzer.strip_citations("该方法已有研究[1]验证，效果显著[2-5]。") == "该方法已有研究验证，效果显著。"
    assert analyzer.strip_citations("见文献[1,3]与[12]") == "见文献与"
    assert analyzer.strip_citations("没有任何角标") == "没有任何角标"


def test_reference_section_ends_at_appendix():
    text = (
        "本文提出了一种融合多源数据的预测模型，并通过对比实验验证了所提方法的有效性。\n\n"
        "参考文献\n\n"
        "[1] 张三. 深度学习综述[J]. 计算机学报, 2020.\n"
        "附录\n\n"
        "实验数据采集于二零二三年三月至六月，现场记录由两名工程师共同核对完成。\n"
    )
    report = analyze_text(text)
    assert report.reference_paragraphs == 2, "只跳过文献表本身"
    assert any("现场记录" in item.text for item in report.sentences), "附录要照常检测"


def test_reference_text_does_not_move_rate():
    body = (
        "综上所述，随着人工智能技术的不断发展，本研究针对当前制造业转型升级过程中存在的"
        "实际问题，提出了一种基于深度学习的智能优化方法。"
    )
    base = analyze_text(body)
    with_refs = analyze_text(
        body + "\n\n参考文献\n\n[1] 张三. 深度学习综述[J]. 计算机学报, 2020, 43(5): 100-110.\n"
    )
    assert with_refs.aigc_rate == base.aigc_rate


# ---------------------------------------------------------------- 章节标题 / 目录
def test_is_heading_detects_numbered_and_toc():
    for line in (
        "6.5.2 性能测试结果\t31",
        "第三章 车牌检测数据集构建",
        "第3章 系统设计",
        "3.2.1 数据预处理",
        "一、研究背景",
        "Chapter 3 System Design",
        "参考文献\t45",
        "本章小结",
    ):
        assert analyzer.is_heading(line), line


def test_is_heading_ignores_body_text():
    for line in (
        "3.2 节介绍了系统的整体架构",
        "1. 实验步骤如下：先采集数据，再做预处理，最后训练模型并评估效果。",
        "2020.3.5 我们开始了第一轮实验并记录了完整数据。",
        "综上所述，随着人工智能技术的不断发展，本研究提出了一种新的方法。",
        "节 6.5.2 中给出了性能测试的完整结果，这里不再重复。",
    ):
        assert not analyzer.is_heading(line), line


def test_headings_are_excluded_from_detection():
    text = (
        "第三章 车牌检测数据集构建\n\n"
        "6.5.2 性能测试结果\n\n"
        "本节对数据集构建过程进行说明，并通过数据增强扩充了样本规模。\n"
    )
    report = analyze_text(text)
    assert report.heading_paragraphs == 2
    assert report.sentence_count == 1
    assert all("车牌检测" not in item.text for item in report.sentences)


def test_toc_block_is_excluded():
    toc = "目录\n\n第一章 绪论\t1\n第二章 相关技术\t5\n第三章 系统设计\t12\n"
    report = analyze_text(toc)
    assert report.sentences == []
    assert report.heading_paragraphs == 4


def test_heading_does_not_move_rate():
    body = (
        "综上所述，随着人工智能技术的不断发展，本研究针对当前制造业转型升级过程中存在的"
        "实际问题，提出了一种基于深度学习的智能优化方法。"
    )
    base = analyze_text(body)
    with_heading = analyze_text("第三章 系统设计\n\n" + body)
    assert with_heading.aigc_rate == base.aigc_rate
    assert with_heading.heading_paragraphs == 1


def test_numbered_list_item_is_not_a_heading():
    """正文里的编号列表不是标题，不能被当成标题吃掉。"""
    report = analyze_text("1. 实验步骤如下：先采集数据，再做预处理，最后训练模型并评估效果。")
    assert report.sentence_count == 1
    assert report.heading_paragraphs == 0


# ---------------------------------------------------------------- 短句保护
def test_short_sentence_is_never_judged_ai():
    """短句不会被单独判成 AI：能并的都并进邻句，不能并的按人工处理。"""
    text = (
        "本研究采用 B/S 架构实现系统。\n"
        "前端使用 Vue 框架。\n"
        "综上所述，随着人工智能技术的不断发展，本研究针对当前制造业转型升级过程中存在的"
        "实际问题，提出了一种基于深度学习的智能优化方法，并通过对比实验完成了验证。\n"
    )
    report = analyze_text(text)
    short = [item for item in report.sentences if item.length < analyzer.SHORT_SENTENCE_CHARS]
    assert all(item.level == LEVEL_HUMAN for item in short), "残留的短单元一律按人工处理"
    assert all(item.counted is False for item in short)
    assert report.uncounted_count == len(short)


def test_isolated_short_paragraph_stays_human():
    """开头就是短句时没有可并的对象，按人工处理且不计入。"""
    report = analyze_text("本系统采用 B/S 架构。")
    assert report.sentence_count == 1
    item = report.sentences[0]
    assert item.level == LEVEL_HUMAN and item.counted is False
    assert "过短" in item.reasons[0]


def test_only_short_sentences_give_zero_rate():
    report = analyze_text("前端使用 Vue 框架。\n系统采用三层架构设计。\n")
    assert report.aigc_rate == 0.0, "内容本身没有 AI 特征，合并后也不该凭空得分"
    assert report.risk == "low"


def test_short_sentences_are_not_dropped_from_display():
    """短句照常出现在标注列表里（只是按人工处理），不能悄悄消失。"""
    report = analyze_text("本研究采用 B/S 架构实现系统。")
    assert report.sentence_count == 1
    assert report.sentences[0].level == LEVEL_HUMAN


def test_min_length_fragments_are_dropped():
    """只有几个字的碎片（图注、表号）不会贡献 AIGC 率。"""
    report = analyze_text("B/S架构。\n图 3-1\n表 2")
    assert all(not item.counted for item in report.sentences)
    assert report.aigc_rate == 0.0


# ---------------------------------------------------------------- 短句合并
AI_SHORT = (
    "本文提出一种新方法。该方法效果显著。实验数据充分。结果表明有效。综上，该方法可行。\n\n"
    "首先构建模型。其次采集数据。然后训练参数。最后评估效果。\n\n"
    "准确率显著提升。召回率保持稳定。综上所述，方法具有优势。"
)
HUMAN_SHORT = (
    "我在现场蹲了两个月。师傅姓王，干了二十多年钳工。机器好不好用，看换模时骂不骂人。"
    "这话糙，但比论文里的指标实在。\n\n"
    "刀崩了三次。声音很闷，像敲湿木头。进给量降到零点零八才算稳住。数据不漂亮，样本也少。"
)


def test_merge_short_sentences_groups_until_long_enough():
    text = "本文提出新方法。该方法效果显著。实验数据充分。结果表明有效。"
    spans = analyzer.split_sentence_spans(text)
    assert len(spans) == 4

    groups = analyzer.merge_short_sentences(spans)
    assert len(groups) == 1, "连续短句应当并成一个检测单元"
    start, end = groups[0][0][0], groups[0][-1][1]
    assert text[start:end] == text, "合并后不能丢字"


def test_merge_leaves_long_sentences_intact():
    text = "综上所述，随着人工智能技术的不断发展，本研究提出了一种新的方法。"
    groups = analyzer.merge_short_sentences(analyzer.split_sentence_spans(text))
    assert len(groups) == 1 and len(groups[0]) == 1, "够长的句子不该被并进去"


def test_merge_appends_trailing_short_sentence():
    """段落末尾凑不满的残留要并入上一组，不能被丢掉。"""
    text = "本段开头是一句足够长的说明性文字。尾部很短。"
    spans = analyzer.split_sentence_spans(text)
    groups = analyzer.merge_short_sentences(spans)
    assert len(groups) == 1
    start, end = groups[0][0][0], groups[0][-1][1]
    assert "尾部很短。" in text[start:end]


def test_short_paragraphs_are_merged_before_detection():
    """一行一句时，段内没处可并 —— 短段落要先并进上一段，否则全篇都是「过短」。"""
    text = (
        "首先，本文提出一种新的车牌识别方法。\n"
        "该方法在多个数据集上进行了验证。\n"
        "实验数据来自实地采集。\n"
        "综上所述，该方法具有良好的实用价值。\n"
    )
    report = analyze_text(text)
    assert report.uncounted_count == 0, "四行短句并在一起后不该再出现「过短」"
    assert report.counts()[LEVEL_AI] + report.counts()[LEVEL_SUSPECT] > 0


def test_merge_paragraphs_keeps_line_breaks():
    """合并段落只影响判定单元，原文换行必须留在文本里（导出时再转 <br/>）。"""
    layout = analyzer.classify_paragraphs(["一句话很短。", "另一句也很短。"])
    assert len(layout) == 1
    assert layout[0].text == "一句话很短。\n另一句也很短。"


def test_ai_written_in_short_sentences_is_detected():
    """回归：通篇短句的 AI 文本不能因为「每句都太短」而整篇漏检。"""
    report = analyze_text(AI_SHORT)
    counts = report.counts()
    assert counts[LEVEL_AI] or counts[LEVEL_SUSPECT], "短句合并后应当能标出可疑内容"
    assert report.aigc_rate >= 30.0
    assert report.uncounted_count == 0, "短句应被合并判定，而不是整段按「过短」放过"


def test_human_short_sentences_stay_human():
    report = analyze_text(HUMAN_SHORT)
    assert report.counts()[LEVEL_AI] == 0
    assert report.aigc_rate == 0.0
    assert report.risk == "low"


# ---------------------------------------------------------------- 打分
def test_ai_text_scores_higher_than_human_text():
    ai = analyze_text(AI_TEXT, source="AI")
    human = analyze_text(HUMAN_TEXT, source="人")
    assert ai.aigc_rate > human.aigc_rate + 30, "AI 文本的 AIGC 率应显著高于人工文本"
    assert ai.counts()[LEVEL_AI] >= ai.sentence_count * 0.6
    assert human.counts()[LEVEL_AI] == 0, "这样的人工文本不该出现整句判为 AI"


def test_mixed_text_separates_two_kinds():
    mixed = analyze_text(AI_TEXT + "\n" + HUMAN_TEXT, source="混合")
    counts = mixed.counts()
    assert counts[LEVEL_AI] > 0 and counts[LEVEL_HUMAN] > 0
    assert 30 < mixed.aigc_rate < 80


def test_strict_sensitivity_flags_more_sentences():
    lenient = analyze_text(AI_TEXT + "\n" + HUMAN_TEXT, sensitivity="lenient")
    strict = analyze_text(AI_TEXT + "\n" + HUMAN_TEXT, sensitivity="strict")
    assert strict.counts()[LEVEL_AI] >= lenient.counts()[LEVEL_AI]
    assert strict.aigc_rate >= lenient.aigc_rate
    assert strict.thresholds[0] < lenient.thresholds[0]


def test_sensitivity_falls_back_to_standard():
    report = analyze_text(AI_TEXT, sensitivity="不存在的档位")
    assert report.sensitivity == analyzer.DEFAULT_SENSITIVITY


def test_empty_text_returns_empty_report():
    report = analyze_text("")
    assert report.sentences == [] and report.aigc_rate == 0.0
    assert report.paragraph_count == 0


def test_short_fragments_are_skipped():
    report = analyze_text("一\n二\n三\n四")
    assert report.sentences == [], "过短碎片不该进入统计"


def test_score_ratio_sums_to_rate():
    report = analyze_text(AI_TEXT + "\n" + HUMAN_TEXT)
    total = sum(report.score_ratio().values())
    assert abs(total - report.aigc_rate) < 0.5


def test_rate_is_zero_when_nothing_is_flagged():
    """回归：AI 与疑似都是 0 句时，AIGC 率必须是 0。

    早期口径直接对倾向分取加权平均，纯人工文稿也会显示 20~30%，和三档计数
    对不上 —— 这里把「三档计数 ⇒ AIGC 率」这条不变量钉住。
    """
    report = analyze_text(HUMAN_TEXT)
    counts = report.counts()
    assert counts[LEVEL_AI] == 0 and counts[LEVEL_SUSPECT] == 0
    assert report.aigc_rate == 0.0
    assert report.risk == "low"


def test_rate_equals_weighted_level_share():
    """AIGC 率 =（AI 字数 × 1.0 + 疑似字数 × 0.5）÷ 参与统计的字数。"""
    report = analyze_text(AI_TEXT + "\n" + HUMAN_TEXT)
    counted = [item for item in report.sentences if item.counted]
    total = sum(item.length for item in counted)
    expected = sum(
        analyzer.LEVEL_WEIGHTS[item.level] * item.length for item in counted
    ) / total * 100
    assert abs(report.aigc_rate - expected) < 0.15


def test_average_score_is_kept_as_reference():
    """倾向分的加权平均另存为 average_score，供报告里交叉参考。"""
    report = analyze_text(AI_TEXT + "\n" + HUMAN_TEXT)
    assert 0.0 < report.average_score < 1.0


def test_char_ratio_sums_to_hundred():
    report = analyze_text(AI_TEXT)
    assert abs(sum(report.char_ratio().values()) - 100.0) < 0.5


def test_level_labels_cover_all_levels():
    assert set(LEVEL_LABELS) == {LEVEL_AI, LEVEL_SUSPECT, LEVEL_HUMAN}


def test_risk_of_steps():
    assert risk_of(10.0)[0] == "low"
    assert risk_of(30.0)[0] == "medium"
    assert risk_of(50.0)[0] == "high"
    assert risk_of(90.0)[0] == "critical"


def test_analysis_is_deterministic():
    first = analyze_text(AI_TEXT + "\n" + HUMAN_TEXT)
    second = analyze_text(AI_TEXT + "\n" + HUMAN_TEXT)
    assert [round(item.score, 6) for item in first.sentences] == [
        round(item.score, 6) for item in second.sentences
    ]


# ---------------------------------------------------------------- 文件
def test_analyze_file_reads_text(tmp_path):
    target = tmp_path / "论文.txt"
    target.write_text(AI_TEXT, encoding="utf-8")
    report = analyze_file(target)
    assert report.source == "论文.txt"
    assert report.sentence_count > 0
    assert report.aigc_rate > 50


def test_analyze_file_rejects_unsupported(tmp_path):
    target = tmp_path / "表格.xlsx"
    target.write_bytes(b"x")
    try:
        analyze_file(target)
    except ValueError as exc:
        assert "暂不支持" in str(exc)
    else:      # pragma: no cover - 走到这里说明没拦住
        raise AssertionError("不支持的类型应报 ValueError")


def test_analyze_file_reports_missing(tmp_path):
    try:
        analyze_file(tmp_path / "不存在.docx")
    except FileNotFoundError:
        pass
    else:      # pragma: no cover
        raise AssertionError("文件不存在应报 FileNotFoundError")


def test_progress_callback_reaches_100(tmp_path):
    target = tmp_path / "论文.txt"
    target.write_text(AI_TEXT, encoding="utf-8")
    seen: list[int] = []
    analyze_file(target, progress=lambda _step, percent: seen.append(percent))
    assert seen and seen[-1] == 100


# ---------------------------------------------------------------- 原文段落流
def test_layout_keeps_skipped_paragraphs():
    report = analyze_text(
        "第三章 系统设计\n\n"
        "本节围绕车牌检测数据集的构建过程展开说明，并通过数据增强扩充了样本规模。\n\n"
        "参考文献\n\n[1] 张三. 深度学习综述[J]. 计算机学报, 2020.\n"
    )
    kinds = [block.kind for block in report.layout]
    assert kinds == [analyzer.KIND_HEADING, analyzer.KIND_BODY,
                     analyzer.KIND_REFERENCE, analyzer.KIND_REFERENCE]
    assert report.paragraph_count == 1, "只有正文段落算检测段落"


def test_sentence_spans_point_into_paragraph():
    report = analyze_text(
        "本节围绕数据集构建展开说明。首先，本文采集了三千张图像，并通过数据增强扩充样本。"
    )
    block = report.layout[0]
    assert report.sentences
    for item in report.sentences:
        assert block.text[item.start:item.end] == item.text


def test_paragraph_can_be_rebuilt_verbatim():
    """按 span 把段落拼回去必须和原文一字不差 —— 导出就是靠这个还原排版。"""
    text = "本节围绕数据集构建展开说明。首先，本文采集了三千张图像，并通过数据增强扩充了样本规模。"
    report = analyze_text(text)
    block = report.layout[0]
    pieces: list[str] = []
    cursor = 0
    for item in report.sentences:
        pieces.append(block.text[cursor:item.start])
        pieces.append(block.text[item.start:item.end])
        cursor = item.end
    pieces.append(block.text[cursor:])
    assert "".join(pieces) == text


def test_paragraph_markup_colors_each_sentence():
    from paper_agent.services.aigc import report as report_mod

    text = (
        "综上所述，随着人工智能技术的不断发展，本研究提出了一种基于深度学习的智能优化方法。"
        "我在车间蹲了两个月才想明白这件事，师傅姓王，干了二十多年钳工。"
    )
    result = analyze_text(text)
    block = result.layout[0]
    verdicts = {(item.paragraph, item.start): item for item in result.sentences}
    markup = report_mod._paragraph_markup(block, 0, verdicts)

    assert markup.count("<font color=") == len(result.sentences)
    for item in result.sentences:
        assert f'<font color="{LEVEL_COLORS[item.level]}">' in markup
        assert item.text in markup


def test_unjudged_fragment_keeps_neutral_color():
    """没被判定的短碎片也要出现在原文里，只是用中性灰。"""
    from paper_agent.services.aigc import report as report_mod

    text = "B/S 架构。本节围绕数据集构建展开说明，并通过数据增强扩充了样本规模。"
    result = analyze_text(text)
    block = result.layout[0]
    verdicts = {(item.paragraph, item.start): item for item in result.sentences}
    markup = report_mod._paragraph_markup(block, 0, verdicts)
    assert "B/S 架构。" in markup
    assert report_mod.SKIPPED_COLOR in markup


# ---------------------------------------------------------------- 导出
def test_export_report_pdf(tmp_path):
    report = analyze_text(AI_TEXT + "\n" + HUMAN_TEXT, source="论文.txt")
    target = tmp_path / "报告.pdf"
    written = export_report_pdf(report, target)
    assert Path(written).is_file() and Path(written).stat().st_size > 1000


def test_export_report_pdf_handles_empty_report(tmp_path):
    target = tmp_path / "空.pdf"
    export_report_pdf(analyze_text(""), target)
    assert target.is_file()


def test_pdf_page_one_is_report_page_two_is_document(tmp_path):
    """第 1 页是结论，第 2 页起是按原文排版的标注稿。"""
    import pytest

    PdfReader = pytest.importorskip("pypdf").PdfReader

    text = (
        "第三章 系统设计\n\n"
        "综上所述，随着人工智能技术的不断发展，本研究提出了一种基于深度学习的智能优化方法。"
        "首先，本文对相关领域的研究现状进行了系统梳理，明确了现有方法存在的不足。\n\n"
        "参考文献\n\n[1] 张三. 深度学习综述[J]. 计算机学报, 2020.\n"
    )
    report = analyze_text(text, source="论文.docx")
    target = tmp_path / "报告.pdf"
    export_report_pdf(report, target)

    pages = PdfReader(str(target)).pages
    assert len(pages) >= 2
    first = pages[0].extract_text() or ""
    second = pages[1].extract_text() or ""
    assert "AIGC 检测报告" in first and "AIGC 率" in first
    assert "原文标注" in second
    assert "第三章 系统设计" in second, "原文里的章节标题要保留，方便对照"
    assert "综上所述" in second and "首先" in second, "正文段落要按原文拼回来"
    assert "计算机学报" in second, "参考文献照原样保留（中性色）"
    assert "第 1 段" not in second, "不再按「第 N 段」切碎展示"


# ---------------------------------------------------------------- 界面
def _dialog(tmp_path):
    """构造检测对话框；记录落到临时目录，避免污染真实数据目录。"""
    from paper_agent.services.aigc import AigcRecordStore
    from paper_agent.ui.dialogs.aigc_dialog import AigcDialog

    return AigcDialog(store=AigcRecordStore(tmp_path / "aigc"))


def test_dialog_loads_file_and_enables_buttons(qapp, tmp_path):
    target = tmp_path / "论文.txt"
    target.write_text(AI_TEXT, encoding="utf-8")
    dialog = _dialog(tmp_path)

    assert not dialog.detect_button.isEnabled()
    dialog._load_file(str(target))
    assert dialog.detect_button.isEnabled()
    assert dialog.file_label.text() == "论文.txt"
    assert dialog.export_button.isEnabled() is False, "还没检测不该能导出"

    dialog._load_file(str(tmp_path / "表格.xlsx"))
    assert dialog._path == str(target), "不支持的类型不应替换当前文件"


def test_dialog_renders_result_and_filter(qapp, tmp_path):
    target = tmp_path / "论文.txt"
    target.write_text(AI_TEXT + "\n" + HUMAN_TEXT, encoding="utf-8")
    dialog = _dialog(tmp_path)
    dialog._load_file(str(target))

    dialog._on_finished(analyze_file(target))

    view = dialog.result_view
    total = view.list_layout.count() - 1
    assert total == dialog._report.sentence_count
    assert dialog.export_button.isEnabled()

    view.set_filter(LEVEL_AI)
    ai_rows = view.list_layout.count() - 1
    assert ai_rows == dialog._report.counts()[LEVEL_AI]
    assert 0 < ai_rows < total

    assert view.stat_labels["sentences"].text() == str(dialog._report.sentence_count)


def test_dialog_saves_a_record(qapp, tmp_path):
    """检测完成要自动存一条记录，供「检测记录」回看。"""
    from paper_agent.services.aigc import AigcRecordStore

    target = tmp_path / "论文.txt"
    target.write_text(AI_TEXT, encoding="utf-8")
    store = AigcRecordStore(tmp_path / "aigc")
    from paper_agent.ui.dialogs.aigc_dialog import AigcDialog

    dialog = AigcDialog(store=store)
    dialog._load_file(str(target))
    dialog._on_finished(analyze_file(target))

    records = store.load_records()
    assert len(records) == 1
    assert records[0].source == "论文.txt"
    assert records[0].sentence_count == dialog._report.sentence_count
    assert abs(records[0].aigc_rate - dialog._report.aigc_rate) < 0.05


def test_dialog_failed_detection_keeps_usable(qapp, tmp_path):
    dialog = _dialog(tmp_path)
    dialog._on_failed("文件读取失败")
    assert dialog.detect_button.isEnabled()
    assert "失败" in dialog.status_label.text()


def test_sidebar_has_aigc_entry(qapp):
    from paper_agent.ui.sidebar.sidebar import Sidebar

    sidebar = Sidebar()
    assert hasattr(sidebar, "aigc_button")
    received: list[bool] = []
    sidebar.aigc_requested.connect(lambda: received.append(True))
    sidebar.aigc_button.click()
    assert received == [True]
