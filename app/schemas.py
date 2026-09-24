"""请求 / 响应数据结构。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Purpose = Literal["register", "reset"]


class SendCodeRequest(BaseModel):
    email: str = Field(..., description="QQ 邮箱地址")
    purpose: Purpose = "register"


class RegisterRequest(BaseModel):
    email: str
    code: str = Field(..., min_length=4, max_length=8, description="邮箱收到的验证码")
    nickname: str = Field(..., min_length=1, max_length=24)
    password: str = Field(..., min_length=6, max_length=64)


class LoginRequest(BaseModel):
    email: str
    password: str = Field(..., min_length=1, max_length=64)


class LoginByCodeRequest(BaseModel):
    email: str
    code: str = Field(..., min_length=4, max_length=8)


class UserOut(BaseModel):
    id: int
    email: str
    nickname: str
    avatar: str | None = None
    created_at: str
    last_login_at: str | None = None


class ProfileUpdate(BaseModel):
    nickname: str | None = Field(None, min_length=1, max_length=24)
    avatar: str | None = Field(None, max_length=200)


class AuthResult(BaseModel):
    token: str
    user: UserOut


class AdminUserOut(BaseModel):
    """管理端看板条目：包含账号密码（明文副本，受开关控制）。"""

    id: int
    email: str
    nickname: str
    avatar: str | None = None
    password: str | None = None
    status: str
    created_at: str
    last_login_at: str | None = None


class StatsOut(BaseModel):
    users: int
    today_users: int
    codes_today: int
    active_tokens: int


# ------------------------------------------------------------------ 大模型代理
class UserKeyOut(BaseModel):
    """桌面端拿到的代理密钥。"""

    key: str
    label: str = ""
    status: str = "active"
    expires_at: str | None = None
    expired: bool = False
    allowed_models: str = ""
    default_ttl_days: int = 30
    proxy_base_url: str = "/api/llm/v1"


class ModelIn(BaseModel):
    model_id: str = Field(..., min_length=1, max_length=120)
    name: str = ""
    desc: str = Field("", max_length=200, description="备注，桌面端显示在模型名下方")
    kind: Literal["chat", "image", "video"] = Field(
        "chat", description="chat=对话，image=文生图，video=文生视频"
    )
    base_url: str = Field(..., min_length=8, max_length=300)


class ModelPatch(BaseModel):
    name: str | None = None
    desc: str | None = None
    kind: Literal["chat", "image", "video"] | None = None
    base_url: str | None = None
    enabled: bool | None = None


class BuiltinModelOut(BaseModel):
    """下发给桌面端的内置模型：只给显示与分流需要的字段，不含供应商地址。"""

    model_id: str
    name: str = ""
    desc: str = ""
    kind: Literal["chat", "image", "video"] = "chat"


class ProviderKeyIn(BaseModel):
    model_id: str = Field(..., min_length=1, max_length=120)
    api_key: str = Field(..., min_length=1, max_length=400)
    label: str = ""


class UserKeyIn(BaseModel):
    user_id: int
    label: str = ""
    days: int | None = None
    allowed_models: str = ""


class UserKeyPatch(BaseModel):
    label: str | None = None
    status: str | None = None
    expires_at: str | None = None


class StatusPatch(BaseModel):
    status: str = "active"
