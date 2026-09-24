"""内置模型清单：同步服务端看板维护的名称与备注。

内置模型的显示名、备注（"备注"就是下拉框里名字下面那行小字）由服务端看板统一维护，
客户端登录后拉一次::

    GET /api/llm/models   →   [{"model_id", "name", "desc"}, ...]

拉下来缓存到本地配置（``AppConfig.builtin_models``），
:meth:`AppConfig.all_models` 再把它套到本地内置模型清单上。这样服务端改完名字，
客户端**不用发新版**，下次启动 / 登录就跟着变。

与代理密钥一样，请求放在 **daemon 线程**里跑，结果用全局信号
``signals.llm_models_updated`` 投回主线程：窗口随时可能被关掉，把 QThread 挂在
窗口上会有「线程还在跑就被销毁」导致进程直接 abort 的风险。

拉不到（未登录 / 服务端没起 / 网络不通）就静默保持本地默认文案 —— 这本来就是
锦上添花的同步，不该弹错误打扰用户。
"""

from __future__ import annotations

import threading

from paper_agent.core.config import AppConfig
from paper_agent.core.signals import signals
from paper_agent.services.auth_client import AuthClient


def normalize(raw: object) -> list[dict]:
    """把服务端返回值收敛成 ``[{"model_id", "name", "desc", "kind"}]``。

    脏数据直接丢掉；``kind`` 只认 ``chat`` / ``image`` / ``video``
    （认不出来的按对话处理）。
    """
    if not isinstance(raw, list):
        return []
    models: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("model_id") or "").strip()
        if not model_id:
            continue
        kind = str(item.get("kind") or "").strip().lower()
        models.append(
            {
                "model_id": model_id,
                "name": str(item.get("name") or "").strip(),
                "desc": str(item.get("desc") or "").strip(),
                "kind": kind if kind in ("chat", "image", "video") else "chat",
            }
        )
    return models


class LlmModelsSyncer:
    """按登录态同步内置模型的名称 / 备注。"""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._busy = False

    def sync(self) -> None:
        """未登录直接清掉缓存；已登录则后台拉一次（重复调用会被忽略）。"""
        if not self._config.logged_in:
            signals.llm_models_updated.emit({"models": []})
            return
        if self._busy:
            return
        self._busy = True
        threading.Thread(
            target=self._fetch,
            args=(self._config.auth_server_url, self._config.auth_token),
            daemon=True,
        ).start()

    def _fetch(self, server_url: str, token: str) -> None:
        """后台线程：只发信号，不碰界面与配置。"""
        try:
            data = AuthClient(server_url).llm_models(token)
        except Exception as exc:      # noqa: BLE001 - 网络异常不能让界面崩
            self._busy = False
            signals.llm_models_updated.emit({"error": str(exc)})
            return
        self._busy = False
        if not isinstance(data, list):
            signals.llm_models_updated.emit({"error": "服务端返回异常"})
            return
        signals.llm_models_updated.emit({"models": normalize(data)})
