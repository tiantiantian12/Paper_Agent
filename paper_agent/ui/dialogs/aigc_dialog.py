"""AIGC 检测：上传文档 → 逐句判定 → 展示 AIGC 率 → 导出标注 PDF。

判定由 ``services.aigc`` 完成（离线启发式引擎），这里只负责交互：
后台线程跑检测避免卡界面，结果按「AI / 疑似 / 人工」三档着色展示，
导出时把同样的配色写进 PDF；每次检测都会存一条记录，可在「检测记录」里回看。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from paper_agent.core.signals import signals
from paper_agent.core.theme import theme_manager
from paper_agent.resources.icons import build_icon
from paper_agent.services.aigc import (
    DEFAULT_SENSITIVITY,
    DOCUMENT_FILTER,
    SENSITIVITY_PRESETS,
    SUPPORTED_SUFFIXES,
    AigcRecordStore,
    analyze_file,
)
from paper_agent.ui.dialogs.aigc_export import export_report_interactively
from paper_agent.ui.widgets.aigc_result_view import AigcResultView

DESCRIPTION = (
    "上传论文（Word / PDF / 文本），逐句估算 AI 生成倾向并给出全文 AIGC 率；"
    "标红为 AI 生成、标黄为疑似 AI、标灰为人工撰写，可导出带标注的 PDF 报告。"
)


class _AnalyzeWorker(QObject):
    """后台跑检测：纯计算，任何异常都收敛成 failed 信号。"""

    progressed = Signal(str, int)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, path: str, sensitivity: str) -> None:
        super().__init__()
        self._path = path
        self._sensitivity = sensitivity

    @Slot()
    def run(self) -> None:
        try:
            report = analyze_file(
                self._path,
                self._sensitivity,
                progress=lambda _step, percent: self.progressed.emit(_step, percent),
            )
        except Exception as exc:      # noqa: BLE001 - 后台异常必须回传到界面
            self.failed.emit(str(exc))
            return
        self.finished.emit(report)


class AigcDialog(QDialog):
    """AIGC 检测对话框。"""

    def __init__(self, parent: QWidget | None = None, store: AigcRecordStore | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("aigcDialog")
        self.setWindowTitle("AIGC 检测")
        self.resize(1020, 720)
        self.setMinimumSize(820, 560)

        self._path = ""
        self._report = None
        self._store = store or AigcRecordStore()
        self._thread: QThread | None = None
        self._worker: _AnalyzeWorker | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 14)
        root.setSpacing(10)

        title = QLabel("AIGC 检测", self)
        title.setObjectName("welcomeTitle")
        root.addWidget(title)

        subtitle = QLabel(DESCRIPTION, self)
        subtitle.setObjectName("welcomeSubtitle")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        root.addLayout(self._build_toolbar())
        root.addWidget(self._build_progress())

        self.result_view = AigcResultView(self)
        root.addWidget(self.result_view, 1)

        root.addLayout(self._build_footer())
        self.setAcceptDrops(True)
        self._refresh_assets()
        signals.theme_changed.connect(self._refresh_assets)

    # ------------------------------------------------------------------ 构建
    def _build_toolbar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self.pick_button = QPushButton("  选择文档", self)
        self.pick_button.setObjectName("primaryButton")
        self.pick_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pick_button.clicked.connect(self._pick_file)
        row.addWidget(self.pick_button)

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(0)
        self.file_label = QLabel("未选择文件，可把文档直接拖到窗口里", self)
        self.file_label.setObjectName("aigcFileMeta")
        self.meta_label = QLabel("", self)
        self.meta_label.setObjectName("aigcFileMeta")
        text_column.addWidget(self.file_label)
        text_column.addWidget(self.meta_label)
        row.addLayout(text_column, 1)

        row.addWidget(QLabel("灵敏度", self))
        self.sensitivity_combo = QComboBox(self)
        self.sensitivity_combo.setObjectName("aigcSensitivityCombo")
        for key, preset in SENSITIVITY_PRESETS.items():
            self.sensitivity_combo.addItem(preset["label"], key)
        index = self.sensitivity_combo.findData(DEFAULT_SENSITIVITY)
        self.sensitivity_combo.setCurrentIndex(max(index, 0))
        self.sensitivity_combo.setToolTip(
            "　".join(f"{p['label']}：{p['desc']}" for p in SENSITIVITY_PRESETS.values())
        )
        row.addWidget(self.sensitivity_combo)

        self.detect_button = QPushButton("开始检测", self)
        self.detect_button.setEnabled(False)
        self.detect_button.clicked.connect(self._start_detection)
        row.addWidget(self.detect_button)

        self.export_button = QPushButton("导出 PDF 报告", self)
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export_pdf)
        row.addWidget(self.export_button)
        return row

    def _build_progress(self) -> QWidget:
        host = QWidget(self)
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.progress = QProgressBar(host)
        self.progress.setObjectName("aigcProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.status_label = QLabel("", host)
        self.status_label.setObjectName("aigcFileMeta")
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)
        return host

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 2, 0, 0)
        row.setSpacing(8)
        hint = QLabel(
            "本结果为本地启发式估算，与知网 / 格子达等官方系统口径不同，仅供参考。", self
        )
        hint.setObjectName("aigcHint")
        row.addWidget(hint, 1)
        self.history_button = QPushButton("检测记录", self)
        self.history_button.clicked.connect(self._open_history)
        row.addWidget(self.history_button)
        close_button = QPushButton("关闭", self)
        close_button.clicked.connect(self.reject)
        row.addWidget(close_button)
        return row

    # ------------------------------------------------------------------ 主题
    def _refresh_assets(self, *_args) -> None:
        """图标颜色跟随主题（主按钮底色是 accent，图标要用 accent_text）。"""
        self.pick_button.setIcon(build_icon("upload", theme_manager.color("accent_text"), 36))
        self.pick_button.setIconSize(QSize(18, 18))

    def _set_file_label(self, text: str, object_name: str) -> None:
        """切换文件名的样式（未选文件时用灰字提示，选中后用正文色强调）。

        改 objectName 后必须 unpolish / polish 一次，否则 QSS 选择器不会重算。
        """
        self.file_label.setObjectName(object_name)
        self.file_label.setText(text)
        self.file_label.style().unpolish(self.file_label)
        self.file_label.style().polish(self.file_label)

    # ------------------------------------------------------------------ 文件
    def _pick_file(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self, "选择要检测的文档", "", DOCUMENT_FILTER
        )
        if path:
            self._load_file(path)

    def dragEnterEvent(self, event):  # noqa: N802
        if self._dropped_path(event) or event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event):  # noqa: N802
        path = self._dropped_path(event)
        if path:
            self._load_file(path)
            event.acceptProposedAction()
            return
        super().dropEvent(event)

    @staticmethod
    def _dropped_path(event) -> str:
        urls = event.mimeData().urls()
        if not urls:
            return ""
        path = urls[0].toLocalFile()
        if not path or Path(path).suffix.lower() not in SUPPORTED_SUFFIXES:
            return ""
        return path

    def _load_file(self, path: str) -> None:
        target = Path(path)
        if not target.is_file():
            signals.toast_requested.emit("文件不存在，请重新选择")
            return
        if target.suffix.lower() not in SUPPORTED_SUFFIXES:
            signals.toast_requested.emit("暂不支持该类型，请用 Word / PDF / 文本")
            return
        self._path = str(target)
        self._set_file_label(target.name, "aigcFileName")
        self.meta_label.setText(str(target.parent))
        self.detect_button.setEnabled(True)
        self.export_button.setEnabled(False)
        self._report = None
        self.result_view.clear()

    # ------------------------------------------------------------------ 检测
    def _start_detection(self) -> None:
        if not self._path or self._thread is not None:
            return
        self.detect_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.progress.setVisible(True)
        self.status_label.setVisible(True)
        self.progress.setValue(0)
        self.status_label.setText("正在读取文档…")

        self._thread = QThread(self)
        self._worker = _AnalyzeWorker(self._path, self.sensitivity_combo.currentData())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progressed.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.start()

    def _on_progress(self, step: str, percent: int) -> None:
        self.progress.setValue(percent)
        self.status_label.setText(step)

    def _on_finished(self, report) -> None:
        self._report = report
        detail = AigcResultView.excluded_text(report)
        self.status_label.setText(
            f"检测完成 · {report.sentence_count} 句" + (f" · {detail}" if detail else "")
        )
        self.detect_button.setEnabled(True)
        self.export_button.setEnabled(bool(report.sentences))
        self.result_view.set_report(report)
        if not report.sentences:
            signals.toast_requested.emit("没有从文档里解析出可检测的句子")
            return
        self._save_record(report)

    def _on_failed(self, error: str) -> None:
        self.status_label.setText(f"检测失败：{error}")
        self.detect_button.setEnabled(True)
        signals.toast_requested.emit(f"AIGC 检测失败：{error}")

    def _cleanup_thread(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        if self._thread is not None:
            self._thread.deleteLater()
            self._thread = None

    # ------------------------------------------------------------------ 记录
    def _save_record(self, report) -> None:
        """把这次检测存进记录；存失败不能影响刚出来的结果。"""
        try:
            self._store.save_report(report)
        except OSError as exc:
            signals.toast_requested.emit(f"检测记录保存失败：{exc}")

    def _open_history(self) -> None:
        from paper_agent.ui.dialogs.aigc_history_dialog import AigcHistoryDialog

        dialog = AigcHistoryDialog(self, store=self._store)
        dialog.exec()

    # ------------------------------------------------------------------ 导出
    def _export_pdf(self) -> None:
        if self._report is None:
            return
        try:
            target = export_report_interactively(self._report, self)
        except Exception as exc:      # noqa: BLE001 - 导出失败要给用户可见的原因
            signals.toast_requested.emit(f"导出失败：{exc}")
            return
        if target is None:
            return
        self.status_label.setText(f"已导出到 {target}")
        signals.toast_requested.emit(f"已导出到 {target}")

    # ------------------------------------------------------------------ 关闭
    def closeEvent(self, event):  # noqa: N802
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
        super().closeEvent(event)

    def reject(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(2000)
        super().reject()
