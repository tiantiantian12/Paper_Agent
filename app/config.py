"""Paper Agent Server —— 全局配置。

配置来源优先级：**环境变量 > 项目根 ``config.json`` > 内置默认值**。
日常调试直接改 ``config.json``（改完重启服务即可）；临时覆盖用环境变量。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# ------------------------------------------------------------------ 路径
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = Path(os.getenv("PAPER_AGENT_SERVER_CONFIG", str(BASE_DIR / "config.json")))


def _load_file_config() -> dict:
    """读取 config.json；文件缺失或格式错误时退回默认值，不让服务起不来。"""
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except OSError:
        return {}
    except ValueError as exc:
        print(f"[config] {CONFIG_FILE} 不是合法 JSON（{exc}），改用默认值", flush=True)
        return {}
    return data if isinstance(data, dict) else {}


_FILE_CONFIG = _load_file_config()


def _raw(env_name: str, path: str, default: object = "") -> object:
    """先取环境变量，再按 ``a.b.c`` 路径取 config.json，都没有则用默认值。"""
    env = os.getenv(env_name)
    if env not in (None, ""):
        return env
    node: object = _FILE_CONFIG
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def _text(env_name: str, path: str, default: str = "") -> str:
    return str(_raw(env_name, path, default))


def _number(env_name: str, path: str, default: int) -> int:
    try:
        return int(_raw(env_name, path, default))
    except (TypeError, ValueError):
        return default


def _flag(env_name: str, path: str, default: bool) -> bool:
    value = _raw(env_name, path, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def config_path() -> str:
    """当前生效的配置文件路径（便于日志 / 排障时确认）。"""
    return str(CONFIG_FILE)


# ------------------------------------------------------------------ 数据目录
# 配置文件里填相对路径时按项目根解析，避免从不同工作目录启动导致数据落错位置
DATA_DIR = Path(_text("PAPER_AGENT_SERVER_DATA", "data_dir", "data"))
if not DATA_DIR.is_absolute():
    DATA_DIR = BASE_DIR / DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "paper_agent.db"

# ------------------------------------------------------------------ 端口
# 主服务端口：桌面端（Paper_Agent）注册 / 登录 / 验证码都走这里
API_HOST = _text("API_HOST", "api_host", "0.0.0.0")
API_PORT = _number("API_PORT", "api_port", 8000)

# 管理端端口：独立端口上的用户看板，展示昵称 / 邮箱 / 账号密码等信息
ADMIN_HOST = _text("ADMIN_HOST", "admin_host", "0.0.0.0")
ADMIN_PORT = _number("ADMIN_PORT", "admin_port", 8010)

# ------------------------------------------------------------------ 邮件
SMTP_CONFIG: dict[str, object] = {
    "smtp_host": _text("SMTP_HOST", "smtp.smtp_host", "smtp.qq.com"),
    "smtp_port": _number("SMTP_PORT", "smtp.smtp_port", 465),
    "sender": _text("SMTP_SENDER", "smtp.sender", ""),
    "password": _text("SMTP_PASSWORD", "smtp.password", ""),
}

SMTP_TIMEOUT = _number("SMTP_TIMEOUT", "smtp.timeout", 15)
MAIL_SUBJECT = _text("MAIL_SUBJECT", "mail_subject", "Paper Agent 注册验证码")

# 开发调试用：不发真实邮件，只把验证码打印到服务端日志（本地联调 / SMTP 不可用时使用）
SMTP_DRY_RUN = _flag("SMTP_DRY_RUN", "smtp.dry_run", False)

# ------------------------------------------------------------------ 验证码
CODE_LENGTH = _number("CODE_LENGTH", "code.length", 4)                     # 四位数字验证码
CODE_TTL_SECONDS = _number("CODE_TTL_SECONDS", "code.ttl_seconds", 300)    # 有效期 5 分钟
CODE_RESEND_INTERVAL = _number("CODE_RESEND_INTERVAL", "code.resend_interval", 60)  # 重发冷却
CODE_MAX_PER_DAY = _number("CODE_MAX_PER_DAY", "code.max_per_day", 20)     # 单邮箱每日上限

# ------------------------------------------------------------------ 账号
PASSWORD_MIN = _number("PASSWORD_MIN", "account.password_min", 6)
PASSWORD_MAX = _number("PASSWORD_MAX", "account.password_max", 64)
NICKNAME_MAX = _number("NICKNAME_MAX", "account.nickname_max", 24)
TOKEN_TTL_DAYS = _number("TOKEN_TTL_DAYS", "account.token_ttl_days", 30)

# 管理端展示需要「账号密码」原文，因此额外保存一份明文副本。
# 登录校验始终走哈希，明文副本仅供看板展示；生产环境请设为 false。
STORE_PLAIN_PASSWORD = _flag("STORE_PLAIN_PASSWORD", "account.store_plain_password", True)

# 管理端访问口令；留空表示不校验（仅本机演示时使用）
ADMIN_ACCESS_TOKEN = _text("ADMIN_ACCESS_TOKEN", "admin_access_token", "")

# 允许注册的邮箱后缀；空表示不限制（注册接口始终要求 QQ 邮箱）
def _allowed_domains() -> list[str]:
    value = _raw("ALLOWED_EMAIL_DOMAINS", "account.allowed_email_domains", [])
    if isinstance(value, list):
        return [str(item).strip().lower() for item in value if str(item).strip()]
    return [item.strip().lower() for item in str(value).split(",") if item.strip()]


ALLOWED_EMAIL_DOMAINS = _allowed_domains()

# ------------------------------------------------------------------ 大模型代理
# 桌面端的内置模型不直连供应商，统一走服务端代理；代理地址形如
# http://127.0.0.1:8000/api/llm/v1/chat/completions
USER_KEY_TTL_DAYS = _number("USER_KEY_TTL_DAYS", "llm.user_key_ttl_days", 30)
# 供应商密钥命中限流后的冷却秒数，冷却期内换池里下一个 Key
PROVIDER_COOLDOWN_SECONDS = _number("PROVIDER_COOLDOWN_SECONDS", "llm.provider_cooldown_seconds", 60)
# 单次代理最多试几个供应商密钥
PROXY_MAX_KEY_TRIES = _number("PROXY_MAX_KEY_TRIES", "llm.max_key_tries", 3)
# 转发到大模型的超时（秒）；流式请求是长连接，读超时只在建立连接时用
PROXY_CONNECT_TIMEOUT = _number("PROXY_CONNECT_TIMEOUT", "llm.connect_timeout", 15)
PROXY_READ_TIMEOUT = _number("PROXY_READ_TIMEOUT", "llm.read_timeout", 600)

# 内置模型与供应商密钥的默认值。
#
# 真正的数据在数据库里（看板里改的就是它），这里的值只在**播种**时用：
# 首次启动创建模型、以及给还没配密钥的模型补上密钥（按 api_key 去重，可反复启动）。
# 想改就改 config.json 的 llm.models，或直接在浏览器看板里改。
DEFAULT_LLM_MODELS: list[dict] = [
    {
        "model_id": "sensenova-6.8-flash-lite",
        "name": "SenseNova 6.8 Flash Lite",
        "desc": "内置模型 · 经服务端代理调用（需登录）",
        "kind": "chat",
        "base_url": "https://token.sensenova.cn/v1",
        "api_keys": [],
    },
    {
        "model_id": "sensenova-u1.5-lite",
        "name": "SenseNova U1.5 Lite",
        "desc": "文生图：输入提示词直接出图",
        "kind": "image",
        "base_url": "https://token.sensenova.cn/v1",
        "api_keys": [],
    },
]


def _llm_models() -> list[dict]:
    """从 config.json 的 ``llm.models`` 读取内置模型；缺失时用内置默认值。

    每项形如::

        {"model_id": "...", "name": "...", "desc": "备注", "kind": "chat|image",
         "base_url": "https://...", "api_keys": ["sk-...", "sk-..."]}

    ``name`` 与 ``desc`` 会随 :func:`app.llm_store.seed_models` 播种进库，并下发给
    桌面端显示在模型下拉框里（看板里随时可改）；``kind`` 决定代理走哪个接口
    （``chat`` → ``/chat/completions``，``image`` → ``/images/generations``，
    ``video`` → ``/videos`` 创建 + ``/agnesapi`` 轮询）。
    也接受单个 ``api_key`` 字段（会并进密钥池）。
    """
    raw = _raw("LLM_MODELS", "llm.models", None)
    if not isinstance(raw, list):
        return [dict(item) for item in DEFAULT_LLM_MODELS]

    models: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("model_id") or "").strip()
        if not model_id:
            continue
        keys = item.get("api_keys") or item.get("api_key") or []
        if isinstance(keys, str):
            keys = [keys]
        kind = str(item.get("kind") or "chat").strip().lower()
        models.append(
            {
                "model_id": model_id,
                "name": str(item.get("name") or model_id),
                "desc": str(item.get("desc") or ""),
                "kind": kind if kind in ("chat", "image", "video") else "chat",
                "base_url": str(item.get("base_url") or "").strip().rstrip("/"),
                "api_keys": [str(key).strip() for key in keys if str(key).strip()],
            }
        )
    return models or [dict(item) for item in DEFAULT_LLM_MODELS]


LLM_MODELS = _llm_models()
# 代码里出现过的旧默认地址：库里还停在这些值上时，跟着 config 纠正一次
LEGACY_LLM_BASE_URLS = {"https://api.sensenova.cn/v1"}

# ------------------------------------------------------------------ 请求日志
# 记录每个请求的「谁（IP / 昵称 / 邮箱）+ 什么时候 + 打了什么 + 结果」，
# 看板「请求日志」页签里看。目的很实在：被刷 / 被打的时候要能查出是谁。
LOG_ENABLED = _flag("LOG_ENABLED", "log.enabled", True)
# 保留天数（超期的自动删）；0 表示不按时间清理
LOG_RETENTION_DAYS = _number("LOG_RETENTION_DAYS", "log.retention_days", 14)
# 最多保留多少条（再多就从最旧的删）；0 表示不限
LOG_MAX_ROWS = _number("LOG_MAX_ROWS", "log.max_rows", 200000)
# 服务前面挂了 Nginx / Cloudflare 之类反代时设为 true，否则拿到的 IP 会是反代自己的
LOG_TRUST_PROXY = _flag("LOG_TRUST_PROXY", "log.trust_proxy", False)
# 这些路径不记录（看板自带的静态资源与健康检查，记进来只是噪音）
LOG_SKIP_PREFIXES = ("/static/",)
LOG_SKIP_PATHS = ("/health", "/favicon.ico")


def log_skip_paths() -> set[str]:
    """不进日志的路径（config.json 的 ``log.skip_paths`` 可覆盖）。"""
    value = _raw("LOG_SKIP_PATHS", "log.skip_paths", None)
    if isinstance(value, list) and value:
        return {str(item).strip() for item in value if str(item).strip()}
    return set(LOG_SKIP_PATHS)


APP_TITLE = "Paper Agent Server"
APP_VERSION = "0.1.0"

# 默认头像：icon:<内置图标名> 或 file:<上传文件名>，与桌面端 utils/avatar.py 约定一致
DEFAULT_AVATAR = _text("DEFAULT_AVATAR", "account.default_avatar", "icon:user")


def smtp_ready() -> bool:
    """SMTP 配置是否完整（缺一项就无法发信）。"""
    return all(
        str(SMTP_CONFIG.get(key) or "").strip()
        for key in ("smtp_host", "sender", "password")
    )
