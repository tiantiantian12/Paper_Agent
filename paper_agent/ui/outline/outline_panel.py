"""论文结构面板：选择一篇论文，显示它真实的章节树与写作进度。"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from paper_agent.core.models import OutlineNode
from paper_agent.core.signals import signals
from paper_agent.core.theme import theme_manager
from paper_agent.resources.icons import build_icon
from paper_agent.services.skills.code_workspace import is_inside
from paper_agent.ui.widgets.icon_button import IconToolButton
from paper_agent.utils.clipboard import copy_files, copy_text

STATE_MARK = {"todo": "待写", "doing": "进行中", "done": "已完成"}
CHEVRON_CLOSED = "chevron-right"     # 折叠：可展开
CHEVRON_OPEN = "chevron-down"        # 展开：可折叠
CHEVRON_SIZE = 15
CHEVRON_STROKE = 2.0

# 代码工作区的图标是「箭头 + 图标」拼成的一张图：箭头在最左边（和常见文件
# 树一致），右边才是文件夹 / 文件。不能展开的节点左边留同样宽的空位，
# 保证文字左边界对齐。
ICON_SIZE = CHEVRON_SIZE                      # 文件夹 / 文件图标宽度
ICON_GAP = 2                                  # 箭头与图标之间的间距
COMPOSITE_WIDTH = CHEVRON_SIZE + ICON_GAP + ICON_SIZE
DEFAULT_EMPTY = "完成一次对话后，\n这里会显示论文的章节结构与进度。"
NO_PAPER_EMPTY = "本对话里还没有论文文件。\n生成或上传 .docx / .pdf 后可在这里查看结构。"
NO_STRUCTURE_EMPTY = "没能从这篇文件里识别出章节标题。\n行首用「第一章 / 1.1 标题」这类写法会更准确。"
WORKSPACE_EMPTY = "工作区还是空的。\n点右上角文件夹图标可以换个目录，或让模型写点代码。"


def _compose_icon(mark: QIcon | None, main: QIcon, height: int) -> QIcon:
    """把「左侧标记（箭头 / 空位）+ 右侧图标」拼成一张图。

    QTreeWidgetItem 每一列只有一个图标位，要在文件夹左边放展开箭头，
    只能自己把两张图并排画到一起（``mark`` 为空时左边留透明空位对齐）。
    """
    canvas = QPixmap(COMPOSITE_WIDTH, height)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    if mark is not None:
        painter.drawPixmap(0, 0, mark.pixmap(CHEVRON_SIZE, height))
    painter.drawPixmap(CHEVRON_SIZE + ICON_GAP, 0, main.pixmap(ICON_SIZE, height))
    painter.end()
    return QIcon(canvas)


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


class OutlinePanel(QWidget):
    """右侧面板：文档模式显示论文结构，编程模式显示代码工作区目录树。"""

    close_requested = Signal()
    paper_selected = Signal(str)      # 选中的论文文件路径
    workspace_pick_requested = Signal()      # 「选择文件夹」
    workspace_refresh_requested = Signal()   # 「刷新」
    file_activated = Signal(str)             # 双击工作区里的文件

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("outlinePanel")
        self._workspace_mode = False
        self._workspace_root = ""      # 当前工作区目录：右键操作的边界

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 8, 10)
        root.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(6)
        self.title_label = QLabel("论文结构")
        self.title_label.setObjectName("sidebarBrand")
        header.addWidget(self.title_label)
        header.addStretch(1)
        # 全部展开 / 全部折叠（默认折叠）
        self.expand_button = IconToolButton(
            "chevron-right", "展开全部", object_name="sessionAction", icon_size=14, parent=self
        )
        self.expand_button.clicked.connect(self._toggle_expand_all)
        header.addWidget(self.expand_button)
        self.close_button = IconToolButton(
            "close", "关闭面板", object_name="sessionAction", icon_size=14, parent=self
        )
        self.close_button.clicked.connect(self.close_requested)
        header.addWidget(self.close_button)
        root.addLayout(header)

        # 一个会话里可能有多篇论文，这里选择要查看结构的那一篇
        self.paper_combo = QComboBox(self)
        self.paper_combo.setObjectName("outlinePaperCombo")
        self.paper_combo.setToolTip("选择要查看结构的论文")
        self.paper_combo.setMaxVisibleItems(12)
        self.paper_combo.currentIndexChanged.connect(self._on_paper_changed)
        self.paper_combo.setVisible(False)
        root.addWidget(self.paper_combo)

        # 编程模式：当前工作区目录 + 选择 / 刷新
        self.workspace_row = QWidget(self)
        workspace_layout = QHBoxLayout(self.workspace_row)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(4)
        self.workspace_label = QLabel("")
        self.workspace_label.setObjectName("workspacePath")
        self.workspace_label.setWordWrap(True)
        workspace_layout.addWidget(self.workspace_label, 1)
        self.pick_button = IconToolButton(
            "folder-open", "选择工作区文件夹", object_name="sessionAction", icon_size=14, parent=self
        )
        self.pick_button.clicked.connect(self.workspace_pick_requested)
        workspace_layout.addWidget(self.pick_button)
        self.refresh_button = IconToolButton(
            "refresh", "刷新目录树", object_name="sessionAction", icon_size=14, parent=self
        )
        self.refresh_button.clicked.connect(self.workspace_refresh_requested)
        workspace_layout.addWidget(self.refresh_button)
        self.workspace_row.setVisible(False)
        root.addWidget(self.workspace_row)

        self.tree = QTreeWidget(self)
        self.tree.setObjectName("outlineTree")
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(12)
        self.tree.setRootIsDecorated(False)      # 自绘箭头，不依赖 QSS 可能被覆盖的原生指示器
        self.tree.setIconSize(QSize(CHEVRON_SIZE, CHEVRON_SIZE))
        self.tree.setFrameShape(QTreeWidget.Shape.NoFrame)
        self.tree.setVerticalScrollMode(QTreeWidget.ScrollMode.ScrollPerPixel)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setExpandsOnDoubleClick(True)
        self.tree.itemExpanded.connect(self._on_item_toggled)
        self.tree.itemCollapsed.connect(self._on_item_toggled)
        self.tree.itemClicked.connect(self._on_item_clicked)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        self._last_click_x = -1
        self.tree.viewport().installEventFilter(self)
        root.addWidget(self.tree, 1)
        # 叶子节点用等宽透明图标占位，保证同级文字左边界对齐
        blank = QPixmap(CHEVRON_SIZE, CHEVRON_SIZE)
        blank.fill(Qt.GlobalColor.transparent)
        self._blank_icon = QIcon(blank)
        self._folder_icon = build_icon("folder", theme_manager.color("text_secondary"), 28)
        self._file_icon = build_icon("file-text", theme_manager.color("text_muted"), 28)
        signals.theme_changed.connect(self._refresh_chevrons)

        self.empty_label = QLabel(DEFAULT_EMPTY)
        self.empty_label.setObjectName("footerLabel")
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.empty_label)

        footer = QHBoxLayout()
        self.stats_label = QLabel("")
        self.stats_label.setObjectName("footerLabel")
        footer.addWidget(self.stats_label)
        footer.addStretch(1)
        root.addLayout(footer)

    # ------------------------------------------------------------------ 工作区
    def set_workspace_mode(self, enabled: bool) -> None:
        """编程模式：面板从「论文结构」切成「代码工作区目录树」。"""
        self._workspace_mode = bool(enabled)
        self.title_label.setText("代码工作区" if self._workspace_mode else "论文结构")
        self.workspace_row.setVisible(self._workspace_mode)
        # 工作区里目录是「单击整行展开 / 折叠」，再叠一层双击展开就会来回跳
        self.tree.setExpandsOnDoubleClick(not self._workspace_mode)
        if self._workspace_mode:
            self.paper_combo.setVisible(False)
            self.expand_button.setToolTip("展开全部 / 折叠全部")
        self.empty_label.setText(
            WORKSPACE_EMPTY if self._workspace_mode else DEFAULT_EMPTY
        )

    def set_workspace(
        self,
        path: str,
        nodes: list[dict] | None = None,
        files: int = 0,
        dirs: int = 0,
        hint: str = "",
    ) -> None:
        """更新工作区信息与目录树。

        标签只显示目录名（默认工作区就是 ``workspace``，不占地方也不泄露长路径），
        完整路径放 tooltip。
        """
        self._workspace_root = path or ""
        name = Path(path).name if path else ""
        self.workspace_label.setText(name or "（未选择工作区）")
        self.workspace_label.setToolTip(path or "未选择工作区")
        nodes = nodes or []
        expanded = self._expanded_titles()
        self.tree.clear()
        for node in nodes:
            self.tree.addTopLevelItem(self._build_file_item(node))
        self._apply_expanded(expanded)
        self._refresh_chevrons()
        self.empty_label.setVisible(not nodes)
        self.tree.setVisible(bool(nodes))
        if not nodes:
            self.empty_label.setText(hint or WORKSPACE_EMPTY)
        self._sync_expand_button()
        self.stats_label.setText(
            f"{files} 个文件 · {dirs} 个子目录" if (files or dirs) else "工作区是空的"
        )

    def _build_file_item(self, node: dict) -> QTreeWidgetItem:
        is_dir = bool(node.get("is_dir"))
        name = str(node.get("name", ""))
        text = f"{name}/" if is_dir else name
        size = int(node.get("size") or 0)
        if not is_dir and size:
            text = f"{name}   {_human_size(size)}"
        item = QTreeWidgetItem([text])
        item.setData(0, Qt.ItemDataRole.UserRole, str(node.get("path", "")))
        item.setData(0, Qt.ItemDataRole.UserRole + 1, is_dir)
        # 不含箭头的原始文本：展开 / 折叠时重排文字要用它，避免箭头越堆越多
        item.setData(0, Qt.ItemDataRole.UserRole + 2, text)
        item.setToolTip(0, str(node.get("path", "")))
        item.setIcon(0, self._folder_icon if is_dir else self._file_icon)
        for child in node.get("children") or []:
            item.addChild(self._build_file_item(child))
        return item

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        """工作区里双击文件：交给系统默认程序打开。"""
        if not self._workspace_mode:
            return
        if not item.data(0, Qt.ItemDataRole.UserRole + 1):
            path = item.data(0, Qt.ItemDataRole.UserRole)
            if path:
                self.file_activated.emit(str(path))

    # ------------------------------------------------------------ 右键菜单
    def _show_context_menu(self, pos) -> None:
        """工作区目录树的右键菜单：复制 / 复制地址 / 重命名 / 删除等。"""
        if not self._workspace_mode:
            return
        item = self.tree.itemAt(pos)
        target = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else ""
        path = str(target or "")
        is_dir = bool(item.data(0, Qt.ItemDataRole.UserRole + 1)) if item is not None else False

        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        menu.setWindowFlags(
            menu.windowFlags()
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )

        if path:
            if not is_dir:
                menu.addAction("打开").triggered.connect(lambda: self.file_activated.emit(path))
                menu.addAction("复制文件").triggered.connect(lambda: self._copy_file(path))
            menu.addAction("复制地址").triggered.connect(lambda: self._copy_path(path))
            reveal = "在文件管理器中显示" if not is_dir else "在文件管理器中打开"
            menu.addAction(reveal).triggered.connect(lambda: self._reveal(path))
            menu.addSeparator()
            menu.addAction("重命名…").triggered.connect(lambda: self._rename(path))
            menu.addAction("删除").triggered.connect(lambda: self._delete(path, is_dir))
        else:
            # 空白处：对整个工作区操作
            menu.addAction("新建文件…").triggered.connect(self._create_file)
            menu.addAction("新建文件夹…").triggered.connect(self._create_folder)
            menu.addSeparator()
            if self._workspace_root:
                menu.addAction("在文件管理器中打开").triggered.connect(
                    lambda: self._reveal(self._workspace_root)
                )
            menu.addAction("刷新").triggered.connect(self.workspace_refresh_requested)

        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------ 菜单动作
    def _copy_file(self, path: str) -> None:
        """把文件本身放进剪贴板（资源管理器里 Ctrl+V 可粘贴一份）。"""
        if copy_files([path]) == 0:
            signals.toast_requested.emit("文件不存在，无法复制")
            return
        signals.toast_requested.emit(f"已复制文件：{Path(path).name}")

    def _copy_path(self, path: str) -> None:
        copy_text(path)
        signals.toast_requested.emit("已复制路径")

    def _reveal(self, path: str) -> None:
        """在系统文件管理器里定位（文件则选中它）。"""
        target = Path(path)
        if not target.exists():
            signals.toast_requested.emit("路径不存在，可能已被移动")
            self.workspace_refresh_requested.emit()
            return
        folder = target if target.is_dir() else target.parent
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", str(target)])
                return
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(target)])
                return
        except OSError:      # pragma: no cover - 起不来就退回通用打开方式
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _rename(self, path: str) -> None:
        source = Path(path)
        if not self._inside_workspace(source) or not source.exists():
            self.workspace_refresh_requested.emit()
            return
        name, ok = QInputDialog.getText(self, "重命名", "新名称：", text=source.name)
        if not ok:
            return
        new_name = (name or "").strip()
        if not new_name or new_name == source.name:
            return
        if any(ch in new_name for ch in ("/", "\\")):
            signals.toast_requested.emit("名称里不能包含路径分隔符")
            return
        target = source.parent / new_name
        if target.exists():
            signals.toast_requested.emit("已存在同名文件")
            return
        if not self._inside_workspace(target):
            return
        try:
            source.rename(target)
        except OSError as exc:
            signals.toast_requested.emit(f"重命名失败：{exc}")
            return
        signals.toast_requested.emit(f"已重命名为 {new_name}")
        self.workspace_refresh_requested.emit()

    def _delete(self, path: str, is_dir: bool) -> None:
        target = Path(path)
        if not self._inside_workspace(target):
            return
        if not target.exists():
            signals.toast_requested.emit("路径不存在，可能已被删除")
            self.workspace_refresh_requested.emit()
            return
        kind = "目录" if is_dir else "文件"
        extra = "（连同里面的全部文件）" if is_dir else ""
        answer = QMessageBox.question(
            self,
            "删除确认",
            f"确定删除这个{kind}吗？\n\n{target.name}{extra}\n\n删除后无法恢复。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            if is_dir:
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            signals.toast_requested.emit(f"删除失败：{exc}")
            return
        signals.toast_requested.emit(f"已删除{kind}：{target.name}")
        self.workspace_refresh_requested.emit()

    def _create_file(self) -> None:
        self._create(False)

    def _create_folder(self) -> None:
        self._create(True)

    def _create(self, is_dir: bool) -> None:
        """在工作区根目录新建文件 / 文件夹。"""
        root = Path(self._workspace_root or "")
        if not self._workspace_root or not root.is_dir():
            signals.toast_requested.emit("工作区目录不存在")
            return
        title = "新建文件夹" if is_dir else "新建文件"
        label = "文件夹名称：" if is_dir else "文件名称（可带后缀）："
        name, ok = QInputDialog.getText(self, title, label)
        if not ok:
            return
        clean = (name or "").strip()
        if not clean:
            return
        if any(ch in clean for ch in ("/", "\\")):
            signals.toast_requested.emit("名称里不能包含路径分隔符")
            return
        target = root / clean
        if not self._inside_workspace(target) or target.exists():
            signals.toast_requested.emit("已存在同名文件" if target.exists() else "名称不可用")
            return
        try:
            if is_dir:
                target.mkdir(parents=False, exist_ok=False)
            else:
                target.write_text("", encoding="utf-8")
        except OSError as exc:
            signals.toast_requested.emit(f"创建失败：{exc}")
            return
        signals.toast_requested.emit(f"已创建：{clean}")
        self.workspace_refresh_requested.emit()

    def _inside_workspace(self, target: Path) -> bool:
        """只允许改动当前工作区内的路径（工程目录之外一律拒绝）。"""
        if not self._workspace_root:
            signals.toast_requested.emit("未选择工作区，无法操作")
            return False
        try:
            # 先归一化：目录里拿到的是短名 / 大小写不一致的路径时，
            # 直接比字符串会误判成「不在工作区内」
            resolved = target.resolve()
        except OSError:
            resolved = target
        if not is_inside(resolved, Path(self._workspace_root)):
            signals.toast_requested.emit("只能操作当前工作区内的文件")
            return False
        return True

    # ------------------------------------------------------------------ 论文
    def set_papers(self, papers: list[tuple[str, str]], current: str = "") -> None:
        """设置可选论文列表（``(路径, 显示名)``）；选择变化时发出 ``paper_selected``。"""
        self.paper_combo.blockSignals(True)
        self.paper_combo.clear()
        for path, label in papers:
            self.paper_combo.addItem(label, path)
        if current:
            index = self.paper_combo.findData(current)
            if index >= 0:
                self.paper_combo.setCurrentIndex(index)
        self.paper_combo.blockSignals(False)
        self.paper_combo.setVisible(bool(papers))

    def _on_paper_changed(self, index: int) -> None:
        path = self.paper_combo.itemData(index)
        if path:
            self.paper_selected.emit(str(path))

    # ------------------------------------------------------------------ 数据
    def set_outline(
        self,
        nodes: list[OutlineNode],
        empty_hint: str = "",
        writing: bool = False,
    ) -> None:
        """更新结构树。

        Args:
            writing: 这篇论文正在被写入（生成中），统计栏会加「正在写入」提示
        """
        # 编程模式下右侧是代码工作区（由 set_workspace 负责），论文结构这套
        # 刷新不该把目录树冲掉：否则生成中会出现「完成一次对话后…」这类论文
        # 空态文案，看起来像工作区丢了。
        if self._workspace_mode:
            return
        # 记住用户手动展开的层级：实时刷新（每 1.2 秒）不该把它们收回去
        expanded = self._expanded_titles()
        self.tree.clear()
        for node in nodes:
            item = self._build_item(node)
            self.tree.addTopLevelItem(item)
        self._apply_expanded(expanded)   # 默认全折叠，只显示一级章节
        self._refresh_chevrons()
        self.empty_label.setVisible(not nodes)
        self.tree.setVisible(bool(nodes))
        if not nodes:
            self.empty_label.setText(empty_hint or DEFAULT_EMPTY)
        self._sync_expand_button()

        total_words = sum(_node_words(node) for node in nodes)
        sections = len(nodes)
        if sections:
            done = sum(1 for n in nodes if n.state == "done")
            text = f"{sections} 章 · 已完成 {done} · 约 {total_words} 字"
            self.stats_label.setText(f"{text} · 正在写入" if writing else text)
        else:
            self.stats_label.setText("")

    # ------------------------------------------------------------ 展开 / 折叠
    def _toggle_expand_all(self) -> None:
        """一键展开 / 折叠全部层级。"""
        if self._all_expanded():
            self.tree.collapseAll()
        else:
            self.tree.expandAll()
        self._sync_expand_button()

    def _sync_expand_button(self, *_args) -> None:
        """按钮的存在与否取决于有没有子层级，图标反映当前是「可展开」还是「可折叠」。"""
        has_children = any(item.childCount() for item in self._walk_items())
        self.expand_button.setVisible(has_children and bool(self.tree.topLevelItemCount()))
        expanded = has_children and self._all_expanded()
        self.expand_button.set_icon_name("chevron-down" if expanded else "chevron-right")
        self.expand_button.setToolTip("折叠全部" if expanded else "展开全部")

    def _expanded_titles(self) -> set[str]:
        """收集当前处于展开状态的「章节路径」（按标题拼接，避免受字数变化影响）。"""
        expanded: set[str] = set()

        def walk(item, trail: tuple[str, ...]) -> None:
            title = item.data(0, Qt.ItemDataRole.UserRole) or item.text(0)
            trail = (*trail, str(title))
            if item.isExpanded() and item.childCount():
                expanded.add("\n".join(trail))
            for index in range(item.childCount()):
                walk(item.child(index), trail)

        for index in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(index), ())
        return expanded

    def _apply_expanded(self, expanded: set[str]) -> None:
        """按 _expanded_titles 的结果还原展开状态；未记录的层级默认折叠。"""
        if not expanded:
            return

        def walk(item, trail: tuple[str, ...]) -> None:
            title = item.data(0, Qt.ItemDataRole.UserRole) or item.text(0)
            trail = (*trail, str(title))
            if "\n".join(trail) in expanded:
                item.setExpanded(True)
            for index in range(item.childCount()):
                walk(item.child(index), trail)

        for index in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(index), ())

    def _on_item_toggled(self, item: QTreeWidgetItem) -> None:
        self._refresh_chevrons()
        self._sync_expand_button()

    def eventFilter(self, watched, event) -> bool:      # noqa: N802
        """记下最后一次点击位置：自绘箭头需要用它判断点的是不是箭头。"""
        if watched is self.tree.viewport() and event.type() == QEvent.Type.MouseButtonRelease:
            self._last_click_x = event.position().toPoint().x()
        return super().eventFilter(watched, event)

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        """点标题左侧的箭头即可展开 / 折叠（自绘图标不是原生指示器，需自行响应）。"""
        if not item.childCount():
            return
        if self._workspace_mode:
            # 工作区里目录名前面就是展开符号，整行可点：路径长、符号小，
            # 只认箭头那一小块很难点中，也没必要
            item.setExpanded(not item.isExpanded())
            return
        if self._last_click_x < 0:
            return
        offset = self._last_click_x - self.tree.visualItemRect(item).x()
        if 0 <= offset <= CHEVRON_SIZE + 6:
            item.setExpanded(not item.isExpanded())

    def _refresh_chevrons(self, *_args) -> None:
        """可展开的节点在左侧画 ▶ / ▼；工作区模式下再拼上文件夹 / 文件图标。"""
        color = theme_manager.color("text_secondary")
        closed = build_icon(CHEVRON_CLOSED, color, CHEVRON_SIZE * 2, CHEVRON_STROKE)
        opened = build_icon(CHEVRON_OPEN, color, CHEVRON_SIZE * 2, CHEVRON_STROKE)
        self._folder_icon = build_icon("folder", color, 28)
        self._file_icon = build_icon("file-text", theme_manager.color("text_muted"), 28)

        if self._workspace_mode:
            # 图标位要并排放「箭头 + 文件夹」，比文档模式的单个箭头宽
            self.tree.setIconSize(QSize(COMPOSITE_WIDTH, CHEVRON_SIZE))
            arrow_open = opened
            arrow_closed = closed
            folder = self._folder_icon
            file_icon = self._file_icon
            icons = {
                "dir_open": _compose_icon(arrow_open, folder, CHEVRON_SIZE),
                "dir_closed": _compose_icon(arrow_closed, folder, CHEVRON_SIZE),
                "dir_leaf": _compose_icon(None, folder, CHEVRON_SIZE),
                "file": _compose_icon(None, file_icon, CHEVRON_SIZE),
            }
            for item in self._walk_items():
                is_dir = bool(item.data(0, Qt.ItemDataRole.UserRole + 1))
                if is_dir and item.childCount():
                    item.setIcon(0, icons["dir_open" if item.isExpanded() else "dir_closed"])
                else:
                    item.setIcon(0, icons["dir_leaf" if is_dir else "file"])
                # 箭头已经画进图标里：文字回到原名，不要再拼前缀
                base = str(item.data(0, Qt.ItemDataRole.UserRole + 2) or item.text(0))
                item.setText(0, base)
            return

        self.tree.setIconSize(QSize(CHEVRON_SIZE, CHEVRON_SIZE))
        for item in self._walk_items():
            if item.childCount():
                item.setIcon(0, opened if item.isExpanded() else closed)
            else:
                item.setIcon(0, self._blank_icon)

    def _all_expanded(self) -> bool:
        result = True
        for item in self._walk_items():
            if item.childCount() and not item.isExpanded():
                result = False
                break
        return result

    def _walk_items(self):
        """遍历全部树节点（含所有层级）。"""

        def walk(item):
            yield item
            for index in range(item.childCount()):
                yield from walk(item.child(index))

        for index in range(self.tree.topLevelItemCount()):
            yield from walk(self.tree.topLevelItem(index))

    def _build_item(self, node: OutlineNode) -> QTreeWidgetItem:
        mark = STATE_MARK.get(node.state, "待写")
        words = f" · 约 {node.words} 字" if node.words else ""
        item = QTreeWidgetItem([f"{node.title}  [{mark}{words}]"])
        item.setData(0, Qt.ItemDataRole.UserRole, node.title)   # 刷新时用来对齐展开状态
        for child in node.children:
            item.addChild(self._build_item(child))
        return item

    def clear(self) -> None:
        self.set_papers([])
        self.set_outline([])


def _node_words(node: OutlineNode) -> int:
    return node.words + sum(_node_words(child) for child in node.children)
