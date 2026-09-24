"""登录 / 注册对话框：无边框窗口 + 自制标题栏。

QQ 邮箱 + 四位验证码注册，邮箱密码登录；外观与主窗口保持一致
（同一个 ``FramelessMixin`` / ``WindowButton`` / 主题 token）。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from paper_agent.core.config import AppConfig
from paper_agent.core.constants import TITLE_BAR_HEIGHT
from paper_agent.core.signals import signals
from paper_agent.core.theme import theme_manager
from paper_agent.resources.icons import build_icon
from paper_agent.services.auth_client import AuthClient, DEFAULT_SERVER_URL
from paper_agent.ui.frameless import FramelessMixin
from paper_agent.ui.title_bar import WindowButton

# 验证码重发倒计时（秒），与服务端冷却保持一致
RESEND_SECONDS = 60

WINDOW_WIDTH = 420
WINDOW_HEIGHT = 580


class LoginTitleBar(QWidget):
    """登录框专用标题栏：品牌 + 副标题 + 最小化 / 关闭。"""

    minimize_requested = Signal()
    close_requested = Signal()

    def __init__(self, subtitle: str = "登录", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("titleBar")
        self.setFixedHeight(TITLE_BAR_HEIGHT)

        root = QHBoxLayout(self)
        root.setContentsMargins(10, 0, 4, 0)
        root.setSpacing(4)

        self.brand_icon = QLabel(self)
        self.brand_icon.setFixedSize(16, 16)
        root.addWidget(self.brand_icon)

        title = QLabel("Paper Agent", self)
        title.setObjectName("titleBarTitle")
        root.addWidget(title)

        self._separator = QFrame(self)
        self._separator.setFrameShape(QFrame.Shape.VLine)
        self._separator.setFixedWidth(1)
        root.addWidget(self._separator)

        self.subtitle_label = QLabel(subtitle, self)
        self.subtitle_label.setObjectName("titleBarSubtitle")
        self.subtitle_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        root.addWidget(self.subtitle_label, 1)

        self.minimize_button = WindowButton("win-minimize", "最小化", parent=self)
        self.minimize_button.clicked.connect(self.minimize_requested)
        root.addWidget(self.minimize_button)

        self.close_button = WindowButton("win-close", "关闭", danger=True, parent=self)
        self.close_button.clicked.connect(self.close_requested)
        root.addWidget(self.close_button)

        self._refresh_assets()
        signals.theme_changed.connect(self._refresh_assets)

    def set_subtitle(self, text: str) -> None:
        self.subtitle_label.setText(text)

    def _refresh_assets(self, *_args) -> None:
        accent = theme_manager.color("accent")
        self.brand_icon.setPixmap(build_icon("sparkles", accent, 32).pixmap(16, 16))
        self._separator.setStyleSheet("background: %s;" % theme_manager.color("border"))


class AuthWorker(QThread):
    """后台线程执行网络请求，避免界面卡住。"""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, func: Callable[[], dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._func = func

    def run(self) -> None:  # noqa: D102 - QThread 入口
        try:
            result = self._func()
        except Exception as exc:  # noqa: BLE001 - 统一转成提示文案
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(result)


class LoginDialog(FramelessMixin, QDialog):
    """登录成功后把令牌写入配置并 ``accept()``；离线使用则清空登录态。

    ``exec()`` 返回 Accepted 时可通过 :attr:`account` 判断是否已登录。
    """

    def __init__(
        self,
        config: AppConfig,
        parent: QWidget | None = None,
        allow_offline: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("loginDialog")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setFixedWidth(WINDOW_WIDTH)
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)

        self.config = config
        self._allow_offline = allow_offline
        self._client = AuthClient(config.auth_server_url)
        self._worker: AuthWorker | None = None
        self._countdown = 0
        self._centered = False
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick_countdown)

        # 登录结果：{"token":..., "user":{...}}；离线使用为 None
        self.account: dict | None = None

        # ---------------- 外壳：无边框窗口 + 自制标题栏 + 内容区
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        self.container = QWidget(self)
        self.container.setObjectName("loginContainer")
        shell.addWidget(self.container)

        outer = QVBoxLayout(self.container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.title_bar = LoginTitleBar("登录 / 注册", self.container)
        self.title_bar.minimize_requested.connect(self.showMinimized)
        self.title_bar.close_requested.connect(self.reject)
        outer.addWidget(self.title_bar)

        root = QVBoxLayout()
        root.setContentsMargins(24, 14, 24, 16)
        root.setSpacing(10)
        outer.addLayout(root)

        caption = QLabel("登录后同步你的账号与论文空间", self.container)
        caption.setObjectName("welcomeSubtitle")
        caption.setWordWrap(True)
        root.addWidget(caption)

        self.tabs = QTabWidget(self.container)
        self.tabs.setObjectName("loginTabs")
        self.tabs.addTab(self._build_login_tab(), "登录")
        self.tabs.addTab(self._build_register_tab(), "注册")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self.tabs)

        self.error_label = QLabel("", self.container)
        self.error_label.setObjectName("formError")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        root.addWidget(self.error_label)

        root.addLayout(self._build_server_row())
        root.addLayout(self._build_footer())

    # ------------------------------------------------------------------ 构建
    def _field(self, label: str, placeholder: str, password: bool = False) -> QLineEdit:
        host = QWidget(self.container)
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        caption = QLabel(label, host)
        caption.setObjectName("formLabel")
        edit = QLineEdit(host)
        edit.setPlaceholderText(placeholder)
        if password:
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(caption)
        layout.addWidget(edit)
        self._form_layout.addWidget(host)
        return edit

    def _build_login_tab(self) -> QWidget:
        page = QWidget(self.container)
        self._form_layout = QVBoxLayout(page)
        self._form_layout.setContentsMargins(14, 16, 14, 12)
        self._form_layout.setSpacing(10)

        self.email_edit = self._field("邮箱", "you@qq.com")
        self.password_edit = self._field("密码", "登录密码", password=True)
        self.password_edit.returnPressed.connect(self._on_login)

        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(0, 0, 0, 0)
        toggle_row.addStretch(1)
        self.password_toggle = QPushButton("显示密码", page)
        self.password_toggle.setObjectName("linkButton")
        self.password_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.password_toggle.clicked.connect(self._toggle_password)
        toggle_row.addWidget(self.password_toggle)
        self._form_layout.addLayout(toggle_row)

        self._form_layout.addStretch(1)

        self.login_button = QPushButton("登录", page)
        self.login_button.setObjectName("primaryButton")
        self.login_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.login_button.clicked.connect(self._on_login)
        self._form_layout.addWidget(self.login_button)
        return page

    def _build_register_tab(self) -> QWidget:
        page = QWidget(self.container)
        self._form_layout = QVBoxLayout(page)
        self._form_layout.setContentsMargins(14, 16, 14, 12)
        self._form_layout.setSpacing(10)

        self.nickname_edit = self._field("昵称", "怎么称呼你")

        # 邮箱 + 验证码
        email_host = QWidget(page)
        email_layout = QVBoxLayout(email_host)
        email_layout.setContentsMargins(0, 0, 0, 0)
        email_layout.setSpacing(4)
        caption = QLabel("QQ 邮箱", email_host)
        caption.setObjectName("formLabel")
        code_row = QHBoxLayout()
        code_row.setSpacing(6)
        self.reg_email_edit = QLineEdit(email_host)
        self.reg_email_edit.setPlaceholderText("you@qq.com")
        self.code_button = QPushButton("获取验证码", email_host)
        self.code_button.setObjectName("codeButton")
        self.code_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.code_button.clicked.connect(self._on_send_code)
        code_row.addWidget(self.reg_email_edit, 1)
        code_row.addWidget(self.code_button)
        email_layout.addWidget(caption)
        email_layout.addLayout(code_row)
        self._form_layout.addWidget(email_host)

        self.code_edit = self._field("验证码", "邮箱收到的 4 位验证码")
        self.reg_password_edit = self._field("密码", "至少 6 位", password=True)
        self.reg_password2_edit = self._field("确认密码", "再输入一次密码", password=True)
        self.reg_password2_edit.returnPressed.connect(self._on_register)

        self._form_layout.addStretch(1)

        self.register_button = QPushButton("注册并登录", page)
        self.register_button.setObjectName("primaryButton")
        self.register_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.register_button.clicked.connect(self._on_register)
        self._form_layout.addWidget(self.register_button)
        return page

    def _build_server_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        caption = QLabel("服务地址", self.container)
        caption.setObjectName("formLabel")
        self.server_edit = QLineEdit(self.container)
        self.server_edit.setPlaceholderText(DEFAULT_SERVER_URL)
        self.server_edit.setText(self.config.auth_server_url)
        self.server_edit.editingFinished.connect(self._on_server_changed)
        row.addWidget(caption)
        row.addWidget(self.server_edit, 1)
        return row

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        if self._allow_offline:
            offline = QPushButton("离线使用", self.container)
            offline.setObjectName("linkButton")
            offline.setCursor(Qt.CursorShape.PointingHandCursor)
            offline.clicked.connect(self._on_offline)
            row.addWidget(offline)
        return row

    # ------------------------------------------------------------------ 交互
    def _toggle_password(self) -> None:
        hidden = self.password_edit.echoMode() == QLineEdit.EchoMode.Password
        mode = QLineEdit.EchoMode.Normal if hidden else QLineEdit.EchoMode.Password
        self.password_edit.setEchoMode(mode)
        self.password_toggle.setText("隐藏密码" if hidden else "显示密码")

    def _on_tab_changed(self, index: int) -> None:
        self.title_bar.set_subtitle("注册新账号" if index == 1 else "登录 / 注册")
        self._show_error("")

    def _on_server_changed(self) -> None:
        value = self.server_edit.text().strip()
        if not value:
            self.server_edit.setText(DEFAULT_SERVER_URL)
            value = DEFAULT_SERVER_URL
        self.config.auth_server_url = value
        self._client = AuthClient(value)

    def _on_offline(self) -> None:
        """跳过登录，直接进入本地可用状态。"""
        self.account = None
        self.config.clear_account()
        self.accept()

    def _show_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))

    def _set_busy(self, busy: bool, button: QPushButton, idle_text: str) -> None:
        button.setEnabled(not busy)
        button.setText("处理中…" if busy else idle_text)
        self.tabs.setEnabled(not busy)

    # ------------------------------------------------------------------ 请求
    def _run(self, func: Callable[[], dict], on_ok: Callable[[dict], None], button: QPushButton, idle_text: str) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self._show_error("")
        self._set_busy(True, button, idle_text)
        self._worker = AuthWorker(func, self)
        self._worker.succeeded.connect(lambda data: self._on_ok(data, on_ok, button, idle_text))
        self._worker.failed.connect(lambda msg: self._on_fail(msg, button, idle_text))
        self._worker.start()

    def _on_ok(self, data: dict, on_ok: Callable[[dict], None], button: QPushButton, idle_text: str) -> None:
        self._set_busy(False, button, idle_text)
        on_ok(data)

    def _on_fail(self, message: str, button: QPushButton, idle_text: str) -> None:
        self._set_busy(False, button, idle_text)
        self._show_error(message)

    def _on_send_code(self) -> None:
        email = self.reg_email_edit.text().strip()
        if not email:
            self._show_error("请先填写 QQ 邮箱")
            return
        if not email.lower().endswith("@qq.com"):
            self._show_error("请使用 QQ 邮箱注册（xxx@qq.com）")
            return

        def task() -> dict:
            return self._client.send_code(email)

        def done(_data: dict) -> None:
            self._countdown = RESEND_SECONDS
            self._timer.start()
            self._tick_countdown()
            self._show_error("")
            self.code_edit.setFocus()

        self._run(task, done, self.code_button, "获取验证码")

    def _on_login(self) -> None:
        email = self.email_edit.text().strip()
        password = self.password_edit.text()
        if not email:
            self._show_error("请填写邮箱")
            return
        if not password:
            self._show_error("请填写密码")
            return

        def task() -> dict:
            return self._client.login(email, password)

        self._run(task, self._finish, self.login_button, "登录")

    def _on_register(self) -> None:
        nickname = self.nickname_edit.text().strip()
        email = self.reg_email_edit.text().strip()
        code = self.code_edit.text().strip()
        password = self.reg_password_edit.text()
        confirm = self.reg_password2_edit.text()

        if not nickname:
            self._show_error("请填写昵称")
            return
        if not email:
            self._show_error("请填写 QQ 邮箱")
            return
        if len(code) < 4:
            self._show_error("请填写邮箱收到的 4 位验证码")
            return
        if len(password) < 6:
            self._show_error("密码至少 6 位")
            return
        if password != confirm:
            self._show_error("两次输入的密码不一致")
            return

        def task() -> dict:
            return self._client.register(email, code, nickname, password)

        self._run(task, self._finish, self.register_button, "注册并登录")

    def _finish(self, data: dict) -> None:
        """登录 / 注册成功：写入配置并关闭。"""
        token = str(data.get("token") or "")
        user = data.get("user") or {}
        if not token:
            self._show_error("服务端未返回登录令牌")
            return
        self.config.set_account(token, user)
        self.account = {"token": token, "user": user}
        self.accept()

    # ------------------------------------------------------------------ 倒计时
    def _tick_countdown(self) -> None:
        if self._countdown <= 0:
            self._timer.stop()
            self.code_button.setEnabled(True)
            self.code_button.setText("重新获取")
            return
        self.code_button.setEnabled(False)
        self.code_button.setText(f"{self._countdown}s 后重发")
        self._countdown -= 1

    # ------------------------------------------------------------------ 窗口
    def title_bar_height(self) -> int:
        """可拖动区域高度（FramelessMixin 用来判断拖动 / 双击）。"""
        return TITLE_BAR_HEIGHT

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.apply_window_effects()
        if not self._centered:
            self._centered = True
            self._center_on_screen()

    def _center_on_screen(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        x = available.x() + (available.width() - self.width()) // 2
        y = available.y() + (available.height() - self.height()) // 2
        self.move(max(x, available.x()), max(y, available.y()))

    def closeEvent(self, event):  # noqa: N802
        if self._worker is not None and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(2000)
        super().closeEvent(event)
