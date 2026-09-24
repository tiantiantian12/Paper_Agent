"""应用入口对象：初始化配置、主题、登录与主窗口。"""

from __future__ import annotations

from PySide6.QtWidgets import QApplication, QDialog

from paper_agent.core.config import AppConfig
from paper_agent.core.constants import APP_ID, APP_NAME, APP_VERSION, ORG_NAME
from paper_agent.core.session_store import SessionStore
from paper_agent.core.theme import theme_manager
from paper_agent.ui.dialogs.login_dialog import LoginDialog
from paper_agent.ui.main_window import MainWindow


class Application(QApplication):
    """Paper Agent 应用。"""

    def __init__(self, argv: list[str]) -> None:
        super().__init__(argv)
        self.setApplicationName(APP_NAME)
        self.setApplicationDisplayName(APP_NAME)
        self.setOrganizationName(ORG_NAME)
        self.setApplicationVersion(APP_VERSION)
        self.setQuitOnLastWindowClosed(True)
        self.setStyle("Fusion")

        self.config = AppConfig()
        self.store = SessionStore()

        theme_manager.set_theme(self.config.theme)
        theme_manager.apply(self)

        # 主窗口在登录成功后才创建，避免未登录也加载全部会话
        self.main_window: MainWindow | None = None

    def run(self) -> int:
        if not self._bootstrap_account():
            return 0

        self.main_window = MainWindow(self.config, self.store)
        self._center_on_primary(self.main_window)
        self.main_window.show()
        return self.exec()

    def _bootstrap_account(self) -> bool:
        """启动时的登录流程。

        已登录直接进入；未登录弹出登录 / 注册对话框，允许「离线使用」跳过；
        关闭对话框则不启动主窗口。
        """
        if self.config.logged_in:
            return True

        dialog = LoginDialog(self.config)
        return dialog.exec() == QDialog.DialogCode.Accepted

    @staticmethod
    def _center_on_primary(window: object) -> None:
        """把无边框窗口居中放到当前屏幕可用区域（避开任务栏）。"""
        screen = window.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        size = window.size()
        width = max(size.width(), window.minimumWidth())
        height = max(size.height(), window.minimumHeight())
        x = available.x() + (available.width() - width) // 2
        y = available.y() + (available.height() - height) // 2
        window.move(max(x, available.x()), max(y, available.y()))
