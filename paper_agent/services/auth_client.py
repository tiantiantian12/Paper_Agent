"""账号服务客户端：对接 Paper Agent Server 的注册 / 登录接口。

与项目其他 HTTP 调用保持一致，只用标准库 ``urllib``，不引入 requests。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from paper_agent.core.constants import SERVER_USER_AGENT

# 服务端默认地址（Paper_Agent_Server 的 API 端口）
DEFAULT_SERVER_URL = "http://127.0.0.1:8000"

REQUEST_TIMEOUT = 10.0


class AuthError(RuntimeError):
    """账号服务调用失败（网络不可达、参数错误、验证码错误等）。"""


class AuthClient:
    """注册 / 登录 / 验证码 / 资料查询。"""

    def __init__(self, base_url: str = DEFAULT_SERVER_URL, timeout: float = REQUEST_TIMEOUT) -> None:
        self.base_url = (base_url or DEFAULT_SERVER_URL).rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------------ 接口
    def send_code(self, email: str, purpose: str = "register") -> dict:
        """请求服务端向 QQ 邮箱发送四位验证码。"""
        return self._post(
            "/api/auth/send-code", {"email": email.strip(), "purpose": purpose}
        )

    def register(self, email: str, code: str, nickname: str, password: str) -> dict:
        """验证码注册，返回 ``{"token":..., "user":{...}}``。"""
        return self._post(
            "/api/auth/register",
            {
                "email": email.strip(),
                "code": code.strip(),
                "nickname": nickname.strip(),
                "password": password,
            },
        )

    def login(self, email: str, password: str) -> dict:
        """邮箱 + 密码登录。"""
        return self._post(
            "/api/auth/login", {"email": email.strip(), "password": password}
        )

    def login_by_code(self, email: str, code: str) -> dict:
        """邮箱 + 验证码快捷登录。"""
        return self._post(
            "/api/auth/login-code", {"email": email.strip(), "code": code.strip()}
        )

    def me(self, token: str) -> dict:
        """用令牌换取用户信息。"""
        return self._get("/api/auth/me", token)

    def update_profile(self, token: str, nickname: str | None = None, avatar: str | None = None) -> dict:
        """更新昵称 / 头像，返回最新的用户信息。"""
        payload: dict[str, str] = {}
        if nickname is not None:
            payload["nickname"] = nickname.strip()
        if avatar is not None:
            payload["avatar"] = avatar
        return self._request("PATCH", "/api/auth/profile", payload, token)

    def llm_key(self, token: str) -> dict:
        """用登录令牌换取（首次会自动创建）内置模型的代理密钥。

        返回 ``{"key", "expires_at", "status", ...}``；密钥不对 / 过期由服务端判定。
        """
        return self._get("/api/llm/user-key", token)

    def llm_models(self, token: str) -> list:
        """服务端当前可用的内置模型（已启用且配了密钥的）。"""
        return self._get("/api/llm/models", token)

    def logout(self, token: str) -> None:
        """通知服务端失效令牌；失败不影响本地退出。"""
        try:
            self._post("/api/auth/logout", {}, token)
        except AuthError:
            pass

    # ------------------------------------------------------------------ 底层
    def _request(self, method: str, path: str, payload: dict | None, token: str = "") -> Any:
        data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                # 服务端请求日志靠它认出「这是桌面端」，而不是随便一个脚本
                "User-Agent": SERVER_USER_AGENT,
            },
        )
        if token:
            request.add_header("Authorization", f"Bearer {token}")

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise AuthError(self._detail(exc)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AuthError(f"无法连接账号服务（{self.base_url}）：{exc}") from exc

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise AuthError("服务端返回内容无法解析") from exc

    def _post(self, path: str, payload: dict, token: str = "") -> Any:
        return self._request("POST", path, payload, token)

    def _get(self, path: str, token: str = "") -> Any:
        return self._request("GET", path, None, token)

    @staticmethod
    def _detail(exc: urllib.error.HTTPError) -> str:
        """把 FastAPI 的 ``{"detail": ...}`` 转成中文提示。"""
        try:
            raw = exc.read().decode("utf-8")
            data = json.loads(raw)
        except Exception:  # pragma: no cover - 非 JSON 响应
            return f"请求失败（{exc.code}）"
        detail = data.get("detail") if isinstance(data, dict) else None
        if isinstance(detail, list):  # pydantic 校验错误
            return "；".join(str(item.get("msg", item)) for item in detail)
        if detail:
            return str(detail)
        return f"请求失败（{exc.code}）"
