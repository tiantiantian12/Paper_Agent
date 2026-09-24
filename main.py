"""
Paper Agent —— 毕业论文写作智能体桌面端。

启动方式::

    python main.py
"""
import os
import sys

# Windows 下让任务栏图标使用独立的 AppUserModelID（需在创建 QApplication 之前设置）
if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PaperAgent.Desktop")
    except Exception:  # pragma: no cover - 非关键能力
        pass

# 高 DPI：Qt6 默认已启用缩放，这里仅统一缩放策略
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

from PySide6.QtWidgets import QApplication  # noqa: E402

from paper_agent.app import Application  # noqa: E402


def main() -> int:
    app = Application(sys.argv)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
