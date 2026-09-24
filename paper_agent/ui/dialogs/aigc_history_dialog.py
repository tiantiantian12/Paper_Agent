"""AIGC 检测记录：回看历史检测、重新导出 PDF、删除记录。

左侧是记录列表（文件名 / 时间 / AIGC 率 / 风险等级），右侧是与检测对话框
完全相同的 :class:`AigcResultView`，所以打开历史记录还能看到当时的逐句标注。
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from paper_agent.core.signals import signals
from paper_agent.services.aigc import (
    LEVEL_AI,
    LEVEL_HUMAN,
    LEVEL_LABELS,
    LEVEL_ORDER,
    LEVEL_SUSPECT,
    AigcRecordStore,
)
from paper_agent.ui.dialogs.aigc_export import export_report_interactively
from paper_agent.ui.widgets.aigc_result_view import AigcResultView


def _stamp(created_at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(created_at))


class _RecordCard(QFrame):
    """列表里的一条记录：文件名 + 时间 + AIGC 率 + 风险。"""

    def __init__(self, record, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("aigcRecordCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.record_id = record.id

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(3)

        head = QHBoxLayout()
        head.setSpacing(6)
        name = QLabel(record.source or "文本", self)
        name.setObjectName("aigcFileName")
        head.addWidget(name, 1)
        rate = QLabel(f"{record.aigc_rate:.1f}%", self)
        rate.setObjectName("aigcRecordRate")
        rate.setProperty("risk", record.risk)
        head.addWidget(rate)
        layout.addLayout(head)

        meta = QHBoxLayout()
        meta.setSpacing(8)
        stamp = QLabel(_stamp(record.created_at), self)
        stamp.setObjectName("aigcStatLabel")
        meta.addWidget(stamp)
        risk = QLabel(record.risk_label, self)
        risk.setObjectName("aigcBadge")
        risk.setProperty("level", _risk_to_level(record.risk))
        meta.addWidget(risk)
        meta.addStretch(1)
        counts = "　".join(
            f"{LEVEL_LABELS[level]} {record.counts.get(level, 0)}" for level in LEVEL_ORDER
        )
        detail = QLabel(counts, self)
        detail.setObjectName("aigcStatLabel")
        meta.addWidget(detail)
        layout.addLayout(meta)


def _risk_to_level(risk: str) -> str:
    """风险等级 → 档位（只用来给徽标挑颜色）。"""
    if risk in ("high", "critical"):
        return LEVEL_AI
    if risk == "medium":
        return LEVEL_SUSPECT
    return LEVEL_HUMAN


class AigcHistoryDialog(QDialog):
    """检测记录对话框。"""

    records_changed = Signal()

    def __init__(
        self, parent: QWidget | None = None, store: AigcRecordStore | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("aigcHistoryDialog")
        self.setWindowTitle("AIGC 检测记录")
        self.resize(1060, 700)
        self.setMinimumSize(880, 560)

        self._store = store or AigcRecordStore()
        self._records: list = []
        self._current_id = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        title = QLabel("AIGC 检测记录", self)
        title.setObjectName("welcomeTitle")
        root.addWidget(title)

        self.subtitle = QLabel("", self)
        self.subtitle.setObjectName("welcomeSubtitle")
        root.addWidget(self.subtitle)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setHandleWidth(1)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self._build_list())
        self.result_view = AigcResultView(self, empty_text="从左侧选一条记录查看当时的标注")
        self.splitter.addWidget(self.result_view)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([320, 640])
        root.addWidget(self.splitter, 1)

        root.addLayout(self._build_footer())
        self._rebuild()

    # ------------------------------------------------------------------ 构建
    def _build_list(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(6)

        self.record_list = QListWidget(panel)
        self.record_list.setObjectName("aigcRecordList")
        self.record_list.setSpacing(0)
        self.record_list.setFrameShape(QFrame.Shape.NoFrame)
        self.record_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.record_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.record_list.currentItemChanged.connect(self._on_current_changed)
        layout.addWidget(self.record_list, 1)

        self.empty_label = QLabel(
            "还没有检测记录\n检测一次文档后会自动存到这里", self
        )
        self.empty_label.setObjectName("aigcEmpty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        layout.addWidget(self.empty_label)
        return panel

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 2, 0, 0)
        row.setSpacing(8)

        hint = QLabel("记录保存在应用数据目录 aigc/ 下，最多保留最近 50 条。", self)
        hint.setObjectName("aigcHint")
        row.addWidget(hint, 1)

        self.export_button = QPushButton("导出 PDF 报告", self)
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export_current)
        row.addWidget(self.export_button)

        self.delete_button = QPushButton("删除", self)
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self._delete_current)
        row.addWidget(self.delete_button)

        self.clear_button = QPushButton("清空", self)
        self.clear_button.clicked.connect(self._clear_all)
        row.addWidget(self.clear_button)

        close_button = QPushButton("关闭", self)
        close_button.clicked.connect(self.accept)
        row.addWidget(close_button)
        return row

    # ------------------------------------------------------------------ 数据
    def _rebuild(self) -> None:
        self._records = self._store.load_records()
        self.record_list.blockSignals(True)
        self.record_list.clear()
        for record in self._records:
            item = QListWidgetItem(self.record_list)
            card = _RecordCard(record, self.record_list)
            item.setSizeHint(card.sizeHint())
            item.setData(Qt.ItemDataRole.UserRole, record.id)
            self.record_list.addItem(item)
            self.record_list.setItemWidget(item, card)
        self.record_list.blockSignals(False)

        has = bool(self._records)
        self.empty_label.setVisible(not has)
        self.record_list.setVisible(has)
        self.clear_button.setEnabled(has)
        self.subtitle.setText(f"共 {len(self._records)} 条记录" if has else "暂无记录")

        if not has:
            self._current_id = ""
            self.result_view.clear()
            self._sync_actions()
            return
        # 尽量停在原来那条上；否则回到最新一条
        target = next(
            (i for i, item in enumerate(self._records) if item.id == self._current_id), 0
        )
        self.record_list.setCurrentRow(target)

    def _sync_actions(self) -> None:
        enabled = bool(self._current_id)
        self.export_button.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)

    def _current_record(self):
        return next((item for item in self._records if item.id == self._current_id), None)

    def _on_current_changed(self, current: QListWidgetItem | None, _previous) -> None:
        if current is None:
            return
        record_id = str(current.data(Qt.ItemDataRole.UserRole) or "")
        self._current_id = record_id
        report = self._store.load_report(record_id) if record_id else None
        if report is None:
            self.result_view.clear()
            signals.toast_requested.emit("这条记录已损坏或已删除")
        else:
            self.result_view.set_report(report)
        self._sync_actions()

    # ------------------------------------------------------------------ 操作
    def _export_current(self) -> None:
        if not self._current_id:
            return
        report = self._store.load_report(self._current_id)
        if report is None:
            signals.toast_requested.emit("这条记录已损坏或已删除")
            return
        try:
            target = export_report_interactively(report, self)
        except Exception as exc:      # noqa: BLE001 - 导出失败要给用户可见的原因
            signals.toast_requested.emit(f"导出失败：{exc}")
            return
        if target is not None:
            signals.toast_requested.emit(f"已导出到 {target}")

    def _delete_current(self) -> None:
        record = self._current_record()
        if record is None:
            return
        answer = QMessageBox.question(
            self,
            "删除检测记录",
            f"确定删除「{record.source}」这条记录吗？删除后无法恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._store.delete(record.id)
        self._current_id = ""
        self._rebuild()
        self.records_changed.emit()
        signals.toast_requested.emit("已删除该检测记录")

    def _clear_all(self) -> None:
        if not self._records:
            return
        answer = QMessageBox.question(
            self,
            "清空检测记录",
            f"确定清空全部 {len(self._records)} 条检测记录吗？删除后无法恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = self._store.clear()
        self._current_id = ""
        self._rebuild()
        self.records_changed.emit()
        signals.toast_requested.emit(f"已清空 {removed} 条检测记录")
