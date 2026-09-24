"""单条消息控件（支持流式追加）。"""

from __future__ import annotations

import json
import time

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from paper_agent.core.config import MODE_DOC, mode_text
from paper_agent.core.constants import STREAM_RENDER_INTERVAL_MS
from paper_agent.core.models import Attachment, Message, PlanStep, TaskRecord, ToolRun
from paper_agent.core.signals import signals
from paper_agent.core.theme import theme_manager
from paper_agent.resources.icons import build_icon
from paper_agent.ui.chat.artifact_card import ArtifactCard
from paper_agent.ui.chat.draft_block import DraftBlock
from paper_agent.ui.chat.image_card import ImageCard
from paper_agent.ui.chat.plan_widget import PlanWidget
from paper_agent.ui.chat.rich_text import RichTextLabel
from paper_agent.ui.chat.task_card import TerminalTaskCard
from paper_agent.ui.chat.thinking_block import ThinkingBlock
from paper_agent.ui.chat.video_card import VideoCard
from paper_agent.ui.widgets.attachment_chip import AttachmentChip
from paper_agent.ui.widgets.icon_button import IconToolButton
from paper_agent.utils.text import count_words

# 没有记录插入位置的历史数据：排在正文与其它卡片之后
LEGACY_ANCHOR = 10 ** 9


class MessageWidget(QFrame):
    """一条消息：用户消息为气泡，助手消息为整块文本 + 悬浮操作。"""

    copy_requested = Signal(str)          # message id
    regenerate_requested = Signal(str)    # message id
    delete_requested = Signal(str)        # message id

    # 单条消息最多记录多少推理字符（防止死循环的模型把会话文件撑爆）
    THINKING_STORE_LIMIT = 20000
    # 单个终端任务落库的输出上限（只留尾部）
    TASK_SAVE_OUTPUT_CHARS = 4000

    def __init__(
        self,
        message: Message,
        parent: QWidget | None = None,
        mode: str = MODE_DOC,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("messageRoot")
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._message = message
        self._mode = mode          # 文档模式「论文助手」/ 编程模式「编程助手」
        self._buffer = message.content
        self._dots = 0
        self._thinking_started = False
        self._thinking_truncated = False
        self._limited_note = ""      # 因轮次上限提前结束时的状态提示
        self._progress_text = ""     # 长任务进度文案（视频生成），空表示没有在跑

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 6, 0, 10)
        root.setSpacing(6)

        if message.is_user:
            self._build_user(root)
        else:
            self._build_assistant(root)

        # 流式渲染节流
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(STREAM_RENDER_INTERVAL_MS)
        self._timer.timeout.connect(self._flush)

        self._dots_timer = QTimer(self)
        self._dots_timer.setInterval(400)
        self._dots_timer.timeout.connect(self._tick_dots)

        self._refresh_assets()
        signals.theme_changed.connect(self._refresh_assets)

    # ------------------------------------------------------------------ 构建
    def _build_user(self, root: QVBoxLayout) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)

        bubble = QWidget(self)
        bubble.setObjectName("userBubble")
        bubble.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        bubble.setMaximumWidth(760)
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(14, 10, 14, 10)
        bubble_layout.setSpacing(8)

        self.attachment_host = QWidget(bubble)
        self.attachment_layout = QHBoxLayout(self.attachment_host)
        self.attachment_layout.setContentsMargins(0, 0, 0, 0)
        self.attachment_layout.setSpacing(6)
        self.attachment_layout.addStretch(1)
        bubble_layout.addWidget(self.attachment_host)
        self._render_attachments()

        self.text_label = RichTextLabel(bubble)
        self.text_label.set_plain(self._message.content)
        bubble_layout.addWidget(self.text_label)

        row.addWidget(bubble, 0, Qt.AlignmentFlag.AlignRight)
        root.addLayout(row)
        self.bubble = bubble

    def _build_assistant(self, root: QVBoxLayout) -> None:
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)

        self.avatar_label = QLabel(self)
        self.avatar_label.setFixedSize(18, 18)
        header.addWidget(self.avatar_label)

        self.name_label = QLabel(mode_text(self._mode, "assistant"))
        self.name_label.setObjectName("roleName")
        header.addWidget(self.name_label)

        self.status_label = QLabel("")
        self.status_label.setObjectName("statusLabel")
        header.addWidget(self.status_label)
        header.addStretch(1)
        root.addLayout(header)

        # 长任务进度条（文生视频）：出片要一两分钟，光靠状态栏那几个字看不出
        # 「还在动」还是「已经卡死」。平时隐藏，有进度事件才出现。
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setObjectName("videoProgress")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        # 思考过程（与正式回答分离，可折叠）
        self.thinking_block = ThinkingBlock(self)
        self.thinking_block.setVisible(False)
        root.addWidget(self.thinking_block)
        if self._message.thinking.strip():
            self.thinking_block.set_thinking(self._message.thinking, self._message.thinking_ms)
            self._thinking_started = True

        # 正在写入的正文（工具参数流实时解码，生成文件前能看到内容）
        self.draft_block = DraftBlock(self)
        root.addWidget(self.draft_block)

        # 任务计划 / 待办（Plan-and-Execute）
        self.plan_widget = PlanWidget(parent=self)
        root.addWidget(self.plan_widget)
        if self._message.plan:
            self.set_plan(
                [
                    {"title": step.title, "status": step.status, "detail": step.detail}
                    for step in self._message.plan
                ]
            )

        # 正文与产物**按发生顺序**混排：产物插在当前正文之后，后续再写的正文
        # 落在该产物下方。否则「写一段 → 出图 → 再写一段」时，后一段会跑到图片
        # 上面去，用户为了看最新输出得往上翻。
        self.content_host = QWidget(self)
        self.content_layout = QVBoxLayout(self.content_host)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(6)
        root.addWidget(self.content_host)

        self._text_labels: list[RichTextLabel] = []   # 按顺序的每个正文片段
        self._segment_starts: list[int] = []          # 每个片段在整体文本中的起始下标
        self._cards: list[QWidget] = []               # 已插入的产物卡片
        self._rendered_paths: set[str] = set()        # 已经渲染出卡片的产物路径
        self._task_cards: list[TerminalTaskCard] = []  # 终端任务卡片（命令 / 代码执行 / 后台服务）
        # 运行中的任务：调用标识 →（卡片, 记录）。记录用于落库，重开软件后还原卡片
        self._active_tasks: dict[str, tuple[TerminalTaskCard, TaskRecord]] = {}
        self._rendered_records: set[str] = set()       # 已经渲染成卡片的任务记录 id
        self._add_text_segment()
        self.text_label = self._text_labels[0]
        self._render_artifacts()


        self.action_row = QHBoxLayout()
        self.action_row.setContentsMargins(0, 2, 0, 0)
        self.action_row.setSpacing(2)

        self.copy_button = IconToolButton(
            "copy", "复制内容", object_name="messageAction", icon_size=16, parent=self
        )
        self.copy_button.clicked.connect(lambda: self.copy_requested.emit(self._message.id))
        self.action_row.addWidget(self.copy_button)

        self.retry_button = IconToolButton(
            "refresh", "重新生成", object_name="messageAction", icon_size=16, parent=self
        )
        self.retry_button.clicked.connect(lambda: self.regenerate_requested.emit(self._message.id))
        self.action_row.addWidget(self.retry_button)

        self.delete_button = IconToolButton(
            "trash", "删除", object_name="messageAction", icon_size=16, parent=self
        )
        self.delete_button.clicked.connect(lambda: self.delete_requested.emit(self._message.id))
        self.action_row.addWidget(self.delete_button)

        self.action_row.addStretch(1)
        self.words_label = QLabel("")
        self.words_label.setObjectName("statusLabel")
        self.action_row.addWidget(self.words_label)
        root.addLayout(self.action_row)

        self._update_status()

    # ------------------------------------------------------------------ 数据
    @property
    def message(self) -> Message:
        return self._message

    def message_id(self) -> str:
        return self._message.id

    def set_mode(self, mode: str) -> None:
        """切换工作模式：称谓与「生成中」文案跟着模式走。"""
        if mode == self._mode:
            return
        self._mode = mode
        if hasattr(self, "name_label"):
            self.name_label.setText(mode_text(mode, "assistant"))
        self._update_status()

    def _render_attachments(self) -> None:
        while self.attachment_layout.count() > 1:
            item = self.attachment_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for attachment in self._message.attachments:
            chip = AttachmentChip(attachment, removable=False, parent=self.attachment_host)
            self.attachment_layout.insertWidget(self.attachment_layout.count() - 1, chip)
        self.attachment_host.setVisible(bool(self._message.attachments))

    def remove_files(self, paths: set[str]) -> None:
        """按路径移除附件 / 产物卡片（论文空间中删除文件后同步消息区）。"""
        if not paths:
            return
        if hasattr(self, "attachment_host"):
            self._message.attachments = [
                item for item in self._message.attachments if item.path not in paths
            ]
            self._render_attachments()
        if hasattr(self, "content_layout"):
            self._message.artifacts = [
                item for item in self._message.artifacts if item.path not in paths
            ]
            self._render_artifacts()

    # ------------------------------------------------------------------ 正文分段
    def _add_text_segment(self, start: int | None = None) -> RichTextLabel:
        """在当前位置之后新增一个正片段（产物之后的正文写到这里）。

        ``start`` 是该片段在整体文本中的起始下标；缺省取当前文本末尾
        （正常流式写入就是这样）。重建历史消息时会传入卡片记录的位置。
        """
        label = RichTextLabel(self.content_host)
        self.content_layout.addWidget(label)
        self._text_labels.append(label)
        self._segment_starts.append(len(self._buffer) if start is None else start)
        return label

    def _reset_segments(self) -> None:
        """正文合成一段（重建布局时用；卡片保持不动由调用方处理）。"""
        while len(self._text_labels) > 1:
            label = self._text_labels.pop()
            self._segment_starts.pop()
            self.content_layout.removeWidget(label)
            label.deleteLater()
        if not self._text_labels:
            self._add_text_segment()
        self._segment_starts[0] = 0
        self.text_label = self._text_labels[0]

    def _clear_cards(self) -> None:
        while self._cards:
            card = self._cards.pop()
            self.content_layout.removeWidget(card)
            card.deleteLater()
        self._rendered_paths.clear()
        self._clear_task_cards()

    # ------------------------------------------------------------------ 终端任务
    def begin_terminal_task(
        self, tool: str, command: str, key: str = ""
    ) -> TerminalTaskCard | None:
        """模型开始跑命令 / 执行代码：插一张「终端任务」卡片。

        卡片插在当前正文之后，并在它下面新开一个正片段 —— 之后的输出写在卡片
        下方，阅读顺序与执行顺序一致（和产物卡片同一套做法）。

        同时往 ``message.tasks`` 里记一条：卡片是实时控件，重开软件就没了，
        靠这条记录才能把「当时跑了什么」还原出来。
        """
        if not hasattr(self, "content_layout"):
            return None
        card = TerminalTaskCard(command, tool, self.content_host)
        record = TaskRecord(
            tool=tool, command=command, status="running", anchor=len(self._buffer)
        )
        self._message.tasks.append(record)
        self._rendered_records.add(record.id)
        self._flush()
        index = self.content_layout.indexOf(self._text_labels[-1]) if self._text_labels else -1
        self.content_layout.insertWidget(index + 1, card)
        self._task_cards.append(card)
        if key:
            self._active_tasks[key] = (card, record)
        self._add_text_segment()
        return card

    def finish_terminal_task(
        self,
        key: str,
        success: bool,
        output: str = "",
        url: str = "",
        note: str = "",
    ) -> None:
        """任务结束：更新对应卡片的状态、耗时与输出，并把结果写进记录。"""
        entry = self._active_tasks.pop(key, None)
        if entry is None:
            return
        card, record = entry
        card.finish(success, output, url=url, note=note)
        self._sync_task_record(card, record)

    def append_terminal_output(self, key: str, text: str) -> bool:
        """实时输出：追加到对应卡片（找不到卡片就丢弃）。"""
        entry = self._active_tasks.get(key)
        if entry is None:
            return False
        entry[0].append_output(text)
        return True

    def task_cards(self) -> list[TerminalTaskCard]:
        """已插入的终端任务卡片（按插入顺序）。"""
        return list(self._task_cards)

    @staticmethod
    def _sync_task_record(card: TerminalTaskCard, record: TaskRecord) -> None:
        """把卡片的最终状态写回记录（供持久化与重开还原）。

        输出只留尾部：一条刷了几千行的构建命令不该把会话文件撑大。
        """
        record.status = card.state
        record.seconds = round(card.elapsed(), 1)
        record.output = card.output_text()[-MessageWidget.TASK_SAVE_OUTPUT_CHARS :]
        record.url = card.url

    def _clear_task_cards(self) -> None:
        while self._task_cards:
            card = self._task_cards.pop()
            self.content_layout.removeWidget(card)
            card.deleteLater()
        self._active_tasks.clear()
        self._rendered_records.clear()

    def _stop_running_tasks(self, reason: str) -> None:
        """生成结束时兜底：还在「运行中」的卡片不能一直转圈（记录同步落库）。"""
        for card in self._task_cards:
            if card.is_running():
                card.stop(reason)
        for card, record in self._active_tasks.values():
            self._sync_task_record(card, record)
        self._active_tasks.clear()

    def _append_task_card(self, record: TaskRecord) -> TerminalTaskCard:
        """按记录还原一张历史任务卡片（重开软件 / 重建消息时用）。"""
        self._rendered_records.add(record.id)
        card = TerminalTaskCard(record.command, record.tool, self.content_host)
        card.set_url(record.url)
        card.set_output(record.output)
        card.restore(record.status, record.seconds)
        self._flush()
        index = self.content_layout.indexOf(self._text_labels[-1]) if self._text_labels else -1
        self.content_layout.insertWidget(index + 1, card)
        self._task_cards.append(card)
        self._add_text_segment(record.anchor if record.anchor >= 0 else None)
        return card

    # ------------------------------------------------------------------ 流式
    def begin_stream(self) -> None:
        self._message.status = "streaming"
        self._buffer = ""
        self._dots_timer.start()
        if hasattr(self, "action_row"):
            self.action_row.setEnabled(False)
        self._update_status()
        if hasattr(self, "draft_block"):
            self.draft_block.clear_draft()
        if hasattr(self, "content_layout"):
            self._clear_cards()
            self._reset_segments()
        self.text_label.set_markdown("")

    def resume_stream(self) -> None:
        """切回正在生成的会话：恢复「正在写作」动效，不清空已有内容。"""
        if self._message.status != "streaming":
            return
        self._buffer = self._message.content
        if hasattr(self, "action_row"):
            self.action_row.setEnabled(False)
        self._dots_timer.start()
        self._update_status()

    def append_delta(self, delta: str) -> None:
        self._buffer += delta
        self._message.content = self._buffer
        if not self._timer.isActive():
            self._timer.start()

    # ------------------------------------------------------------------ 实时草稿
    def append_draft(self, text: str, tool: str = "") -> None:
        """正在写入的正文（工具参数流实时解码的结果）——只用于显示，不入库。

        分段写入（append）时模型会多次调用工具，这里让它们共用同一个区块，
        草稿一路往下长，用户看到的是「一篇文档在逐渐成形」。
        """
        if not hasattr(self, "draft_block"):
            return
        block = self.draft_block
        if not block.has_content():
            block.begin(tool)
        elif not block.is_streaming:
            block.resume()
        block.append(text)

    def finish_draft(self, filename: str = "") -> None:
        """参数写完（工具开始执行）：折叠草稿，交给产物卡片接着展示。"""
        if hasattr(self, "draft_block"):
            self.draft_block.finish(filename)

    def begin_thinking(self) -> None:
        """开始显示推理过程（懒初始化，避免无思考的模型出现空块）。"""
        if self._thinking_started:
            return
        self._thinking_started = True
        self._message.thinking = ""
        self.thinking_block.begin()
        self.thinking_block.setVisible(True)

    def append_thinking(self, delta: str) -> None:
        if not self._thinking_started:
            self.begin_thinking()
        # 模型偶尔死循环输出几万字推理：超过上限就不再记录，
        # 否则单条消息就能把会话文件撑到几百 KB（界面也会卡）。
        if len(self._message.thinking) >= self.THINKING_STORE_LIMIT:
            if not self._thinking_truncated:
                self._thinking_truncated = True
                self.thinking_block.append(
                    f"\n…（推理超过 {self.THINKING_STORE_LIMIT} 字，已停止记录）\n"
                )
            return
        self.thinking_block.append(delta)
        self._message.thinking += delta

    def finish_thinking(self) -> None:
        """结束推理过程：停止计时并折叠为摘要。"""
        if not self._thinking_started:
            return
        self.thinking_block.finish(self._message.thinking_ms or 0)
        self._message.thinking_ms = self.thinking_block.elapsed_ms()

    def finish_stream(self, status: str = "done", error: str = "") -> None:
        self._timer.stop()
        self._dots_timer.stop()
        self._limited_note = ""      # 上一轮的重试 / 限额提示不该带到这一轮
        # 视频任务已经收尾（成功 / 失败 / 中断），进度条留着只会误导
        self.clear_progress()
        # 本轮结束了还有卡片停在「运行中」：说明工具结果没回来（停止 / 出错），
        # 不能让它在界面上永远转圈
        self._stop_running_tasks("已中断")
        self._message.status = status
        self._message.error = error
        self._message.content = self._buffer or self._message.content
        self._flush()
        if hasattr(self, "action_row"):
            self.action_row.setEnabled(True)
            # 失败 / 中止时强化「重新生成」入口，方便用户手动重试。
            # 用 accent（随主题取前景色）而非 accent_text，避免浅色下白图标看不见。
            failed = status in ("error", "aborted")
            self.retry_button.set_color_token("accent" if failed else "text_secondary")
            self.retry_button.setToolTip("重试：重新生成这条回复" if failed else "重新生成")
        self._update_status()

    # ------------------------------------------------------------------ 智能体
    def set_plan(self, steps: list[dict]) -> None:
        """更新计划（写入消息以便持久化）。"""
        self._message.plan = [
            PlanStep(
                title=str(step.get("title", "")),
                status=str(step.get("status", "pending")),
                detail=str(step.get("detail", "")),
            )
            for step in steps
        ]
        self.plan_widget.set_steps(steps)

    def update_plan_step(self, index: int, status: str) -> None:
        """更新单步状态。"""
        if 0 <= index < len(self._message.plan):
            self._message.plan[index].status = status
        self.plan_widget.update_step(index, status)

    def add_artifact(self, name: str, path: str, kind: str = "") -> None:
        """新增产物卡片（去重与类型推断都在 Message 层完成）。"""
        # 记下插入位置：重开软件后据此还原「正文 → 卡片 → 正文」的顺序，
        # 否则重建时卡片都会堆到消息最下面，看起来像卡片自己跑位置了。
        attachment = self._message.add_artifact(name, path, kind, anchor=len(self._buffer))
        if attachment is not None:
            self._append_artifact_card(attachment)

    def artifact_cards(self) -> list[QWidget]:
        """已插入的产物卡片（按插入顺序）。"""
        return list(self._cards)

    def sync_artifacts(self) -> None:
        """把「数据里有、界面上没渲染」的产物补上。

        正常流程下卡片是随 ARTIFACT 事件实时插入的；但只要中间任何一步没渲染成功
        （比如消息控件当时不在、或插入过程抛了异常），用户就会看到「论文空间里有文件、
        聊天里没有卡片」。生成结束时对一遍账，缺什么补什么（终端任务卡片同理）。
        """
        if not hasattr(self, "content_layout"):
            return
        for artifact in self._message.artifacts:
            if artifact.path and artifact.path not in self._rendered_paths:
                self._append_artifact_card(artifact)
        for record in self._message.tasks:
            if record.id not in self._rendered_records:
                self._append_task_card(record)

    def _append_artifact_card(self, artifact: Attachment) -> None:
        """图片直接内联展示，其它产物用文件卡片。

        卡片插在当前正文之后，并在它下面新开一个正片段：之后写的内容就会
        显示在卡片下方，阅读顺序与生成顺序一致。
        """
        host = self.content_host
        if artifact.is_image:
            card: QWidget = ImageCard(
                artifact.name, artifact.path, host, created_at=artifact.created_at
            )
        elif artifact.is_video:
            card = VideoCard(
                artifact.name, artifact.path, host, created_at=artifact.created_at
            )
        else:
            card = ArtifactCard(
                artifact.name,
                artifact.path,
                host,
                created_at=artifact.created_at,
                previewable=True,
            )
        # 先把当前片段渲染完（否则后续落到这里的内容会跟卡片抢位置）
        self._flush()
        index = self.content_layout.indexOf(self._text_labels[-1]) if self._text_labels else -1
        self.content_layout.insertWidget(index + 1, card)
        self._cards.append(card)
        self._rendered_paths.add(artifact.path)
        # 之后的正文写到卡片下面；有记录位置的历史卡片沿用记录的位置
        spot = artifact.anchor if artifact.anchor >= 0 else None
        self._add_text_segment(spot)

    def _render_artifacts(self) -> None:
        """重建「正文片段 + 卡片」序列（历史消息 / 文件被删除时调用）。

        产物卡片与终端任务卡片共用同一套锚点：有 ``anchor`` 的按记录的位置还原
        （和生成时看到的顺序一致），没有的历史数据按「正文在前、卡片在后」渲染。
        """
        self._buffer = self._message.content
        self._clear_cards()
        self._reset_segments()
        self._text_labels[0].set_markdown("")

        # (锚点, 类型, 原始下标, 对象)：同一位置的产物排在任务前面，保持稳定顺序
        blocks = [
            (item.anchor if item.anchor >= 0 else LEGACY_ANCHOR, 0, index, item)
            for index, item in enumerate(self._message.artifacts)
        ] + [
            (item.anchor if item.anchor >= 0 else LEGACY_ANCHOR, 1, index, item)
            for index, item in enumerate(self._message.tasks)
        ]
        for _anchor, _kind, _index, item in sorted(blocks, key=lambda entry: entry[:3]):
            if isinstance(item, TaskRecord):
                self._append_task_card(item)
            else:
                self._append_artifact_card(item)

        # 卡片后面没有正文时，插入过程留下的尾部空片段没有意义，去掉
        while len(self._text_labels) > 1 and self._segment_starts[-1] >= len(self._buffer):
            label = self._text_labels.pop()
            self._segment_starts.pop()
            self.content_layout.removeWidget(label)
            label.deleteLater()
        self._flush()
        # 容器必须保持可见：它一开始是空的（新建消息时正文还没到），
        # 一旦在这里按「有没有内容」把它隐藏，后续流式写入的正文与产物卡片
        # 全都落在隐藏容器里 —— 界面看起来就是「模型什么都没输出」，
        # 直到重启重新构造控件才显示出来。
        self.content_host.setVisible(True)

    def set_final_content(self, text: str) -> None:
        """用清洗后的正文替换显示（代码块已转成文件）。"""
        self._buffer = text
        self._message.content = text
        self._reset_segments()
        self._text_labels[0].set_markdown(text)
        if hasattr(self, "words_label"):
            self.words_label.setText(f"{count_words(text)} 字")

    def add_tool_run(self, name: str, args: str = "", result: str = "", status: str = "done") -> None:
        """记录一次工具（Skill）调用（同一调用的 running → done 就地去重，见 Message）。"""
        self._message.add_tool_run(name=name, args=args, result=result, status=status)

    def _flush(self) -> None:
        self._message.content = self._buffer
        if self._message.is_user:
            self.text_label.set_plain(self._buffer)
        elif not hasattr(self, "content_layout"):
            self.text_label.set_markdown(self._buffer)
        elif not self._buffer.strip():
            pass                          # 还没内容：不动（也不隐藏容器）
        else:
            self.content_host.setVisible(True)
            # 按片段切分整体文本：每个片段只显示自己那一段
            total = len(self._buffer)
            for index, label in enumerate(self._text_labels):
                start = self._segment_starts[index]
                end = (
                    self._segment_starts[index + 1]
                    if index + 1 < len(self._segment_starts)
                    else total
                )
                label.set_markdown(self._buffer[start:end])
        if hasattr(self, "words_label"):
            self.words_label.setText(f"{count_words(self._buffer)} 字")

    def _tick_dots(self) -> None:
        self._dots = (self._dots + 1) % 4
        self._update_status()

    def _update_status(self) -> None:
        if not hasattr(self, "status_label"):
            return
        status = self._message.status
        if self._progress_text:
            # 视频这类长任务：进度比「正在写作·」有用得多，优先显示
            self.status_label.setText(self._progress_text)
            self._apply_status_state("")
        elif status == "streaming":
            self.status_label.setText(mode_text(self._mode, "busy") + "·" * self._dots)
            self._apply_status_state("")
        elif self._limited_note:
            # 倒计时 / 限额这类「正在等待」的提示优先于原始错误，
            # 用户才知道是在自动重试而不是卡住了（原始错误放在 tooltip 里）
            self.status_label.setText(self._limited_note)
            self._apply_status_state("waiting")
        elif status == "error":
            self.status_label.setText(self._message.error or "生成失败")
            self._apply_status_state("error")
        elif status == "aborted":
            self.status_label.setText("已停止")
            self._apply_status_state("")
        else:
            model = self._message.model
            self.status_label.setText(model or "")
            self._apply_status_state("")

    def _apply_status_state(self, state: str) -> None:
        """切换状态栏的语义色（错误=红、等待重试=橙），避免失败被当成「还在写」。"""
        if self.status_label.property("state") == (state or None):
            return
        self.status_label.setProperty("state", state or None)
        style = self.status_label.style()
        style.unpolish(self.status_label)
        style.polish(self.status_label)

    def mark_note(self, note: str) -> None:
        """在状态栏显示一条提示（工具轮次上限、自动重试倒计时等）。"""
        self._limited_note = note
        self.status_label.setToolTip(self._message.error or "")
        self._update_status()

    def mark_progress(self, payload: str) -> None:
        """长任务（文生视频）进度。

        ``payload`` 是 JSON：``{"percent": 40, "text": "视频生成中 40%…"}``，
        ``percent`` 为负表示还查不到进度（排队中）—— 这时用 Qt 的不确定进度条
        （来回滑动那种），而不是一个停在 0% 的条，后者看着像卡死了。
        """
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        try:
            percent = int(data.get("percent"))
        except (TypeError, ValueError):
            percent = -1
        text = str(data.get("text") or "")

        self._progress_text = text
        if hasattr(self, "progress_bar"):
            self.progress_bar.setVisible(True)
            if percent < 0:
                self.progress_bar.setRange(0, 0)          # 不确定进度：排队中
            else:
                percent = max(0, min(percent, 100))
                self.progress_bar.setRange(0, 100)
                self.progress_bar.setValue(percent)
            self.progress_bar.setToolTip(text or "视频生成中…")
        self._update_status()

    def clear_progress(self) -> None:
        """收起进度条（本轮结束 / 出错 / 被停止时调用）。"""
        self._progress_text = ""
        if not hasattr(self, "progress_bar"):
            return
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

    # ------------------------------------------------------------------ 主题
    def _refresh_assets(self, *_args) -> None:
        if hasattr(self, "avatar_label"):
            color = theme_manager.color("accent")
            self.avatar_label.setPixmap(build_icon("sparkles", color, 36).pixmap(18, 18))
