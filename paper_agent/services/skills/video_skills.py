"""文生视频 Skill：让智能体在执行任务的过程中主动生成视频。

时长超过单段上限（12 秒）时会**自动分段续接**再合成一条长视频，见
:mod:`paper_agent.services.video_long`：每段一出片就先挂到聊天里（用户能立刻看到内容、
不对就停），全部出完后才多一条合成好的长视频。

这里还负责把模型给的「素材」翻译成接口认的形式 —— 模型手里只有文件名，不会读文件、
更贴不出 Base64，而这活本来就不该它干：

* 首尾帧给**本地图片文件名** → 读出字节内联成 ``data:`` 地址；
* 首尾帧给**上一段视频的文件名** → 取那段末帧（模型经常这么用，语义上也对）；
* ``continue_from`` 给视频文件名 → 同上；
* 已有几段要拼成长片 → :class:`MergeVideosTool`（不重新生成、不花额度）。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from paper_agent.core.constants import ARTIFACTS_DIR
from paper_agent.services import session_artifacts
from paper_agent.services.background_assets import is_cancelled
from paper_agent.services.video_client import (
    ASPECT_OPTIONS,
    SECOND_OPTIONS,
    new_artifact_path,
)
from paper_agent.services.video_frames import (
    image_data_url,
    last_frame_data_url,
    missing_ffmpeg_hint,
    sample_frames,
)
from paper_agent.services.video_long import (
    CONTINUITY_CHARACTER,
    CONTINUITY_CHOICES,
    CONTINUITY_SEAMLESS,
    MAX_REFERENCE_IMAGES,
    MAX_SEGMENT,
    MAX_TOTAL_SECONDS,
    clamp_total,
    generate_segments,
)
from paper_agent.services.video_merge import VideoMergeError, ffmpeg_exe, merge_videos
from paper_agent.services.skills.base import Tool, ToolParam, ToolResult
from paper_agent.services.skills.video_prompt import PROMPT_GUIDE, SHOTS_GUIDE

VIDEO_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv", ".m4v")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")

# 供应商只认这几档时长（见 video_client.SECOND_OPTIONS）。写进工具说明是为了让模型
# **别填 11 这种数**：填了会被就近上调到 12，而它自己以为「我要的是 11 秒」。
_SECOND_STEPS = " / ".join(f"{item} 秒" for item in SECOND_OPTIONS)


# anchor 允许一次给多个（人物正脸 + 场景 + 服装细节），按这些符号分开；
# 但 URL / data 链接自己就带逗号，不拆（见 _split_anchor）。
ANCHOR_SPLIT_RE = re.compile(r"[,，;；、\s]+")


def _split_anchor(value: str) -> list[str]:
    """把 anchor 串拆成多个文件名；URL / data 链接整串算一个。"""
    raw = (value or "").strip()
    if not raw:
        return []
    if raw.lower().startswith(("http://", "https://", "data:")):
        return [raw]
    return [item for item in ANCHOR_SPLIT_RE.split(raw) if item]


def session_paths(
    session_files: Sequence[dict] | None,
    extra_dirs: Sequence[str] | None = None,
) -> list[str]:
    """可用于解析的本地文件：本会话清单 + 本会话产物目录 / 根目录旧文件 + 会话工程目录。

    **不进别的会话目录** —— 视频名字都长成 ``视频-20260923-144501.mp4``，
    一起扫就会把别的会话刚生成的片子当成自己的（用户报的「串文件」）。
    """
    return [
        *session_artifacts.candidates(session_files, base=ARTIFACTS_DIR),
        *session_artifacts.extra_paths(extra_dirs),
    ]


def available_names(
    session_files: Sequence[dict] | None,
    suffixes: Sequence[str],
    extra_dirs: Sequence[str] | None = None,
    limit: int = 6,
) -> list[str]:
    """本会话里**确实存在**的、符合后缀的文件名（报错时列给模型看）。"""
    return session_artifacts.available_names(
        session_files, suffixes, extra_dirs, base=ARTIFACTS_DIR, limit=limit
    )


def name_hint(
    session_files: Sequence[dict] | None,
    suffixes: Sequence[str],
    extra_dirs: Sequence[str] | None = None,
) -> str:
    """报错时附一句「本会话现在能用的文件：a.png、b.png…」（见 session_artifacts）。"""
    return session_artifacts.name_hint(
        session_files, suffixes, extra_dirs, base=ARTIFACTS_DIR
    )


def resolve_asset(
    name: str,
    session_files: Sequence[dict] | None,
    suffixes: Sequence[str],
    extra_dirs: Sequence[str] | None = None,
) -> str:
    """把模型给的文件名解析成本地路径（全名 / 不带后缀 / 完整路径 / 名字的一部分都认）。

    模型只会给名字（如 ``视频-20260922-233354.mp4``），有时还会漏后缀，所以这里
    既做精确匹配也做模糊兜底；找不到返回空串，由调用方给出可操作的报错。
    """
    raw = (name or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    if Path(raw).is_file():
        return raw
    target = Path(raw).name.lower()
    stem = Path(target).stem.lower()
    candidates = session_paths(session_files, extra_dirs)
    for suffix in ("", *suffixes):
        wanted = f"{stem}{suffix}"
        for path in candidates:
            if Path(path).name.lower() == wanted:
                return path
    for path in candidates:      # 兜底：模型可能只给了名字的一部分
        item = Path(path)
        if item.suffix.lower() not in suffixes:
            continue
        if stem and (stem in item.stem.lower() or item.stem.lower() in stem):
            return path
    return ""


def timeline_notes(outcome: dict, storyboard: Sequence[str]) -> str:
    """把「这次时间线是怎么处理的」如实写进结果 —— 别让用户猜内容去哪了。

    踩过的坑：``≤12`` 秒 + 3 条分镜时旧代码只发第 1 条、后两条**静默丢掉**，结果备注只
    说「不足的沿用了最后一条」，用户和模型都看不出内容被砍（会话 ``5bb3daf924d8``）。
    现在该说的一句都不省：合并了几条、切了几段、哪几秒没写内容。
    """
    meta = outcome.get("meta") or {}
    count = int(meta.get("count") or 0)
    shots = int(meta.get("shots") or 0)
    merged = int(meta.get("merged") or 0)
    written = float(meta.get("timeline_end") or 0)
    total = float(meta.get("total") or 0)
    requested = float(meta.get("requested") or 0)
    notes: list[str] = []

    # 档位被上调（11 → 12）：必须说，不然用户会以为「我要 11 秒怎么给了 12 秒」
    if requested and total and abs(total - requested) >= 1:
        notes.append(
            f"{requested:.0f} 秒不在供应商档位（4/5/6/8/10/12）里，已按 {total:.0f} 秒生成"
        )
    if merged and count <= 1:
        notes.append(
            f"{shots} 条分镜已合成一条时间线发出去（约 {total:.0f} 秒是一整段，内容没丢）"
        )
    elif merged:
        notes.append(f"多出来的 {merged} 条分镜并进了最后一段（内容没丢）")
    if written and total and written < total - 1:
        notes.append(
            f"你的时间线只写到 {written:.0f} 秒，后面约 {total - written:.0f} 秒没写画面 —— "
            "已补一句「把剩下的时间往下演」，否则那几秒会原地重演"
        )
    if written and total and written > total + 1:
        notes.append(
            f"你写的时间线到 {written:.0f} 秒，但时长是 {total:.0f} 秒（多出的部分会被压进这一段）"
        )
    if storyboard and count > 1 and len(storyboard) < count:
        notes.append(
            f"你给了 {len(storyboard)} 条分镜、实际分了 {count} 段（每段≤{MAX_SEGMENT} 秒），"
            "不足的沿用了最后一条"
        )
    if not notes:
        return ""
    return "\n（" + "；".join(notes) + "）"


class GenerateVideoTool(Tool):
    """按提示词生成视频（提交任务 → 轮询 → 落盘）。

    Args:
        generator: 形如 ``VideoClient.generate`` 的可调用对象，
            接收提示词、返回 ``[{"name", "path", "kind"}, ...]``；为 None 时工具不可用。
        images_provider: 本轮的图片素材（图生视频用，返回图片地址列表）。
        session_files: 本会话工作区文件清单，用来把 ``continue_from`` 里的
            文件名解析成本地视频（模型只会给名字，不会给绝对路径）。
        extra_dirs: 额外的「按名字找文件」目录。编程模式传会话工程目录：用户上传的图
            会被复制进去、模型自己也常把素材写在那里，不带上它就会「找不到参考图」
            （它们不在 ``data/artifacts`` 里）。

    模式（``mode``）不需要传：有首尾帧就是 keyframe（衔接上一段），
    有参考图片就是 reference（图生视频），只有文字就是 text。
    """

    name = "generate_video"
    description = (
        "根据提示词生成一段视频（文生视频 / 图生视频 / 首尾帧控制）。需要演示动画、"
        "场景片段、宣传短片或动态素材时调用；**按下面的规范写提示词**。"
        "出片要几十秒到几分钟，期间会一直等待，不要重复调用。"
        f"\n**要长视频就把 seconds 直接写大**（最长 {MAX_TOTAL_SECONDS} 秒）：超过 "
        f"{MAX_SEGMENT} 秒会**自动**拆成几段，并**自动锚定人物与场景** —— 先从第 1 段采几帧"
        "当「人物 / 场景基准」，之后每一段都挂上这几帧，所以人脸与场景前后一致、不会乱变。"
        f"**≤{MAX_SEGMENT} 秒就是一次生成、一段片子**（不会分段）。"
        "**分段、取样、衔接、拼接、按时间线切片全是客户端自动做的**，你不需要（也没法）"
        "自己抽帧或拼接，不要为此多次调用、更不要用 execute_python 去调 ffmpeg。"
        "分段会一段一段出现在聊天里，全部出完再补一条合成好的长视频。"
        "\n默认如此（段与段之间是硬切）；确实要「一镜到底」那种画面连续，传 "
        "`continuity=seamless`（靠上一段末帧接着拍，人物容易飘、不保证认得出）。"
        "\n**用户传了真人照片、要「主角就是这个人」时，直接 `anchor` 那张照片**（每段都挂它"
        "当基准）——**不要**先 generate_image 做一张「定妆图」再拿它去拍：每多一次生成就少"
        "一分像（图生图只是照提示词重画，实测两次都被判「不像本人」）。"
        "\n拍续集、要把两段接起来时，用 continue_from 给上一段视频的**文件名**即可"
        "（客户端自己取末帧当这一段的首帧）。"
        "\n首尾帧（first_frame / last_frame）只给**文件名或路径**（给视频就取它末帧），"
        "客户端会自己读文件、内联成接口要的格式；**绝对不要把 Base64 贴进来**。"
        "\n**怎么写提示词与分镜（照这个来，用户不用叮嘱）：**\n"
        + PROMPT_GUIDE
        + "\n**要长视频（或要明确切换画面）就用 shots 给时间线**（写法见 shots 参数）；"
        "也可以**把整片时间线直接写进 prompt**（`0-4秒：…`）—— 两种都行，客户端按实际分段切开。"
        "**每个时间段只写它那几秒的画面**，别把整片的画面复制到每一段里"
        "（要 30 秒却拿到三段重复内容就是这么来的）。"
        "\n**长视频用 subject 固定主角与场景**（如「穿白 T 恤的短发年轻男子 / 废弃高铁车厢」）："
        "每段原样带上，能明显收敛「换脸 / 换背景」；要写**可辨识的具体特征**，各段措辞也要一致"
        "（别一段叫「年轻男子」、另一段叫「少年」）。"
        "\n**每段的收尾镜头要能看清人物**：下一段的首帧就是这一段的最后一帧 —— 收在背影、"
        "远景或糊画面里，下一段只能自己编一张脸。"
        "\n已经有几段视频想拼成一条长片 → 用 merge_videos，别重新生成。"
    )

    def __init__(
        self,
        generator: Callable[..., list[dict[str, Any]]] | None = None,
        images_provider: Callable[[], list[str]] | None = None,
        session_files: list[dict] | None = None,
        progress: Any = None,
        extra_dirs: Sequence[str] | None = None,
    ) -> None:
        self._generator = generator
        self._images_provider = images_provider
        self._session_files = session_files or []
        self._progress = progress      # 进度桥：分段时写「第 i/N 段」+ 整体百分比
        # 额外的「按名字找文件」目录：编程模式传会话工程目录（用户上传的图与模型自己写的
        # 文件都在那里，不在 data/artifacts 里）
        self._extra_dirs = [str(item) for item in (extra_dirs or ()) if str(item or "").strip()]
        self._cancel: Any = None
        self._artifact_hook: Callable[[dict[str, Any]], None] | None = None
        self.parameters = [
            ToolParam(
                "prompt", "string",
                "视频内容描述。**照工具说明里的规范写**：五段式（主体 / 动作序列 / 环境光影 / "
                "镜头语言 / 风格），用整句自然语言；**从 0 起写成带时间戳、铺满总时长的时间线**"
                "（`0-4秒：… 4-8秒：…`）。**台词照常写**（「某某说：『台词』」，人物会真的开口"
                "说话），不要改成画外音或字幕",
            ),
            ToolParam(
                "seconds", "string",
                f"时长（秒）**只能是 {_SECOND_STEPS} 这几档**（供应商限制；填别的会被就近上调："
                f"7→8、9→10、11→12，并如实告知用户）——所以**别填 11 这种数**，想接近就填 12。"
                f"{MAX_SEGMENT + 1}–{MAX_TOTAL_SECONDS} 直接写总数（如 30），"
                f"客户端自动分段续拍并合成一条长视频。默认 \"5\"",
                required=False,
            ),
            # 用 enum 约束住：别的写法（"竖屏" / "2.39:1"）会被供应商拒或静默回落 16:9，
            # 用户选的是竖屏却拿到横屏 —— 和秒数落回 5 秒是同一类「静默改需求」
            ToolParam("aspect_ratio", "string",
                      "画幅（**只能填这几个值**）：16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9，默认 16:9",
                      required=False, enum=list(ASPECT_OPTIONS)),
            ToolParam(
                "seed", "integer",
                "随机种子（可选）。**别指望它复现**：实测供应商不认这个字段（同一颗种子两次"
                "结果并不一致），传了只是原样发过去",
                required=False,
            ),
            ToolParam(
                "anchor", "string",
                "人物 / 场景的**基准图**（长视频建议给）：本地图片文件名，或某段视频的文件名"
                "（给视频会从它上面采几帧）。**一次可以给多张**（逗号分开，最多 5 张）："
                "如「主角正脸.png, 场景.jpg」—— 人物、场景、服装各来一张，比只给一张更稳。"
                "给了它，每一段都会挂上它当参考 —— 人物与场景不乱变；不给也行，客户端会"
                "自动拿第 1 段的画面当基准",
                required=False,
            ),
            ToolParam(
                "continuity", "string",
                "长视频怎么接：`character`（默认）每段挂参考图、人物与场景稳（段间是硬切）；"
                "`seamless` 首尾帧链、画面真连续但人物容易飘。默认 character",
                required=False,
                enum=list(CONTINUITY_CHOICES),
            ),
            ToolParam(
                "shots", "array",
                "整片时间线 / 分镜（可选，长视频建议传；也可以直接把时间线写进 prompt）："
                "每条带自己的**时间戳**秒数范围，合起来是整片时间线。"
                + SHOTS_GUIDE
                + "。不传则几段共用同一条提示词，容易三段落一个样（客户端会自动补"
                "「叙事阶段」和「衔接到下一段」的指令，但不如时间线精确）",
                required=False,
                items="string",
            ),
            ToolParam(
                "subject", "string",
                "主角与场景的**固定措辞**（长视频建议传）：如「穿白 T 恤的短发年轻男子，"
                "废弃高铁车厢」。每段都会原样带上，能明显收敛「后面几段主角换脸 / 背景对不上」。"
                "**要写可辨识的具体特征**（发色发型、痣/疤、眼镜、衣服的颜色与图案）——"
                "「年轻男子」这种泛泛描述几乎没约束力，越具体越稳",
                required=False,
            ),
            ToolParam(
                "continue_from", "string",
                "接着哪一段视频往下拍：填上一段视频的**文件名**（如 视频-20260922-033106.mp4）。"
                "客户端会自己取它的末帧作为本段首帧，保证前后画面连贯",
                required=False,
            ),
            ToolParam(
                "first_frame", "string",
                "起始画面：给**文件名 / 路径**即可（本地图片，或某段视频的文件名——给视频"
                "就取它的末帧）；也可给 http(s) 链接。客户端自己读文件内联，不要贴 Base64",
                required=False,
            ),
            ToolParam(
                "last_frame", "string",
                "收尾画面：同样是文件名 / 路径（或 http(s) 链接），限定这一段收在哪个画面",
                required=False,
            ),
        ]

    # ------------------------------------------------------------------ 取消 / 实时产物
    def set_cancel(self, cancel: Any) -> None:
        """Esc 要能停下长视频：5 段跑下来十几分钟，中途必须能中断。"""
        self._cancel = cancel

    def set_artifact_hook(self, hook: Any) -> None:
        """分段出片时「出一段挂一段」，不用等整轮跑完才看到东西。"""
        self._artifact_hook = hook

    def _stopped(self) -> bool:
        return is_cancelled(self._cancel)

    def _publish(self, item: dict[str, Any], index: int, count: int) -> None:
        """把刚出的一段挂到聊天里。"""
        if self._artifact_hook is None:
            return
        try:
            self._artifact_hook(
                {
                    "name": item.get("name") or Path(str(item.get("path", ""))).name,
                    "path": str(item.get("path", "")),
                    "kind": "video",
                }
            )
        except Exception:      # noqa: BLE001 - 挂卡片失败不该影响出片
            return None

    # ------------------------------------------------------------------ 解析
    def _candidates(self) -> list[str]:
        return session_paths(self._session_files, self._extra_dirs)

    def _resolve_video(self, name: str) -> str:
        """把「上一段视频」的名字解析成本地文件路径（全名 / 不带后缀 / 完整路径都认）。"""
        return resolve_asset(name, self._session_files, VIDEO_SUFFIXES, self._extra_dirs)

    def _video_hint(self) -> str:
        return name_hint(self._session_files, VIDEO_SUFFIXES, self._extra_dirs)

    def _anchor_frames(self, value: str) -> tuple[list[str], str]:
        """把「人物 / 场景基准」参数变成参考图列表，返回 ``(图列表, 说明)``。

        **可以一次给多个**（逗号 / 空格 / 顿号分开）：人物正脸一张 + 场景一张 +
        服装细节一张 —— 供应商一轮最多收 ``MAX_REFERENCE_IMAGES``（5）张，按它截断。
        每一项都能是：本地**图片**（那一张就是基准）、本地**视频**（从它上面采几帧 ——
        一帧常常正好是背影，几帧才覆盖得到正脸与场景）、http(s) / data 地址。
        """
        raw = (value or "").strip().strip('"').strip("'")
        if not raw:
            return [], ""
        names = _split_anchor(raw)
        frames: list[str] = []
        labels: list[str] = []
        kinds: list[str] = []
        for name in names:
            items, label, kind = self._one_anchor(name)
            if not items:
                # 有一个认不出来就明确报错：悄悄少给基准，出来的片子会莫名其妙地不像
                if kind == "missing":
                    return [], (
                        f"没找到基准图 / 基准视频「{label}」。可以给本会话已有的图片或视频"
                        "**文件名**（先列一遍本会话文件：list_artifacts / list_files）、"
                        "绝对路径，或 http(s) 链接。"
                        + name_hint(
                            self._session_files,
                            (*IMAGE_SUFFIXES, *VIDEO_SUFFIXES),
                            self._extra_dirs,
                        )
                    )
                if kind == "video":
                    return [], (
                        f"没能从 {label} 采到基准帧。"
                        + ("" if ffmpeg_exe() else missing_ffmpeg_hint())
                    )
                return [], f"基准图「{label}」读不出来（文件损坏或格式不支持）"
            frames.extend(items)
            labels.append(label)
            kinds.append(kind)

        frames = frames[:MAX_REFERENCE_IMAGES]
        if len(names) == 1:
            if kinds[0] == "image":
                note = f"（已把 {labels[0]} 作为人物 / 场景基准）"
            elif kinds[0] == "video":
                note = f"（已从 {labels[0]} 采 {len(frames)} 帧作为人物 / 场景基准）"
            else:
                note = ""
        else:
            note = (
                f"（已用 {len(frames)} 张人物 / 场景基准图：{'、'.join(labels)}）"
            )
        return frames, note

    def _one_anchor(self, name: str) -> tuple[list[str], str, str]:
        """解析一项 anchor，返回 ``(图列表, 说明用名字, 类型)``；类型见下面的字面量。"""
        lowered = name.lower()
        if lowered.startswith(("http://", "https://", "data:")):
            return [name], name, "url"

        image = resolve_asset(name, self._session_files, IMAGE_SUFFIXES, self._extra_dirs)
        if image:
            inlined = image_data_url(image)
            return ([inlined], Path(image).name, "image") if inlined else ([], Path(image).name, "broken")

        video = resolve_asset(name, self._session_files, VIDEO_SUFFIXES, self._extra_dirs)
        if video:
            frames = sample_frames(video)
            return frames, Path(video).name, "video"

        return [], name, "missing"

    def _inline_frame(self, value: str, *, role: str) -> tuple[str, str]:
        """把首/尾帧参数翻译成接口认的地址，返回 ``(地址, 说明)``。

        模型给的东西五花八门：http 链接、data URL、本地图片路径，甚至**上一段视频的
        文件名**（它以为「首帧」就是取那段视频的末帧）。这活本来就不该它干 ——
        它读不了文件、更贴不出 Base64（一张 720P 的帧十几万字符，参数装不下），
        而供应商对本地路径是直接回 400 的。所以统一在这里翻译：

        * ``http(s)://`` / ``data:`` → 原样用；
        * 本地**图片** → 读出字节内联成 data URL；
        * 本地**视频** → 取它的末帧内联。

        认不出来返回 ``("", 原因)``，由调用方报错 —— 绝不把本地路径原样发出去。
        """
        raw = (value or "").strip().strip('"').strip("'")
        if not raw:
            return "", ""
        lowered = raw.lower()
        if lowered.startswith(("http://", "https://", "data:")):
            return raw, ""

        image = resolve_asset(raw, self._session_files, IMAGE_SUFFIXES, self._extra_dirs)
        if image:
            inlined = image_data_url(image)
            if inlined:
                return inlined, f"（已把本地图片 {Path(image).name} 内联为接口可用的地址）"
            return "", f"{role}「{raw}」这张图读不出来（文件损坏或格式不支持）"

        video = resolve_asset(raw, self._session_files, VIDEO_SUFFIXES, self._extra_dirs)
        if video:
            frame = last_frame_data_url(video)
            if frame:
                return frame, f"（已取 {Path(video).name} 的末帧作为{role}）"
            return "", (
                f"没能从 {Path(video).name} 里取到{role}帧。"
                + ("" if ffmpeg_exe() else missing_ffmpeg_hint())
            )

        return "", (
            f"没找到{role}帧「{raw}」。可以给：本会话已有的图片 / 视频**文件名**"
            "（先列一遍本会话文件：list_artifacts / list_files）、绝对路径，或 http(s) 链接。"
            + name_hint(
                self._session_files, (*IMAGE_SUFFIXES, *VIDEO_SUFFIXES), self._extra_dirs
            )
        )

    # ------------------------------------------------------------------ 执行
    def run(
        self,
        prompt: str = "",
        seconds: str = "",
        aspect_ratio: str = "",
        seed: Any = None,
        shots: Any = None,
        subject: str = "",
        anchor: str = "",
        continuity: str = "",
        continue_from: str = "",
        first_frame: str = "",
        last_frame: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        if self._generator is None:
            return ToolResult(success=False, error="未配置文生视频模型")
        text = (prompt or "").strip()
        if not text:
            # 模型有时把整条时间线只写在 shots 里、prompt 留空 —— 这也算给了内容。
            # （以前这里直接报「提示词为空」，时间线还没机会被用上就退回去了）
            text = "\n\n".join(
                shot for item in _as_list(shots) if (shot := _shot_text(item))
            )
        if not text:
            return ToolResult(
                success=False, error="视频提示词为空（prompt 与 shots 至少给一个）"
            )

        first = (first_frame or "").strip()
        last = (last_frame or "").strip()
        note = ""
        source = ""
        anchor_frames: list[str] = []
        wanted = clamp_total(seconds) or 5
        # 注意别和下面的「视频模式」文案变量重名（那个 mode 是 文生视频/图生视频…）
        continuity_mode = (continuity or "").strip() or CONTINUITY_CHARACTER
        if continuity_mode not in CONTINUITY_CHOICES:
            continuity_mode = CONTINUITY_CHARACTER

        if (anchor or "").strip():
            anchor_frames, extra = self._anchor_frames(anchor)
            if not anchor_frames:
                return ToolResult(success=False, error=extra)
            note += extra

        if (continue_from or "").strip():
            source = self._resolve_video(continue_from)
            if not source:
                return ToolResult(
                    success=False,
                    error=(
                        f"没找到上一段视频「{continue_from}」。先看一遍本会话的文件清单"
                        "（list_artifacts / list_files）再用准确的文件名重试。"
                        + self._video_hint()
                    ),
                )
            frame = last_frame_data_url(source)
            if not frame:
                return ToolResult(
                    success=False,
                    error=(
                        f"没能从 {Path(source).name} 里取到末帧，无法衔接。"
                        + ("" if ffmpeg_exe() else missing_ffmpeg_hint())
                    ),
                )
            first = frame
            note = f"（已取上一段 {Path(source).name} 的末帧作为首帧，画面接得上）"
            if (
                continuity_mode == CONTINUITY_CHARACTER
                and wanted > MAX_SEGMENT
                and not anchor_frames
            ):
                # 要接着它拍一条长视频：第一段从它的末帧起（动作接得上），
                # 后面每一段挂从它身上采的几帧当基准（人物与场景别乱变）
                anchor_frames = sample_frames(source)
                if anchor_frames:
                    note += f"（并从 {Path(source).name} 采 {len(anchor_frames)} 帧作为人物 / 场景基准）"

        # 首尾帧：模型只会给「文件名 / 路径 / 链接」，读文件与内联由我们代劳
        if first:
            first, extra = self._inline_frame(first, role="首")
            if not first:
                return ToolResult(success=False, error=extra)
            note += extra
        if last:
            last, extra = self._inline_frame(last, role="尾")
            if not last:
                return ToolResult(success=False, error=extra)
            note += extra

        # 图生视频：本轮用户带了图片就自动用上的（智能体不用自己判断）
        images = list(self._images_provider() or []) if self._images_provider else []
        try:
            value = int(seed) if seed not in (None, "") else None
        except (TypeError, ValueError):
            value = None

        # 注意别用 `text` 当海象变量 —— 它是上面那条 prompt，被覆盖掉就等于把模型写的
        # 风格 / 场景整段丢掉（曾经真的这样：同时给 prompt + shots 时，prompt 被换成了
        # 最后一条分镜，成片里「冷青绿调、35mm 胶片颗粒」这些全没了）
        storyboard = [shot for item in _as_list(shots) if (shot := _shot_text(item))]
        outcome = generate_segments(
            self._generator,
            text,
            seconds=wanted,
            aspect_ratio=aspect_ratio or "16:9",
            seed=value,
            images=images,
            first_frame=first,
            last_frame=last,
            progress=self._progress,
            on_segment=self._publish,
            shots=storyboard,
            subject=(subject or "").strip(),
            continuity=continuity_mode,
            anchor_frames=anchor_frames,
            is_stopped=self._stopped,
        )

        segments = list(outcome.get("segments") or [])
        merged = outcome.get("merged") or None
        paths = [str(item["path"]) for item in segments if item.get("path")]
        if merged:
            paths.append(str(merged["path"]))
        if outcome.get("error"):
            # 中途失败（比如取不到上一段末帧）：把已经出的段交出去，别白跑
            return ToolResult(
                success=False, error=str(outcome["error"]), artifact_paths=paths
            )
        if not segments:
            return ToolResult(success=False, error="文生视频接口未返回视频")

        if source:
            mode = f"续集（接 {Path(source).name} 的末帧）"
        elif first or last:
            mode = "首尾帧控制"
        elif images:
            mode = "图生视频"
        else:
            mode = "文生视频"

        names = "、".join(
            str(item.get("name") or Path(str(item.get("path", ""))).name)
            for item in segments
        )
        total = int(outcome.get("seconds") or wanted)
        plan = len(outcome.get("planned") or []) or len(segments)
        if len(segments) == 1:
            body = f"已生成 1 段视频（{mode}）：{names}"
        elif merged:
            body = (
                f"已生成 {len(segments)} 段视频并合成为一个约 {total} 秒的长视频"
                f"（{mode}）：{Path(str(merged['path'])).name}"
                f"\n分段（可单独查看，也已经逐段展示给用户了）：{names}"
            )
        else:
            body = (
                f"已生成 {len(segments)} 段视频（{mode}）：{names}"
                f"\n没能合成长视频：{outcome.get('merge_error') or '未知原因'}"
            )
        if len(segments) > 1:
            if str((outcome.get("meta") or {}).get("mode") or "") == "timeline":
                body += (
                    f"\n（已按你写的整片时间线切成 {len(segments)} 段：每段只拿到属于自己那几秒的"
                    "画面，时间戳已重算成这一段自己的 0 起点）"
                )
            elif storyboard:
                body += "\n（已按你给的分镜逐段生成）"
            else:
                body += (
                    "\n（没给分镜，几段共用同一条提示词——下次做长视频建议用 shots 给每段一条，"
                    "免得重复）"
                )
            if outcome.get("reference_count"):
                body += (
                    f"\n（每段都挂了 {outcome['reference_count']} 张人物 / 场景基准图："
                    "人物与场景前后一致，段与段之间是硬切）"
                )
            elif outcome.get("continuity") == CONTINUITY_SEAMLESS:
                body += "\n（首尾帧链：画面一段接一段，人物靠单帧锚）"
        # 时间线是怎么处理的，一句都不省（合并了几条 / 切了几段 / 哪几秒没写内容）
        body += timeline_notes(outcome, storyboard)
        if outcome.get("stopped"):
            body = f"用户中途停止，只出了 {len(segments)}/{plan} 段。{body}"
        elif merged and outcome.get("merge_error"):
            body += f"\n（合成时有告警：{outcome['merge_error']}）"
        return ToolResult(content=f"{body}{note}", artifact_paths=paths)


class MergeVideosTool(Tool):
    """把已有的几段视频按顺序拼成一条长视频 —— 不重新生成、不花额度。

    什么时候用：手上已经有几段成片（自己分段拍的、或早先几次调用留下的），
    只差把它们接起来。**别再让模型自己去调 ffmpeg**：它跑不了命令、也不该管这些，
    这里一条命令的事（``-c copy`` 无损；参数不一致时自动统一编码）。
    """

    name = "merge_videos"
    description = (
        "把已有的几段视频**按给定顺序**拼成一条长视频（不重新生成，不消耗视频额度）。"
        "手上已经有几段成片、只差接起来时用它；别为了拼接重新调用 generate_video。"
        "各段编码参数一致时无损拼接（很快），不一致会自动先统一编码再拼。"
    )

    def __init__(
        self,
        session_files: Sequence[dict] | None = None,
        extra_dirs: Sequence[str] | None = None,
    ) -> None:
        self._session_files = session_files or []
        # 与 GenerateVideoTool 同一个理由：编程模式下还要能在会话工程目录里按名字找
        self._extra_dirs = [str(item) for item in (extra_dirs or ()) if str(item or "").strip()]
        self.parameters = [
            ToolParam(
                "videos", "array",
                "要拼接的视频**文件名**列表，按播放顺序排（如 "
                "[\"视频-20260922-231931.mp4\", \"视频-20260922-232240.mp4\"]）；"
                "至少两段",
            ),
            ToolParam("name", "string", "产物文件名（可选，默认「长视频-时间戳-N秒.mp4」）",
                      required=False),
        ]

    def run(self, videos: Any = None, name: str = "", **kwargs: Any) -> ToolResult:
        names = [str(item).strip() for item in _as_list(videos) if str(item).strip()]
        if len(names) < 2:
            return ToolResult(
                success=False,
                error="至少要给两段视频才能拼接（videos 传文件名列表）",
            )

        paths: list[str] = []
        for item in names:
            resolved = resolve_asset(item, self._session_files, VIDEO_SUFFIXES, self._extra_dirs)
            if not resolved:
                return ToolResult(
                    success=False,
                    error=(
                        f"没找到视频「{item}」。先用 list_artifacts / list_files 看准本会话的"
                        "文件名，再用准确的文件名重试。"
                        + name_hint(self._session_files, VIDEO_SUFFIXES, self._extra_dirs)
                    ),
                )
            paths.append(resolved)

        if not ffmpeg_exe():
            return ToolResult(success=False, error=missing_ffmpeg_hint())

        seconds = int(round(sum(_duration(item) for item in paths)))
        stem = (name or "").strip()
        if stem:
            stem = Path(stem).name
            if not stem.lower().endswith(VIDEO_SUFFIXES):
                stem += ".mp4"
        else:
            stem = f"长视频-{time.strftime('%Y%m%d-%H%M%S')}-{seconds}秒.mp4"
        target = new_artifact_path(stem)

        try:
            info = merge_videos(paths, target)
        except VideoMergeError as exc:
            return ToolResult(
                success=False,
                error=f"拼接失败：{exc}",
                artifact_paths=paths,      # 分段本身是好的，交出去别白费
            )

        joined = "、".join(Path(item).name for item in paths)
        return ToolResult(
            content=(
                f"已把 {len(paths)} 段拼成一条约 {int(round(info.get('duration') or seconds))} 秒的"
                f"长视频：{target.name}"
                f"（{'无损拼接' if not info.get('reencoded') else '参数不一致，已统一编码后拼接'}）"
                f"\n来源：{joined}"
            ),
            artifact_paths=[str(target)],
        )


# 分镜条目里可能装正文的键名（模型各有习惯）
SHOT_TEXT_KEYS = ("prompt", "text", "description", "shot", "content", "desc")


def _shot_text(item: Any) -> str:
    """分镜条目 → 正文。

    模型经常把 ``shots`` 写成**对象数组**而不是字符串数组：

    .. code-block:: json

        shots = [{"prompt": "0-2秒：她沉睡…", "subject": "长直黑发…"}]

    直接 ``str(item)`` 会得到 ``{'prompt': '…', 'subject': '…'}`` 这种 Python 字典
    字面量，然后被原样发给视频模型 —— 开头一堆括号引号是噪音，主体还被重复写了两遍，
    指令被稀释（实测成片会出现「原地重演上一段」）。这里把正文取出来。

    条目里的 ``subject`` **不用**：那是模型自己给这一段写的（常和全局 subject 不一致，
    比如丧尸镜头的 subject 还抄着女主角），统一用调用方传的全局 subject。
    """
    if isinstance(item, dict):
        for key in SHOT_TEXT_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        # 键名都不认识：把值拼起来，总比 str(dict) 干净
        return "；".join(
            str(value).strip() for value in item.values() if str(value).strip()
        )
    return str(item or "").strip()


def _as_list(value: Any) -> list[Any]:
    """容错：模型有时把列表写成 JSON 字符串，或只给一个文件名。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            import json

            parsed = json.loads(text)
            if isinstance(parsed, list):
                return list(parsed)
        except ValueError:
            pass
    return [item.strip() for item in text.replace("，", ",").split(",") if item.strip()]


def _duration(path: str) -> float:
    from paper_agent.services.video_merge import probe_duration

    return probe_duration(path)
