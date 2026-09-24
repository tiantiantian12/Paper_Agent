"""SQLite 数据访问层。

建表、连接获取与事务封装，全部使用标准库 ``sqlite3``，不引入 ORM。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from app.config import DB_PATH

_LOCAL = threading.local()
_LOCK = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT    NOT NULL UNIQUE,
    nickname       TEXT    NOT NULL,
    avatar         TEXT,
    password_hash  TEXT    NOT NULL,
    password_salt  TEXT    NOT NULL,
    password_plain TEXT,
    email_domain   TEXT,
    status         TEXT    NOT NULL DEFAULT 'active',
    created_at     TEXT    NOT NULL,
    last_login_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS verification_codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT    NOT NULL,
    code       TEXT    NOT NULL,
    purpose    TEXT    NOT NULL DEFAULT 'register',
    consumed   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_codes_email ON verification_codes(email, purpose);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- 内置模型：客户端看到的 model_id → 供应商（OpenAI 兼容）地址
-- name / desc 会下发给桌面端，直接显示在模型下拉框里（desc 即「备注」）
-- kind：chat = 对话（/chat/completions），image = 文生图（/images/generations）
CREATE TABLE IF NOT EXISTS llm_models (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id   TEXT    NOT NULL UNIQUE,
    name       TEXT    NOT NULL,
    desc       TEXT    NOT NULL DEFAULT '',
    kind       TEXT    NOT NULL DEFAULT 'chat',
    base_url   TEXT    NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL
);

-- 供应商密钥池：同一个模型可以配多个 Key，限流时轮换并冷却
CREATE TABLE IF NOT EXISTS provider_keys (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id       TEXT    NOT NULL,
    api_key        TEXT    NOT NULL,
    label          TEXT,
    status         TEXT    NOT NULL DEFAULT 'active',
    cooldown_until TEXT,
    last_error     TEXT,
    last_used_at   TEXT,
    created_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_provider_keys_model ON provider_keys(model_id);

-- 用户密钥：桌面端调用服务端代理时携带，过期时间由服务端设定
CREATE TABLE IF NOT EXISTS user_keys (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    key_value      TEXT    NOT NULL UNIQUE,
    label          TEXT,
    status         TEXT    NOT NULL DEFAULT 'active',
    allowed_models TEXT,
    expires_at     TEXT,
    last_used_at   TEXT,
    created_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_keys_user ON user_keys(user_id);

-- 请求日志：谁（IP / 昵称 / 邮箱）、什么时候、打了哪个接口、结果如何。
-- 被刷 / 被 DDoS 时靠它回溯来源；写入走批量提交（见 app/request_logs.py），
-- 单个请求不会因为写日志而卡住。
CREATE TABLE IF NOT EXISTS request_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL,
    service     TEXT    NOT NULL DEFAULT 'api',   -- api = 主服务，admin = 看板
    ip          TEXT    NOT NULL DEFAULT '',      -- 按 log.trust_proxy 决定的「真实来源 IP」
    peer_ip     TEXT    NOT NULL DEFAULT '',      -- 直连的 TCP 对端（反代后面就是反代的地址）
    forwarded   TEXT    NOT NULL DEFAULT '',      -- 原始 X-Forwarded-For（有就留着，便于对照）
    method      TEXT    NOT NULL DEFAULT '',
    path        TEXT    NOT NULL DEFAULT '',
    model       TEXT    NOT NULL DEFAULT '',      -- 打的是哪个模型（看板按它统计「谁被限流了」）
    status      INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    user_id     INTEGER,
    nickname    TEXT    NOT NULL DEFAULT '',
    email       TEXT    NOT NULL DEFAULT '',
    credential  TEXT    NOT NULL DEFAULT '',      -- 打码后的凭证（哪把密钥在打）
    client      TEXT    NOT NULL DEFAULT '',      -- 桌面端 / 浏览器 / 脚本 …（按 UA 归类）
    detail      TEXT    NOT NULL DEFAULT '',      -- 这条请求在干什么（模型 / 邮箱 / 参数）
    user_agent  TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_logs_created ON request_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_logs_ip ON request_logs(ip, id);
CREATE INDEX IF NOT EXISTS idx_logs_user ON request_logs(user_id, id);
CREATE INDEX IF NOT EXISTS idx_logs_status ON request_logs(status, id);
-- 注意：request_logs 的 model 是后来加的列，它的索引不能建在这里 ——
-- 老库建表语句会被 IF NOT EXISTS 跳过，此时 model 列还不存在，建索引会直接报错。
-- 索引放在 init_db() 里、等补列之后再建。
"""


def connect() -> sqlite3.Connection:
    """获取当前线程的连接（长连接，减少重复开销）。"""
    conn: sqlite3.Connection | None = getattr(_LOCAL, "conn", None)
    if conn is not None:
        return conn
    with _LOCK:
        conn = getattr(_LOCAL, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(str(DB_PATH), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # 写锁等待：同一进程起多个服务（API + 看板）时避免瞬时锁报错
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            # WAL 需要短暂独占锁，拿不到就沿用默认日志模式，不影响功能
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:  # pragma: no cover - 降级路径
            pass
        _LOCAL.conn = conn
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """写操作用：自动提交 / 回滚。"""
    conn = connect()
    try:
        with conn:
            yield conn
    except Exception:
        conn.rollback()
        raise


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """老库补列（SQLite 没有 IF NOT EXISTS 的 ADD COLUMN）。"""
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    """建表（幂等），并写入内置模型的初始配置。"""
    with _LOCK:
        conn = connect()
        conn.executescript(SCHEMA)
        _ensure_column(conn, "users", "avatar", "TEXT")
        _ensure_column(conn, "llm_models", "desc", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "llm_models", "kind", "TEXT NOT NULL DEFAULT 'chat'")
        # 老库补上「打的是哪个模型」：看板要按模型看限流。
        # 索引必须等补完列再建：老库的建表语句被 IF NOT EXISTS 跳过了，
        # 若按 SCHEMA 里的顺序建索引，model 列还不存在，启动就会崩。
        _ensure_column(conn, "request_logs", "model", "TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_model ON request_logs(model, id)")
        conn.commit()
    # 局部导入：llm_store 依赖本模块，放模块顶层会形成循环导入
    from app import llm_store

    llm_store.seed_models()


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return connect().execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    """执行写操作并返回最后插入行（仅适用于 users 表，见 :func:`insert`）。"""
    conn = connect()
    with conn:
        cur = conn.execute(sql, params)
        rowid = cur.lastrowid
    if rowid is None:
        return None
    return conn.execute("SELECT * FROM users WHERE id=?", (rowid,)).fetchone()


def insert(sql: str, params: tuple = (), table: str = "users") -> sqlite3.Row | None:
    """执行 INSERT 并回查新行；``table`` 指定要回查的表名。

    早期版本把回查写死成 ``users``，插入其它表时会返回不相干的行，所以新增
    这个通用版本（主键是 INTEGER PRIMARY KEY 时 rowid 就是 id）。
    """
    conn = connect()
    with conn:
        cur = conn.execute(sql, params)
        rowid = cur.lastrowid
    if rowid is None:
        return None
    return conn.execute(f"SELECT * FROM {table} WHERE rowid=?", (rowid,)).fetchone()
