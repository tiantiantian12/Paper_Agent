"""导出 AIGC 检测报告（检测对话框与检测记录对话框共用）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QWidget

from paper_agent.services.aigc import export_report_pdf


def export_report_interactively(report, parent: QWidget | None = None) -> Path | None:
    """弹保存对话框导出 PDF，导出后自动打开；用户取消返回 None。

    Args:
        report: :class:`AigcReport`
        parent: 对话框的父窗口

    Raises:
        Exception: 导出失败时把原始异常抛给调用方去提示。
    """
    stem = Path(report.source or "文本").stem or "文本"
    path, _selected = QFileDialog.getSaveFileName(
        parent, "导出 AIGC 检测报告", f"AIGC检测报告_{stem}.pdf", "PDF 文档 (*.pdf)"
    )
    if not path:
        return None
    target = Path(path)
    if target.suffix.lower() != ".pdf":
        target = target.with_suffix(".pdf")
    export_report_pdf(report, target)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
    return target
