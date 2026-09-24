"""内置模型的代理密钥：向服务端换取，结果通过全局信号回主线程。

内置模型（如 SenseNova 6.8 Flash Lite）不直连供应商，请求统一走服务端代理：

    桌面端 ──(Bearer 用户密钥)──▶ 服务端 /api/llm/v1 ──(供应商 Key)──▶ 大模型

供应商的 Base URL 与 API Key 都在服务端看板里维护，桌面端只需要一把「用户密钥」。
这里负责登录后把它取回来（服务端首次会自动签发），并在退出登录时清掉。

网络请求放在 **daemon 线程**里跑，结果用全局 ``signals.llm_key_updated`` 投回主线程：
窗口随时可能被关掉/回收，把 QThread 挂在窗口上会有「线程还在跑就被销毁」导致
进程直接 abort 的风险，走全局信号总线就没有这个生命周期问题。
"""

from __future__ import annotations

import threading

from paper_agent.core.config import AppConfig
from paper_agent.core.signals import signals
from paper_agent.services.auth_client import AuthClient


class LlmKeySyncer:
    """按登录态同步代理密钥。"""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._busy = False

    def sync(self, notify: bool = False) -> None:
        """未登录直接清空；已登录则后台取一次（重复调用会被忽略）。"""
        if not self._config.logged_in:
            signals.llm_key_updated.emit({"key": "", "expires_at": "", "notify": notify})
            return
        if self._busy:
            return
        self._busy = True
        threading.Thread(
            target=self._fetch,
            args=(self._config.auth_server_url, self._config.auth_token, notify),
            daemon=True,
        ).start()

    def _fetch(self, server_url: str, token: str, notify: bool) -> None:
        """后台线程：只发信号，不碰界面与配置。"""
        try:
            data = AuthClient(server_url).llm_key(token)
        except Exception as exc:      # noqa: BLE001 - 网络异常不能让界面崩
            self._busy = False
            signals.llm_key_updated.emit({"error": str(exc), "notify": notify})
            return
        self._busy = False
        if not isinstance(data, dict):
            signals.llm_key_updated.emit({"error": "服务端返回异常", "notify": notify})
            return
        signals.llm_key_updated.emit(
            {
                "key": str(data.get("key") or ""),
                "expires_at": str(data.get("expires_at") or ""),
                "notify": notify,
            }
        )
