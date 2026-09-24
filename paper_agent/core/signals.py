"""全局信号总线（单例）。"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class _AppSignals(QObject):
    """跨模块的轻量事件总线。"""

    # 主题
    theme_changed = Signal(str)                 # theme name: light/dark
    # 会话
    session_created = Signal(str)               # session id
    session_activated = Signal(str)             # session id
    session_renamed = Signal(str, str)          # session id, title
    session_deleted = Signal(str)               # session id
    session_updated = Signal(str)               # session id
    # 智能体
    agent_started = Signal(str, str)            # session id, message id
    agent_token = Signal(str, str, str)         # session id, message id, delta
    agent_finished = Signal(str, str)           # session id, message id
    agent_failed = Signal(str, str, str)        # session id, message id, error
    agent_thinking = Signal(str, str)           # session id, text（工具调用/思考过程）
    # 内置模型代理密钥（服务端下发，见 services/llm_key.py）
    llm_key_updated = Signal(dict)              # {"key", "expires_at"} 或 {"error"}
    # 内置模型的名称 / 备注（服务端看板维护，见 services/llm_models.py）
    llm_models_updated = Signal(dict)           # {"models": [...]} 或 {"error"}
    # 其它
    toast_requested = Signal(str)               # 提示文本
    outline_changed = Signal(str)               # session id


signals = _AppSignals()
