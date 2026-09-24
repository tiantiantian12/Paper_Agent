"""主窗口：无边框外壳 + 侧栏 + 消息区 + 输入区 + 结构面板。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from paper_agent.core.config import (
    MODE_CODE,
    MODE_DOC,
    AppConfig,
    THEME_DARK,
    THEME_LIGHT,
    mode_text,
)
from paper_agent.core.constants import (
    APP_NAME,
    ARTIFACTS_DIR,
    OUTLINE_PANEL_WIDTH,
    SIDEBAR_MIN_WIDTH,
    SIDEBAR_WIDTH,
    TITLE_BAR_HEIGHT,
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
)
from paper_agent.core.models import (
    Attachment,
    ChatSession,
    Message,
    PlanStep,
    ToolRun,
    sort_sessions,
)
from paper_agent.core.session_store import SessionStore
from paper_agent.core.signals import signals
from paper_agent.core.theme import theme_manager
from paper_agent.resources.icons import build_icon
from paper_agent.services.agent_service import (
    AgentService,
    build_chat_messages,
    build_user_content,
)
from paper_agent.services.key_pool import looks_rate_limited
from paper_agent.services.llm_key import LlmKeySyncer
from paper_agent.services.llm_models import LlmModelsSyncer
from paper_agent.services.reasoning import is_unsupported_error
from paper_agent.services import session_artifacts
from paper_agent.services.skills import code_workspace
from paper_agent.services.skills.code_workspace import build_tree, code_root
from paper_agent.services.paper_outline import (
    extract_markdown_outline,
    extract_outline,
    outline_signature,
    paper_candidates,
    source_label,
    workspace_snapshot,
)

# 限流类错误要等额度窗口恢复：重试次数更多、间隔更长
RATE_LIMIT_RETRY_DELAYS_MS = (20_000, 45_000, 90_000)
DEFAULT_RETRY_DELAYS_MS = (1_500, 4_000)

# 点了停止但某个工具还卡着时：倒计时多久后弹窗询问是否强制结束
STOP_FORCE_ASK_SECONDS = 10

# 重试时回灌给模型的「上一次进度」摘要：够模型判断断点即可，别把上下文撑爆
RESUME_MAX_RUNS = 12          # 最多回灌几条工具调用记录
RESUME_ARG_CHARS = 160        # 单条工具入参截断长度
RESUME_RESULT_CHARS = 240     # 单条工具返回截断长度
RESUME_BODY_CHARS = 1200      # 已写正文保留的尾部长度

# 工具状态 → 给模型看的说法。**不能用 running/done/failed 原词**：小模型会把
# 「running」当成「已经跑过了」，把「failed」当成语气词，然后照着上一轮的叙述编过程。
_RUN_STATE_TEXT = {
    "done": "成功",
    "failed": "失败",
    "running": "未完成（没有拿到返回）",
    "aborted": "被用户中断",
}


def retry_plan(error: str) -> tuple[int, tuple[int, ...]]:
    """按错误类型给出自动重试的（次数, 间隔毫秒序列）。

    限流（429 / TPM / RPM）必须等额度窗口恢复，间隔太短只会再撞一次；
    网络抖动、5xx 这类快速重试即可。
    """
    if looks_rate_limited(error):
        return len(RATE_LIMIT_RETRY_DELAYS_MS), RATE_LIMIT_RETRY_DELAYS_MS
    return len(DEFAULT_RETRY_DELAYS_MS), DEFAULT_RETRY_DELAYS_MS


def tree_signature(nodes: list[dict]) -> tuple[int, int]:
    """目录树指纹（节点数 + 总大小）：用来判断「工作区有没有变化」。

    实时刷新每 1.2 秒跑一次，没变化就别重绘 —— 否则展开状态会被反复重置。
    """
    def walk(items: list[dict]) -> tuple[int, int]:
        count = 0
        size = 0
        for item in items:
            count += 1
            size += int(item.get("size") or 0)
            sub_count, sub_size = walk(item.get("children") or [])
            count += sub_count
            size += sub_size
        return count, size

    return walk(nodes)


def parse_tool_args(raw: str) -> dict:
    """工具入参（JSON 字符串）转 dict；坏数据返回空 dict。"""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


from paper_agent.ui.chat.chat_view import ChatView
from paper_agent.ui.chat.task_card import command_of, is_terminal_tool
from paper_agent.ui.composer.composer import Composer
from paper_agent.ui.frameless import FramelessMixin
from paper_agent.ui.outline.outline_panel import (
    NO_PAPER_EMPTY,
    NO_STRUCTURE_EMPTY,
    OutlinePanel,
)
from paper_agent.ui.sidebar.sidebar import Sidebar
from paper_agent.ui.title_bar import TitleBar
from paper_agent.ui.widgets.toast import Toast
from paper_agent.utils.clipboard import copy_text
from paper_agent.utils.files import (
    delete_managed_file,
    human_readable_size,
    iter_managed_files,
    normalized_path,
)
from paper_agent.utils.system_theme import system_prefers_dark
from paper_agent.utils.text import derive_title


class MainWindow(FramelessMixin, QWidget):
    """应用主窗口。"""

    theme_changed = Signal(str)

    def __init__(self, config: AppConfig, store: SessionStore) -> None:
        super().__init__()
        self.config = config
        self.store = store
        self.sessions: dict[str, ChatSession] = {}
        self.current: ChatSession | None = None

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
        self.resize(*config.default_size)
        self.setWindowTitle(APP_NAME)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        self.agent = AgentService(self, max_parallel=config.max_parallel)
        # 内置模型的代理密钥：登录后向服务端换取，退出登录时清掉
        self.llm_key_syncer = LlmKeySyncer(self.config)
        # 内置模型的名称 / 备注：看板里维护，登录后同步过来（只影响显示）
        self.llm_models_syncer = LlmModelsSyncer(self.config)
        self._auto_retry_count = 0      # 连续自动重试次数（成功后归零）
        # 「谁在生成」一律问 agent（那里按会话登记任务），界面不再自己记一份 ——
        # 两份状态迟早对不上。这里只额外记住「正在等停止的那一条」：
        # 停止过程可能要跨会话切换，不能只看当前会话。
        self._stopping_message_id = ""
        self._scroll_positions: dict[str, int] = {}   # 每个会话上次浏览到的位置
        self._outline_seen: dict[str, tuple[str, ...]] = {}   # 会话已见过的论文列表
        self._outline_live_path = ""      # 本轮正在写的论文文件
        self._outline_signature = None    # 上一次刷新的内容指纹，用于避免重复重绘
        self._workspace_signature: tuple | None = None   # 工作区目录树指纹
        self._outline_timer = QTimer(self)
        self._outline_timer.setInterval(1200)
        self._outline_timer.timeout.connect(self._tick_outline)
        self._retry_timer = QTimer(self)          # 待执行的自动重试
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._run_pending_retry)
        self._retry_message_id = ""
        self._stop_timer = QTimer(self)           # 停止中的等待倒计时
        self._stop_timer.setInterval(1000)
        self._stop_timer.timeout.connect(self._tick_stopping)
        self._stop_wait_s = 0
        # 生成状态兜底：收尾信号万一没到，输入区会一直停在「停止生成」
        self._stream_guard = QTimer(self)
        self._stream_guard.setInterval(1000)
        self._stream_guard.timeout.connect(self._tick_stream_guard)
        self._stream_guard.start()
        self._centered = False
        self._outline_width = OUTLINE_PANEL_WIDTH   # 结构面板宽度（下次打开沿用）
        self._panes_ready = False                   # 首次显示后再校正三栏宽度
        self._build_ui()
        self._connect_signals()
        # 工作模式跟会话走：激活会话时会把它自己的模式同步到界面
        self._load_sessions()
        self._refresh_account()
        self._restore_window_state()

    # ------------------------------------------------------------------ 构建
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.container = QWidget(self)
        self.container.setObjectName("appContainer")
        outer.addWidget(self.container)

        root = QVBoxLayout(self.container)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---------------- 标题栏
        self.title_bar = TitleBar(self.container)
        self.title_bar.setFixedHeight(TITLE_BAR_HEIGHT)
        root.addWidget(self.title_bar)

        # ---------------- 主体
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self.container)
        self.splitter.setHandleWidth(1)
        self.splitter.setChildrenCollapsible(False)

        self.sidebar = Sidebar(self.splitter)
        self.sidebar.setMinimumWidth(SIDEBAR_MIN_WIDTH)
        self.splitter.addWidget(self.sidebar)

        chat_page = QWidget(self.splitter)
        chat_page.setObjectName("chatPage")
        chat_layout = QVBoxLayout(chat_page)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(0)

        # 传当前工作模式：助手称谓与「生成中」文案由模式决定。
        # 启动后 _load_sessions 会按激活会话的模式再同步一次。
        self.chat_view = ChatView(chat_page, mode=MODE_DOC, user_name=self.config.display_name)
        chat_layout.addWidget(self.chat_view, 1)

        self.composer = Composer(self.config, chat_page)
        chat_layout.addWidget(self.composer)
        self.splitter.addWidget(chat_page)

        self.outline_panel = OutlinePanel(self.splitter)
        self.outline_panel.setMinimumWidth(220)
        self.splitter.addWidget(self.outline_panel)

        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([SIDEBAR_WIDTH, self.width() - SIDEBAR_WIDTH, 0])
        root.addWidget(self.splitter, 1)

        self.outline_panel.setVisible(bool(self.config.outline_visible))
        self.sidebar.setVisible(bool(self.config.sidebar_visible))

        # ---------------- 提示条
        self.toast = Toast(self)
        self._apply_window_icon()

    def _connect_signals(self) -> None:
        # 标题栏
        self.title_bar.toggle_sidebar_requested.connect(self.toggle_sidebar)
        self.title_bar.toggle_outline_requested.connect(self.toggle_outline)
        self.title_bar.theme_toggle_requested.connect(self._toggle_theme)
        self.title_bar.settings_requested.connect(self._show_settings_menu)
        self.title_bar.minimize_requested.connect(self.showMinimized)
        self.title_bar.maximize_requested.connect(self._toggle_maximized)
        self.title_bar.close_requested.connect(self.close)

        # 侧栏
        self.sidebar.new_session_requested.connect(self.new_session)
        self.sidebar.session_selected.connect(self.activate_session)
        self.sidebar.session_rename_requested.connect(self.rename_session)
        self.sidebar.session_delete_requested.connect(self.delete_session)
        self.sidebar.session_export_requested.connect(self.export_session)
        self.sidebar.theme_toggle_requested.connect(self._toggle_theme)
        self.sidebar.collapse_requested.connect(self.toggle_sidebar)
        self.sidebar.paper_space_requested.connect(self._open_paper_space)
        self.sidebar.account_requested.connect(self._open_account)
        self.sidebar.aigc_requested.connect(self._open_aigc)

        # 聊天区
        self.chat_view.message_copy_requested.connect(self._copy_message)
        self.chat_view.message_regenerate_requested.connect(self._regenerate_message)
        self.chat_view.message_delete_requested.connect(self._delete_message)
        self.chat_view.suggestion_selected.connect(self._use_suggestion)

        # 输入区
        self.composer.submit_requested.connect(self._on_submit)
        self.composer.stop_requested.connect(self._stop_generation)
        self.composer.model_changed.connect(self._on_model_changed)
        self.composer.mode_changed.connect(self._on_mode_changed)
        self.composer.manage_models_requested.connect(self._open_model_settings)

        # 侧栏设置入口
        self.sidebar.settings_button.clicked.connect(self._show_settings_menu)

        # 结构面板
        self.outline_panel.close_requested.connect(self.toggle_outline)
        self.outline_panel.paper_selected.connect(self._on_outline_paper_selected)
        self.outline_panel.workspace_pick_requested.connect(self._pick_workspace)
        self.outline_panel.workspace_refresh_requested.connect(self._refresh_workspace_panel)
        self.outline_panel.file_activated.connect(self._open_workspace_file)

        # 智能体
        self.agent.token.connect(self._on_agent_token)
        self.agent.thinking.connect(self._on_agent_thinking)
        self.agent.plan.connect(self._on_agent_plan)
        self.agent.tool.connect(self._on_agent_tool)
        self.agent.artifact.connect(self._on_agent_artifact)
        self.agent.content_final.connect(self._on_agent_content_final)
        self.agent.limit_hit.connect(self._on_agent_limit)
        self.agent.note.connect(self._on_agent_note)
        self.agent.draft.connect(self._on_agent_draft)
        self.agent.open_url.connect(self._on_agent_open_url)
        self.agent.output.connect(self._on_agent_output)
        self.agent.progress.connect(self._on_agent_progress)
        self.agent.finished.connect(self._on_agent_finished)
        self.agent.failed.connect(self._on_agent_failed)

        # 全局
        signals.toast_requested.connect(self.toast.show_message)
        signals.theme_changed.connect(self._on_theme_changed)
        signals.llm_key_updated.connect(self._on_llm_key_updated)
        signals.llm_models_updated.connect(self._on_llm_models_updated)

        # 快捷键
        QShortcut(QKeySequence("Ctrl+B"), self, activated=self.toggle_sidebar)
        QShortcut(QKeySequence("Ctrl+Shift+O"), self, activated=self.toggle_outline)
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self.new_session)
        QShortcut(QKeySequence("Esc"), self, activated=self._stop_generation)

    # ------------------------------------------------------------------ 会话
    def _ordered_sessions(self) -> list[ChatSession]:
        """侧栏展示顺序：最近更新的排在最上面（新建的对话因此置顶）。

        会话字典本身是插入顺序，新建的会落在末尾；展示统一走这里排序。
        """
        return sort_sessions(self.sessions.values())

    def _load_sessions(self) -> None:
        sessions = sort_sessions(self.store.load_all())
        self.sessions = {s.id: s for s in sessions}
        self._pin_legacy_workspaces(sessions)
        self.sidebar.set_sessions(list(self.sessions.values()))
        if sessions:
            self.activate_session(sessions[0].id)
        else:
            self.new_session(silent=True)

    # 编程模式里「会往工程目录里写东西」的工具：只有用过它们的会话，文件才可能落在
    # **共享**工作区根目录里（改版前的布局），也只有它们需要被钉在那儿。
    #
    # 注意**别把文档类工具算进来**（create_docx / generate_video / list_files…）：
    # 一个「只是切到编程模式、没写过任何工程文件」的会话被误钉到共享目录后，
    # 用户上传的附件会跟着落到 `workspace/` 根目录（别人工程的旁边），
    # 而不是本会话自己的 `workspace/<会话 id>/` —— 用户实际遇到的就是这个。
    CODE_WRITE_TOOLS = {"write_file", "run_command"}

    @staticmethod
    def _tool_name(run: object) -> str:
        """工具名（``ToolRun`` 对象或旧数据里的 dict 都认）。"""
        if isinstance(run, dict):
            return str(run.get("name") or "")
        return str(getattr(run, "name", "") or "")

    def _wrote_code_files(self, session: ChatSession) -> bool:
        """这个会话有没有在工程目录里真正写过东西（决定它属于哪套工作区布局）。"""
        return any(
            self._tool_name(run) in self.CODE_WRITE_TOOLS
            for message in session.messages
            for run in (message.tool_runs or [])
        )

    def _legacy_root_files(self, sessions: list[ChatSession]) -> list[Path]:
        """共享工作区根目录里的**老文件**（排除各会话自己的子目录）。

        新布局下 ``workspace/<会话 id>/`` 是各会话自己的目录，它们存在于父目录里
        并不代表「改版前的老文件」。早先这里只判断「父目录非空」，于是只要有任何
        一个会话用过编程模式，**所有**没记 code_root 的编程会话都会被误钉到共享目录。
        """
        legacy = code_root()                       # 不带会话 = 共享目录
        try:
            entries = list(legacy.iterdir())
        except OSError:
            return []
        owned = {session.id for session in sessions}
        return [
            item for item in entries if not (item.is_dir() and item.name in owned)
        ]

    def _pin_legacy_workspaces(self, sessions: list[ChatSession]) -> None:
        """把「改版前就在写代码」的会话钉在旧的共享工作区上。

        工作区改成会话级以后，新会话各自用 ``workspace/<会话 id>``；但改版前那些
        会话的文件还在共享的 ``workspace`` 里 —— 不钉住的话它们会突然「空掉」，
        而且历史消息里记的绝对路径也会失效。

        改版前的文件都堆在这一个目录里，而历史记录只写了文件名、无法分辨谁写的，
        所以把**写过工程文件**的会话统一指回它（谁都没丢东西）；想分开的话，在右侧
        面板点文件夹图标给某个会话单独选目录即可。**没写过东西的会话一律走自己的目录**。
        """
        if not self._legacy_root_files(sessions):
            return
        legacy = code_root()
        for session in sessions:
            if session.mode != MODE_CODE:
                continue
            own = code_root(session.id)
            own_used = own.is_dir() and any(own.iterdir())
            if session.code_root == str(legacy):
                # 被钉在共享目录上：自己的目录里有东西、或压根没写过工程文件 → 解绑
                if own_used or not self._wrote_code_files(session):
                    session.code_root = ""
                    self.store.save(session)
                continue
            if session.code_root or own_used:
                continue
            if not self._wrote_code_files(session):
                continue      # 没写过东西：它自己会拿到 workspace/<会话 id>
            session.code_root = str(legacy)
            self.store.save(session)

    def _ensure_session_workspace(self, session: ChatSession, mode: str) -> None:
        """切到编程模式时把本会话的工程目录固定下来。

        工作区改成「每个会话一份」后，新会话用的是 ``workspace/<会话 id>``。若不写进
        会话，它和「改版前就在共享目录里写代码」的老会话就无法区分，重启时会被
        :meth:`_pin_legacy_workspaces` 误钉到共享目录（表现为工作区突然变成别人
        的工程、上传的附件也落到那边去）。**真写过工程文件的**会话按老会话处理，
        保持原样（交给 _pin_legacy_workspaces 判断）。
        """
        if mode != MODE_CODE or session.code_root:
            return
        if self._wrote_code_files(session):
            return
        session.code_root = str(self._session_workspace(session))

    def _refresh_sidebar(self) -> None:
        self.sidebar.set_sessions(self._ordered_sessions(), self.current.id if self.current else "")

    def new_session(self, silent: bool = False) -> None:
        """新建会话；若当前会话为空则直接复用。"""
        self._cancel_pending_retry()
        if self.current is not None and not self.current.messages:
            self.activate_session(self.current.id)
            self.composer.set_focus()
            return

        # 新会话固定从文档模式开始：写论文是主线，要写代码时手动切一次
        session = ChatSession(title="新的论文对话", model=self.config.model, mode=MODE_DOC)
        self.sessions[session.id] = session
        self._refresh_sidebar()
        self.activate_session(session.id)
        if not silent:
            self.composer.set_focus()

    def activate_session(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            return
        # 切换会话不打断生成：内容继续写回它所属的会话，切回来即可见
        if self.current is not None and self.current.id != session_id:
            self._remember_scroll(self.current.id)
            # 换会话后重试会打到别的会话去，且重试额度也不该跨会话共用
            self._cancel_pending_retry()
            self._auto_retry_count = 0

        self.current = session
        # 模式先跟着会话切好，后面刷新结构面板 / 工作区才知道该显示哪一边
        self._apply_session_mode(session.mode)
        self.chat_view.clear_messages()
        for message in session.messages:
            widget = self.chat_view.add_message(message)
            if message.status == "streaming":
                if message.id == self._streaming_id(session_id):
                    widget.resume_stream()
                else:
                    # 上次异常退出留下的中间态：按已停止处理，避免一直转圈
                    message.status = "aborted"
                    widget.finish_stream("aborted")
            elif message.status != "done":
                widget.finish_stream(message.status, message.error)
        self.chat_view.set_welcome_visible(not session.messages)
        self.sidebar.set_active(session_id)
        self.title_bar.set_session_title(session.title)
        # 右侧面板已经在 _apply_session_mode 里按模式刷过了（论文结构 / 代码工作区）
        # 会话里记的模型可能已经不存在了（比如那个自定义模型被删掉了）：
        # 落回一个真实存在的模型，并把会话里这份一起改掉、立刻落盘
        model = session.model if self.config.find_model(session.model) else self.config.model
        self.config.model = model
        if session.model != model:
            session.model = model
            self.store.save(session)
        self.composer.model_button.set_current(model)
        self.composer.sync_video_mode()
        self.composer.set_streaming(self._is_streaming_session(session))
        QApplication.processEvents()
        self._restore_scroll(session_id)

    # ------------------------------------------------------- 工作模式
    def current_mode(self) -> str:
        """当前会话的工作模式（没有会话时按文档模式）。"""
        return self.current.mode if self.current is not None else MODE_DOC

    def _apply_session_mode(self, mode: str) -> None:
        """把会话的模式铺到界面上：输入区、助手称谓、右侧面板。

        模式是**会话级**的：论文会话与工程会话可以并存，切换会话时界面跟着变，
        互不影响。
        """
        mode = mode if mode in (MODE_DOC, MODE_CODE) else MODE_DOC
        self.composer.set_mode(mode)
        self.chat_view.set_mode(mode)
        self.outline_panel.set_workspace_mode(mode == MODE_CODE)
        if mode == MODE_CODE:
            self.composer.deep_button.setChecked(False)
            self._refresh_workspace_panel()
        elif self.current is not None:
            self._refresh_outline(self.current)

    # ------------------------------------------------------- 浏览位置
    def _remember_scroll(self, session_id: str) -> None:
        """记住某个会话当前浏览到的位置。"""
        self._scroll_positions[session_id] = self.chat_view.scroll_position()

    def _restore_scroll(self, session_id: str) -> None:
        """还原上次浏览到的位置；没有记录时停在最新消息处。

        消息控件的高度要分几轮才结算完，这里交给 ``apply_scroll``
        在结算窗口内跟随修正，否则会被截断到顶部。
        """
        position = self._scroll_positions.get(session_id)
        QTimer.singleShot(0, lambda: self.chat_view.apply_scroll(position))

    # ------------------------------------------------------- 生成归属定位
    def _locate_message(self, message_id: str) -> tuple[ChatSession | None, Message | None]:
        """按消息 ID 找到所属会话与消息（生成可能发生在非当前会话）。"""
        for session in self.sessions.values():
            message = session.get_message(message_id)
            if message is not None:
                return session, message
        return None, None

    def _streaming_id(self, session_id: str) -> str:
        """某个会话正在生成的那条助手消息；没在跑就空串。"""
        return self.agent.streaming_message_id(session_id)

    def _current_streaming_id(self) -> str:
        """当前会话正在生成的那条助手消息（输入区 / 停止按钮都只关这个）。"""
        return self._streaming_id(self.current.id) if self.current is not None else ""

    def _pending_streaming_message(self, session: ChatSession | None) -> str:
        """会话里「界面上还显示正在生成」的那条消息（agent 那边可能已经结束了）。"""
        if session is None:
            return ""
        for message in reversed(session.messages):
            if message.status == "streaming":
                return message.id
        return ""

    def _is_streaming_session(self, session: ChatSession) -> bool:
        """该会话是不是正在生成（多个会话可以同时在跑）。"""
        return session is not None and bool(self._streaming_id(session.id))

    def _submit_block_reason(self, session: ChatSession) -> str:
        """能不能在这个会话里开一轮新生成；不能就返回给用户看的原因（空串 = 可以）。

        两道限制：同一个会话同时只能有一个任务（同会话并发只会让上下文打架），
        全局并发不超过 ``agent.max_parallel``（免费档的视频 / 生图很容易被限流，
        一起猛跑只会一起卡）。
        """
        if self.agent.is_session_running(session.id):
            return "这个会话正在生成中：等它跑完，或先点「停止生成」"
        if not self.agent.has_capacity:
            return (
                f"已经同时有 {self.agent.running_count} 个任务在跑"
                f"（上限 {self.agent.max_parallel} 个），等一个跑完再发；"
                "想同时跑更多可以调 settings.ini 的 chat/maxParallel"
                "（别调太大 —— 并发越高越容易撞供应商的限流）"
            )
        return ""

    def rename_session(self, session_id: str, title: str) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            return
        session.title = title
        session.touch()
        self.store.save(session)
        self._refresh_sidebar()
        if self.current and self.current.id == session_id:
            self.title_bar.set_session_title(title)

    def delete_session(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            return
        answer = QMessageBox.question(
            self,
            "删除对话",
            f"确定要删除“{session.title}”吗？该操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        # 正在这个会话里生成：先停掉，避免工作线程继续跑
        if self._is_streaming_session(session):
            self._stop_generation()

        self.sessions.pop(session_id, None)
        self._outline_seen.pop(session_id, None)
        self._scroll_positions.pop(session_id, None)
        self._cancel_pending_retry()
        self.store.delete(session_id)
        removed = self._cleanup_session_files(session)
        self._cleanup_session_workspace(session)
        self._cleanup_session_artifacts(session)
        if self.current and self.current.id == session_id:
            self.current = None
            self.chat_view.clear_messages()
            self.outline_panel.clear()
            self.title_bar.set_session_title("")
            remaining = sorted(self.sessions.values(), key=lambda s: s.updated_at, reverse=True)
            if remaining:
                self.activate_session(remaining[0].id)
            else:
                self.new_session(silent=True)
        self._refresh_sidebar()
        signals.toast_requested.emit(
            f"对话已删除，同时清理了 {removed} 个文件" if removed else "对话已删除"
        )

    # ------------------------------------------------------------ 文件清理
    def _cleanup_session_files(self, session: ChatSession) -> int:
        """删除会话时，清掉只属于它的产物 / 附件（其它对话还在引用的保留）。"""
        keep = {
            normalized_path(item.path)
            for other in self.sessions.values()
            if other is not session and other.id != session.id   # 自己的文件不算被引用
            for item in other.paper_files()
        }
        removed = 0
        for item in session.paper_files():
            if normalized_path(item.path) in keep:
                continue
            deleted, _detail = delete_managed_file(item.path)
            if deleted:
                removed += 1
        return removed

    def _cleanup_session_workspace(self, session: ChatSession) -> None:
        """删掉会话**专属**的代码工作区目录（``workspace/<会话 id>``）。

        这块目录是切到编程模式时应用自己按会话 id 建的，会话没了就是纯垃圾 ——
        不清理的话每用过一次编程模式就沉淀一个空目录，越攒越多。

        只删这一个：用户自己选过的目录、``PAPERAGENT_CODE_ROOT`` 指定的目录一律
        不碰（那是他真实的工程），还被别的会话指着的目录也不动。
        """
        # 走模块属性而不是 from ... import：测试会替换 default_root 指到临时目录
        root = code_workspace.default_root()
        owned = root / session.id
        if not owned.is_dir() or owned == root:
            return
        target = str(owned)
        occupied = any(
            other is not session
            and other.code_root
            and normalized_path(other.code_root) == normalized_path(target)
            for other in self.sessions.values()
        )
        if occupied:
            return
        shutil.rmtree(owned, ignore_errors=True)

    def _cleanup_session_artifacts(self, session: ChatSession) -> None:
        """删掉会话专属的产物目录（``artifacts/<会话 id>``）。

        里面的文件已由 ``_cleanup_session_files`` 按引用清过一轮（还在被别的对话引用的
        会留下），这里只收掉空壳 —— ``drop_session`` 也只在目录空时才动手。
        """
        session_artifacts.drop_session(session.id, base=ARTIFACTS_DIR)

    def _cleanup_orphan_files(self) -> None:
        """清理不再被任何对话引用的产物 / 附件。"""
        referenced = {
            normalized_path(item.path)
            for session in self.sessions.values()
            for item in session.paper_files()
        }
        orphans = [path for path in iter_managed_files() if normalized_path(path) not in referenced]
        if not orphans:
            signals.toast_requested.emit("没有需要清理的文件")
            return

        total = 0
        for path in orphans:
            try:
                total += path.stat().st_size
            except OSError:
                continue
        answer = QMessageBox.question(
            self,
            "清理未引用的文件",
            f"发现 {len(orphans)} 个不再被任何对话引用的文件（共 {human_readable_size(total)}），"
            "删除后无法恢复。确定清理吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        removed = 0
        for path in orphans:
            if delete_managed_file(path)[0]:
                removed += 1
        # 文件清完顺手收掉空的会话产物目录，否则每清一次就留一堆空壳
        session_artifacts.prune_empty(base=ARTIFACTS_DIR)
        signals.toast_requested.emit(f"已清理 {removed} 个文件（{human_readable_size(total)}）")

    def export_session(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            return
        lines = [f"# {session.title}", ""]
        for message in session.messages:
            who = "我" if message.is_user else mode_text(session.mode, "assistant")
            lines.extend([f"### {who}", "", message.content, ""])
        default_name = "".join(c for c in session.title if c not in '\\/:*?"<>|')[:40] or "对话"
        path, _selected = QFileDialog.getSaveFileName(
            self, "导出对话", f"{default_name}.md", "Markdown (*.md);;文本文件 (*.txt)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as file:
                file.write("\n".join(lines))
            signals.toast_requested.emit(f"已导出到 {path}")
        except OSError as exc:
            signals.toast_requested.emit(f"导出失败：{exc}")

    # ------------------------------------------------------------------ 发送
    def _on_submit(
        self,
        text: str,
        attachments: list,
        session: ChatSession | None = None,
        resume: str = "",
    ) -> None:
        """提交一次生成；``session`` 为空时作用于当前会话。

        传入非当前会话时（例如后台会话失败后的自动重试）只更新数据、不动界面。
        ``resume`` 为上一次失败尝试的进度摘要（见 :meth:`_resume_context`）。
        """
        if self.current is None and session is None:
            self.new_session(silent=True)
        target = session or self.current
        if target is None:
            return

        blocked = self._submit_block_reason(target)
        if blocked:
            signals.toast_requested.emit(blocked)
            return

        # 用户主动发起新一轮：作废等待中的自动重试（否则重试会删掉这一轮），
        # 同时重置重试额度。重试链路自己走这里时会被 _auto_retry 还原计数。
        self._cancel_pending_retry()
        self._auto_retry_count = 0

        visible = target is self.current

        if not target.messages:
            target.title = derive_title(text) if text else "附件对话"

        user_message = Message(
            role="user",
            content=text,
            attachments=list(attachments),
            model=self.config.model,
        )
        target.add_message(user_message)
        if visible:
            self.chat_view.add_message(user_message)

        assistant_message = Message(
            role="assistant", content="", model=self.config.model, status="streaming"
        )
        target.add_message(assistant_message)
        widget = self.chat_view.add_message(assistant_message) if visible else None
        if widget is not None:
            widget.begin_stream()
            self.title_bar.set_session_title(target.title)

        # 组装请求：内置模型经服务端代理，自定义模型走自己的 OpenAI 兼容接口，
        # 选中文生图模型时直接把用户输入当图片提示词
        endpoint = self._current_endpoint()
        image_mode = self.config.is_image_model(self.config.model)
        video_mode = self.config.is_video_model(self.config.model)
        problem = "" if (image_mode or video_mode) else self._endpoint_problem(endpoint)
        if problem:
            self._fail_pending(assistant_message, widget, target, problem)
            return

        # 历史里的用户图片也要带上，否则追问时模型会「看不见」之前发过的图。
        # 注意这里只收 status == "done"：被打断 / 失败的半截回答不进历史，
        # 否则模型会当成「已经答过一遍」而不重新往下写。
        history = [
            self._history_entry(m)
            for m in target.messages[:-2]
            if (m.content.strip() or m.attachments) and m.status == "done"
        ]
        # 上一条回复是被打断 / 失败的：把它的进度回灌给模型，
        # 用户再发「继续」时才是真的从断点接着写，而不是从头再来。
        prior = target.messages[-3] if len(target.messages) >= 3 else None
        resume = resume or self._prior_turn_resume(prior)
        # 会话工作区清单：让模型直接知道本会话已有文件，不用再叫它去磁盘搜
        session_files = workspace_snapshot(target)
        # 用**目标会话**的模式：后台会话重试时，当前会话可能已经切过模式了
        mode = target.mode or MODE_DOC
        # 编程模式：带上**该会话**工程目录的文件清单，模型才知道「默认动哪些文件」
        code_files: list[str] = []
        code_root_path = ""
        if mode == MODE_CODE:
            from paper_agent.services.skills.code_workspace import file_index

            workspace = self._session_workspace(target)
            code_root_path = str(workspace)
            # 上传的附件先**落到工程目录里**，再扫文件清单 —— 否则模型在工程目录里
            # 看不见用户传的文件（工具都钉在工程目录内），只会回「找不到这个文件」。
            # 顺序很重要：先复制，清单里才会带上它们。
            if attachments:
                copied = code_workspace.import_attachments(workspace, attachments)
                if copied:
                    self._refresh_workspace_panel()
            code_files, _total = file_index(workspace)

        messages = build_chat_messages(
            history,
            text,
            list(attachments),
            workspace=session_files,
            # 编程模式下「深度写作」没有意义，忽略该开关
            deep=self.composer.deep_button.isChecked() and mode != "code",
            resume=resume,
            mode=mode,
            code_files=code_files,
            code_root=code_root_path,
        )

        if visible:
            self.composer.set_streaming(True)
        self._start_live_outline(target)
        self.agent.start(
            assistant_message.id,
            messages=messages,
            model=self.config.model,
            effort=self.config.reasoning_effort,
            endpoint=endpoint,
            attachments=list(attachments),
            # 选中的是服务端的生图模型就走服务端代理，否则用本地配的那套
            image=self.config.image_config(self.config.model),
            image_mode=image_mode,
            video=self.config.video_config(self.config.model),
            video_mode=video_mode,
            video_params=self.composer.video_button.params,
            session_files=session_files,
            mode=mode,
            code_root=code_root_path,
            session_id=target.id,
        )
        self._refresh_sidebar()
        self.store.save(target)

    def _fail_pending(self, message: Message, widget, session: ChatSession, reason: str) -> None:
        """还没发请求就发现配置有问题：把这条助手消息就地标成失败。"""
        message.status = "error"
        message.error = reason
        if widget is not None:
            widget.finish_stream("error", reason)
        signals.toast_requested.emit(reason)
        self._refresh_sidebar()
        self.store.save(session)

    def _endpoint_problem(self, endpoint: dict | None) -> str:
        """模型配置能不能用；能用返回空串。

        自定义模型缺配置要向用户明说；内置模型则分两种情况：压根没登录时保持
        离线示例引擎（不打扰），登录了却拿不到服务端密钥才是真的有问题。
        """
        if endpoint is not None:
            return ""
        if self.config.is_custom_model(self.config.model):
            return "该自定义模型配置不完整，请先在「自定义模型」中补全"
        if not self.config.is_builtin_model(self.config.model):
            return ""                     # 普通内置示例模型：照旧走示例引擎
        if not self.config.logged_in:
            return ""
        if self.config.llm_key_expired:
            return "内置模型的代理密钥已过期，请在服务端续期后重新登录"
        return "内置模型需要服务端分配代理密钥：请确认服务端在看板里配好模型，并重新登录"

    @staticmethod
    def _history_entry(message: Message) -> dict:
        """历史消息：用户消息带图片时改用多模态 content。"""
        content = message.content
        if message.is_user and message.attachments:
            content = build_user_content(content, message.attachments)
        return {"role": message.role, "content": content}

    def _current_endpoint(self) -> dict | None:
        """当前选中模型对应的真实接口配置。

        - **自定义模型**：用户自己填的 Base URL 与 Key
        - **内置模型**：走服务端代理，Base URL 指向服务端，Key 用服务端签发的
          用户密钥（供应商的 Key 由服务端看板统一管理，桌面端拿不到也不需要）
        - 两者都拿不到时返回 ``None``，回落到内置示例引擎（离线预览）
        """
        model = self.config.find_model(self.config.model)
        if not model:
            return None
        if not model.get("custom"):
            key = self.config.llm_user_key
            if not model.get("builtin") or not key:
                return None
            return {
                "base_url": self.config.llm_proxy_url,
                "api_key": key,
                "api_keys": [key],
                "model_id": model["id"],
                "name": model["name"],
                "reasoning": "auto",
                "proxy": True,
            }
        custom = self.config.custom_model(model["id"])
        if custom is None:
            return None
        valid, _message = custom.is_valid()
        if not valid:
            return None
        pool = custom.key_pool
        return {
            "base_url": custom.base_url,
            "api_key": pool[0] if pool else "",
            "api_keys": pool,
            "model_id": custom.model_id,
            "name": custom.name,
            "reasoning": custom.reasoning,
        }

    def _stop_generation(self) -> None:
        """停止「当前会话」这一轮生成（别的会话在跑的不会被牵连）。"""
        message_id = self._current_streaming_id()
        # 没有在生成也要给个反馈：否则用户点了按钮完全没反应，
        # 只会觉得这个按钮是坏的。
        if not message_id or not self.agent.is_message_running(message_id):
            if message_id:
                self._stop_pending_ui()
            else:
                signals.toast_requested.emit("当前没有正在生成的内容")
            return

        self._stopping_message_id = message_id
        self.agent.stop(message_id)
        # stop() 刻意不阻塞：它会断开在途请求，由 worker 回报「已停止」。
        # 模型如果正卡在某个工具里（联网出图、代码执行），abort 断不到工具内部的
        # 网络请求，只能等它自己返回；这段时间必须持续可见地告诉用户在等待，
        # 不然观感就是「点了没反应」。
        widget = self.chat_view.widget_for(message_id)
        if widget is not None:
            widget.mark_note("正在停止…")
        signals.toast_requested.emit("正在停止生成…")
        self._stop_wait_s = 0
        self._stop_timer.start()

    def _stop_pending_ui(self) -> None:
        """线程其实已经结束，但界面状态没跟上（收尾信号丢失等异常情形）。"""
        self._stop_timer.stop()
        self._stop_wait_s = 0
        message_id = (
            self._stopping_message_id
            or self._current_streaming_id()
            or self._pending_streaming_message(self.current)
        )
        self._stopping_message_id = ""
        widget = self.chat_view.widget_for(message_id)
        if widget is not None:
            widget.finish_stream("aborted", "已停止生成")
        self.composer.set_streaming(self._is_streaming_session(self.current))
        signals.toast_requested.emit("已停止生成")

    def _tick_stopping(self) -> None:
        """停止中的倒计时：持续提示，并在久等不停时询问是否强制结束。

        看的是「正在停的那一条」而不是当前会话：用户等停止的过程中可能切到别的会话。
        """
        message_id = self._stopping_message_id
        if not message_id or not self.agent.is_message_running(message_id):
            # 线程其实已经结束了，只是收尾信号没到：直接把界面回正，
            # 否则发送键会一直停在「停止生成」
            self._stop_pending_ui()
            return
        self._stop_wait_s += 1
        widget = self.chat_view.widget_for(message_id)
        if widget is not None:
            widget.mark_note(f"正在停止…已等待 {self._stop_wait_s} 秒")
        if self._stop_wait_s < STOP_FORCE_ASK_SECONDS:
            return

        self._stop_timer.stop()
        answer = QMessageBox.question(
            self,
            "仍在停止",
            f"已等待 {self._stop_wait_s} 秒，当前这一步（可能是联网出图或代码执行）"
            "还无法安全中断。\n\n是否强制结束？（可能导致正在写的文件不完整）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._stop_wait_s = 0        # 用户愿意等：重新计时
            self._stop_timer.start()
            return
        self.agent.terminate(self._stopping_message_id)
        self._stop_pending_ui()

    def _on_agent_token(self, message_id: str, delta: str) -> None:
        widget = self.chat_view.widget_for(message_id)
        if widget is not None:
            widget.append_delta(delta)
            self.chat_view.maybe_follow()
            return
        # 消息不在当前会话：先写回模型，切回来时内容才是完整的
        _owner, message = self._locate_message(message_id)
        if message is not None:
            message.content += delta

    def _on_agent_thinking(self, message_id: str, delta: str) -> None:
        widget = self.chat_view.widget_for(message_id)
        if widget is not None:
            widget.append_thinking(delta)
            self.chat_view.maybe_follow()
            return
        _owner, message = self._locate_message(message_id)
        if message is not None:
            message.thinking += delta

    # ------------------------------------------------------- 智能体事件
    def _on_agent_plan(self, message_id: str, payload: str) -> None:
        """Planner 产出的计划 / todo 列表。"""
        try:
            data = json.loads(payload)
        except ValueError:
            return
        steps = data.get("steps", [])
        widget = self.chat_view.widget_for(message_id)
        if widget is not None:
            widget.set_plan(steps)
            self.chat_view.maybe_follow()
            return
        _owner, message = self._locate_message(message_id)
        if message is not None:
            message.plan = [
                PlanStep(
                    title=str(step.get("title", "")),
                    status=str(step.get("status", "pending")),
                    detail=str(step.get("detail", "")),
                )
                for step in steps
            ]

    def _on_agent_tool(self, message_id: str, payload: str) -> None:
        """工具（Skill）调用过程：写入思考区并记录，终端类调用另出一张卡片。"""
        try:
            data = json.loads(payload)
        except ValueError:
            return
        name = data.get("name", "")
        status = data.get("status", "")
        args = data.get("args", "")
        result = data.get("result", "")
        note = f"\n调用工具：{name}\n" if status == "running" else f"{name} 返回：{result}\n"

        widget = self.chat_view.widget_for(message_id)
        if widget is not None:
            widget.append_thinking(note)
            widget.add_tool_run(name=name, args=args, result=result, status=status)
            if status == "running":
                # 参数已经写完、工具开始执行：把实时草稿折叠掉，换成产物卡片
                widget.finish_draft(self._draft_filename(args))
            self._update_terminal_task(widget, name, args, status, data)
            self.chat_view.maybe_follow()
            return
        _owner, message = self._locate_message(message_id)
        if message is not None:
            message.thinking += note
            message.add_tool_run(name=name, args=args, result=result, status=status)

    @staticmethod
    def _update_terminal_task(
        widget, name: str, args: str, status: str, data: dict
    ) -> None:
        """终端类工具（跑命令 / 执行代码 / 起服务）在聊天里出一张任务卡片。

        卡片是给用户看的：终端有没有任务在跑、跑的是什么、跑了多久、输出什么。
        事件只有「开始」和「结束」两条，卡片因此按调用标识（工具名 + 原始入参）
        配对 —— 模型可能连续调好几次同一个工具，配对错了会把 A 的结果写到 B 上。
        """
        if not is_terminal_tool(name):
            return
        key = f"{name}|{args}"
        if status == "running":
            command = command_of(name, parse_tool_args(args))
            widget.begin_terminal_task(name, command, key)
            return
        widget.finish_terminal_task(
            key,
            success=status == "done",
            output=data.get("result", ""),
            url=str(data.get("open_url") or ""),
        )

    @staticmethod
    def _draft_filename(args: str) -> str:
        """从工具入参里取文件名，折叠草稿时显示「已写入 论文.docx」。"""
        try:
            data = json.loads(args or "{}")
        except ValueError:
            return ""
        name = data.get("filename") if isinstance(data, dict) else ""
        return str(name or "")

    def _on_agent_artifact(self, message_id: str, payload: str) -> None:
        """智能体产出的文件：渲染成可点击卡片。"""
        try:
            data = json.loads(payload)
        except ValueError:
            return
        name = data.get("name", "文件")
        path = data.get("path", "")
        kind = data.get("kind", "")

        widget = self.chat_view.widget_for(message_id)
        if widget is not None:
            widget.add_artifact(name, path, kind)
            self.chat_view.maybe_follow()
        else:
            _owner, message = self._locate_message(message_id)
            if message is not None:
                message.add_artifact(name, path, kind)

        # 这是本轮正在写的论文：结构面板立刻切换到这一篇
        owner, _message = self._locate_message(message_id)
        if owner is self.current and Path(path).suffix.lower() in (".docx", ".pdf"):
            self._outline_live_path = path
            self._outline_signature = None
            self._refresh_outline(owner)

    def _on_agent_content_final(self, message_id: str, text: str) -> None:
        """用清洗后的正文替换显示（代码块已落成文件）。"""
        widget = self.chat_view.widget_for(message_id)
        if widget is not None:
            widget.set_final_content(text)
            return
        _owner, message = self._locate_message(message_id)
        if message is not None:
            message.content = text

    def _on_agent_limit(self, message_id: str, payload: str) -> None:
        """引擎达到工具调用轮次上限：状态栏与提示条都要告知用户。"""
        try:
            limit = json.loads(payload).get("limit", "")
        except ValueError:
            limit = ""
        note = f"已达工具上限（{limit} 轮）" if limit else "已达工具上限"
        widget = self.chat_view.widget_for(message_id)
        if widget is not None:
            widget.mark_note(note)
        signals.toast_requested.emit(
            f"已达到工具调用上限（{limit} 轮），任务可能未完成，回复「继续」可接着做"
        )

    def _on_agent_draft(self, message_id: str, payload: str) -> None:
        """正在写入的正文：工具参数流实时解码出来的内容。"""
        try:
            data = json.loads(payload)
        except ValueError:
            return
        text = data.get("text", "")
        if not text:
            return
        widget = self.chat_view.widget_for(message_id)
        if widget is not None:
            widget.append_draft(text, data.get("name", ""))
            self.chat_view.maybe_follow()

    def _on_agent_note(self, message_id: str, text: str) -> None:
        """引擎的过程提示（如「接口限流，10 秒后重试本轮」）。"""
        widget = self.chat_view.widget_for(message_id)
        if widget is not None:
            widget.mark_note(text)

    def _on_agent_progress(self, message_id: str, value: str) -> None:
        """长任务进度（文生视频）：空串 = 还不知道进度（排队中）。"""
        widget = self.chat_view.widget_for(message_id)
        if widget is not None:
            widget.mark_progress(value)

    def _on_agent_output(self, message_id: str, payload: str) -> None:
        """终端任务的实时输出：追加到对应的任务卡片上。"""
        try:
            data = json.loads(payload)
        except ValueError:
            return
        chunk = data.get("chunk", "")
        if not chunk:
            return
        widget = self.chat_view.widget_for(message_id)
        if widget is None:
            return                       # 消息不在当前会话：输出不落库，丢了也无妨
        key = f"{data.get('name', '')}|{data.get('args', '')}"
        if widget.append_terminal_output(key, chunk):
            self.chat_view.maybe_follow()

    def _on_agent_open_url(self, message_id: str, payload: str) -> None:
        """模型起了本地服务：用系统默认浏览器打开。

        模型跑在无 GUI 的工具环境里，它自己开不了浏览器（也正因如此它常回一句
        「我这边无法弹出浏览器」）；这件事只能由应用代劳。
        """
        try:
            data = json.loads(payload)
        except ValueError:
            return
        url = str(data.get("url") or "").strip()
        if not url:
            return
        QDesktopServices.openUrl(QUrl(url))
        signals.toast_requested.emit(f"已在浏览器打开 {url}")
        widget = self.chat_view.widget_for(message_id)
        if widget is not None:
            widget.mark_note(f"已打开 {url}")

    def _on_agent_finished(self, message_id: str) -> None:
        self._auto_retry_count = 0      # 生成成功，重置重试计数
        widget = self.chat_view.widget_for(message_id)
        if widget is not None:
            widget.finish_draft()
            widget.finish_thinking()
            widget.sync_artifacts()      # 数据里有、界面漏渲染的产物补上
            widget.finish_stream("done")
        self._after_generation(message_id)

    def _on_agent_failed(self, message_id: str, error: str) -> None:
        aborted = error == "__aborted__"
        friendly = error if aborted else self._friendly_error(error)
        widget = self.chat_view.widget_for(message_id)
        if widget is not None:
            widget.finish_draft()
            widget.finish_thinking()
            widget.finish_stream("aborted" if aborted else "error", friendly)
        if aborted:
            signals.toast_requested.emit("已停止生成")
        self._after_generation(message_id, "aborted" if aborted else "error", friendly)

        # 自动重试：仅针对非人为中止的失败（网络抖动、超时、限流、5xx 等）
        if error == "__aborted__":
            return
        retry_limit, delays = retry_plan(error)
        if self._auto_retry_count >= retry_limit:
            signals.toast_requested.emit("多次重试仍未成功，请检查模型配置、额度或网络")
            return

        self._auto_retry_count += 1
        delay_ms = delays[min(self._auto_retry_count, len(delays)) - 1]
        seconds = max(1, round(delay_ms / 1000))
        attempt = f"第 {self._auto_retry_count}/{retry_limit} 次"

        if looks_rate_limited(error):
            # 限流要等额度窗口恢复，且必须换 Key（池里被限流的 Key 已进入冷却）
            note = f"接口限流，{seconds} 秒后自动重试（{attempt}）"
            signals.toast_requested.emit(
                f"{note}；在「自定义模型」里配置多个 API Key 可自动轮换"
            )
        else:
            note = f"{seconds} 秒后自动重试（{attempt}）"
            signals.toast_requested.emit(f"生成失败，{note}")

        if widget is not None:
            widget.mark_note(note)      # 等待过程写在消息里，避免看起来像卡死
        self._retry_message_id = message_id
        self._retry_timer.start(delay_ms)

    # ------------------------------------------------------------ 自动重试
    def _cancel_pending_retry(self) -> None:
        """作废待执行的自动重试。

        必须作废的原因：重试会从旧提问起重建整轮对话，用户在等待期间
        新发的消息会被连带删除；切走会话后重试也会打到别的会话里去。
        """
        pending, self._retry_message_id = self._retry_message_id, ""
        self._retry_timer.stop()
        if not pending:
            return
        widget = self.chat_view.widget_for(pending)
        if widget is not None:
            widget.mark_note("")        # 撤掉「N 秒后自动重试」的倒计时
        signals.toast_requested.emit("已取消等待中的自动重试")

    def _run_pending_retry(self) -> None:
        """到点了，真正执行重试前再确认一次上下文还成立。"""
        message_id, self._retry_message_id = self._retry_message_id, ""
        if not message_id:
            return
        owner, message = self._locate_message(message_id)
        session = owner or self.current
        if session is None or message is None:
            return
        if self.agent.is_session_running(session.id):
            return          # 这个会话已经有任务在跑了（用户又发了新的）
        # 失败那条已经不是最后一条：用户又聊了别的，重试会删掉这些新消息
        if session.messages and session.messages[-1].id != message_id:
            signals.toast_requested.emit("已放弃自动重试（这轮之后又有新消息）")
            return
        self._auto_retry(message_id)

    def _auto_retry(self, message_id: str) -> None:
        """执行一次自动重试：回到对应的用户提问重新生成。"""
        owner, _message = self._locate_message(message_id)
        if owner is not None and self.agent.is_session_running(owner.id):
            return          # 同一个会话同时只跑一个：已经有新的在跑就不重试了
        # _regenerate_message 内部会走 _on_submit（提交时会清零计数），
        # 因此先取出再恢复，保证重试链路上的计数不被重置。
        attempts = self._auto_retry_count
        self._regenerate_message(message_id)
        self._auto_retry_count = attempts

    @staticmethod
    def _friendly_error(error: str) -> str:
        """把常见接口错误翻译成可操作的提示。"""
        text = (error or "未知错误").strip()
        lowered = text.lower()
        if "文生图" in text or "生成图片" in text:
            # 文生图走独立接口与 Key，提示要指向对应配置
            if "http 401" in lowered or "未配置" in text:
                return f"{text}（文生图 Key 无效或未配置，可在 settings.ini 的 image/ 段修改）"
            if "无法连接" in text:
                return f"{text}（请检查文生图服务地址与网络）"
            return text
        if "http 401" in lowered or "unauthorized" in lowered:
            return f"{text}（API Key 无效或已过期，请到「自定义模型」中检查）"
        if "http 403" in lowered:
            return f"{text}（没有访问权限，请检查 Key 的权限或额度）"
        if "http 404" in lowered:
            return f"{text}（接口地址或模型 ID 不正确）"
        if "http 429" in lowered:
            return f"{text}（请求过于频繁或额度不足，请稍后再试）"
        if "http 400" in lowered and is_unsupported_error(text):
            return (
                f"{text}（该服务端不接受这个推理强度参数，可在「自定义模型 → "
                "推理强度参数」里改为「不发送」）"
            )
        if "http 5" in lowered:
            return f"{text}（服务侧异常，稍后通常会自动恢复）"
        if "无法连接" in text or "timed out" in lowered or "超时" in text:
            return f"{text}（网络不通或服务超时，请检查网络与 Base URL）"
        return text

    def _after_generation(self, message_id: str, status: str = "done", error: str = "") -> None:
        """一次生成结束：写回消息状态、持久化所属会话，并按需刷新界面。

        生成可能属于非当前会话（切换会话后仍在后台进行），此时只更新数据。
        """
        owner, message = self._locate_message(message_id)
        is_current = owner is not None and owner is self.current
        if message_id == self._stopping_message_id:
            # 停的就是它：停止倒计时收工
            self._stopping_message_id = ""
            self._stop_timer.stop()
            self._stop_wait_s = 0
        if is_current:
            self._stop_live_outline()
        # 输入区按「当前会话还在不在跑」回正：别的会话结束了，
        # 不能把当前会话正在生成时的「停止生成」状态给清掉。
        self.composer.set_streaming(self._is_streaming_session(self.current))
        if message is not None and self.chat_view.widget_for(message_id) is None:
            # 控件已随会话切换被销毁：把最终状态直接写回模型
            message.status = status
            message.error = error
        if owner is None:
            return

        # 产物可能新增了论文文件：重新解析真实结构（后台会话只更新数据）
        self._refresh_outline(owner, panel=is_current)
        self.store.save(owner)
        self._refresh_sidebar()

    def _tick_stream_guard(self) -> None:
        """生成状态兜底：按真实状态把输入区对齐，别停在「停止生成」。

        正常流程由 finished / failed 信号收尾，但凡中间漏了一环（线程已结束却没
        回报、消息被提前删掉、强制结束），界面就会一直显示成还在生成。
        """
        if self._is_streaming_session(self.current):
            return
        if self._stop_timer.isActive() or not self.composer.is_streaming:
            return
        self.composer.set_streaming(False)

    # ------------------------------------------------------------------ 消息操作
    def _copy_message(self, message_id: str) -> None:
        session = self.current
        if session is None:
            return
        message = session.get_message(message_id)
        if message is None:
            return
        copy_text(message.content)
        signals.toast_requested.emit("已复制到剪贴板")

    def _delete_message(self, message_id: str) -> None:
        session = self.current
        if session is None:
            return
        session.remove_message(message_id)
        self.chat_view.remove_message(message_id)
        self.chat_view.set_welcome_visible(not session.messages)
        self.store.save(session)

    def _regenerate_message(self, message_id: str) -> None:
        """回到对应的用户提问重新生成（连同该提问携带的附件一起重发）。"""
        owner, _message = self._locate_message(message_id)
        session = owner or self.current
        if session is None:
            return
        index = next(
            (i for i, m in enumerate(session.messages) if m.id == message_id),
            None,
        )
        if index is None:
            return
        # 只对「失败中断」的回复做接续：成功了用户手动点重生成，
        # 说明他想要的是另一版，不该把上一版喂回去。
        failed = session.messages[index]
        resume = (
            self._resume_context(failed) if failed.status in ("error", "aborted") else ""
        )

        # 回溯到对应的用户提问：文字与附件都要带上，否则重试会丢掉附件
        prompt = ""
        attachments: list[Attachment] = []
        start = index
        for i in range(index - 1, -1, -1):
            if session.messages[i].is_user:
                prompt = session.messages[i].content
                attachments = list(session.messages[i].attachments)
                start = i
                break

        if not prompt and not attachments:
            signals.toast_requested.emit("没有可重新生成的提问")
            return

        # 连同原提问一起移除，交由 _on_submit 重建，避免提问被重复一份
        for message in session.messages[start:]:
            self.chat_view.remove_message(message.id)
            session.remove_message(message.id)

        self._on_submit(prompt, attachments, session, resume=resume)

    def _prior_turn_resume(self, previous: Message | None) -> str:
        """上一条助手回复若是被打断 / 失败的，返回它的进度摘要。

        只挑「确实有进度」的：工具调用过、产出了文件，或者已经写了 200 字以上。
        刚起头就断开的那种（比如才吐了几十个字）不值得塞回上下文。
        """
        if previous is None or previous.is_user:
            return ""
        if previous.status not in ("aborted", "error"):
            return ""
        progressed = bool(previous.tool_runs or previous.artifacts) or len(
            (previous.content or "").strip()
        ) >= 200
        return self._resume_context(previous) if progressed else ""

    @staticmethod
    def _resume_context(message: Message) -> str:
        """上一次尝试的进度摘要（给模型看的，不是给用户看的）。

        包含三部分：工具调用记录、已产出文件、已写正文的尾部。
        这样重试时模型知道哪些步骤已经成功，直接接着往下做。
        """
        lines: list[str] = []

        if message.tool_runs:
            # 状态必须写清楚：以前这里统一写「已完成」、状态还留着英文 running/done/failed，
            # 模型看到「→ running：」（返回为空）就以为没跑成，转头按自己的叙述编过程
            # ——「明明没跑却说 5 段全跑完了」有一半是这么来的。
            lines.append(
                "上一次的工具调用记录（工具名 / 入参 / 结果）："
                "成功 = 真的做完了；失败 = 报错了；未完成 = 没拿到返回，**不要当成已完成**。"
            )
            for run in message.tool_runs[-RESUME_MAX_RUNS:]:
                args = " ".join((run.args or "").split())
                if len(args) > RESUME_ARG_CHARS:
                    args = args[:RESUME_ARG_CHARS] + "…"
                result = " ".join((run.result or "").split())
                if len(result) > RESUME_RESULT_CHARS:
                    result = result[:RESUME_RESULT_CHARS] + "…"
                state = _RUN_STATE_TEXT.get(run.status, "未完成（没有拿到返回）")
                lines.append(f"- {run.name}（{args}）→ {state}：{result or '（无返回内容）'}")

        if message.artifacts:
            lines.append("上一次已产出的文件（沿用即可，不要重新生成）：")
            for item in message.artifacts:
                lines.append(f"- {item.name} → {item.path}")

        body = (message.content or "").strip()
        if body:
            # 只需要尾部：让模型知道写到哪里断的，不必把整篇再发一遍。
            # 断的时候常常正好断在特殊代码块（```docx 之类）中间，残缺的围栏
            # 塞进上下文会干扰模型，直接砍掉最后那半截。
            tail = body[-RESUME_BODY_CHARS:]
            if tail.count("```") % 2:
                tail = tail[: tail.rfind("```")].rstrip()
            lines.append("上一次写到一半的正文（末尾片段，可从这里接续）：")
            lines.append(tail)

        return "\n".join(lines)

    def _use_suggestion(self, prompt: str) -> None:
        self.composer.editor.setPlainText(prompt)
        self.composer.set_focus()

    # ------------------------------------------------------------------ 面板
    def toggle_sidebar(self) -> None:
        visible = not self.sidebar.isVisible()
        self.sidebar.setVisible(visible)
        self.config.sidebar_visible = visible

    def toggle_outline(self) -> None:
        visible = not self.outline_panel.isVisible()
        self._set_outline_visible(visible)
        if visible and self.current:
            self._refresh_outline(self.current)
            # 生成途中才打开面板：补上实时刷新（只跟当前会话那一轮）
            if self._is_streaming_session(self.current):
                self._outline_signature = None
                self._outline_timer.start()
        elif not visible:
            self._outline_timer.stop()

    def _set_outline_visible(self, visible: bool) -> None:
        """显示/隐藏结构面板，并把它占的宽度在聊天区与面板之间交接清楚。"""
        if visible == self.outline_panel.isVisible():
            return
        if visible:
            self.outline_panel.setVisible(True)
            self._apply_outline_width()
        else:
            self._remember_outline_width()
            self.outline_panel.setVisible(False)
        self.config.outline_visible = visible

    def _apply_outline_width(self) -> None:
        """打开结构面板时给它一个真正能看的宽度。

        面板自身的最小宽度只有 220，Qt 只按最小宽度摆放，一打开就是一条缝；
        这里按上次用过的宽度（默认 ``OUTLINE_PANEL_WIDTH``）分配，同时保证
        中间聊天栏不被压到最小宽度以下，避免三栏宽度之和超出窗口。
        """
        sizes = self.splitter.sizes()
        if len(sizes) != 3:
            return
        total = sum(sizes)
        sidebar = sizes[0] if self.sidebar.isVisible() else 0
        chat_floor = self.splitter.widget(1).minimumSizeHint().width()
        minimum = self.outline_panel.minimumWidth()
        width = max(self._outline_width, minimum)
        width = min(width, max(total - sidebar - chat_floor, minimum))
        self.splitter.setSizes([sidebar, total - sidebar - width, width])

    def _remember_outline_width(self) -> None:
        """记住用户调过的宽度，下次打开沿用（最小宽度不值得记）。"""
        width = self.outline_panel.width()
        if width > 0:
            self._outline_width = max(width, OUTLINE_PANEL_WIDTH)

    def _toggle_maximized(self) -> None:
        self.toggle_maximized()
        self.title_bar.set_maximized(self.is_maximized)

    def title_bar_height(self) -> int:
        return self.title_bar.height()

    # ------------------------------------------------------------------ 主题
    def _toggle_theme(self) -> None:
        self._set_theme(THEME_DARK if not theme_manager.is_dark else THEME_LIGHT)

    def _set_theme(self, name: str) -> None:
        theme_manager.set_theme(name)
        app = QApplication.instance()
        if app is not None:
            theme_manager.apply(app)  # type: ignore[arg-type]
        self.config.theme = name
        self._apply_window_icon()
        self.theme_changed.emit(name)

    def _on_theme_changed(self, _name: str) -> None:
        self._apply_window_icon()

    def _apply_window_icon(self) -> None:
        """设置任务栏 / 窗口边框图标。

        图标颜色跟随 **系统主题**（决定任务栏底色），而不是应用主题：
        应用切深色时任务栏未必跟着变深，若图标跟着变白就会看不清。
        """
        app = QApplication.instance()
        if app is None:
            return
        color = "#FFFFFF" if system_prefers_dark() else "#1B1B19"
        app.setWindowIcon(build_icon("sparkles", color, 64))

    def _on_model_changed(self, model_id: str) -> None:
        if self.current:
            self.current.model = model_id
            self.store.save(self.current)

    def _on_mode_changed(self, mode: str) -> None:
        """工作模式切换：文档（论文/PPT）↔ 编程（配环境/写代码/跑代码）。

        模式记在**当前会话**上：切走的会话保留它自己的模式，切回来还是那个模式。
        """
        if self.current is not None:
            self.current.mode = mode
            self._ensure_session_workspace(self.current, mode)
            self.store.save(self.current)     # 立刻落盘，异常退出也不丢
        self._apply_session_mode(mode)
        if mode == MODE_CODE:
            signals.toast_requested.emit(
                f"已切到编程模式：本会话的工作区是 {self._session_workspace()}"
            )
        else:
            signals.toast_requested.emit("已切回文档模式：写论文 / 排版 Word、PPT、PDF")

    # ------------------------------------------------------------ 代码工作区
    def _session_workspace(self, session: ChatSession | None = None) -> Path:
        """该会话的代码工作区目录（**会话级**，会话之间互不共享）。"""
        target = session or self.current
        if target is None:
            return code_root()
        return code_root(target.id, target.code_root)

    def _refresh_workspace_panel(self, skip_if_unchanged: bool = False) -> None:
        """刷新右侧的工作区目录树（只在编程模式下有意义）。

        Args:
            skip_if_unchanged: 目录树没变化就不重绘（实时刷新每 1.2 秒跑一次，
                重绘会把用户展开的目录收回去）
        """
        if self.current_mode() != MODE_CODE:
            return
        root = self._session_workspace()
        nodes, files, dirs = build_tree(root)
        signature = tree_signature(nodes)
        if skip_if_unchanged and signature == self._workspace_signature:
            return
        self._workspace_signature = signature
        self.outline_panel.set_workspace(str(root), nodes, files, dirs)
        self._outline_seen.pop("__workspace__", None)

    def _pick_workspace(self) -> None:
        """选择工作区文件夹：记在**当前会话**上，别的会话不受影响。"""
        if self.current is None:
            return
        current = self.current.code_root or str(self._session_workspace())
        chosen = QFileDialog.getExistingDirectory(self, "选择代码工作区", current)
        if not chosen:
            return
        self.current.code_root = chosen
        self.store.save(self.current)
        self._refresh_workspace_panel()
        signals.toast_requested.emit(f"本会话工作区已切换：{chosen}")

    def _open_workspace_file(self, path: str) -> None:
        """双击工作区里的文件：用系统默认程序打开。"""
        target = Path(path)
        if not target.is_file():
            signals.toast_requested.emit("文件不存在，可能已被移动")
            self._refresh_workspace_panel()
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    # ------------------------------------------------------------------ 论文结构
    def _refresh_outline(self, session: ChatSession, *, panel: bool = True) -> None:
        """解析并展示「所选论文」的真实章节结构。

        一个会话里可能生成/上传多篇论文，面板只展示当前选中的那一篇，
        避免多篇结构混在一起；结构全部来自真实文件，不再使用样例数据。
        编程模式下右侧面板是代码工作区，改刷目录树。
        """
        # 按**这个会话**的模式判断：后台会话结束时也会走到这里，
        # 那时当前会话可能已经切到另一种模式了
        if (session.mode or MODE_DOC) == MODE_CODE:
            self._refresh_workspace_panel()
            return
        candidates = paper_candidates(session)
        paths = [item.path for item in candidates]
        seen = self._outline_seen.get(session.id, ())
        self._outline_seen[session.id] = tuple(paths)

        # 出现新论文时自动切到它（用户刚生成完，多半想看这一篇）；
        # 否则保留用户手动选择的论文，避免被后续对话抢走选中项。
        if paths and paths[0] not in seen:
            current = paths[0]
        elif session.outline_source in paths:
            current = session.outline_source
        else:
            current = paths[0] if paths else ""

        if current:
            session.outline_source = current
            session.outline = extract_outline(current)
        else:
            session.outline_source = ""
            session.outline = []
        if not panel:
            return

        self.outline_panel.set_papers(
            [(item.path, source_label(item)) for item in candidates], current
        )
        writing = (
            bool(current)
            and self.agent.is_session_running(session.id)
            and current == self._outline_live_path
        )
        if not current:
            self.outline_panel.set_outline([], NO_PAPER_EMPTY)
        elif not Path(current).exists():
            self.outline_panel.set_outline([], "这篇论文的文件已不存在，可能已被清理。")
        else:
            self.outline_panel.set_outline(
                session.outline, NO_STRUCTURE_EMPTY, writing=writing
            )

    # ------------------------------------------------------------ 实时结构
    def _start_live_outline(self, session: ChatSession) -> None:
        """开始一轮生成：空掉上一轮的「正在写」状态并启动实时刷新。

        结构面板只反映**当前会话**：后台会话（自动重试 / 并发时另一个会话）开始在跑时
        不能去清当前会话的实时结构状态。
        """
        if session is not self.current:
            return
        self._outline_live_path = ""
        self._outline_signature = None
        if self.outline_panel.isVisible():
            self._outline_timer.start()

    def _streaming_text(self, session: ChatSession) -> str:
        """该会话正在生成的助手正文（还没落成文件时用作实时结构来源）。"""
        message_id = self._streaming_id(session.id)
        if not message_id:
            return ""
        message = session.get_message(message_id)
        return message.content if message is not None else ""

    def _tick_outline(self) -> None:
        """生成过程中实时刷新结构：优先用正在写入的文件，其次用正在输出的正文。

        实时结构只跟**当前会话那一轮**：别的会话在后台跑，不该动这个面板。
        """
        session = self.current
        if session is None or not self.agent.is_session_running(session.id):
            self._outline_timer.stop()
            return
        if not self.outline_panel.isVisible():
            return

        if (session.mode or MODE_DOC) == MODE_CODE:
            # 编程模式没有「实时章节结构」可言：改成实时刷工作区，
            # 模型一边写文件、目录树一边长出来
            self._refresh_workspace_panel(skip_if_unchanged=True)
            return

        live_file = self._outline_live_path
        if live_file and Path(live_file).exists():
            # 文件内容变了才重绘（extract_outline 内部按 mtime 缓存，重复调用开销很小）
            signature: object = (live_file, Path(live_file).stat().st_mtime)
        else:
            nodes = extract_markdown_outline(self._streaming_text(session))
            signature = outline_signature(nodes)

        if signature == self._outline_signature:
            return
        self._outline_signature = signature

        if live_file and Path(live_file).exists():
            self._refresh_outline(session)
        else:
            self.outline_panel.set_outline(nodes, writing=True)

    def _stop_live_outline(self) -> None:
        self._outline_timer.stop()
        self._outline_live_path = ""
        self._outline_signature = None

    def _on_outline_paper_selected(self, path: str) -> None:
        """面板里切换了要查看的论文。"""
        session = self.current
        if session is None or not path:
            return
        session.outline_source = path
        session.outline = extract_outline(path)
        if not Path(path).exists():
            self.outline_panel.set_outline([], "这篇论文的文件已不存在，可能已被清理。")
        else:
            self.outline_panel.set_outline(session.outline, NO_STRUCTURE_EMPTY)
        self.store.save(session)

    # ------------------------------------------------------------------ 论文空间
    def _open_paper_space(self) -> None:
        """打开「我的论文空间」：本对话中上传与生成的文件。"""
        if self.current is None:
            signals.toast_requested.emit("请先新建或选择一个对话")
            return
        from paper_agent.ui.dialogs.paper_space_dialog import PaperSpaceDialog

        dialog = PaperSpaceDialog(self.current, self)
        dialog.files_changed.connect(self._on_paper_files_changed)
        dialog.exec()

    def _on_paper_files_changed(self, removed_paths: list) -> None:
        """论文空间中删除文件后：同步消息区的附件 / 产物卡片，再持久化会话。"""
        session = self.current
        if session is None:
            return
        removed = {str(path) for path in removed_paths}
        for message in session.messages:
            widget = self.chat_view.widget_for(message.id)
            if widget is not None:
                widget.remove_files(removed)
        # 删掉的可能是当前选中的论文：重新解析结构
        self._refresh_outline(session)
        # 界面同步完成后再落盘，确保磁盘内容与内存一致
        self.store.save(session)

    # ------------------------------------------------------------------ AIGC 检测
    def _open_aigc(self) -> None:
        """打开 AIGC 检测：上传文档 → 逐句判定 → 导出标注 PDF。"""
        from paper_agent.ui.dialogs.aigc_dialog import AigcDialog

        AigcDialog(self).exec()

    def _open_aigc_history(self) -> None:
        """打开 AIGC 检测记录：回看历史检测、重新导出、删除。"""
        from paper_agent.ui.dialogs.aigc_history_dialog import AigcHistoryDialog

        AigcHistoryDialog(self).exec()

    # ------------------------------------------------------------------ 自定义模型
    def _open_model_settings(self) -> None:
        """打开自定义模型管理对话框。"""
        from paper_agent.ui.dialogs.model_settings_dialog import ModelSettingsDialog

        dialog = ModelSettingsDialog(self.config, self)
        dialog.models_changed.connect(self._on_models_changed)
        dialog.exec()

    def _on_models_changed(self) -> None:
        """自定义模型增删改后刷新下拉框。"""
        models = self.config.all_models()
        current = self.config.model
        if not any(m["id"] == current for m in models):
            current = models[0]["id"] if models else ""
            self.config.model = current
        self.composer.refresh_models(current)
        if self.current is not None:
            self.current.model = current
            self.store.save(self.current)

    # ------------------------------------------------------------------ 设置菜单
    def _show_settings_menu(self) -> None:
        from PySide6.QtGui import QAction, QActionGroup
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        menu.setWindowFlags(
            menu.windowFlags()
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )

        theme_group = QActionGroup(menu)
        for name, label in ((THEME_LIGHT, "浅色主题"), (THEME_DARK, "深色主题")):
            action = QAction(label, menu)
            action.setCheckable(True)
            action.setChecked(theme_manager.theme.name == name)
            action.setActionGroup(theme_group)
            action.triggered.connect(lambda _c=False, n=name: self._set_theme(n))
            menu.addAction(action)

        menu.addSeparator()

        shortcut_group = QActionGroup(menu)
        for mode, label in (("enter", "Enter 发送"), ("ctrl+enter", "Ctrl + Enter 发送")):
            action = QAction(label, menu)
            action.setCheckable(True)
            action.setChecked(self.config.send_shortcut == mode)
            action.setActionGroup(shortcut_group)
            action.triggered.connect(lambda _c=False, m=mode: self.composer.set_send_shortcut(m))
            menu.addAction(action)

        menu.addSeparator()
        action_outline = QAction("显示论文结构面板", menu)
        action_outline.setCheckable(True)
        action_outline.setChecked(self.outline_panel.isVisible())
        action_outline.triggered.connect(self.toggle_outline)
        menu.addAction(action_outline)

        menu.addSeparator()
        menu.addAction("AIGC 检测…").triggered.connect(self._open_aigc)
        menu.addAction("AIGC 检测记录…").triggered.connect(self._open_aigc_history)
        menu.addAction("清理未引用的文件…").triggered.connect(self._cleanup_orphan_files)

        menu.addSeparator()
        self._add_account_actions(menu)

        menu.addSeparator()
        menu.addAction("自定义模型…").triggered.connect(self._open_model_settings)

        menu.addSeparator()
        menu.addAction("关于 Paper Agent").triggered.connect(self._show_about)

        button = self.title_bar.settings_button
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    # ------------------------------------------------------------------ 账号
    def _refresh_account(self) -> None:
        """把登录态同步到侧栏（头像 + 昵称）与欢迎页问候语。"""
        name = self.config.display_name
        self.sidebar.set_account(name, self.config.avatar)
        self.chat_view.welcome.set_user(name)
        # 登录态一变就顺带刷新内置模型的代理密钥与名称 / 备注（未登录会被清空）
        self.llm_key_syncer.sync()
        self.llm_models_syncer.sync()

    def _on_llm_key_updated(self, payload: dict) -> None:
        """代理密钥同步结果（全局信号，已在主线程）：写入配置，必要时提示。

        未登录时收到的是空密钥，等于把上次登录留下的旧密钥清掉。
        """
        error = str(payload.get("error") or "")
        if error:
            if self.config.logged_in:
                signals.toast_requested.emit(f"未能获取内置模型代理密钥：{error}")
            return
        key = str(payload.get("key") or "")
        expires = str(payload.get("expires_at") or "")
        if not key:
            self.config.clear_llm_key()
            return
        if key != self.config.llm_user_key or expires != self.config.llm_key_expires:
            self.config.llm_user_key = key
            self.config.llm_key_expires = expires
            self.config.sync()

    def _on_llm_models_updated(self, payload: dict) -> None:
        """服务端下发的内置模型名称 / 备注（全局信号，已在主线程）。

        变化时存进配置并重建模型下拉框 —— 服务端看板改完名字，重登一次就能看到。
        拉不到（未登录 / 服务端没起）就沿用本地默认文案，不提示、不报错。
        """
        if payload.get("error"):
            return
        models = payload.get("models")
        if not isinstance(models, list) or models == self.config.builtin_models:
            return
        self.config.builtin_models = models
        self.config.sync()
        self.composer.refresh_models(self.config.model)

    def _open_account(self) -> None:
        """账号资料：换头像、改昵称、登录 / 退出登录。"""
        from paper_agent.ui.dialogs.account_dialog import AccountDialog

        dialog = AccountDialog(self.config, self)
        dialog.exec()
        self._refresh_account()

    def _add_account_actions(self, menu) -> None:
        """设置菜单里的账号项：已登录显示昵称并可退出，未登录提供登录入口。"""
        from PySide6.QtGui import QAction

        account = self.config.auth_user
        if account:
            nickname = account.get("nickname") or account.get("email") or "已登录"
            action = QAction(f"账号：{nickname}", menu)
            action.triggered.connect(self._show_account)
            menu.addAction(action)
            menu.addAction("退出登录").triggered.connect(self._logout)
        else:
            menu.addAction("登录 / 注册…").triggered.connect(self._login)

    def _show_account(self) -> None:
        self._open_account()

    def _login(self) -> None:
        from paper_agent.ui.dialogs.login_dialog import LoginDialog

        dialog = LoginDialog(self.config, self, allow_offline=False)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.account:
            user = dialog.account.get("user") or {}
            # 登录后要立刻刷新侧栏并取回内置模型的代理密钥
            self._refresh_account()
            signals.toast_requested.emit(
                f"已登录：{user.get('nickname') or user.get('email')}"
            )

    def _logout(self) -> None:
        import threading

        from paper_agent.services.auth_client import AuthClient

        token = self.config.auth_token
        if token:
            client = AuthClient(self.config.auth_server_url)
            threading.Thread(target=client.logout, args=(token,), daemon=True).start()
        self.config.clear_account()
        self._refresh_account()
        signals.toast_requested.emit("已退出登录")

    def _show_about(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("关于 Paper Agent")
        dialog.setFixedWidth(360)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(10)

        title = QLabel("Paper Agent")
        title.setObjectName("welcomeTitle")
        layout.addWidget(title)

        desc = QLabel(
            "面向毕业论文写作的智能体工作台：选题分析、章节大纲、正文写作、"
            "文献整理、AIGC 检测与降重润色，全流程辅助。\n\n"
            "内置模型（如 SenseNova 6.8 Flash Lite）经服务端代理调用，"
            "供应商密钥由服务端看板统一管理；未登录时回复由内置示例引擎生成。"
        )
        desc.setObjectName("welcomeSubtitle")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        row = QHBoxLayout()
        row.addStretch(1)
        ok = QPushButton("好的")
        ok.setObjectName("primaryButton")
        ok.setFixedWidth(90)
        ok.clicked.connect(dialog.accept)
        row.addWidget(ok)
        layout.addLayout(row)
        dialog.exec()

    # ------------------------------------------------------------------ 窗口
    def _restore_window_state(self) -> None:
        geometry = self.config.window_geometry
        if geometry:
            self.restoreGeometry(geometry)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self._centered:
            self._centered = True
            self._center_on_screen()
        self.apply_window_effects()
        self.title_bar.set_session_title(self.current.title if self.current else "")
        if not self._panes_ready:
            # 首次显示后三栏宽度才是最终的：恢复上次打开的结构面板宽度
            self._panes_ready = True
            if self.outline_panel.isVisible():
                self._apply_outline_width()

    def _center_on_screen(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        width = max(self.width(), self.minimumWidth())
        height = max(self.height(), self.minimumHeight())
        x = available.x() + (available.width() - width) // 2
        y = available.y() + (available.height() - height) // 2
        self.move(max(x, available.x()), max(y, available.y()))

    def closeEvent(self, event):  # noqa: N802
        self.config.window_geometry = self.saveGeometry()
        if self.current is not None:
            self._remember_scroll(self.current.id)
            self.store.save(self.current)
        # 后台生成中的会话可能不是当前会话（并发时还不止一个），退出前都要落盘
        for session_id in self.agent.running_sessions():
            streaming = self.sessions.get(session_id)
            if streaming is not None and streaming is not self.current:
                self.store.save(streaming)
        if self.agent.is_running:
            self.agent.stop()
        self.config.sync()
        super().closeEvent(event)
