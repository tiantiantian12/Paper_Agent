"""注册 / 登录 / 验证码接口（桌面端调用）。"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request

from app import llm_store, request_logs, store
from app.schemas import (
    AuthResult,
    LoginByCodeRequest,
    LoginRequest,
    ProfileUpdate,
    RegisterRequest,
    SendCodeRequest,
    UserOut,
)
from app.security import verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _bad(exc: Exception, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


def _issue_llm_key(user_id: int) -> None:
    """给账号签一把内置模型的代理密钥（默认有效期 30 天）。

    签发失败不影响注册 / 登录：桌面端首取时会再补一次，看板里也能手工生成。
    """
    try:
        llm_store.ensure_user_key(user_id)
    except Exception as exc:      # noqa: BLE001 - 密钥只是附加能力，不能让登录挂掉
        print(f"[llm] 为用户 {user_id} 签发代理密钥失败：{exc}", flush=True)


def current_user(token: str | None):
    """按 Bearer Token 取当前用户；无效返回 401。"""
    if not token:
        raise HTTPException(status_code=401, detail="缺少登录凭证")
    user = store.user_by_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    return user


@router.post("/send-code")
def send_code(payload: SendCodeRequest, request: Request) -> dict:
    """发送邮箱验证码（注册默认限定 QQ 邮箱）。"""
    request_logs.note(request, f"发送验证码 · {payload.email}（{payload.purpose}）")
    try:
        email = (
            store.require_qq_email(payload.email)
            if payload.purpose == "register"
            else store.normalize_email(payload.email)
        )
        existing = store.get_user_by_email(email)
        if payload.purpose == "register" and existing:
            raise store.AuthError("该邮箱已注册，请直接登录")
        if payload.purpose == "reset" and not existing:
            raise store.AuthError("该邮箱尚未注册")
        code = store.issue_code(email, payload.purpose)
    except store.AuthError as exc:
        raise _bad(exc) from exc

    from app.config import SMTP_DRY_RUN
    from app.smtp_service import MailError, send_code_email

    if SMTP_DRY_RUN:
        # 本地联调：跳过真实发信，验证码打印在服务端日志里
        print(f"[smtp-dry-run] {email} 验证码 {code}（{payload.purpose}）", flush=True)
    else:
        try:
            send_code_email(
                email,
                code,
                payload.purpose,
                existing["nickname"] if existing else "",
            )
        except MailError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"ok": True, "email": email, "expires_in": store.CODE_TTL_SECONDS}


@router.post("/register", response_model=AuthResult)
def register(payload: RegisterRequest, request: Request) -> dict:
    """邮箱验证码注册。"""
    request_logs.note(request, f"注册 · {payload.email} / {payload.nickname}")
    try:
        store.require_qq_email(payload.email)
        store.verify_code(payload.email, payload.code, "register")
        user = store.create_user(payload.email, payload.nickname, payload.password)
    except store.AuthError as exc:
        raise _bad(exc) from exc

    _issue_llm_key(user["id"])
    token = store.create_session(user["id"])
    request_logs.identify(request, user)
    return {"token": token, "user": store.user_row_to_out(user)}


@router.post("/login", response_model=AuthResult)
def login(payload: LoginRequest, request: Request) -> dict:
    """邮箱 + 密码登录。"""
    request_logs.note(request, f"密码登录 · {payload.email}")
    try:
        email = store.normalize_email(payload.email)
    except store.AuthError as exc:
        raise _bad(exc) from exc

    user = store.get_user_by_email(email)
    if user is None or not verify_password(
        payload.password, user["password_salt"], user["password_hash"]
    ):
        raise HTTPException(status_code=401, detail="邮箱或密码不正确")
    if user["status"] != "active":
        raise HTTPException(status_code=403, detail="账号已被禁用")

    store.touch_login(user["id"])
    token = store.create_session(user["id"])
    request_logs.identify(request, user)
    return {"token": token, "user": store.user_row_to_out(user)}


@router.post("/login-code", response_model=AuthResult)
def login_by_code(payload: LoginByCodeRequest, request: Request) -> dict:
    """邮箱 + 验证码快捷登录（未注册则自动注册，昵称为邮箱前缀）。"""
    request_logs.note(request, f"验证码登录 · {payload.email}")
    try:
        email = store.normalize_email(payload.email)
        store.verify_code(payload.email, payload.code, "register")
    except store.AuthError as exc:
        raise _bad(exc) from exc

    user = store.get_user_by_email(email)
    if user is None:
        try:
            user = store.create_user(email, email.split("@")[0], email.split("@")[0] + "#" + email[:4])
        except store.AuthError as exc:
            raise _bad(exc) from exc
    store.touch_login(user["id"])
    _issue_llm_key(user["id"])
    token = store.create_session(user["id"])
    request_logs.identify(request, user)
    return {"token": token, "user": store.user_row_to_out(user)}


@router.patch("/profile", response_model=UserOut)
def update_profile(
    payload: ProfileUpdate,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    """更新昵称 / 头像。"""
    request_logs.note(request, "更新昵称 / 头像")
    token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    user = current_user(token)
    try:
        row = store.update_profile(user["id"], payload.nickname, payload.avatar)
    except store.AuthError as exc:
        raise _bad(exc) from exc
    return store.user_row_to_out(row)


@router.get("/me", response_model=UserOut)
def me(request: Request, authorization: str | None = Header(default=None)) -> dict:
    """用 Token 换取当前用户信息。"""
    request_logs.note(request, "取当前用户信息")
    token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    return store.user_row_to_out(current_user(token))


@router.post("/logout")
def logout(request: Request, authorization: str | None = Header(default=None)) -> dict:
    request_logs.note(request, "退出登录")
    token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    if token:
        store.drop_session(token)
    return {"ok": True}
