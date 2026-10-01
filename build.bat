@echo off
REM 一键打包 Paper Agent 桌面端为 exe（需先装好 Python 环境与依赖）
python -m pip install -q pyinstaller
python build_exe.py
echo.
echo 产物在 dist/Paper_Agent.exe，把它拷到服务端的 data/downloads/ 即可被下载。
pause
