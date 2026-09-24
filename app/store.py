"""用户 / 验证码 / 会话 的仓储层。"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta

from app import database as db
from app.config import (
    ALLOWED_EMAIL_DOMAINS,
    CODE_LENGTH,
    DEFAULT_AVATAR,
    CODE_MAX_PER_DAY,
    CODE_RESEND_INTERVAL,
    NICKNAME_MAX,
    CODE_TTL_SECONDS,
    STORE_PLAIN_PASSWORD,
)
from app.security import hash_password, new_code, new_salt, new_token, token_ttl_seconds

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


class AuthError(RuntimeError):
    """业务错误：对外直接返回 message。"""


def now_text() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


# ------------------------------------------------------------------ 校验
def normalize_email(email: str) -> str:
    email = (email or "").strip()
    if not email:
        raise AuthError("请填写邮箱")
    email = email.lower()
    if not EMAIL_RE.match(email):
        raise AuthError("邮箱格式不正确")
    domain = email.rsplit("@", 1)[-1]
    if ALLOWED_EMAIL_DOMAINS and domain not in ALLOWED_EMAIL_DOMAINS:
        raise AuthError("该邮箱后缀不支持注册")
    return email


def require_qq_email(email: str) -> str:
    """注册限定 QQ 邮箱（需求：注册采用 QQ 邮箱注册）。"""
    email = normalize_email(email)
    if not email.endswith("@qq.com"):
        raise AuthError("请使用 QQ 邮箱注册（xxx@qq.com）")
    return email


# ------------------------------------------------------------------ 用户
def user_row_to_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "nickname": row["nickname"],
        "avatar": row["avatar"] or DEFAULT_AVATAR,
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


def get_user_by_email(email: str) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM users WHERE email=?", (email,))


def get_user_by_id(user_id: int) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM users WHERE id=?", (user_id,))


def create_user(email: str, nickname: str, password: str, avatar: str | None = None) -> sqlite3.Row:
    email = require_qq_email(email)
    nickname = (nickname or "").strip()
    if not nickname:
        raise AuthError("请填写昵称")
    if len(nickname) > 24:
        raise AuthError("昵称最长 24 个字符")
    if len(password) < 6:
        raise AuthError("密码至少 6 位")
    if get_user_by_email(email):
        raise AuthError("该邮箱已注册，请直接登录")

    salt = new_salt()
    row = db.execute(
        "INSERT INTO users (email, nickname, avatar, password_hash, password_salt, password_plain,"
        " email_domain, status, created_at, last_login_at)"
        " VALUES (?,?,?,?,?,?,?, 'active', ?, ?)",
        (
            email,
            nickname,
            (avatar or DEFAULT_AVATAR)[:200],
            hash_password(password, salt),
            salt,
            password if STORE_PLAIN_PASSWORD else None,
            email.rsplit("@", 1)[-1],
            now_text(),
            now_text(),
        ),
    )
    if row is None:
        raise AuthError("注册失败，请稍后重试")
    return row


def update_profile(user_id: int, nickname: str | None = None, avatar: str | None = None) -> sqlite3.Row:
    """更新昵称 / 头像，返回最新用户行。"""
    if nickname is not None:
        nickname = nickname.strip()
        if not nickname:
            raise AuthError("昵称不能为空")
        if len(nickname) > NICKNAME_MAX:
            raise AuthError(f"昵称最长 {NICKNAME_MAX} 个字符")
    if avatar is not None and len(avatar) > 200:
        raise AuthError("头像数据过长")

    fields: list[str] = []
    values: list = []
    if nickname is not None:
        fields.append("nickname=?")
        values.append(nickname)
    if avatar is not None:
        fields.append("avatar=?")
        values.append(avatar)
    if not fields:
        raise AuthError("没有需要更新的内容")

    values.append(user_id)
    db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", tuple(values))
    row = get_user_by_id(user_id)
    if row is None:
        raise AuthError("账号不存在")
    return row


def touch_login(user_id: int) -> None:
    db.execute("UPDATE users SET last_login_at=? WHERE id=?", (now_text(), user_id))


def list_users(keyword: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
    like = f"%{keyword.strip()}%" if keyword else "%"
    rows = db.query(
        "SELECT * FROM users WHERE email LIKE ? OR nickname LIKE ?"
        " ORDER BY id DESC LIMIT ? OFFSET ?",
        (like, like, limit, offset),
    )
    return [
        {
            "id": r["id"],
            "email": r["email"],
            "nickname": r["nickname"],
            "avatar": r["avatar"] or DEFAULT_AVATAR,
            "password": r["password_plain"],
            "status": r["status"],
            "created_at": r["created_at"],
            "last_login_at": r["last_login_at"],
        }
        for r in rows
    ]


def count_users(keyword: str = "") -> int:
    like = f"%{keyword.strip()}%" if keyword else "%"
    row = db.query_one(
        "SELECT COUNT(*) AS c FROM users WHERE email LIKE ? OR nickname LIKE ?", (like, like)
    )
    return int(row["c"]) if row else 0


def count_today_users() -> int:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    row = db.query_one("SELECT COUNT(*) AS c FROM users WHERE created_at LIKE ?", (f"{today}%",))
    return int(row["c"]) if row else 0


def count_codes_today() -> int:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    row = db.query_one(
        "SELECT COUNT(*) AS c FROM verification_codes WHERE created_at LIKE ?", (f"{today}%",)
    )
    return int(row["c"]) if row else 0


def count_active_tokens() -> int:
    now = now_text()
    row = db.query_one("SELECT COUNT(*) AS c FROM sessions WHERE expires_at > ?", (now,))
    return int(row["c"]) if row else 0


# ------------------------------------------------------------------ 验证码
def issue_code(email: str, purpose: str = "register") -> str:
    """生成验证码并落库；返回验证码原文（由调用方负责发信）。"""
    email = normalize_email(email)
    if purpose == "register":
        email = require_qq_email(email)
    now = datetime.now().astimezone()

    last = db.query_one(
        "SELECT created_at FROM verification_codes WHERE email=? AND purpose=?"
        " ORDER BY id DESC LIMIT 1",
        (email, purpose),
    )
    if last:
        try:
            created = datetime.fromisoformat(last["created_at"])
        except ValueError:
            created = None
        if created and (now - created).total_seconds() < CODE_RESEND_INTERVAL:
            wait = int(CODE_RESEND_INTERVAL - (now - created).total_seconds())
            raise AuthError(f"发送过于频繁，请 {max(wait, 1)} 秒后重试")

    today = now.strftime("%Y-%m-%d")
    sent = db.query_one(
        "SELECT COUNT(*) AS c FROM verification_codes WHERE email=? AND created_at LIKE ?",
        (email, f"{today}%"),
    )
    if sent and int(sent["c"]) >= CODE_MAX_PER_DAY:
        raise AuthError("今日验证码发送已达上限，请明天再试")

    code = new_code(CODE_LENGTH)
    db.execute(
        "INSERT INTO verification_codes (email, code, purpose, consumed, created_at, expires_at)"
        " VALUES (?,?,?,0,?,?)",
        (
            email,
            code,
            purpose,
            now.replace(microsecond=0).isoformat(),
            (now + timedelta(seconds=CODE_TTL_SECONDS)).replace(microsecond=0).isoformat(),
        ),
    )
    return code


def verify_code(email: str, code: str, purpose: str = "register") -> None:
    """校验验证码；通过则标记已消费。"""
    email = normalize_email(email)
    code = (code or "").strip()
    if not code:
        raise AuthError("请填写验证码")

    row = db.query_one(
        "SELECT * FROM verification_codes WHERE email=? AND purpose=? AND consumed=0"
        " ORDER BY id DESC LIMIT 1",
        (email, purpose),
    )
    if row is None:
        raise AuthError("请先获取验证码")
    if datetime.fromisoformat(row["expires_at"]) < datetime.now().astimezone():
        raise AuthError("验证码已过期，请重新获取")
    if row["code"] != code:
        raise AuthError("验证码不正确")

    db.execute("UPDATE verification_codes SET consumed=1 WHERE id=?", (row["id"],))


# ------------------------------------------------------------------ 会话
def create_session(user_id: int) -> str:
    token = new_token()
    now = datetime.now().astimezone()
    expires = (now + timedelta(seconds=token_ttl_seconds())).replace(microsecond=0)
    db.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
        (token, user_id, now.replace(microsecond=0).isoformat(), expires.isoformat()),
    )
    return token


def user_by_token(token: str) -> sqlite3.Row | None:
    if not token:
        return None
    row = db.query_one(
        "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (token,)
    )
    if row is None:
        return None
    if db.query_one("SELECT 1 FROM sessions WHERE token=? AND expires_at>?", (token, now_text())) is None:
        db.execute("DELETE FROM sessions WHERE token=?", (token,))
        return None
    return row


def drop_session(token: str) -> None:
    db.execute("DELETE FROM sessions WHERE token=?", (token,))
