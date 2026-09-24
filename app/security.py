"""密码哈希与令牌工具（仅用标准库）。"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from app.config import TOKEN_TTL_DAYS

PBKDF2_ROUNDS = 120_000


def new_salt() -> str:
    return secrets.token_hex(16)


def hash_password(password: str, salt: str) -> str:
    """PBKDF2-HMAC-SHA256 派生密码摘要（十六进制）。"""
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    )
    return digest.hex()


def verify_password(password: str, salt: str, expected: str) -> bool:
    """恒定时间比较，避免时序侧信道。"""
    actual = hash_password(password, salt)
    return hmac.compare_digest(actual, expected or "")


def new_token() -> str:
    """生成登录令牌（URL 安全的随机串）。"""
    return secrets.token_urlsafe(32)


def new_code(length: int) -> str:
    """生成纯数字验证码。"""
    return "".join(secrets.choice("0123456789") for _ in range(length))


def token_ttl_seconds() -> int:
    return TOKEN_TTL_DAYS * 24 * 3600
