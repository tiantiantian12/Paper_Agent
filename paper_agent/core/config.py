"""应用配置：基于 QSettings 的持久化设置。"""

from __future__ import annotations

import json

from PySide6.QtCore import QSettings

from paper_agent.core.constants import (
    IMAGE_API_BASE_URL,
    IMAGE_API_KEY,
    IMAGE_MODEL_ID,
    SETTINGS_FILE,
    WINDOW_DEFAULT_HEIGHT,
    WINDOW_DEFAULT_WIDTH,
)
from paper_agent.core.models import CustomModel
from paper_agent.services.auth_client import DEFAULT_SERVER_URL
from paper_agent.utils.avatar import DEFAULT_AVATAR
from paper_agent.utils.system_theme import system_prefers_dark

# 模型条目字段：id / name / desc；kind="image" 表示文生图模型
# builtin=False 表示这是服务端代理的内置模型：Base URL 与 API Key 都由服务端管理，
# 桌面端只携带服务端签发的用户密钥（见 services/llm_key.py）
DEFAULT_MODELS = [
    {
        "id": "sensenova-6.8-flash-lite",
        "name": "SenseNova 6.8 Flash Lite",
        "desc": "内置模型 · 经服务端代理调用（需登录）",
        "builtin": True,
    },
    {"id": "gpt-5", "name": "GPT-5", "desc": "长文推理，适合论文全流程"},
    {"id": "gpt-5-mini", "name": "GPT-5 mini", "desc": "轻量快速，适合润色与格式"},
    {"id": "claude-sonnet-4.5", "name": "Claude Sonnet 4.5", "desc": "文风自然，适合写作与改写"},
    {"id": "deepseek-v3", "name": "DeepSeek-V3", "desc": "中文论文性价比之选"},
    {
        "id": IMAGE_MODEL_ID,
        "name": "SenseNova U1.5 Lite",
        "desc": "文生图：输入提示词直接出图",
        "kind": "image",
    },
]

REASONING_EFFORTS = [
    {"id": "none", "name": "关闭", "desc": "不推理，最快最省（格式转换 / 简单问答）"},
    {"id": "low", "name": "低", "desc": "轻度推理 · temperature 0.3 · 工具 24 轮"},
    {"id": "medium", "name": "中", "desc": "中等推理 · temperature 0.7 · 工具 40 轮"},
    {"id": "high", "name": "高", "desc": "增强推理 · temperature 0.9 · 工具 60 轮"},
    {"id": "max", "name": "最深", "desc": "深度推理 · temperature 0.9 · 工具 80 轮（更慢更贵）"},
]

THEME_LIGHT = "light"
THEME_DARK = "dark"

# 智能体工作模式：文档模式写论文/做 PPT，编程模式配环境/写代码/跑代码
CHAT_MODES = [
    {
        "id": "doc",
        "name": "文档",
        "desc": "写论文 · Word / PPT / PDF / Excel 排版出稿",
    },
    {
        "id": "code",
        "name": "编程",
        "desc": "配环境 · 写代码 · 跑代码（在工程目录内执行）",
    },
]
MODE_DOC = "doc"
MODE_CODE = "code"

# 两种模式下助手的自称与「生成中」文案：文档模式写论文，编程模式写代码/搭工程
MODE_TEXTS = {
    MODE_DOC: {"assistant": "论文助手", "busy": "正在写作"},
    MODE_CODE: {"assistant": "编程助手", "busy": "正在创作"},
}


def mode_text(mode: str, key: str) -> str:
    """取某模式下的界面文案（``assistant`` 称谓 / ``busy`` 生成中提示）。

    未知模式按文档模式处理，避免新旧配置不一致时界面出现空白。
    """
    return MODE_TEXTS.get(mode, MODE_TEXTS[MODE_DOC]).get(key, "")


class AppConfig(QSettings):
    """轻量配置封装，属性访问 + 变更通知。"""

    def __init__(self) -> None:
        super().__init__(str(SETTINGS_FILE), QSettings.IniFormat)

    # ------------------------------------------------------------ 主题
    @property
    def theme(self) -> str:
        """上次选择的主题；从未选过时跟随系统外观。"""
        default = THEME_DARK if system_prefers_dark() else THEME_LIGHT
        return self.value("appearance/theme", default, type=str)

    @theme.setter
    def theme(self, value: str) -> None:
        self.setValue("appearance/theme", value)

    @property
    def font_scale(self) -> float:
        return float(self.value("appearance/fontScale", 1.0))

    @font_scale.setter
    def font_scale(self, value: float) -> None:
        self.setValue("appearance/fontScale", value)

    # ------------------------------------------------------------ 模型
    @property
    def model(self) -> str:
        """当前选中的模型 id。

        自定义模型被删掉之后，``chat/model`` 里可能还留着那个已经失效的本地 id
        （界面会显示成一串 id、发请求也找不到模型配置）。这里统一兜底到内置默认
        模型，保证读出来的永远是「列表里真实存在」的模型。
        """
        value = self.value("chat/model", DEFAULT_MODELS[0]["id"], type=str)
        if value and self.find_model(value):
            return value
        for item in self.all_models():
            if item.get("builtin"):
                return str(item["id"])
        return DEFAULT_MODELS[0]["id"]

    @model.setter
    def model(self, value: str) -> None:
        self.setValue("chat/model", value)

    @property
    def reasoning_effort(self) -> str:
        return self.value("chat/reasoningEffort", "medium", type=str)

    @reasoning_effort.setter
    def reasoning_effort(self, value: str) -> None:
        self.setValue("chat/reasoningEffort", value)

    @property
    def mode(self) -> str:
        """智能体工作模式：``doc`` 文档 / ``code`` 编程。"""
        value = self.value("chat/mode", MODE_DOC, type=str)
        return value if value in {item["id"] for item in CHAT_MODES} else MODE_DOC

    @mode.setter
    def mode(self, value: str) -> None:
        self.setValue("chat/mode", value)

    @property
    def code_root(self) -> str:
        """编程模式的工作区目录；空字符串表示用默认目录（数据目录下的 workspace）。"""
        return self.value("code/root", "", type=str)

    @code_root.setter
    def code_root(self, value: str) -> None:
        self.setValue("code/root", str(value or ""))

    @property
    def max_parallel(self) -> int:
        """最多同时跑几个生成任务（默认 2）。

        每个会话各自一个任务、互不干扰（在 A 会话出视频时 B 会话照样能问问题），
        但总数有上限：免费档的视频 / 生图容易被限流，同时猛跑只会一起卡。
        调大之前先想清楚 —— 并发越高越容易撞供应商的速率限制。
        """
        try:
            value = int(self.value("chat/maxParallel", 2, type=int))
        except (TypeError, ValueError):
            value = 2
        return max(1, min(value, 8))

    @max_parallel.setter
    def max_parallel(self, value: int) -> None:
        self.setValue("chat/maxParallel", max(1, min(int(value or 2), 8)))

    @property
    def send_shortcut(self) -> str:
        """Enter 发送 / Ctrl+Enter 发送。"""
        return self.value("chat/sendShortcut", "enter", type=str)

    @send_shortcut.setter
    def send_shortcut(self, value: str) -> None:
        self.setValue("chat/sendShortcut", value)

    # ------------------------------------------------------------ 自定义模型
    @property
    def custom_models(self) -> list[CustomModel]:
        """用户自定义的 OpenAI 兼容模型列表。"""
        raw = self.value("models/custom", "", type=str)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [CustomModel.from_dict(item) for item in data if isinstance(item, dict)]

    @custom_models.setter
    def custom_models(self, models: list[CustomModel]) -> None:
        payload = json.dumps([m.to_dict() for m in models], ensure_ascii=False)
        self.setValue("models/custom", payload)

    def custom_model(self, model_id: str) -> CustomModel | None:
        """按本地 id 或模型 ID 查找自定义模型。"""
        for model in self.custom_models:
            if model.id == model_id or model.model_id == model_id:
                return model
        return None

    def add_custom_model(self, model: CustomModel) -> None:
        models = self.custom_models
        models.append(model)
        self.custom_models = models

    def update_custom_model(self, model: CustomModel) -> None:
        models = self.custom_models
        for index, item in enumerate(models):
            if item.id == model.id:
                models[index] = model
                break
        else:
            models.append(model)
        self.custom_models = models

    def remove_custom_model(self, model_id: str) -> None:
        models = [m for m in self.custom_models if m.id != model_id]
        self.custom_models = models

    def all_models(self) -> list[dict]:
        """内置模型 + 自定义模型（统一为下拉框数据结构）。

        内置模型清单 = **本地默认清单** ∪ **服务端看板下发的模型**：

        - 本地已有同名 ``model_id`` 的：显示名与备注以服务端下发的为准（看板改完，
          客户端下次启动 / 登录就同步过来），本地没配过的 id 沿用默认文案
        - 服务端多配的（本地清单里没有）：**直接追加**成内置模型，走同一个服务端代理
          链路 —— 所以看板里加个模型、配好密钥，客户端不用发新版就能选、能用
        """
        overrides = {
            str(item.get("model_id")): item
            for item in self.builtin_models
            if item.get("model_id")
        }
        builtin: list[dict] = []
        for model in DEFAULT_MODELS:
            item = dict(model, custom=False)
            item.setdefault("builtin", False)
            override = overrides.get(str(item.get("id") or ""))
            if override:
                if str(override.get("name") or "").strip():
                    item["name"] = str(override["name"]).strip()
                item["desc"] = str(override.get("desc") or "").strip() or item.get("desc", "")
                # 类型只「升级」不「降级」：服务端标成 image 就按 image（看板里新加的
                # 生图模型就是这么来的）；服务端说 chat、但本地这条本来就是生图模型
                # 时保持 image —— 否则连上还不认识 kind 字段的旧版服务端时，本地内置
                # 的生图模型会被一路改成对话模型，选中它反而去聊天了。
                server_kind = str(override.get("kind") or "")
                if server_kind == "image":
                    item["kind"] = "image"
                elif server_kind == "video":
                    item["kind"] = "video"
                elif server_kind == "chat" and item.get("kind") not in ("image", "video"):
                    item["kind"] = "chat"
            builtin.append(item)

        # 服务端新增的模型插在本地「服务端代理」条目后面，别掉到文生图那一条下面去
        # （见 _server_added_models）
        server_added = self._server_added_models({str(item["id"]) for item in builtin})
        cut = max(
            (index for index, item in enumerate(builtin) if item.get("builtin")), default=-1
        ) + 1
        builtin[cut:cut] = server_added

        custom = [
            {
                "id": m.id,
                "name": m.name,
                "desc": m.display_desc,
                "custom": True,
                "builtin": False,       # 自定义模型一律不算「服务端代理的内置模型」
                "model_id": m.model_id,
                "base_url": m.base_url,
                "api_key": m.api_key,
                "api_keys": m.key_pool,
                "reasoning": m.reasoning,
            }
            for m in self.custom_models
        ]
        return builtin + custom

    def _server_added_models(self, known: set[str]) -> list[dict]:
        """服务端看板里新增、本地清单里没有的模型 —— 一律按「服务端代理」处理。

        ``known`` 是本地内置模型的 id 集合；与之重名的走覆盖逻辑，不重复追加。
        本地自定义模型的 id 是随机串，理论上撞不上，撞上时以自定义模型为准。
        """
        added: list[dict] = []
        for item in self.builtin_models:
            model_id = str(item.get("model_id") or "").strip()
            if not model_id or model_id in known or self.is_custom_model(model_id):
                continue
            known.add(model_id)
            kind = str(item.get("kind") or "").strip().lower()
            added.append(
                {
                    "id": model_id,
                    "name": str(item.get("name") or "").strip() or model_id,
                    "desc": str(item.get("desc") or "").strip()
                    or "服务端内置模型 · 经服务端代理调用",
                    "kind": kind if kind in ("chat", "image", "video") else "chat",
                    "custom": False,
                    "builtin": True,     # 走服务端代理：Base URL / Key 都在服务端
                    "server": True,      # 本地清单里没有，完全来自服务端
                }
            )
        return added

    def find_model(self, model_id: str) -> dict | None:
        for model in self.all_models():
            if model["id"] == model_id:
                return model
        return None

    def is_custom_model(self, model_id: str) -> bool:
        return any(m.id == model_id for m in self.custom_models)

    # ------------------------------------------------------------ 文生图
    @property
    def image_base_url(self) -> str:
        return self.value("image/baseUrl", IMAGE_API_BASE_URL, type=str)

    @image_base_url.setter
    def image_base_url(self, value: str) -> None:
        self.setValue("image/baseUrl", value)

    @property
    def image_api_key(self) -> str:
        return self.value("image/apiKey", IMAGE_API_KEY, type=str)

    @image_api_key.setter
    def image_api_key(self, value: str) -> None:
        self.setValue("image/apiKey", value)

    @property
    def image_model(self) -> str:
        return self.value("image/model", IMAGE_MODEL_ID, type=str)

    @image_model.setter
    def image_model(self, value: str) -> None:
        self.setValue("image/model", value)

    def server_image_model(self, model_id: str = "") -> dict | None:
        """该用哪个服务端生图模型出图；服务端没配生图模型时返回 None。

        规则：

        - 传进来的正是服务端那个生图模型 → 用它
        - 传进来的是**本地配的**生图模型（服务端没配同一个 id）→ 返回 None，
          继续用本地那套配置（用户明确选的就是本地的）
        - 其余情况（选的是对话模型、或没传）→ 用服务端配的第一个生图模型：
          在对话里顺手画一张图属于「附加能力」，同样该走服务端代理
        """
        images = [
            item for item in self.builtin_models if str(item.get("kind") or "") == "image"
        ]
        if not images:
            return None
        target = (model_id or "").strip()
        if target:
            for item in images:
                if str(item.get("model_id") or "") == target:
                    return item
            local = self.find_model(target)
            if local and local.get("kind") == "image":
                return None
        return images[0]

    def image_config(self, model_id: str = "") -> dict:
        """文生图接口配置（供 AgentService / Skills 复用）。

        服务端配了生图模型（看板里 ``kind = 文生图``）且已登录、代理密钥可用时**走
        服务端代理**：Base URL 指向服务端、Key 用服务端签发的用户密钥，供应商那把
        Key 留在看板的密钥池里（可以配多把轮换）。

        否则回落到本地配置（``settings.ini`` 的 ``image/*`` 段，默认是内置的那把
        SenseNova Key），离线 / 未登录时照样能出图。

        Args:
            model_id: 当前选中的模型。选中的是对话模型时，照样用服务端配的生图模型
                （对话里让智能体顺手画一张图是很常见的要求）；只有选中的是**本地**
                那个生图模型（服务端没配同一个 id）才继续用本地配置。
        """
        server_model = self.server_image_model(model_id)
        if server_model and self.llm_user_key and self.logged_in and not self.llm_key_expired:
            return {
                "base_url": self.llm_proxy_url,
                "api_key": self.llm_user_key,
                "model_id": str(server_model["model_id"]),
                "proxy": True,
            }
        # 本地兜底只认本地配的模型：免得把服务端那个模型名发给本地那把 Key 的服务商
        return {
            "base_url": self.image_base_url,
            "api_key": self.image_api_key,
            "model_id": self.image_model,
        }

    def is_image_model(self, model_id: str) -> bool:
        """该模型是否为文生图模型。"""
        model = self.find_model(model_id)
        return bool(model and model.get("kind") == "image")

    def is_video_model(self, model_id: str) -> bool:
        """该模型是否为文生视频模型（服务端 ``kind = video``）。"""
        model = self.find_model(model_id)
        return bool(model and model.get("kind") == "video")

    # ------------------------------------------------------------ 视频
    def server_video_model(self, model_id: str = "") -> dict | None:
        """服务端下发的视频模型；没配返回 None（此时没有出视频能力）。

        规则与生图一致：传进来的正好是服务端那个视频模型就按它，
        否则（选的是对话模型、让智能体顺手出一段视频）用服务端配的第一个。
        """
        videos = [
            item for item in self.builtin_models if str(item.get("kind") or "") == "video"
        ]
        if not videos:
            return None
        target = (model_id or "").strip()
        if target:
            for item in videos:
                if str(item.get("model_id") or "") == target:
                    return item
        return videos[0]

    def video_config(self, model_id: str = "") -> dict:
        """文生视频接口配置（供 AgentService / Skills 复用）。

        服务端配了视频模型且登录状态正常时走服务端代理；否则返回空配置
        （``VideoClient.available`` 为 False，不会去撞服务端）。
        """
        server_model = self.server_video_model(model_id)
        if server_model and self.llm_user_key and self.logged_in and not self.llm_key_expired:
            return {
                "base_url": self.llm_proxy_url,
                "api_key": self.llm_user_key,
                "model_id": str(server_model["model_id"]),
                "proxy": True,
            }
        return {"base_url": "", "api_key": "", "model_id": ""}

    # ------------------------------------------------------------ 账号
    @property
    def auth_server_url(self) -> str:
        """账号服务端地址（Paper_Agent_Server 的 API 端口）。"""
        value = self.value("auth/serverUrl", DEFAULT_SERVER_URL, type=str)
        return (value or DEFAULT_SERVER_URL).strip().rstrip("/")

    @auth_server_url.setter
    def auth_server_url(self, value: str) -> None:
        self.setValue("auth/serverUrl", (str(value or "").strip() or DEFAULT_SERVER_URL))

    @property
    def auth_token(self) -> str:
        """登录令牌；为空表示未登录（离线使用）。"""
        return self.value("auth/token", "", type=str)

    @property
    def auth_user(self) -> dict | None:
        """已登录用户资料：``{"id", "email", "nickname", ...}``。"""
        raw = self.value("auth/user", "", type=str)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @property
    def logged_in(self) -> bool:
        return bool(self.auth_token) and self.auth_user is not None

    @property
    def avatar(self) -> str:
        """头像规格：``icon:xxx`` 内置头像 / ``file:xxx.png`` 上传的头像。"""
        return self.value("account/avatar", DEFAULT_AVATAR, type=str)

    @avatar.setter
    def avatar(self, value: str) -> None:
        self.setValue("account/avatar", str(value or DEFAULT_AVATAR))

    @property
    def display_name(self) -> str:
        """界面上展示的昵称；未登录时为空字符串。"""
        user = self.auth_user
        if not user:
            return ""
        return str(user.get("nickname") or user.get("email") or "")

    @property
    def display_email(self) -> str:
        user = self.auth_user
        return str(user.get("email") or "") if user else ""

    def set_account(self, token: str, user: dict) -> None:
        """保存登录态（令牌 + 用户资料）。"""
        self.setValue("auth/token", token or "")
        self.setValue("auth/user", json.dumps(user or {}, ensure_ascii=False))
        # 服务端头像优先，未设置时沿用本地选过的头像
        avatar = str((user or {}).get("avatar") or "")
        if avatar:
            self.setValue("account/avatar", avatar)
        self.sync()

    def patch_account(self, **fields) -> None:
        """局部更新已保存的用户资料（昵称 / 头像等）。"""
        user = dict(self.auth_user or {})
        user.update({key: value for key, value in fields.items() if value is not None})
        self.setValue("auth/user", json.dumps(user, ensure_ascii=False))
        self.sync()

    def clear_account(self) -> None:
        """退出登录：清掉令牌、资料、代理密钥与服务端下发的内置模型文案。"""
        self.remove("auth/token")
        self.remove("auth/user")
        self.clear_llm_key()
        self.clear_builtin_models()
        self.sync()

    # ------------------------------------------------------------ 内置模型文案
    # 名称与备注由服务端看板维护，登录后下发（见 services/llm_models.py）。
    # 这里只做本地缓存：拉不到就用 DEFAULT_MODELS 里的默认文案。
    @property
    def builtin_models(self) -> list[dict]:
        """服务端下发的内置模型：``[{"model_id", "name", "desc"}]``。"""
        raw = self.value("llm/builtinModels", "", type=str)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict) and item.get("model_id")]

    @builtin_models.setter
    def builtin_models(self, models: list[dict]) -> None:
        self.setValue("llm/builtinModels", json.dumps(list(models or []), ensure_ascii=False))

    def clear_builtin_models(self) -> None:
        self.remove("llm/builtinModels")

    # ------------------------------------------------------------ 代理密钥
    # 内置模型不直连供应商：Base URL 指向服务端，Key 是服务端签发的用户密钥。
    # 密钥只在登录后由服务端下发（见 services/llm_key.py），随时可能被停用 / 过期。
    @property
    def llm_user_key(self) -> str:
        return self.value("llm/userKey", "", type=str)

    @llm_user_key.setter
    def llm_user_key(self, value: str) -> None:
        self.setValue("llm/userKey", str(value or ""))

    @property
    def llm_key_expires(self) -> str:
        """代理密钥的到期时间（ISO 字符串）；空表示永不过期。"""
        return self.value("llm/keyExpires", "", type=str)

    @llm_key_expires.setter
    def llm_key_expires(self, value: str) -> None:
        self.setValue("llm/keyExpires", str(value or ""))

    @property
    def llm_key_expired(self) -> bool:
        """密钥是否已过期（时间格式不合法时按未过期处理，交给服务端判定）。"""
        raw = self.llm_key_expires
        if not raw:
            return False
        from datetime import datetime

        try:
            expires = datetime.fromisoformat(raw)
        except ValueError:
            return False
        if expires.tzinfo is None:
            expires = expires.astimezone()
        return expires < datetime.now().astimezone()

    def clear_llm_key(self) -> None:
        self.remove("llm/userKey")
        self.remove("llm/keyExpires")

    @property
    def llm_proxy_url(self) -> str:
        """内置模型的代理地址（OpenAI 兼容）。"""
        return f"{self.auth_server_url}/api/llm/v1"

    def is_builtin_model(self, model_id: str) -> bool:
        """是否为「服务端代理」的内置模型。"""
        if self.is_custom_model(model_id):
            return False
        model = self.find_model(model_id)
        return bool(model and model.get("builtin"))

    # ------------------------------------------------------------ 窗口
    @property
    def window_geometry(self) -> bytes:
        return self.value("window/geometry", b"", type=bytes)

    @window_geometry.setter
    def window_geometry(self, value: bytes) -> None:
        self.setValue("window/geometry", value)

    @property
    def sidebar_visible(self) -> bool:
        return bool(int(self.value("window/sidebarVisible", 1)))

    @sidebar_visible.setter
    def sidebar_visible(self, value: bool) -> None:
        self.setValue("window/sidebarVisible", int(value))

    @property
    def outline_visible(self) -> bool:
        return bool(int(self.value("window/outlineVisible", 0)))

    @outline_visible.setter
    def outline_visible(self, value: bool) -> None:
        self.setValue("window/outlineVisible", int(value))

    @property
    def default_size(self) -> tuple[int, int]:
        return (
            self.value("window/width", WINDOW_DEFAULT_WIDTH, type=int),
            self.value("window/height", WINDOW_DEFAULT_HEIGHT, type=int),
        )
