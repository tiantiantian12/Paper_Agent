"""文档预览内容构建：Word / PDF / PPT / 表格 / 图片 / 文本 / 异常。"""

from __future__ import annotations

from paper_agent.services.document_preview import PREVIEW_LIMIT, build_preview
from paper_agent.services.skills.document_skills import (
    build_csv,
    build_docx,
    build_pdf,
    build_pptx,
    build_xlsx,
)

PAPER_MD = """# 第一章 绪论

## 1.1 研究背景

自动识别车牌在智能交通里非常重要。

| 项目 | 配置 |
| --- | --- |
| CPU | i7-12700 |

# 参考文献

[1] 张三. 研究[J]. 2025.
"""


def test_docx_preview_keeps_structure(tmp_path):
    path = tmp_path / "论文.docx"
    build_docx(PAPER_MD, path, style="thesis")
    preview = build_preview(path)

    assert preview.kind == "markdown" and preview.available
    assert "# 第一章 绪论" in preview.content, "标题层级要保留"
    assert "## 1.1 研究背景" in preview.content
    assert "自动识别车牌" in preview.content
    assert "| 项目 | 配置 |" in preview.content, "表格要能预览"


def test_docx_preview_mentions_embedded_images(tmp_path, sample_png):
    path = tmp_path / "配图论文.docx"
    build_docx(f"# 第一章\n\n![图 1-1 架构]({sample_png})\n", path, style="thesis")
    preview = build_preview(path)
    assert "内嵌 1 张图片" in preview.content


def test_pdf_preview_pages(tmp_path):
    path = tmp_path / "论文.pdf"
    # 内容要够长，PDF 才有分页（单页时不显示页码标题）
    build_pdf("# 第一章 绪论\n\n" + "这里是用于分页的正文内容。" * 900 + "\n", path)
    preview = build_preview(path)
    assert preview.kind == "markdown"
    assert "第 1 页" in preview.content


def test_pptx_preview(tmp_path):
    path = tmp_path / "答辩.pptx"
    build_pptx("# 研究背景\n\n- 现状\n- 问题\n\n# 方法\n\n- 方案\n", path)
    preview = build_preview(path)
    assert "## 第 1 页" in preview.content and "研究背景" in preview.content


def test_csv_preview_is_table(tmp_path):
    path = tmp_path / "数据.csv"
    build_csv("名称,数值\n准确率,0.95\n召回率,0.93\n", path)
    preview = build_preview(path)
    assert preview.kind == "markdown"
    assert "| 名称 | 数值 |" in preview.content
    assert "| 准确率 | 0.95 |" in preview.content
    assert "| --- | --- |" in preview.content


def test_xlsx_preview_is_table(tmp_path):
    path = tmp_path / "数据.xlsx"
    build_xlsx("名称,数值\n准确率,0.95\n", path)
    preview = build_preview(path)
    assert "| 名称 | 数值 |" in preview.content


def test_image_preview_returns_path(tmp_path, sample_png):
    preview = build_preview(sample_png)
    assert preview.kind == "image" and preview.content == str(sample_png)


def test_text_and_markdown_preview(tmp_path):
    text = tmp_path / "笔记.txt"
    text.write_text("第一行\n第二行\n", encoding="utf-8")
    assert build_preview(text).kind == "text"

    markdown = tmp_path / "笔记.md"
    markdown.write_text("# 标题\n\n正文\n", encoding="utf-8")
    preview = build_preview(markdown)
    assert preview.kind == "markdown" and "# 标题" in preview.content


def test_unsupported_and_missing_files(tmp_path):
    unknown = tmp_path / "模型.bin"
    unknown.write_bytes(b"\x00\x01")
    preview = build_preview(unknown)
    assert not preview.available and "暂不支持预览" in preview.note

    missing = build_preview(tmp_path / "不存在.docx")
    assert not missing.available and "文件不存在" in missing.note


def test_empty_file(tmp_path):
    path = tmp_path / "空.md"
    path.write_text("   \n", encoding="utf-8")
    preview = build_preview(path)
    assert preview.content == "" and "为空" in preview.note


def test_long_content_is_truncated(tmp_path):
    path = tmp_path / "长文.md"
    path.write_text("内容。" * 2000, encoding="utf-8")
    preview = build_preview(path)
    assert preview.truncated is True
    assert len(preview.content) < len("内容。" * 2000)
    assert "仅预览前" in preview.content


def test_custom_limit(tmp_path):
    path = tmp_path / "长文.txt"
    path.write_text("a" * 100, encoding="utf-8")
    preview = build_preview(path, limit=10)
    assert preview.truncated and preview.content.startswith("a" * 10)


def test_default_limit_is_sane():
    assert PREVIEW_LIMIT >= 1000
