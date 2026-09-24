"""请求日志：记录「谁、什么时候、打了哪个接口、结果如何」。

目的很实在：服务被刷 / 被打的时候，要能查出**是谁**在打。每条记录包含：

============ ==========================================================
``created_at`` 时间（本地时区，精确到秒）
``ip``       来源 IP（按 ``log.trust_proxy`` 决定取直连地址还是 X-Forwarded-For）
``nickname`` / ``email`` / ``user_id``  识别出的账号（带对凭证的请求才有）
``credential`` 打码后的凭证 —— 哪把用户密钥在打（明文概不入库）
``detail``   这条请求在干什么（哪个模型、什么参数、哪个邮箱）
``status`` / ``duration_ms`` / ``path`` / ``method`` / ``user_agent``
============ ==========================================================

两个设计要点：

1. **不拖慢请求**：中间件只把一条元组塞进内存队列，落库由后台线程**批量**完成。
   被刷的时候写日志不会变成瓶颈；队列满了丢最旧的并计数（看板里能看到「丢了 N 条」）。
2. **真实来源**：``ip`` 默认取 TCP 对端地址（伪造不了）；前面挂了 Nginx / Cloudflare
   时把 ``log.trust_proxy`` 打开，改取 ``X-Forwarded-For`` 第一跳，原始头另存一份。
"""

from __future__ import annotations

import csv
import io
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Callable

from app import database as db
from app.config import (
    ADMIN_ACCESS_TOKEN,
    LOG_ENABLED,
    LOG_MAX_ROWS,
    LOG_RETENTION_DAYS,
    LOG_SKIP_PREFIXES,
    LOG_TRUST_PROXY,
    log_skip_paths,
)

# ------------------------------------------------------------------ 队列 / 刷盘
QUEUE_LIMIT = 20000              # 内存里最多压多少条（再多就丢最旧的）
FLUSH_INTERVAL = 2.0             # 后台线程刷盘间隔（秒）
PRUNE_INTERVAL = 600.0           # 清理超期记录的间隔（秒）

_QUEUE: deque[tuple] = deque()
_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_DROPPED = 0                     # 队列打满后丢掉的条数（被打时这个数会涨）
_LAST_PRUNE = 0.0

_COLUMNS = (
    "created_at, service, ip, peer_ip, forwarded, method, path, model, status, duration_ms,"
    " user_id, nickname, email, credential, client, detail, user_agent"
)
_PLACEHOLDERS = ",".join("?" * len(_COLUMNS.split(",")))


def _now_text() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


# ------------------------------------------------------------------ 身份识别
# 凭证 → 身份，缓存一小会儿：被刷的时候每个请求都查库会放大负担
_IDENTITY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_IDENTITY_TTL = 60.0
_IDENTITY_CACHE_LIMIT = 2048


def _mask(value: str) -> str:
    """凭证只留一小截用于对账，绝不整串入库。"""
    value = (value or "").strip()
    if not value:
        return ""
    kind = "key" if value.startswith("pk-") else "token"
    return f"{kind} {value[:10]}…"


def _identify(credential: str) -> dict[str, Any]:
    """按凭证找出账号；查不到（没带 / 无效 / 过期）返回空身份。"""
    if not credential:
        return {"credential": ""}
    hit = _IDENTITY_CACHE.get(credential)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1]

    info: dict[str, Any] = {"credential": _mask(credential)}
    try:
        user = None
        if credential.startswith("pk-"):
            row = db.query_one("SELECT user_id FROM user_keys WHERE key_value=?", (credential,))
            if row is not None:
                user = _user_lite(row["user_id"])
        else:
            sessions = db.query_one(
                "SELECT user_id FROM sessions WHERE token=? AND expires_at>?",
                (credential, _now_text()),
            )
            if sessions is not None:
                user = _user_lite(sessions["user_id"])
        if user is not None:
            info.update(
                user_id=user["id"], nickname=user["nickname"], email=user["email"]
            )
    except Exception:      # noqa: BLE001 - 识别失败不能影响请求本身
        pass

    with _LOCK:
        if len(_IDENTITY_CACHE) >= _IDENTITY_CACHE_LIMIT:
            _IDENTITY_CACHE.clear()
        _IDENTITY_CACHE[credential] = (now + _IDENTITY_TTL, info)
    return info


def _user_lite(user_id: int) -> dict[str, Any] | None:
    row = db.query_one("SELECT id, nickname, email FROM users WHERE id=?", (user_id,))
    if row is None:
        return None
    return {"id": row["id"], "nickname": row["nickname"], "email": row["email"]}


def _client_kind(user_agent: str) -> str:
    """按 UA 粗分来源：桌面端 / 浏览器 / 脚本。"""
    ua = (user_agent or "").lower()
    if not ua:
        return "未知"
    if "paperagent" in ua:
        return "桌面端"
    if any(bot in ua for bot in ("curl", "wget", "python", "go-http", "java", "okhttp", "axios")):
        return "脚本"
    if any(browser in ua for browser in ("mozilla", "chrome", "safari", "firefox", "edge")):
        return "浏览器"
    return "其他"


# ------------------------------------------------------------------ 中间件
def _header(scope_headers: list, name: str) -> str:
    """从 ASGI 原始头里取一个值（键是小写字节串）。"""
    needle = name.encode("latin-1")
    for key, value in scope_headers:
        if key == needle:
            try:
                return value.decode("latin-1")
            except UnicodeDecodeError:      # pragma: no cover - 非 latin-1 头
                return ""
    return ""


def _skip(path: str, method: str) -> bool:
    if path in log_skip_paths():
        return True
    if path == "/favicon.ico":
        return True
    if any(path.startswith(prefix) for prefix in LOG_SKIP_PREFIXES):
        return True
    # 看板自己在轮询日志列表，记进去只会把自己刷屏（改动日志的操作照记）
    if method == "GET" and path.rstrip("/") in ("/api/admin/logs", "/api/admin/logs/stats"):
        return True
    return False


def _source_ip(scope: dict, headers: list) -> tuple[str, str, str]:
    """返回 ``(归因 IP, 直连 IP, 原始 X-Forwarded-For)``。"""
    client = scope.get("client") or ("", 0)
    peer = str(client[0] or "")
    forwarded = _header(headers, "x-forwarded-for") or _header(headers, "x-real-ip")
    forwarded = forwarded.strip()[:200]
    if LOG_TRUST_PROXY and forwarded:
        return forwarded.split(",")[0].strip(), peer, forwarded
    return peer, peer, forwarded


def _credential_of(headers: list) -> str:
    user_key = _header(headers, "x-user-key").strip()
    if user_key:
        return user_key
    authorization = _header(headers, "authorization").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return authorization


def note(request: Any, text: str, model: str = "") -> None:
    """给当前请求补一句「在干什么」，会写进日志的 ``detail`` / ``model`` 列。

    在路由里调用（那里已经解析过请求体，顺手记下模型 / 参数最准确）。
    ``model`` 单独存一列：看板要按模型回答「**是哪个模型被限流了**」——
    光靠 detail 里的自由文本没法聚合。
    """
    try:
        request.state.log_detail = (text or "")[:300]
        if model:
            request.state.log_model = (model or "")[:80]
    except Exception:      # noqa: BLE001 - 纯附加信息，失败不影响请求
        pass


# ------------------------------------------------------------------ 看板自己
# 看板请求带的是「管理口令」（X-Admin-Token），不是账号令牌，也不是用户代理密钥 ——
# 所以按账号那套识别它必然是「未识别」。这里单独认一下，免得自己人看起来像攻击者；
# 口令填错的访问也照记，那本身就是要紧的信号。
ADMIN_PATH_PREFIX = "/api/admin"
ADMIN_NAME = "看板管理员"


def _admin_identity(headers: list) -> dict[str, Any]:
    """看板请求的身份：管理口令持有人说清是「谁在操作看板」。"""
    if not ADMIN_ACCESS_TOKEN:
        return {"nickname": ADMIN_NAME, "credential": "未设口令"}
    token = _header(headers, "x-admin-token").strip()
    if token == ADMIN_ACCESS_TOKEN:
        return {"nickname": ADMIN_NAME, "credential": "口令已验证"}
    if not token:
        return {"nickname": ADMIN_NAME, "credential": "未带口令"}
    # 口令不对：可能是有人试口令，这条要显眼
    return {"nickname": "口令错误的访问", "credential": "口令不正确"}


def identify(request: Any, user: Any) -> None:
    """路由里认出账号后回填身份。

    登录 / 注册这类请求本身还没带可用凭证（凭证是刚刚才签发的），只有回填一下
    日志里才看得出「这次登录是谁」。
    """
    try:
        request.state.log_user_id = user["id"]
        request.state.log_nickname = user["nickname"]
        request.state.log_email = user["email"]
    except Exception:      # noqa: BLE001
        pass


def record(
    *,
    service: str,
    ip: str,
    peer_ip: str,
    forwarded: str,
    method: str,
    path: str,
    status: int,
    duration_ms: int,
    credential: str,
    identity: dict[str, Any],
    detail: str,
    user_agent: str,
    model: str = "",
) -> None:
    entry = (
        _now_text(),
        service,
        ip[:60],
        peer_ip[:60],
        forwarded,
        method[:10],
        path[:300],
        (model or "")[:80],
        int(status),
        int(duration_ms),
        identity.get("user_id"),
        str(identity.get("nickname") or "")[:60],
        str(identity.get("email") or "")[:120],
        str(identity.get("credential") or "")[:60],
        _client_kind(user_agent),
        (detail or "")[:300],
        user_agent[:300],
    )
    global _DROPPED
    with _LOCK:
        if len(_QUEUE) >= QUEUE_LIMIT:
            _DROPPED += 1
            return
        _QUEUE.append(entry)
    _ensure_thread()


class RequestLogMiddleware:
    """纯 ASGI 中间件（不用 BaseHTTPMiddleware，避免影响流式响应）。"""

    def __init__(self, app: Callable, service: str = "api") -> None:
        self.app = app
        self.service = service

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http" or not LOG_ENABLED:
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        method = str(scope.get("method") or "GET")
        if _skip(path, method):
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        state = scope.setdefault("state", {})
        started = time.perf_counter()
        status = 0

        async def send_wrapper(message: dict) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message.get("status") or 0)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            status = status or 500
            raise
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            try:
                if path.startswith(ADMIN_PATH_PREFIX):
                    # 看板请求认的是管理口令，不套账号那套（否则全成了「未识别」）
                    identity = _admin_identity(headers)
                    credential = ""
                else:
                    credential = _credential_of(headers)
                    identity = _identify(credential)
                # 路由里若已识别出账号（例如刚登录成功），以它为准
                for field in ("user_id", "nickname", "email"):
                    value = state.get(f"log_{field}")
                    if value:
                        identity[field] = value
                ip, peer, forwarded = _source_ip(scope, headers)
                record(
                    service=self.service,
                    ip=ip,
                    peer_ip=peer,
                    forwarded=forwarded,
                    method=method,
                    path=path,
                    status=status or 500,
                    duration_ms=duration_ms,
                    credential=credential,
                    identity=identity,
                    detail=str(state.get("log_detail") or ""),
                    user_agent=_header(headers, "user-agent"),
                    model=str(state.get("log_model") or ""),
                )
            except Exception as exc:      # noqa: BLE001 - 记日志绝不能影响请求
                print(f"[log] 记录请求日志失败：{exc}", flush=True)


# ------------------------------------------------------------------ 落库
def _ensure_thread() -> None:
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _THREAD = threading.Thread(target=_loop, name="request-log", daemon=True)
        _THREAD.start()


def _loop() -> None:
    while True:
        time.sleep(FLUSH_INTERVAL)
        try:
            flush()
            _maybe_prune()
        except Exception as exc:      # noqa: BLE001 - 刷盘失败不能让线程退出
            print(f"[log] 请求日志刷盘失败：{exc}", flush=True)


def flush() -> int:
    """把队列里的记录批量写库，返回写入条数。"""
    with _LOCK:
        if not _QUEUE:
            return 0
        batch = list(_QUEUE)
        _QUEUE.clear()
    with db.transaction() as conn:
        conn.executemany(
            f"INSERT INTO request_logs ({_COLUMNS}) VALUES ({_PLACEHOLDERS})", batch
        )
    return len(batch)


def pending() -> int:
    with _LOCK:
        return len(_QUEUE)


def dropped() -> int:
    with _LOCK:
        return _DROPPED


def _maybe_prune() -> None:
    global _LAST_PRUNE
    now = time.monotonic()
    if now - _LAST_PRUNE < PRUNE_INTERVAL:
        return
    _LAST_PRUNE = now
    prune()


def prune() -> int:
    """按 ``log.retention_days`` / ``log.max_rows`` 清掉过期与超量的记录。"""
    removed = 0
    with db.transaction() as conn:
        if LOG_RETENTION_DAYS > 0:
            cutoff = (
                datetime.now().astimezone() - timedelta(days=LOG_RETENTION_DAYS)
            ).replace(microsecond=0).isoformat()
            removed += conn.execute(
                "DELETE FROM request_logs WHERE created_at < ?", (cutoff,)
            ).rowcount
        if LOG_MAX_ROWS > 0:
            row = conn.execute("SELECT COUNT(*) AS c FROM request_logs").fetchone()
            excess = int(row["c"] if row else 0) - LOG_MAX_ROWS
            if excess > 0:
                removed += conn.execute(
                    "DELETE FROM request_logs WHERE id IN"
                    " (SELECT id FROM request_logs ORDER BY id LIMIT ?)",
                    (excess,),
                ).rowcount
    return removed


def start() -> None:
    """服务启动时调一次：先清一次超期记录，并拉起刷盘线程。"""
    _maybe_prune()
    _ensure_thread()


def stop() -> None:
    """服务退出时调一次：把队列里剩下的写完。"""
    for _ in range(3):
        if flush() == 0:
            break


# ------------------------------------------------------------------ 查询
def _window(hours: float) -> str:
    """时间窗起点（ISO 文本）；``hours<=0`` 表示不限时间。"""
    if not hours or hours <= 0:
        return ""
    return (
        datetime.now().astimezone() - timedelta(hours=float(hours))
    ).replace(microsecond=0).isoformat()


def _filters(
    *,
    q: str = "",
    ip: str = "",
    status: str = "",
    service: str = "",
    model: str = "",
    hours: float = 24,
) -> tuple[str, list]:
    where: list[str] = []
    values: list = []

    since = _window(hours)
    if since:
        where.append("created_at >= ?")
        values.append(since)
    if ip:
        where.append("ip = ?")
        values.append(ip.strip())
    if service in ("api", "admin"):
        where.append("service = ?")
        values.append(service)
    if (model or "").strip():
        where.append("model = ?")
        values.append(model.strip())

    status = (status or "").strip().lower()
    if status == "error":
        where.append("status >= 400")
    elif status == "4xx":
        where.append("status BETWEEN 400 AND 499")
    elif status == "5xx":
        where.append("status >= 500")
    elif status.isdigit():
        where.append("status = ?")
        values.append(int(status))

    keyword = (q or "").strip()
    if keyword:
        like = f"%{keyword}%"
        where.append(
            "(ip LIKE ? OR nickname LIKE ? OR email LIKE ? OR path LIKE ?"
            " OR detail LIKE ? OR credential LIKE ? OR forwarded LIKE ? OR model LIKE ?)"
        )
        values.extend([like] * 8)

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return clause, values


def query_logs(
    *,
    q: str = "",
    ip: str = "",
    status: str = "",
    service: str = "",
    model: str = "",
    hours: float = 24,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """明细查询（先刷一次队列，保证看板看到的是最新的）。"""
    flush()
    clause, values = _filters(
        q=q, ip=ip, status=status, service=service, model=model, hours=hours
    )
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    total_row = db.query_one(f"SELECT COUNT(*) AS c FROM request_logs{clause}", tuple(values))
    rows = db.query(
        f"SELECT * FROM request_logs{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(values) + (limit, offset),
    )
    return {
        "total": int(total_row["c"] if total_row else 0),
        "limit": limit,
        "offset": offset,
        "items": [dict(row) for row in rows],
    }


def ip_stats(*, hours: float = 24, limit: int = 20) -> list[dict[str, Any]]:
    """按 IP 聚合：谁打得最多、错误多少、用的哪个账号。"""
    flush()
    clause, values = _filters(hours=hours)
    rows = db.query(
        "SELECT ip, COUNT(*) AS hits, SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS errors,"
        " MIN(created_at) AS first_at, MAX(created_at) AS last_at,"
        f" COUNT(DISTINCT COALESCE(user_id, -1)) AS users FROM request_logs{clause}"
        " GROUP BY ip ORDER BY hits DESC LIMIT ?",
        tuple(values) + (max(1, min(int(limit), 200)),),
    )
    items = [dict(row) for row in rows]
    if not items:
        return items

    # 每个 IP 最近一次「认得出账号」的请求，用来回答「这是谁」
    names = {item["ip"]: item for item in items}
    placeholders = ",".join("?" * len(names))
    for row in db.query(
        "SELECT ip, nickname, email, MAX(id) AS last_id FROM request_logs"
        f" WHERE ip IN ({placeholders}) AND user_id IS NOT NULL GROUP BY ip",
        tuple(names),
    ):
        target = names.get(row["ip"])
        if target is not None:
            target["nickname"] = row["nickname"]
            target["email"] = row["email"]
    for item in items:
        item.setdefault("nickname", "")
        item.setdefault("email", "")
        item["errors"] = int(item["errors"] or 0)
    return items


def model_stats(*, hours: float = 24, limit: int = 20) -> list[dict[str, Any]]:
    """按模型聚合：**到底是哪个模型在挨限流**（HTTP 429）。

    免费档的供应商最容易卡在这里（视频 / 生图尤甚）。以前模型名只写在 ``detail``
    的自由文本里，只能一条条翻日志；现在 ``model`` 是单独一列，这里一次回答：
    每个模型近期的请求数、**429 次数**、其他错误数、最后一次被限流的时间。
    """
    flush()
    clause, values = _filters(hours=hours)
    scoped = clause + (" AND" if clause else " WHERE") + " model <> ''"
    rows = db.query(
        "SELECT model, COUNT(*) AS hits,"
        " SUM(CASE WHEN status = 429 THEN 1 ELSE 0 END) AS limited,"
        " SUM(CASE WHEN status >= 400 AND status <> 429 THEN 1 ELSE 0 END) AS errors,"
        " MAX(CASE WHEN status = 429 THEN created_at END) AS last_limited_at,"
        " MAX(created_at) AS last_at"
        f" FROM request_logs{scoped} GROUP BY model"
        " ORDER BY limited DESC, hits DESC LIMIT ?",
        tuple(values) + (max(1, min(int(limit), 200)),),
    )
    items = []
    for row in rows:
        item = dict(row)
        for key in ("hits", "limited", "errors"):
            item[key] = int(item[key] or 0)
        item["last_limited_at"] = item["last_limited_at"] or ""
        item["last_at"] = item["last_at"] or ""
        items.append(item)
    return items


def path_stats(*, hours: float = 24, limit: int = 12) -> list[dict[str, Any]]:
    """按接口聚合：在被猛打的是哪个接口（一眼看出是刷登录还是刷模型）。"""
    flush()
    clause, values = _filters(hours=hours)
    rows = db.query(
        "SELECT path, method, COUNT(*) AS hits,"
        " SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS errors"
        f" FROM request_logs{clause} GROUP BY path, method ORDER BY hits DESC LIMIT ?",
        tuple(values) + (max(1, min(int(limit), 100)),),
    )
    return [dict(row) for row in rows]


def summary(*, hours: float = 24) -> dict[str, Any]:
    """顶部统计卡：总量、异常、独立 IP / 账号、最新一条、丢弃条数。"""
    flush()
    clause, values = _filters(hours=hours)
    row = db.query_one(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS errors,"
        " COUNT(DISTINCT ip) AS ips, COUNT(DISTINCT user_id) AS users,"
        " MAX(created_at) AS latest_at, MAX(id) AS latest_id"
        f" FROM request_logs{clause}",
        tuple(values),
    )
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    today_row = db.query_one(
        "SELECT COUNT(*) AS c FROM request_logs WHERE created_at LIKE ?", (f"{today}%",)
    )
    total_row = db.query_one("SELECT COUNT(*) AS c FROM request_logs")
    return {
        "total": int(row["total"] or 0) if row else 0,
        "errors": int(row["errors"] or 0) if row else 0,
        "ips": int(row["ips"] or 0) if row else 0,
        "users": int(row["users"] or 0) if row else 0,
        "latest_at": (row["latest_at"] if row else "") or "",
        "today": int(today_row["c"] or 0) if today_row else 0,
        "kept": int(total_row["c"] or 0) if total_row else 0,
        "pending": pending(),
        "dropped": dropped(),
        "retention_days": LOG_RETENTION_DAYS,
        "max_rows": LOG_MAX_ROWS,
    }


def clear(
    *,
    hours: float = 0,
    ip: str = "",
    status: str = "",
    q: str = "",
    service: str = "",
    model: str = "",
) -> int:
    """清空日志（可按同样的筛选条件只清一部分）。"""
    flush()
    clause, values = _filters(
        q=q, ip=ip, status=status, service=service, model=model, hours=hours
    )
    with db.transaction() as conn:
        removed = conn.execute(f"DELETE FROM request_logs{clause}", tuple(values)).rowcount
    return int(removed or 0)


def export_csv(
    *,
    q: str = "",
    ip: str = "",
    status: str = "",
    service: str = "",
    model: str = "",
    hours: float = 24,
    limit: int = 20000,
) -> str:
    """导出成 CSV（留证 / 交给上游排查用）。"""
    flush()
    clause, values = _filters(
        q=q, ip=ip, status=status, service=service, model=model, hours=hours
    )
    rows = db.query(
        f"SELECT * FROM request_logs{clause} ORDER BY id DESC LIMIT ?",
        tuple(values) + (max(1, min(int(limit), 100000)),),
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    # Excel 打开中文 CSV 认 BOM，否则是乱码
    writer.writerow(
        ["时间", "服务", "IP", "直连IP", "X-Forwarded-For", "方法", "路径", "模型", "状态码",
         "耗时ms", "用户ID", "昵称", "邮箱", "凭证", "来源", "具体信息", "User-Agent"]
    )
    for row in rows:
        writer.writerow(
            [row["created_at"], row["service"], row["ip"], row["peer_ip"], row["forwarded"],
             row["method"], row["path"], row["model"], row["status"], row["duration_ms"],
             row["user_id"] or "",
             row["nickname"], row["email"], row["credential"], row["client"], row["detail"],
             row["user_agent"]]
        )
    return "\ufeff" + buffer.getvalue()
