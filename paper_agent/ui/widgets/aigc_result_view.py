"""AIGC 检测结果视图：环形 AIGC 率 + 三档统计 + 逐句标注。

检测对话框与检测记录对话框共用这一个视图，保证两处看到的标注完全一致。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from paper_agent.services.aigc import (
    LEVEL_AI,
    LEVEL_HUMAN,
    LEVEL_LABELS,
    LEVEL_ORDER,
    LEVEL_SUSPECT,
)
from paper_agent.ui.widgets.aigc_gauge import AigcGauge

# 筛选：key 与档位一致，"all" 表示不筛选
FILTERS: tuple[tuple[str, str], ...] = (
    ("all", "全部"),
    (LEVEL_AI, LEVEL_LABELS[LEVEL_AI]),
    (LEVEL_SUSPECT, LEVEL_LABELS[LEVEL_SUSPECT]),
    (LEVEL_HUMAN, LEVEL_LABELS[LEVEL_HUMAN]),
)


class SentenceRow(QFrame):
    """一句的判定结果：左侧色条 + 档位徽标 + 正文 + 命中理由。"""

    def __init__(self, verdict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("aigcSentence")
        self.setProperty("level", verdict.level)
        self.setFrameShape(QFrame.Shape.NoFrame)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)

        stripe = QFrame(self)
        stripe.setObjectName("aigcStripe")
        stripe.setProperty("level", verdict.level)
        stripe.setFixedWidth(3)
        stripe.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        layout.addWidget(stripe)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)

        head = QHBoxLayout()
        head.setSpacing(6)
        badge = QLabel(verdict.level_label, self)
        badge.setObjectName("aigcBadge")
        badge.setProperty("level", verdict.level)
        head.addWidget(badge)

        score = QLabel(f"倾向 {verdict.score:.2f}", self)
        score.setObjectName("aigcStatLabel")
        head.addWidget(score)
        head.addStretch(1)
        column.addLayout(head)

        text = QLabel(verdict.text, self)
        text.setObjectName("aigcSentenceText")
        text.setProperty("level", verdict.level)
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(text)

        if verdict.reasons:
            reason = QLabel("　".join(verdict.reasons), self)
            reason.setObjectName("aigcReason")
            reason.setWordWrap(True)
            column.addWidget(reason)

        layout.addLayout(column, 1)


class DistributionBar(QFrame):
    """三档字数占比的堆叠条。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("aigcDistribution")
        self.setFixedHeight(10)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._segments = {}
        for level in LEVEL_ORDER:
            segment = QFrame(self)
            segment.setObjectName("aigcStripe")
            segment.setProperty("level", level)
            layout.addWidget(segment, 0)
            self._segments[level] = segment

    def set_ratios(self, ratios: dict[str, float]) -> None:
        total = sum(max(ratios.get(level, 0.0), 0.0) for level in LEVEL_ORDER)
        for level in LEVEL_ORDER:
            weight = ratios.get(level, 0.0)
            if total <= 0 or weight <= 0:
                self._segments[level].setVisible(False)
                continue
            self._segments[level].setVisible(True)
            # 拉伸系数按占比分配：至少给 1，避免占比极小时被压成看不见的一条
            self.layout().setStretchFactor(self._segments[level], max(int(weight * 10), 1))


class AigcResultView(QWidget):
    """检测结果：左边结论与统计，右边逐句标注。"""

    def __init__(self, parent: QWidget | None = None, empty_text: str = "") -> None:
        super().__init__(parent)
        self.setObjectName("aigcResultView")
        self._report = None
        self._filter = "all"

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setHandleWidth(1)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self._build_summary())
        self.splitter.addWidget(self._build_sentences())
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([300, 620])
        root.addWidget(self.splitter, 1)

        self.empty_label.setText(
            empty_text or "选择文档后点「开始检测」，结果会按句标色显示在这里"
        )

    # ------------------------------------------------------------------ 构建
    def _build_summary(self) -> QWidget:
        panel = QWidget(self)
        panel.setObjectName("aigcSummary")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 2, 12, 2)
        layout.setSpacing(10)

        gauge_row = QHBoxLayout()
        gauge_row.addStretch(1)
        self.gauge = AigcGauge(panel)
        self.gauge.set_value(0.0)
        gauge_row.addWidget(self.gauge)
        gauge_row.addStretch(1)
        layout.addLayout(gauge_row)

        self.risk_label = QLabel("尚未检测", panel)
        self.risk_label.setObjectName("aigcStatValue")
        self.risk_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.risk_label)

        layout.addWidget(self._build_stat_row())
        layout.addWidget(self._build_legend())
        layout.addStretch(1)

        hint = QLabel(
            "判定依据：用词可预测性、搭配复用率、模板化表达、句长规整度与突发性，"
            "并按全文冗余度做整体校准。",
            panel,
        )
        hint.setObjectName("aigcHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return panel

    def _build_stat_row(self) -> QWidget:
        host = QWidget(self)
        layout = QHBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.stat_labels: dict[str, QLabel] = {}
        for key, caption in (
            ("sentences", "句子"),
            ("chars", "字数"),
            (LEVEL_AI, LEVEL_LABELS[LEVEL_AI]),
            (LEVEL_SUSPECT, LEVEL_LABELS[LEVEL_SUSPECT]),
            (LEVEL_HUMAN, LEVEL_LABELS[LEVEL_HUMAN]),
        ):
            cell = QFrame(host)
            cell.setObjectName("formCard")
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(8, 6, 8, 6)
            cell_layout.setSpacing(2)
            value = QLabel("—", cell)
            value.setObjectName("aigcStatValue")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            caption_label = QLabel(caption, cell)
            caption_label.setObjectName("aigcStatLabel")
            caption_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell_layout.addWidget(value)
            cell_layout.addWidget(caption_label)
            layout.addWidget(cell)
            self.stat_labels[key] = value
        return host

    def _build_legend(self) -> QWidget:
        host = QWidget(self)
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.distribution = DistributionBar(host)
        self.distribution.setVisible(False)
        layout.addWidget(self.distribution)
        self.legend_label = QLabel("", host)
        self.legend_label.setObjectName("aigcHint")
        self.legend_label.setWordWrap(True)
        layout.addWidget(self.legend_label)
        return host

    def _build_sentences(self) -> QWidget:
        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 2, 0, 2)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        header.addWidget(QLabel("逐句标注", panel))
        self.filter_group = QButtonGroup(self)
        self.filter_group.setExclusive(True)
        for key, text in FILTERS:
            button = QPushButton(text, panel)
            button.setObjectName("aigcFilter")
            button.setCheckable(True)
            button.setChecked(key == "all")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, k=key: self.set_filter(k))
            self.filter_group.addButton(button)
            header.addWidget(button)
        header.addStretch(1)
        self.filter_count = QLabel("", panel)
        self.filter_count.setObjectName("aigcFileMeta")
        header.addWidget(self.filter_count)
        layout.addLayout(header)

        self.scroll = QScrollArea(panel)
        self.scroll.setObjectName("previewScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 10, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_host)
        layout.addWidget(self.scroll, 1)

        self.empty_label = QLabel("", panel)
        self.empty_label.setObjectName("aigcEmpty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        layout.addWidget(self.empty_label)
        return panel

    # ------------------------------------------------------------------ 数据
    @property
    def report(self):
        return self._report

    def set_report(self, report) -> None:
        """渲染一次检测结果。"""
        self._report = report
        self.gauge.set_value(report.aigc_rate, report.risk, "AIGC 率")
        self.risk_label.setText(report.risk_label)
        counts = report.counts()
        self.stat_labels["sentences"].setText(str(report.sentence_count))
        self.stat_labels["chars"].setText(str(report.char_count))
        for level in LEVEL_ORDER:
            self.stat_labels[level].setText(str(counts.get(level, 0)))

        ratios = report.char_ratio()
        self.distribution.setVisible(True)
        self.distribution.set_ratios(ratios)
        legend = "字数占比　" + "　".join(
            f"{LEVEL_LABELS[level]} {ratios.get(level, 0.0):.1f}%" for level in LEVEL_ORDER
        )
        if report.thresholds:
            legend += f"　·　阈值 {report.thresholds[0]:.2f} / {report.thresholds[1]:.2f}"
        # 折算规则写在这里：否则「三档都是 0，AIGC 率却有百分之几十」会让人困惑
        legend += (
            "\nAIGC 率 =（AI 句字数 × 1.0 + 疑似句字数 × 0.5）÷ 参与统计的总字数；"
            f"平均每句倾向 {report.average_score:.2f}"
        )
        excluded = self.excluded_text(report)
        if excluded:
            legend += "　·　" + excluded
        self.legend_label.setText(legend)
        self.set_filter(self._filter)

    def clear(self) -> None:
        """回到未检测状态。"""
        self._report = None
        self.gauge.set_value(0.0)
        self.risk_label.setText("尚未检测")
        for label in self.stat_labels.values():
            label.setText("—")
        self.distribution.setVisible(False)
        self.legend_label.setText("")
        self._clear_sentences()
        self.filter_count.setText("")
        self.scroll.setVisible(False)
        self.empty_label.setVisible(True)

    # ------------------------------------------------------------------ 筛选
    def set_filter(self, key: str) -> None:
        self._filter = key
        self._clear_sentences()
        sentences = self._visible_sentences()
        for verdict in sentences:
            self.list_layout.insertWidget(
                self.list_layout.count() - 1, SentenceRow(verdict, self.list_host)
            )
        self.filter_count.setText(f"{len(sentences)} 句" if sentences else "")
        self.scroll.setVisible(bool(sentences))
        self.empty_label.setVisible(not sentences)
        if not sentences and self._report is not None:
            self.empty_label.setText("当前筛选条件下没有句子")

    def _visible_sentences(self) -> list:
        if self._report is None:
            return []
        if self._filter == "all":
            return list(self._report.sentences)
        return [item for item in self._report.sentences if item.level == self._filter]

    def _clear_sentences(self) -> None:
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    @staticmethod
    def excluded_text(report) -> str:
        """被排除在检测之外的内容（参考文献、标题、过短句）。"""
        parts = []
        if getattr(report, "reference_paragraphs", 0):
            parts.append(f"参考文献 {report.reference_paragraphs} 段未检测")
        if getattr(report, "heading_paragraphs", 0):
            parts.append(f"章节标题 / 目录 {report.heading_paragraphs} 行未检测")
        if getattr(report, "uncounted_count", 0):
            parts.append(f"{report.uncounted_count} 句过短不计入")
        return "　·　".join(parts)
