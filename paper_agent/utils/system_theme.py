"""系统外观偏好检测。

任务栏 / 窗口边框图标需要适配 **Windows 任务栏底色**（系统主题），
而不是应用自身的主题。否则应用切到深色后图标变成白色，
而系统任务栏仍是浅色，图标就会看不见。
"""

from __future__ import annotations

import sys


def system_prefers_dark() -> bool:
    """判断系统是否为深色外观。

    Windows 读取 ``AppsUseLightTheme`` 注册表项（0 表示深色）；
    其它平台或读取失败时保守返回 ``False``（按浅色处理）。
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return int(value) == 0
    except Exception:            # noqa: BLE001 - 读不到时按浅色处理
        return False
