"""账号资料：查看 / 更换头像与昵称，登录或退出登录。"""

from __future__ import annotations

import threading

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from paper_agent.core.config import AppConfig
from paper_agent.core.constants import TITLE_BAR_HEIGHT
from paper_agent.core.signals import signals
from paper_agent.services.auth_client import AuthClient
from paper_agent.ui.dialogs.login_dialog import AuthWorker, LoginDialog, LoginTitleBar
from paper_agent.ui.frameless import FramelessMixin
from paper_agent.utils.avatar import (
    BUILTIN_AVATARS,
    avatar_pixmap,
    builtin_names,
    icon_spec,
    save_uploaded_avatar,
)

WINDOW_WIDTH = 420
WINDOW_HEIGHT = 560
PREVIEW_SIZE = 68
OPTION_SIZE = 40


class _AvatarOption(QPushButton):
    """头像候选：圆形小图，选中时描边。"""

    def __init__(self, spec: str, tooltip: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        self.setObjectName("avatarOption")
        self.setCheckable(True)
        self.setFixedSize(OPTION_SIZE, OPTION_SIZE)
        self.setIcon(QIcon(avatar_pixmap(spec, OPTION_SIZE - 6)))
        self.setIconSize(QSize(OPTION_SIZE - 6, OPTION_SIZE - 6))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if tooltip:
            self.setToolTip(tooltip)


class AccountDialog(FramelessMixin, QDialog):
    """账号资料窗口。``exec()`` 结束后由调用方刷新界面上的账号显示。"""

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("accountDialog")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setFixedWidth(WINDOW_WIDTH)
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)

        self.config = config
        self._client = AuthClient(config.auth_server_url)
        self._worker: AuthWorker | None = None
        self._centered = False
        self.logged_out = False

        self._spec = config.avatar
        self._initial_spec = self._spec
        self._initial_nickname = config.display_name

        # ---------------- 外壳
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        self.container = QWidget(self)
        self.container.setObjectName("accountContainer")
        shell.addWidget(self.container)

        outer = QVBoxLayout(self.container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.title_bar = LoginTitleBar("账号", self.container)
        self.title_bar.minimize_requested.connect(self.showMinimized)
        self.title_bar.close_requested.connect(self.reject)
        outer.addWidget(self.title_bar)

        root = QVBoxLayout()
        root.setContentsMargins(24, 16, 24, 16)
        root.setSpacing(12)
        outer.addLayout(root)

        root.addLayout(self._build_identity())
        root.addLayout(self._build_avatar_picker())
        root.addStretch(1)

        self.error_label = QLabel("", self.container)
        self.error_label.setObjectName("formError")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        root.addWidget(self.error_label)

        root.addLayout(self._build_footer())
        self._refresh()

    # ------------------------------------------------------------------ 构建
    def _build_identity(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(14)

        column = QVBoxLayout()
        column.setSpacing(6)

        self.avatar_button = QPushButton(self.container)
        self.avatar_button.setObjectName("accountAvatarButton")
        self.avatar_button.setFixedSize(PREVIEW_SIZE, PREVIEW_SIZE)
        self.avatar_button.setIconSize(self.avatar_button.size())
        self.avatar_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.avatar_button.setToolTip("当前头像")
        column.addWidget(self.avatar_button)
        column.addStretch(1)
        row.addLayout(column)

        info = QVBoxLayout()
        info.setContentsMargins(0, 6, 0, 0)
        info.setSpacing(8)

        caption = QLabel("昵称", self.container)
        caption.setObjectName("formLabel")
        info.addWidget(caption)

        self.nickname_edit = QLineEdit(self.container)
        self.nickname_edit.setPlaceholderText("登录后可在服务端同步昵称")
        self.nickname_edit.setMaxLength(24)
        self.nickname_edit.textChanged.connect(self._sync_save_state)
        info.addWidget(self.nickname_edit)

        self.email_label = QLabel("", self.container)
        self.email_label.setObjectName("accountEmail")
        info.addWidget(self.email_label)

        info.addStretch(1)
        row.addLayout(info, 1)
        return row

    def _build_avatar_picker(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(8)

        caption = QLabel("头像", self.container)
        caption.setObjectName("formLabel")
        column.addWidget(caption)

        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        self.options: list[_AvatarOption] = []
        labels = dict((name, label) for name, _color, label in BUILTIN_AVATARS)
        for index, name in enumerate(builtin_names()):
            option = _AvatarOption(icon_spec(name), labels.get(name, ""), self.container)
            option.clicked.connect(lambda _c=False, s=option.spec: self._select(s))
            self.options.append(option)
            self.grid.addWidget(option, index // 4, index % 4)
        column.addLayout(self.grid)

        self.upload_button = QPushButton("从本地上传图片…", self.container)
        self.upload_button.setObjectName("linkButton")
        self.upload_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.upload_button.clicked.connect(self._on_upload)
        upload_row = QHBoxLayout()
        upload_row.setContentsMargins(0, 0, 0, 0)
        upload_row.addWidget(self.upload_button)
        upload_row.addStretch(1)
        column.addLayout(upload_row)
        return column

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.logout_button = QPushButton("退出登录", self.container)
        self.logout_button.setObjectName("linkButton")
        self.logout_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.logout_button.clicked.connect(self._on_logout)
        row.addWidget(self.logout_button)

        row.addStretch(1)

        self.save_button = QPushButton("保存", self.container)
        self.save_button.setObjectName("primaryButton")
        self.save_button.setFixedWidth(96)
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.clicked.connect(self._on_save)
        row.addWidget(self.save_button)
        return row

    # ------------------------------------------------------------------ 状态
    def _refresh(self) -> None:
        """按当前登录状态刷新可编辑项与按钮文案。"""
        logged = self.config.logged_in
        self._spec = self.config.avatar
        self._initial_spec = self._spec
        self._initial_nickname = self.config.display_name

        self.avatar_button.setIcon(QIcon(avatar_pixmap(self._spec, PREVIEW_SIZE)))
        self.nickname_edit.setText(self.config.display_name)
        self.nickname_edit.setEnabled(logged)
        self.email_label.setText(self.config.display_email or "未登录")
        self.logout_button.setText("退出登录" if logged else "登录 / 注册")
        self.title_bar.set_subtitle("账号资料" if logged else "尚未登录")
        self._mark_selected()
        self._sync_save_state()

    def _mark_selected(self) -> None:
        """内置头像的选中态；上传的头像不在列表里，因此都不选中。"""
        for option in self.options:
            option.setChecked(option.spec == self._spec)

    def _select(self, spec: str) -> None:
        self._spec = spec
        self.avatar_button.setIcon(QIcon(avatar_pixmap(spec, PREVIEW_SIZE)))
        self._mark_selected()
        self._sync_save_state()

    def _sync_save_state(self) -> None:
        nickname = self.nickname_edit.text().strip()
        changed = self._spec != self._initial_spec or nickname != self._initial_nickname
        self.save_button.setEnabled(changed)
        self.save_button.setText("保存" if changed else "已保存")

    # ------------------------------------------------------------------ 操作
    def _on_upload(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self, "选择头像图片", "", "图片文件 (*.png *.jpg *.jpeg *.bmp *.webp)"
        )
        if not path:
            return
        try:
            spec = save_uploaded_avatar(path)
        except ValueError as exc:
            signals.toast_requested.emit(str(exc))
            return
        self._select(spec)

    def _on_logout(self) -> None:
        if not self.config.logged_in:
            dialog = LoginDialog(self.config, self, allow_offline=False)
            if dialog.exec() == QDialog.DialogCode.Accepted and dialog.account:
                self.logged_out = False
                self._refresh()
                signals.toast_requested.emit("已登录")
            return

        token = self.config.auth_token
        self.config.clear_account()
        if token:
            client = AuthClient(self.config.auth_server_url)
            threading.Thread(target=client.logout, args=(token,), daemon=True).start()
        self.logged_out = True
        self.accept()

    def _on_save(self) -> None:
        spec = self._spec
        nickname = self.nickname_edit.text().strip()
        changed_avatar = spec != self._initial_spec
        changed_nickname = nickname != self._initial_nickname
        if not (changed_avatar or changed_nickname):
            self.accept()
            return

        self.config.avatar = spec
        if not self.config.logged_in:
            # 离线使用：头像只存本地
            self._initial_spec = spec
            self._sync_save_state()
            self.accept()
            return

        if self._worker is not None and self._worker.isRunning():
            return
        self._show_error("")
        self.save_button.setEnabled(False)
        self.save_button.setText("保存中…")
        token = self.config.auth_token

        def task() -> dict:
            return self._client.update_profile(
                token,
                nickname if changed_nickname else None,
                spec if changed_avatar else None,
            )

        self._worker = AuthWorker(task, self)
        self._worker.succeeded.connect(self._on_saved)
        self._worker.failed.connect(self._on_save_failed)
        self._worker.start()

    def _on_saved(self, data: dict) -> None:
        self.config.patch_account(
            nickname=data.get("nickname"), avatar=data.get("avatar") or self._spec
        )
        self.config.avatar = data.get("avatar") or self._spec
        self._refresh()
        signals.toast_requested.emit("账号资料已更新")
        self.accept()

    def _on_save_failed(self, message: str) -> None:
        self.save_button.setEnabled(True)
        self.save_button.setText("保存")
        self._show_error(message)

    def _show_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))

    # ------------------------------------------------------------------ 窗口
    def title_bar_height(self) -> int:
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
