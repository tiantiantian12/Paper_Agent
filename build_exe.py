"""用 PyInstaller 把 Paper Agent 桌面端打包为 Windows 可执行文件。

前置依赖::

    pip install -r requirements.txt
    pip install pyinstaller

用法::

    python build_exe.py
    # 产物：dist/Paper_Agent.exe

构建完成后，把 dist/Paper_Agent.exe 拷到服务端的下载目录
（默认 ``Paper_Agent_Server/data/downloads/Paper_Agent.exe``）。
若文件名不同，改 ``Paper_Agent_Server/config.json`` 的 ``exe_name`` 即可。
"""
from __future__ import annotations

import PyInstaller.__main__


def main() -> None:
    PyInstaller.__main__.run(
        [
            "main.py",
            "--name=Paper_Agent",
            "--windowed",
            "--onefile",
            "--noconfirm",
            "--clean",
            "--hidden-import=PySide6.QtCore",
            "--hidden-import=PySide6.QtGui",
            "--hidden-import=PySide6.QtWidgets",
            "--collect-all=PySide6",
            "--collect-submodules=paper_agent",
            "--collect-all=imageio_ffmpeg",
            "--add-data=paper_agent/resources;paper_agent/resources",
        ]
    )


if __name__ == "__main__":
    main()
