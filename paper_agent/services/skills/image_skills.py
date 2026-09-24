"""文生图 / 图生图 Skill：让智能体在执行任务的过程中主动生成配图。

「定妆图 / 角色照」这类**要长得像某个人**的活儿，必须走**图生图**：只给文字描述的话，
模型是按描述重画一张脸（实测：全是文字时出来的人和用户传的照片**不是一个人**）。
所以这里除了让模型自己填 ``reference_image``，还做了兜底 —— 传了照片又在说人物 /
形象 / 定妆，就自动把那张照片挂上当参考图（见 :meth:`GenerateImageTool.run`）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Sequence

from paper_agent.core.constants import ARTIFACTS_DIR
from paper_agent.services import session_artifacts
from paper_agent.services.image_client import IMAGE_MIME, image_data_url
from paper_agent.services.skills.base import Tool, ToolParam, ToolResult
from paper_agent.services.skills.image_prompt import (
    IMAGE_GUIDE,
    PROMPT_PARAM_HINT,
    REFERENCE_PARAM_HINT,
)

# 自动兜底时最多用几张（多了提示词会被冲淡，请求体也大）
MAX_AUTO_REFERENCES = 2
# 提示词里有这些词，说明在说「人 / 形象」—— 这种才把用户上传的照片挂上当参考图。
# 只传照片而提示词说的是「海报 / 流程图 / 风景」时不要挂（那会把无关照片掺进去）。
SUBJECT_HINTS = (
    "定妆", "角色", "人物", "人像", "肖像", "主角", "主人公",
    "这个人", "照片里", "本人", "五官", "长相", "形象", "外貌", "脸",
)
# reference_image 允许一次给多张（「这两个人各来一张」），按这些符号分开
SPLIT_RE = re.compile(r"[,，;；、\s]+")


class GenerateImageTool(Tool):
    """按提示词生成图片，也可以基于参考图改造（图生图）。

    Args:
        generator: 形如 ``ImageClient.generate`` 的可调用对象，
            接收提示词与 ``reference_images``、返回 ``[{"name", "path"}, ...]``；
            为 None 时工具不可用。
        session_files: 本会话工作区文件清单，用来把 ``reference_image`` 里的
            文件名解析成本地图片（模型只会给名字，不会给绝对路径）。
        images_provider: 本轮用户上传的图片（已经是 ``data:`` 地址）。用于**兜底**：
            模型忘了填 ``reference_image``、但提示词明显在说人物形象时自动挂上，
            免得「传了照片却还是文生图」。
        extra_dirs: 额外的「按名字找参考图」目录。编程模式传会话工程目录：用户上传的图
            会被复制进去，不带上它按文件名给 ``reference_image`` 就会「没找到参考图」。
    """

    name = "generate_image"
    description = (
        "根据提示词生成配图（文生图）。需要插图、示意图、封面图或概念图时调用；"
        "提示词请具体描述画面主体、风格、构图与光线。"
        "\n需要**图生图**时用 reference_image 指定参考图：适用于「把这张图改成 XX 风格」"
        "「保持构图重画」「在原有配图基础上调整」，提示词里写清要保留什么、要改什么。"
        "\n**人物 / 定妆类照下面的规范来**（用户不用每次叮嘱）：\n" + IMAGE_GUIDE
    )

    def __init__(
        self,
        generator: Callable[..., list[dict[str, Any]]] | None = None,
        session_files: list[dict] | None = None,
        images_provider: Callable[[], list[str]] | None = None,
        extra_dirs: Sequence[str] | None = None,
    ) -> None:
        self._generator = generator
        self._session_files = session_files or []
        self._images_provider = images_provider
        # 额外的「按名字找参考图」目录（编程模式传会话工程目录）：用户上传的图与模型
        # 自己写进工作区的图都不在 data/artifacts 里，不带上就会「没找到参考图」
        self._extra_dirs = [str(item) for item in (extra_dirs or ()) if str(item or "").strip()]
        self.parameters = [
            ToolParam("prompt", "string", PROMPT_PARAM_HINT),
            ToolParam("filename_hint", "string", "可选：期望的文件名", required=False),
            ToolParam("reference_image", "string", REFERENCE_PARAM_HINT, required=False),
        ]

    # ------------------------------------------------------------------ 解析
    @staticmethod
    def _split(value: str) -> list[str]:
        """``reference_image`` 允许一次给多张：「a.png, b.png」→ ``["a.png", "b.png"]``。

        但 **URL / data 链接不拆**：``data:image/png;base64,xxxx`` 自己就带逗号，
        按逗号切开就废了（而且这种整串本来就是一张图）。
        """
        raw = (value or "").strip()
        if not raw:
            return []
        if raw.lower().startswith(("http://", "https://", "data:")):
            return [raw]
        return [item for item in SPLIT_RE.split(raw) if item]

    def _auto_references(self, prompt: str) -> list[str]:
        """用户传了照片 + 提示词在说人物 → 把照片挂上当参考图（图生图）。

        只在「有本轮上传的图」且「提示词像是在说人 / 形象」时才挂：
        只传了照片却要画流程图 / 海报 / 风景时挂上去，等于把无关照片掺进画面。
        """
        if self._images_provider is None:
            return []
        if not any(hint in prompt for hint in SUBJECT_HINTS):
            return []
        try:
            uploaded = [item for item in (self._images_provider() or []) if item]
        except Exception:      # noqa: BLE001 - 兜底失败就退回文生图，别打断出图
            return []
        return uploaded[:MAX_AUTO_REFERENCES]

    def _resolve_reference(self, value: str) -> str:
        """把参考图解析成接口认识的地址：http(s) 原样用，本地图片内联成 data URI。"""
        raw = (value or "").strip().strip('"').strip("'")
        if not raw:
            return ""
        if raw.startswith(("http://", "https://", "data:")):
            return raw
        direct = Path(raw)
        if direct.is_file():
            return image_data_url(direct)

        target = Path(raw).name.lower()
        stem = Path(target).stem.lower()
        # 候选只来自**本会话**（清单 + 本会话产物目录 + 根目录旧文件 + 会话工程目录）：
        # 配图的名字都是 `配图-时间戳-1.png`，扫别的会话目录就会拿错图
        candidates = [
            *session_artifacts.candidates(self._session_files, base=ARTIFACTS_DIR),
            *session_artifacts.extra_paths(self._extra_dirs),
        ]

        images = [
            path for path in candidates if Path(path).suffix.lower() in IMAGE_MIME
        ]
        for path in images:      # 先按全名（含或不含后缀），再按名字片段
            if Path(path).name.lower() in (target, f"{stem}{Path(path).suffix.lower()}"):
                return image_data_url(path)
        for path in images:
            if stem and stem in Path(path).stem.lower():
                return image_data_url(path)
        return ""

    # ------------------------------------------------------------------ 执行
    def run(
        self,
        prompt: str = "",
        filename_hint: str = "",
        reference_image: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        if self._generator is None:
            return ToolResult(success=False, error="未配置文生图模型")
        text = (prompt or "").strip()
        if not text:
            return ToolResult(success=False, error="图片提示词为空")

        references: list[str] = []
        note = ""
        for name in self._split(reference_image):
            resolved = self._resolve_reference(name)
            if not resolved:
                return ToolResult(
                    success=False,
                    error=(
                        f"没找到参考图「{name}」。请给图片 URL、本地路径，"
                        "或本会话文件清单里准确的图片文件名"
                        "（先列一遍本会话文件：list_artifacts / list_files）。"
                        + session_artifacts.name_hint(
                            self._session_files, IMAGE_MIME, self._extra_dirs,
                            base=ARTIFACTS_DIR,
                        )
                    ),
                )
            references.append(resolved)
        if references:
            note = "（图生图，已带上参考图）"

        if not references:
            # 兜底：模型忘了填 reference_image，但用户明明传了照片、又明显在说人物 ——
            # 这种情况走文生图出来的是另一张脸（实测就是这样），所以替它挂上。
            auto = self._auto_references(text)
            if auto:
                references = auto
                note = "（图生图，已自动带上你上传的照片作为形象参考）"

        try:
            # 只有真要图生图时才多传一个参数：纯文生图保持原来的调用形态
            items = (
                self._generator(text, reference_images=references)
                if references
                else self._generator(text)
            )
        except Exception as exc:      # noqa: BLE001 - 统一回传给模型
            return ToolResult(success=False, error=str(exc))
        if not items:
            return ToolResult(success=False, error="文生图接口未返回图片")

        names = "、".join(str(item.get("name", "")) for item in items)
        return ToolResult(
            content=f"已生成 {len(items)} 张配图{note}：{names}",
            artifact_paths=[str(item["path"]) for item in items if item.get("path")],
        )
