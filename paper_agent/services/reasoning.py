"""推理强度 → 接口参数。

各模型服务对「推理强度」的字段名与取值并不统一，这里统一收口：

- ``reasoning_effort``：OpenAI o 系列 / GPT-5，以及多数照抄该字段的兼容服务，
  取 ``low`` / ``medium`` / ``high``
- ``enable_thinking``：阿里云百炼、智谱、vLLM 等，取 ``true`` / ``false``
- ``thinking`` + ``budget_tokens``：Claude 风格，按 token 预算控制思考长度
- ``off``：不发送任何推理参数（最保守，兼容一切服务）
- ``auto``：按模型 ID / 显示名猜一个；猜不出来等价于 ``off``

猜错也不会坏事：服务端若拒绝该参数（400 unknown parameter 之类），
``openai_client`` 会撤掉参数重试一次并记住，用户侧只在「思考」里看到一行说明。
"""

from __future__ import annotations

import re

REASONING_MODES = (
    "auto",
    "reasoning_effort",
    "sensenova_effort",
    "enable_thinking",
    "thinking_budget",
    "off",
)

MODE_LABELS: dict[str, str] = {
    "auto": "自动（按模型名判断）",
    "reasoning_effort": "reasoning_effort（o 系列 / GPT-5）",
    "sensenova_effort": "reasoning_effort（商汤：另有 xhigh / none）",
    "enable_thinking": "enable_thinking（百炼 / 智谱 / vLLM）",
    "thinking_budget": "thinking.budget_tokens（Claude 风格）",
    "off": "不发送（兼容性最好）",
}

# 应用内的推理档位（界面下拉用它）
VALID_EFFORTS = ("none", "low", "medium", "high", "max")

# 档位 → 各家要发的取值。
# 商汤实测：reasoning_effort 只接受 low / medium / high / xhigh / none，
# 文档里写的 max 会被 400 拒绝，所以这里映射成 xhigh；
# 它同时接受 temperature，不需要像 o 系列那样省略。
EFFORT_VALUES: dict[str, dict[str, object]] = {
    "reasoning_effort": {
        "none": None, "low": "low", "medium": "medium", "high": "high", "max": "high",
    },
    "sensenova_effort": {
        "none": "none", "low": "low", "medium": "medium", "high": "high", "max": "xhigh",
    },
    "enable_thinking": {
        "none": False, "low": True, "medium": True, "high": True, "max": True,
    },
    "thinking_budget": {
        "none": None, "low": 1024, "medium": 4096, "high": 16384, "max": 32768,
    },
}

# 认得出「这是推理模型」的特征（用于 auto 模式）。
# 这些是足够长的词，做子串匹配不会误伤；短名字（o1/o3/gpt-5）另用词边界匹配，
# 否则 cogito3 这类普通模型名会被误判成推理模型。
REASONING_HINTS = (
    "reasoner", "reasoning", "deepseek-r1", "glm-z1", "qwq",
    "magistral", "thinking", "qwen3",
)
REASONING_TOKEN = re.compile(r"(?:^|[\s\-_/])(?:o[134]|gpt-?5)(?:$|[\s\-_/])")
# 走「商汤口径」的模型（多一个 xhigh，且有真正关闭推理的 none）
SENSENOVA_HINTS = ("sensenova", "sense-nova", "sensenova.cn", "日日新")

# 服务端「不认识这个参数」的典型说法
UNKNOWN_PARAM_MARKERS = (
    "unknown parameter", "unexpected keyword", "unrecognized",
    "unsupported parameter", "unknown field", "extra fields not permitted",
    "no such field", "未知参数", "不认识的参数", "无法识别",
)
# 「拒绝」类措辞：单独不够，必须同时提到推理相关词才认定是在拒绝推理参数
REJECT_MARKERS = (
    "not supported", "unsupported", "invalid parameter", "invalid value",
    "不支持", "非法", "无效", "不是有效",
)
# 推理相关词（用于确认服务端抱怨的确实是推理参数）
REASONING_WORDS = ("reasoning", "thinking", "effort", "budget", "推理", "思考")


def normalise_effort(effort: str) -> str:
    """把推理强度归一化为 low / medium / high。"""
    value = (effort or "").strip().lower()
    return value if value in VALID_EFFORTS else "medium"


def resolve_mode(mode: str, model_id: str = "", name: str = "") -> str:
    """把模式解析成具体字段名；``auto`` 识别不出就返回 ``off``（不发参数）。"""
    value = (mode or "").strip().lower()
    if value in REASONING_MODES and value != "auto":
        return value
    text = f"{model_id} {name}".strip().lower()
    if not text:
        return "off"
    if any(hint in text for hint in SENSENOVA_HINTS):
        return "sensenova_effort"      # 同为 reasoning_effort，但多 xhigh/none
    if any(hint in text for hint in REASONING_HINTS) or REASONING_TOKEN.search(text):
        return "reasoning_effort"
    return "off"


def reasoning_payload(
    mode: str, effort: str = "medium", model_id: str = "", name: str = ""
) -> dict:
    """构造要合并进请求体的推理参数字典（不需要发送时返回空字典）。"""
    field = resolve_mode(mode, model_id, name)
    level = normalise_effort(effort)
    values = EFFORT_VALUES.get(field)
    if not values:
        return {}
    value = values.get(level)
    if value is None:
        # 「关闭」档在不支持 none 的服务商上 = 不发这个参数（用服务端默认）。
        # 注意不能写 `value == 0`：False == 0，会把 enable_thinking=false 误判成不发。
        return {}
    if field == "thinking_budget":
        return {"thinking": {"type": "enabled", "budget_tokens": value}}
    if field == "enable_thinking":
        return {"enable_thinking": bool(value)}
    # reasoning_effort 与 sensenova_effort 都是往 chat 接口写 reasoning_effort
    return {"reasoning_effort": str(value)}


def is_unsupported_error(text: str) -> bool:
    """服务端是否在抱怨「不认识这个（推理）参数」（用于撤掉推理参数重试）。

    必须同时提到推理相关词，避免把 ``unknown parameter: temperature``、
    ``model not supported`` 这类无关 400 也当成推理参数问题处理。
    """
    lowered = (text or "").lower()
    if not any(word in lowered for word in REASONING_WORDS):
        return False
    return any(marker in lowered for marker in UNKNOWN_PARAM_MARKERS) or any(
        marker in lowered for marker in REJECT_MARKERS
    )


def mode_label(mode: str) -> str:
    return MODE_LABELS.get((mode or "").strip().lower(), MODE_LABELS["auto"])
