"""工具参数流 → 实时草稿正文的增量提取。"""

from __future__ import annotations

from paper_agent.services.stream_draft import DraftExtractor


def feed_all(extractor: DraftExtractor, fragments: list[str]) -> str:
    return "".join(extractor.feed(fragment) for fragment in fragments)


def test_full_arguments_in_one_piece():
    extractor = DraftExtractor()
    text = feed_all(
        extractor,
        ['{"filename": "论文.docx", "markdown": "# 第一章\\n\\n正文。", "doc_type": "thesis"}'],
    )
    assert text == "# 第一章\n\n正文。"
    assert extractor.total == len(text)
    assert extractor.finished


def test_fragments_are_rejoined_without_loss():
    raw = '{"filename": "a.docx", "markdown": "第一章 引言\\n\\n这里是正文[1]。"}'
    extractor = DraftExtractor()
    text = feed_all(extractor, [raw[i : i + 3] for i in range(0, len(raw), 3)])
    assert text == "第一章 引言\n\n这里是正文[1]。"


def test_escape_split_across_fragments():
    """反斜杠被切在两个分片之间时不能吐出半个字符。"""
    extractor = DraftExtractor()
    pieces = ['{"markdown": "第一行\\', 'n第二行"}']
    assert extractor.feed(pieces[0]) == "第一行"
    assert extractor.feed(pieces[1]) == "\n第二行"


def test_unicode_escape_split_across_fragments():
    extractor = DraftExtractor()
    assert extractor.feed('{"markdown": "\\u4e') == ""
    assert extractor.feed('2d\\u6587"}') == "中文"


def test_non_string_field_is_ignored():
    extractor = DraftExtractor()
    assert extractor.feed('{"markdown": null') == ""
    assert extractor.finished


def test_quotes_inside_content_do_not_end_early():
    extractor = DraftExtractor()
    text = feed_all(extractor, ['{"markdown": "他说：\\"这里\\"是引号。"}'])
    assert text == '他说："这里"是引号。'


def test_irrelevant_tool_arguments_produce_nothing():
    """参数里没有长文本字段（如 execute_python 只有 code）时什么都不吐。"""
    extractor = DraftExtractor()
    assert extractor.feed('{"code": "print(1)"}') == ""
    assert extractor.feed('print(2)"}') == ""


def test_content_field_alias_is_supported():
    extractor = DraftExtractor()
    assert feed_all(extractor, ['{"filename": "数据.csv", "content": "a,b\\n1,2"}']) == "a,b\n1,2"


def test_stops_after_string_ends():
    extractor = DraftExtractor()
    assert extractor.feed('{"markdown": "正文", "doc_type": "thesis"}') == "正文"
    assert extractor.feed(" 后续不应该被当成正文") == ""
