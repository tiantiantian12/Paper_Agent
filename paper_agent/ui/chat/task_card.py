"""终端任务卡片：把「终端正在跑什么」摆到聊天里。

编程模式下模型跑命令 / 执行代码时，界面上原先只有推理区里一行
「调用工具：run_command」，用户既不知道在跑什么，也不知道跑了多久（推理区
默认还是折叠的）。这里把终端类工具调用做成一张卡片：

- 运行中：显示命令 + 实时计时，一眼能看出「终端在跑任务」；
- 结束后：转成已完成 / 失败 / 已中断 + 耗时，输出可展开查看；
- 后台服务（start_service）：显示「后台运行中」与访问地址，可一键打开。
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from paper_agent.core.signals import signals
from paper_agent.core.theme import theme_manager
from paper_agent.resources.icons import build_icon
from paper_agent.ui.widgets.elide_label import ElideLabel

# 需要当成「终端任务」呈现的工具：跑命令 / 执行代码 / 起后台服务
TERMINAL_TOOLS = ("run_command", "execute_python", "start_service")
TOOL_TITLES = {
    "run_command": "终端命令",
    "execute_python": "执行 Python",
    "start_service": "后台服务",
}
# 模型把「要跑的东西」放在这些参数里
COMMAND_KEYS = ("command", "code")


def is_terminal_tool(name: str) -> bool:
    return name in TERMINAL_TOOLS


def command_of(name: str, args: dict) -> str:
    """从工具入参里取出「正在跑的命令 / 代码」。"""
    for key in COMMAND_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def task_title(name: str) -> str:
    return TOOL_TITLES.get(name, name or "终端任务")


class TerminalTaskCard(QFrame):
    """一个终端任务：命令 + 状态 + 计时 + 输出。"""

    MAX_OUTPUT_HEIGHT = 180
    MAX_OUTPUT_LINES = 400          # 输出只留最近这么多行：装依赖 / 编译能刷几千行
    RENDER_INTERVAL_MS = 100        # 输出渲染节流（和正文流式渲染一个思路）

    def __init__(self, command: str, tool: str = "run_command", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("taskCard")
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._tool = tool
        self._command = command or ""
        self._state = "running"
        self._output = ""           # 与输出框内容保持一致
        self._pending = ""          # 还没渲染的输出（攒到节流时间点一起写）
        self._url = ""
        self._background = tool == "start_service"
        self._expanded = False
        self._started = time.monotonic()
        self._seconds = 0.0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---------------------------------------------------------- 头部
        self.header = QWidget(self)
        self.header.setObjectName("taskHeader")
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        header = QHBoxLayout(self.header)
        header.setContentsMargins(8, 5, 8, 5)
        header.setSpacing(6)

        self.arrow_label = QLabel(self.header)
        self.arrow_label.setFixedSize(14, 14)
        self.arrow_label.setVisible(False)      # 有输出才值得折叠
        header.addWidget(self.arrow_label)

        self.icon_label = QLabel(self.header)
        self.icon_label.setFixedSize(14, 14)
        header.addWidget(self.icon_label)

        self.title_label = QLabel(task_title(tool), self.header)
        self.title_label.setObjectName("taskTitle")
        header.addWidget(self.title_label)

        # 命令可能很长：用可省略标签，别让它把消息列的最小宽度顶起来
        self.command_label = ElideLabel(self._command, self.header)
        self.command_label.setObjectName("taskCommand")
        header.addWidget(self.command_label, 1)

        self.meta_label = QLabel("", self.header)
        self.meta_label.setObjectName("taskMeta")
        header.addWidget(self.meta_label)
        root.addWidget(self.header)

        # ---------------------------------------------------------- 访问地址
        self.url_row = QWidget(self)
        self.url_row.setObjectName("taskUrlRow")
        url_layout = QHBoxLayout(self.url_row)
        url_layout.setContentsMargins(30, 0, 8, 6)
        url_layout.setSpacing(6)
        self.url_label = ElideLabel("", self.url_row)
        self.url_label.setObjectName("taskUrl")
        url_layout.addWidget(self.url_label, 1)
        self.open_button = QPushButton("打开", self.url_row)
        self.open_button.setObjectName("taskOpen")
        self.open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_button.clicked.connect(self.open_url)
        url_layout.addWidget(self.open_button)
        self.url_row.setVisible(False)
        root.addWidget(self.url_row)

        # ---------------------------------------------------------- 输出
        self.output = QPlainTextEdit(self)
        self.output.setObjectName("taskOutput")
        self.output.setReadOnly(True)
        self.output.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.output.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.output.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.output.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.output.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.output.setMaximumHeight(self.MAX_OUTPUT_HEIGHT)
        self.output.setVisible(False)
        root.addWidget(self.output)

        # 运行中每秒刷新一次计时：用户才知道命令还在跑、跑了多久
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()

        # 输出按 100ms 批量渲染：命令可能每秒刷几百行，一行一渲染会卡界面
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(self.RENDER_INTERVAL_MS)
        self._render_timer.timeout.connect(self._flush_output)

        self._refresh_assets()
        self._apply_state()
        self._refresh()          # 一插进来就显示「运行中」，别等第一次计时
        signals.theme_changed.connect(self._refresh_assets)

    # ------------------------------------------------------------------ 状态
    @property
    def command(self) -> str:
        return self._command

    @property
    def state(self) -> str:
        """``running`` / ``done`` / ``failed`` / ``aborted``。"""
        return self._state

    @property
    def url(self) -> str:
        return self._url

    @property
    def expanded(self) -> bool:
        return self._expanded

    def is_running(self) -> bool:
        return self._state == "running"

    def elapsed(self) -> float:
        """已经跑了几秒（运行中按当前时间算）。"""
        if self._state == "running":
            return time.monotonic() - self._started
        return self._seconds

    def meta_text(self) -> str:
        return self.meta_label.text()

    def output_text(self) -> str:
        return self._output

    def finish(self, success: bool, output: str = "", url: str = "", note: str = "") -> None:
        """任务结束：转成完成 / 失败，并展示输出与访问地址。"""
        if self._state != "running":
            return
        self._tick_timer.stop()
        self._flush_output()
        self._seconds = time.monotonic() - self._started
        self._state = "done" if success else "failed"
        if url:
            self.set_url(url)
        # 已经实时流过输出就别用最终摘要覆盖：引擎给的只是截断后的前 600 字，
        # 会比卡片上已有的内容少一截
        if output.strip() and not self._output.strip():
            self.set_output(output)
        # 失败要能直接看到报错；成功且没展开过时保持收起，需要时再点开
        if self._output.strip() and not success:
            self.set_expanded(True)
        self._refresh(note)

    def restore(self, status: str, seconds: float = 0.0) -> None:
        """重建历史任务的卡片：落成当时的状态与耗时，不再计时。

        ``running``（重开软件时那条命令早就结束了）按「已中断」呈现 —— 界面
        已经接不上那个进程，显示成还在跑只会误导。
        """
        self._tick_timer.stop()
        self._render_timer.stop()
        self._state = "aborted" if status == "running" else status
        self._seconds = max(0.0, float(seconds or 0))
        if self._output.strip() and self._state == "failed":
            self.set_expanded(True)
        self._refresh()

    def stop(self, reason: str = "已中断") -> None:
        """生成被停止时，还在跑的任务卡不能一直转圈。"""
        if self._state != "running":
            return
        self._tick_timer.stop()
        self._flush_output()
        self._seconds = time.monotonic() - self._started
        self._state = "aborted"
        self._refresh(reason)

    def append_output(self, text: str) -> None:
        """实时追加一段输出（命令跑着的时候就能看到进度）。

        渲染走 100ms 节流：装依赖、编译这类命令一秒能刷几百行，逐行重排界面会卡。
        """
        if not text or self._state != "running":
            return
        self._pending += text
        if not self._render_timer.isActive():
            self._render_timer.start()
        if not self._expanded:
            # 想看进度就得看得见：一有输出就展开输出区
            self.set_expanded(True)

    def set_output(self, text: str) -> None:
        self._pending = ""
        self._render_timer.stop()
        self._output = (text or "").strip()
        self.output.setPlainText(self._output)
        self._sync_arrow_visible()
        self._fit_height()

    def _flush_output(self) -> None:
        """把攒下的输出写进输出框，并只保留尾部（长任务能刷几千行）。"""
        pending, self._pending = self._pending, ""
        if not pending:
            return
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(pending)
        self.output.setTextCursor(cursor)
        if self.output.document().blockCount() > self.MAX_OUTPUT_LINES:
            kept = "\n".join(
                self.output.toPlainText().splitlines()[-self.MAX_OUTPUT_LINES :]
            )
            self.output.setPlainText(kept)
        else:
            kept = self.output.toPlainText()
        self._output = kept.strip()
        self._sync_arrow_visible()
        self.output.ensureCursorVisible()
        self._fit_height()

    def set_url(self, url: str) -> None:
        self._url = url or ""
        self.url_label.setText(self._url)
        self.url_row.setVisible(bool(self._url))

    def open_url(self) -> None:
        if self._url:
            QDesktopServices.openUrl(QUrl(self._url))
            signals.toast_requested.emit(f"已在浏览器打开 {self._url}")

    # ------------------------------------------------------------------ 折叠
    def set_expanded(self, expanded: bool) -> None:
        # 不要求「已有输出」：实时输出是攒在 _pending 里批量渲染的，
        # 一收到第一段就展开，用户才能看到进度
        self._expanded = bool(expanded)
        self.output.setVisible(self._expanded)
        self._sync_arrow_visible()
        self._refresh_arrow()

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    # ------------------------------------------------------------------ 内部
    def _tick(self) -> None:
        if self._state == "running":
            self._refresh()

    def _state_text(self) -> str:
        if self._background and self._state == "done":
            # 服务进程还在后台跑，写「已完成」会让人以为它停了
            return "后台运行中"
        return {
            "running": "运行中",
            "done": "已完成",
            "failed": "失败",
            "aborted": "已中断",
        }.get(self._state, "")

    def _refresh(self, note: str = "") -> None:
        text = note or self._state_text()
        seconds = self.elapsed()
        self.meta_label.setText(f"{text} · {seconds:.0f} 秒" if seconds >= 1 else text)
        self._apply_state()
        self._refresh_assets()

    def _apply_state(self) -> None:
        """状态色（运行中=橙、成功=绿、失败=红）交给 QSS 的 state 属性。"""
        state = self._state
        if self.meta_label.property("state") == state:
            return
        for widget in (self, self.meta_label):
            widget.setProperty("state", state)
            style = widget.style()
            style.unpolish(widget)
            style.polish(widget)

    def _sync_arrow_visible(self) -> None:
        """有输出（或用户手动展开过）才显示折叠箭头。"""
        self.arrow_label.setVisible(bool(self._output.strip()) or self._expanded)

    def _refresh_arrow(self) -> None:
        name = "chevron-down" if self._expanded else "chevron-right"
        color = theme_manager.color("text_muted")
        self.arrow_label.setPixmap(build_icon(name, color, 28).pixmap(14, 14))

    def _refresh_assets(self, *_args) -> None:
        icon = {
            "running": "refresh",
            "done": "check",
            "failed": "close",
            "aborted": "stop",
        }.get(self._state, "terminal")
        color = {
            "running": "warning",
            "done": "success",
            "failed": "danger",
            "aborted": "text_muted",
        }.get(self._state, "text_secondary")
        self.icon_label.setPixmap(
            build_icon(icon, theme_manager.color(color), 28).pixmap(14, 14)
        )
        self._refresh_arrow()

    def _fit_height(self) -> None:
        """输出不足一屏就收缩，多了封顶滚动（和草稿区一致的做法）。"""
        line_height = self.output.fontMetrics().lineSpacing()
        lines = max(1, self.output.document().lineCount())
        self.output.setFixedHeight(min(lines * line_height + 12, self.MAX_OUTPUT_HEIGHT))

    # ------------------------------------------------------------------ 事件
    def mousePressEvent(self, event):       # noqa: N802
        if self.header.geometry().contains(event.position().toPoint()):
            self.toggle()
            return
        super().mousePressEvent(event)
