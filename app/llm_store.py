"""大模型代理的仓储层：内置模型、供应商密钥池、用户密钥。

三张表的关系::

    llm_models      model_id → 供应商 OpenAI 兼容地址（内置模型清单）
    provider_keys   model_id → 多个供应商 Key（轮换 + 限流冷却）
    user_keys       user_id  → 代理密钥（桌面端携带，带过期时间）

所有时间统一用本地时区的 ISO 字符串，与 ``store.now_text()`` 保持一致。
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta

from app import database as db
from app.config import (
    LEGACY_LLM_BASE_URLS,
    LLM_MODELS,
    PROVIDER_COOLDOWN_SECONDS,
    USER_KEY_TTL_DAYS,
)

# 模型类型：
#   chat  = 对话（{base_url}/chat/completions）
#   image = 文生图（{base_url}/images/generations）
#   video = 文生视频（{base_url}/videos + /agnesapi 轮询）
KIND_CHAT = "chat"
KIND_IMAGE = "image"
KIND_VIDEO = "video"
MODEL_KINDS = (KIND_CHAT, KIND_IMAGE, KIND_VIDEO)
KIND_LABELS = {KIND_CHAT: "对话", KIND_IMAGE: "文生图", KIND_VIDEO: "文生视频"}


def normalize_kind(value: str | None) -> str:
    """收敛模型类型：认不出来的一律按「对话」处理。"""
    text = (value or "").strip().lower()
    return text if text in MODEL_KINDS else KIND_CHAT


class LlmError(RuntimeError):
    """业务错误：对外直接返回 message。"""


def now_text() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def _used_stamp() -> str:
    """记录「刚用过这把钥匙」的时间戳，**精确到毫秒**。

    池子按「最久未用」挑钥匙，而 ``now_text()`` 只精确到秒 —— 同一秒内连着挑两次，
    两把钥匙的时间戳一样，排序就退化到按 id，永远挑到同一把（等于没轮换）。
    这里用毫秒：ISO 标准格式，看板的时间格式化也能正常解析。
    """
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ------------------------------------------------------------------ 内置模型
def seed_models() -> None:
    """按 ``config.json`` 播种内置模型与供应商密钥（幂等，可反复启动）。

    - 模型：不存在就建；已存在且**还停在旧的内置默认地址**上时跟随 config 纠正
      （看板里手工改过的地址不会被覆盖）
    - 密钥：按 ``api_key`` 去重后补进密钥池，所以配置里放两把就是两把
    """
    for item in LLM_MODELS:
        model_id = str(item["model_id"])
        base_url = str(item.get("base_url") or "")
        row = get_model(model_id)
        desc = str(item.get("desc") or "")
        try:
            if row is None:
                add_model(
                    model_id,
                    str(item.get("name") or model_id),
                    base_url,
                    desc,
                    str(item.get("kind") or KIND_CHAT),
                )
            else:
                if base_url and row["base_url"] in LEGACY_LLM_BASE_URLS and row["base_url"] != base_url:
                    set_model(model_id, base_url=base_url)
                # 备注还空着（老库刚补的列 / 从没填过）就用 config 里的补上；
                # 看板里填过的一律不动
                if desc and not _desc_of(row):
                    set_model(model_id, desc=desc)
        except LlmError as exc:
            # 配置写错了不能拖垮整个服务：跳过这一个，日志里说清楚
            print(f"[llm] 跳过模型 {model_id}：{exc}", flush=True)
            continue
        for api_key in item.get("api_keys") or []:
            if not provider_key_exists(model_id, api_key):
                add_provider_key(model_id, api_key, "内置密钥")


def provider_key_exists(model_id: str, api_key: str) -> bool:
    """密钥池里是否已有这把 Key（用于播种时去重）。"""
    return (
        db.query_one(
            "SELECT 1 FROM provider_keys WHERE model_id=? AND api_key=?",
            (model_id, (api_key or "").strip()),
        )
        is not None
    )


def list_models() -> list[dict]:
    rows = db.query("SELECT * FROM llm_models ORDER BY id")
    result = []
    for row in rows:
        result.append(
            {
                "id": row["id"],
                "model_id": row["model_id"],
                "name": row["name"],
                "desc": _desc_of(row),
                "kind": _kind_of(row),
                "base_url": row["base_url"],
                "enabled": bool(row["enabled"]),
                "created_at": row["created_at"],
                "key_count": count_provider_keys(row["model_id"], only_active=True),
            }
        )
    return result


def _desc_of(row: sqlite3.Row) -> str:
    """备注列可能不存在（极老的库还没迁移过），取不到就当空。"""
    try:
        return row["desc"] or ""
    except (IndexError, KeyError):
        return ""


def _kind_of(row: sqlite3.Row) -> str:
    try:
        return normalize_kind(row["kind"])
    except (IndexError, KeyError):
        return KIND_CHAT


def get_model(model_id: str) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM llm_models WHERE model_id=?", (model_id,))


def add_model(model_id: str, name: str, base_url: str, desc: str = "",
              kind: str = KIND_CHAT) -> dict:
    model_id = (model_id or "").strip()
    name = (name or "").strip()
    base_url = (base_url or "").strip().rstrip("/")
    if not model_id:
        raise LlmError("请填写模型 ID")
    if not base_url.startswith(("http://", "https://")):
        raise LlmError("Base URL 需要以 http:// 或 https:// 开头")
    if get_model(model_id):
        raise LlmError("该模型 ID 已存在")
    db.execute(
        "INSERT INTO llm_models (model_id, name, desc, kind, base_url, enabled, created_at)"
        " VALUES (?,?,?,?,?,1,?)",
        (model_id, name or model_id, (desc or "").strip(), normalize_kind(kind), base_url,
         now_text()),
    )
    row = get_model(model_id)
    return {
        "model_id": row["model_id"],
        "name": row["name"],
        "desc": _desc_of(row),
        "kind": _kind_of(row),
        "base_url": row["base_url"],
    }


def set_model(model_id: str, name: str | None = None, base_url: str | None = None,
              enabled: bool | None = None, desc: str | None = None,
              kind: str | None = None) -> None:
    if get_model(model_id) is None:
        raise LlmError("模型不存在")
    fields: list[str] = []
    values: list = []
    if name is not None:
        fields.append("name=?")
        values.append(name.strip() or model_id)
    if desc is not None:
        fields.append("desc=?")
        values.append(desc.strip()[:200])
    if kind is not None:
        fields.append("kind=?")
        values.append(normalize_kind(kind))
    if base_url is not None:
        url = base_url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise LlmError("Base URL 需要以 http:// 或 https:// 开头")
        fields.append("base_url=?")
        values.append(url)
    if enabled is not None:
        fields.append("enabled=?")
        values.append(1 if enabled else 0)
    if not fields:
        raise LlmError("没有需要更新的内容")
    values.append(model_id)
    db.execute(f"UPDATE llm_models SET {', '.join(fields)} WHERE model_id=?", tuple(values))


def delete_model(model_id: str) -> None:
    db.execute("DELETE FROM llm_models WHERE model_id=?", (model_id,))
    db.execute("DELETE FROM provider_keys WHERE model_id=?", (model_id,))


# ------------------------------------------------------------------ 供应商密钥
def count_provider_keys(model_id: str, only_active: bool = False) -> int:
    sql = "SELECT COUNT(*) AS c FROM provider_keys WHERE model_id=?"
    if only_active:
        sql += " AND status='active'"
    row = db.query_one(sql, (model_id,))
    return int(row["c"]) if row else 0


def list_provider_keys(model_id: str = "") -> list[dict]:
    if model_id:
        rows = db.query(
            "SELECT * FROM provider_keys WHERE model_id=? ORDER BY id", (model_id,)
        )
    else:
        rows = db.query("SELECT * FROM provider_keys ORDER BY model_id, id")
    return [_provider_key_out(row) for row in rows]


def _provider_key_out(row: sqlite3.Row) -> dict:
    cooling = False
    until = _parse(row["cooldown_until"])
    if until and until > _now():
        cooling = True
    return {
        "id": row["id"],
        "model_id": row["model_id"],
        "api_key": mask_key(row["api_key"]),
        "label": row["label"] or "",
        "status": row["status"],
        "cooling": cooling,
        "last_error": row["last_error"] or "",
        "last_used_at": row["last_used_at"],
        "created_at": row["created_at"],
    }


def mask_key(value: str) -> str:
    """密钥只回显首尾，中间打码。"""
    text = value or ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}{'*' * 6}{text[-4:]}"


def add_provider_key(model_id: str, api_key: str, label: str = "") -> dict:
    api_key = (api_key or "").strip()
    if not api_key:
        raise LlmError("请填写 API Key")
    if get_model(model_id) is None:
        raise LlmError("请先添加该模型")
    db.execute(
        "INSERT INTO provider_keys (model_id, api_key, label, status, created_at)"
        " VALUES (?,?,?,'active',?)",
        (model_id, api_key, (label or "").strip(), now_text()),
    )
    rows = list_provider_keys(model_id)
    return rows[-1] if rows else {}


def provider_key_secret(key_id: int) -> str:
    """取一把供应商密钥的明文。

    列表接口只回打码值（防着有人路过瞄一眼就把所有 Key 抄走），看板点「复制」
    时才走这里按 id 单取。
    """
    row = db.query_one("SELECT api_key FROM provider_keys WHERE id=?", (key_id,))
    if row is None:
        raise LlmError("密钥不存在")
    return row["api_key"] or ""


def delete_provider_key(key_id: int) -> None:
    db.execute("DELETE FROM provider_keys WHERE id=?", (key_id,))


def set_provider_key_status(key_id: int, status: str) -> None:
    if status not in ("active", "disabled"):
        raise LlmError("状态只能是 active / disabled")
    db.execute("UPDATE provider_keys SET status=? WHERE id=?", (status, key_id))


def pick_provider_key(model_id: str) -> sqlite3.Row | None:
    """取一个可用的供应商密钥：未禁用、不在冷却中，按「最久未用」优先。"""
    now = now_text()
    return db.query_one(
        "SELECT * FROM provider_keys WHERE model_id=? AND status='active'"
        " AND (cooldown_until IS NULL OR cooldown_until < ?)"
        " ORDER BY COALESCE(last_used_at, '') ASC, id ASC LIMIT 1",
        (model_id, now),
    )


def mark_provider_key_used(key_id: int) -> None:
    db.execute("UPDATE provider_keys SET last_used_at=?, last_error=NULL WHERE id=?",
               (_used_stamp(), key_id))


def mark_provider_key_error(key_id: int, message: str, cooldown: bool = True) -> None:
    """记录失败；命中限流 / 服务异常时把该 Key 放进冷却，下一轮自动换一个。"""
    until = _iso(_now() + timedelta(seconds=PROVIDER_COOLDOWN_SECONDS)) if cooldown else None
    db.execute(
        "UPDATE provider_keys SET last_error=?, cooldown_until=? WHERE id=?",
        ((message or "")[:300], until, key_id),
    )


# ------------------------------------------------------------------ 用户密钥
def new_user_key() -> str:
    return "pk-" + secrets.token_urlsafe(24)


def default_expiry(days: int | None = None) -> str | None:
    """默认过期时间；days <= 0 表示永不过期。"""
    days = USER_KEY_TTL_DAYS if days is None else days
    if days is None or days <= 0:
        return None
    return _iso(_now() + timedelta(days=days))


def create_user_key(user_id: int, label: str = "", days: int | None = None,
                    allowed_models: str = "") -> sqlite3.Row:
    row = db.insert(
        "INSERT INTO user_keys (user_id, key_value, label, status, allowed_models,"
        " expires_at, created_at) VALUES (?,?,?,'active',?,?,?)",
        (
            user_id,
            new_user_key(),
            (label or "").strip(),
            (allowed_models or "").strip(),
            default_expiry(days),
            now_text(),
        ),
        table="user_keys",
    )
    if row is None:
        raise LlmError("创建用户密钥失败")
    return row


def ensure_user_key(user_id: int, label: str = "默认密钥") -> sqlite3.Row:
    """保证账号有一把可用密钥，没有就新签一把（默认有效期 30 天）。

    注册 / 首次登录时调用，这样每个新账号在服务端都能直接看到自己的密钥。
    """
    row = db.query_one(
        "SELECT * FROM user_keys WHERE user_id=? AND status='active' ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    if row is not None:
        expires = _parse(row["expires_at"])
        if expires is None or expires > _now():
            return row
    return create_user_key(user_id, label)


def list_user_keys(user_id: int | None = None) -> list[dict]:
    if user_id is None:
        rows = db.query(
            "SELECT k.*, u.nickname, u.email FROM user_keys k"
            " LEFT JOIN users u ON u.id=k.user_id ORDER BY k.id DESC"
        )
    else:
        rows = db.query(
            "SELECT k.*, u.nickname, u.email FROM user_keys k"
            " LEFT JOIN users u ON u.id=k.user_id WHERE k.user_id=? ORDER BY k.id DESC",
            (user_id,),
        )
    return [_user_key_out(row) for row in rows]


def _user_key_out(row: sqlite3.Row) -> dict:
    expires = _parse(row["expires_at"])
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "nickname": row["nickname"] if "nickname" in row.keys() else "",
        "email": row["email"] if "email" in row.keys() else "",
        "key": row["key_value"],
        "label": row["label"] or "",
        "status": row["status"],
        "allowed_models": row["allowed_models"] or "",
        "expires_at": row["expires_at"],
        "expired": bool(expires and expires < _now()),
        "last_used_at": row["last_used_at"],
        "created_at": row["created_at"],
    }


def user_key_by_value(value: str) -> sqlite3.Row | None:
    if not value:
        return None
    return db.query_one("SELECT * FROM user_keys WHERE key_value=?", (value.strip(),))


def resolve_user_key(value: str) -> sqlite3.Row:
    """校验代理密钥：存在、未禁用、未过期。"""
    row = user_key_by_value(value)
    if row is None:
        raise LlmError("代理密钥无效")
    if row["status"] != "active":
        raise LlmError("代理密钥已被停用")
    expires = _parse(row["expires_at"])
    if expires and expires < _now():
        raise LlmError("代理密钥已过期，请在服务端续期")
    return row


def touch_user_key(key_id: int) -> None:
    db.execute("UPDATE user_keys SET last_used_at=? WHERE id=?", (now_text(), key_id))


def set_user_key(key_id: int, status: str | None = None, label: str | None = None,
                 expires_at: str | None = None) -> None:
    fields: list[str] = []
    values: list = []
    if status is not None:
        if status not in ("active", "disabled"):
            raise LlmError("状态只能是 active / disabled")
        fields.append("status=?")
        values.append(status)
    if label is not None:
        fields.append("label=?")
        values.append(label.strip())
    if expires_at is not None:
        # 传空字符串表示永不过期
        fields.append("expires_at=?")
        values.append(expires_at.strip() or None)
    if not fields:
        raise LlmError("没有需要更新的内容")
    values.append(key_id)
    db.execute(f"UPDATE user_keys SET {', '.join(fields)} WHERE id=?", tuple(values))


def delete_user_key(key_id: int) -> None:
    db.execute("DELETE FROM user_keys WHERE id=?", (key_id,))


def model_allowed(row: sqlite3.Row, model_id: str) -> bool:
    """用户密钥是否允许访问该模型（allowed_models 为空表示不限制）。"""
    allowed = (row["allowed_models"] or "").strip()
    if not allowed:
        return True
    return model_id in [item.strip() for item in allowed.split(",") if item.strip()]


def count_user_keys() -> int:
    row = db.query_one("SELECT COUNT(*) AS c FROM user_keys")
    return int(row["c"]) if row else 0


def count_active_user_keys() -> int:
    now = now_text()
    row = db.query_one(
        "SELECT COUNT(*) AS c FROM user_keys WHERE status='active'"
        " AND (expires_at IS NULL OR expires_at > ?)",
        (now,),
    )
    return int(row["c"]) if row else 0
