"""我的论文空间：汇总当前对话中用户上传的文件与模型生成的文件。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from paper_agent.core.models import ChatSession, PaperFile
from paper_agent.core.signals import signals
from paper_agent.ui.chat.artifact_card import ArtifactCard
from paper_agent.utils.files import delete_managed_file

# 顶部筛选：key 与 PaperFile.source 对应，"all" 表示不筛选
FILTERS: tuple[tuple[str, str], ...] = (
    ("all", "全部"),
    ("user", "我上传的"),
    ("model", "模型生成"),
)


class PaperSpaceDialog(QDialog):
    """论文空间面板：列出本对话的全部文件，区分来源并显示时间，支持删除。"""

    files_changed = Signal(list)   # 已移除的文件路径

    def __init__(self, session: ChatSession, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("paperSpaceDialog")
        self.setWindowTitle("我的论文空间")
        self.resize(700, 620)
        self.setMinimumSize(560, 420)

        self._session = session
        self._files: list[PaperFile] = session.paper_files()
        self._filter = "all"
        self.removed_paths: list[str] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        title = QLabel("我的论文空间", self)
        title.setObjectName("welcomeTitle")
        root.addWidget(title)

        self.subtitle = QLabel(self._subtitle_text(), self)
        self.subtitle.setObjectName("welcomeSubtitle")
        self.subtitle.setWordWrap(True)
        root.addWidget(self.subtitle)

        root.addLayout(self._build_filters())

        # ------------------------------ 文件列表
        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("paperSpaceScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.list_host = QWidget()
        self.list_host.setObjectName("paperSpaceList")
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 8, 0)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_host)
        root.addWidget(self.scroll, 1)

        self.empty_label = QLabel("", self)
        self.empty_label.setObjectName("paperSpaceEmpty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setVisible(False)
        root.addWidget(self.empty_label, 1)

        root.addLayout(self._build_footer())

        self._rebuild()

    # ------------------------------------------------------------------ 构建
    def _build_filters(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(6)

        self.filter_group = QButtonGroup(self)
        self.filter_group.setExclusive(True)
        for key, text in FILTERS:
            button = QPushButton(text, self)
            button.setObjectName("paperSpaceFilter")
            button.setCheckable(True)
            button.setChecked(key == "all")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, k=key: self._on_filter_changed(k))
            self.filter_group.addButton(button)
            row.addWidget(button)

        row.addStretch(1)

        self.count_label = QLabel("", self)
        self.count_label.setObjectName("paperSpaceCount")
        row.addWidget(self.count_label)
        return row

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 2, 0, 0)
        row.setSpacing(8)

        hint = QLabel(
            "点击文件用系统默认程序打开，右侧按钮可复制路径 / 复制文件 / 另存为 / 删除", self
        )
        hint.setObjectName("paperSpaceHint")
        row.addWidget(hint)
        row.addStretch(1)

        self.status_label = QLabel("", self)
        self.status_label.setObjectName("paperSpaceHint")
        row.addWidget(self.status_label)

        close_button = QPushButton("关闭", self)
        close_button.clicked.connect(self.accept)
        row.addWidget(close_button)
        return row

    # ------------------------------------------------------------------ 数据
    def _subtitle_text(self) -> str:
        uploaded = sum(1 for item in self._files if item.is_user_upload)
        generated = len(self._files) - uploaded
        return (
            f"对话：{self._session.title} · 共 {len(self._files)} 个文件"
            f"（我上传 {uploaded} · 模型生成 {generated}）"
        )

    def _visible_files(self) -> list[PaperFile]:
        if self._filter == "all":
            return list(self._files)
        return [item for item in self._files if item.source == self._filter]

    def _empty_text(self) -> str:
        if not self._files:
            return "这个对话还没有文件\n上传附件，或让模型生成文档后，都会汇总到这里"
        return "当前筛选条件下没有文件"

    # ------------------------------------------------------------------ 交互
    def _on_filter_changed(self, key: str) -> None:
        self._filter = key
        self._rebuild()

    def _rebuild(self) -> None:
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        files = self._visible_files()
        for record in files:
            card = ArtifactCard(
                record.name,
                record.path,
                self.list_host,
                source=record.source,
                created_at=record.created_at,
                removable=True,
                previewable=False,     # 论文空间不放行内预览，改为复制路径 / 复制文件
                copyable=True,
            )
            card.delete_requested.connect(self._on_delete)
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)

        self.count_label.setText(f"{len(files)} 个文件" if files else "")
        self.scroll.setVisible(bool(files))
        self.empty_label.setVisible(not files)
        self.empty_label.setText(self._empty_text())

    def _on_delete(self, path: str) -> None:
        """删除一个文件：确认 → 清理磁盘 → 从会话移除 → 刷新列表。"""
        record = next((item for item in self._files if item.path == path), None)
        if record is None or not self._confirm_delete(record):
            return

        deleted, detail = delete_managed_file(record.path)
        if not self._session.remove_paper_file(record):
            signals.toast_requested.emit(f"「{record.name}」已不在对话中")
            return
        if record.path not in self.removed_paths:
            self.removed_paths.append(record.path)

        self._files = self._session.paper_files()
        self.subtitle.setText(self._subtitle_text())
        self._rebuild()
        self.status_label.setText(f"已移除「{record.name}」· {detail}")
        self.files_changed.emit(list(self.removed_paths))

    def _confirm_delete(self, record: PaperFile) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("删除文件")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"确定删除「{record.name}」吗？")
        box.setInformativeText(
            "该文件会从对话中移除；位于应用数据目录的文件会同时从磁盘删除。此操作不可撤销。"
        )
        confirm = box.addButton("删除", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is confirm
