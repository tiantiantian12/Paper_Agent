"""长视频：把「超过单段上限（12 秒）」的需求拆成多段、续接生成、再合成一条。

供应商单次最长只给 12 秒（``video_client.SECOND_OPTIONS``），要更长的只能这样拼：

    第 1 段（首帧 = 用户给的首帧）
      ↓ 取它的末帧
    第 2 段（首帧 = 第 1 段末帧）  ← 首尾帧衔接，画面接得上
      ↓ 取它的末帧
    第 3 段（尾帧 = 用户给的尾帧）
      ↓ ffmpeg concat
    长视频

为什么要**边出边给**：一段要一两分钟，几段下来十几分钟，中途什么都不显示用户会以为
卡死；更要紧的是 —— 先看到每段内容，发现不对可以立刻按 Esc 停掉，不用等整个长视频
跑完才发现白跑（合成的长视频是最后才有的）。

所以 :func:`generate_segments` 的协作方式是：

* ``on_segment(item, index, total)``：每段一出片就回调（界面立刻挂一张卡片）；
* ``progress``：进度回调对象，收到 ``\u7b2c i/N \u6bb5`` 文案 + **整体**百分比（跨段单调递增，
  不会每段开头掉回 0）；
* 返回里同时给出分段与合成结果 —— 合成失败不算整件事失败，分段照样能看。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Sequence

from paper_agent.services.skills import video_timeline
from paper_agent.services.skills.video_prompt import closing_block, handoff_block, pad_block
from paper_agent.services.video_client import MAX_TOTAL_SECONDS, new_artifact_path
from paper_agent.services.video_frames import last_frame_data_url, sample_frames
from paper_agent.services.video_merge import VideoMergeError, merge_videos

# 供应商允许的单段时长档位（与 video_client.SECOND_OPTIONS 一致）
SEGMENT_CHOICES = (12, 10, 8, 6, 5, 4)
MAX_SEGMENT = 12
MIN_SEGMENT = 4

# 只有一条提示词却要分几段时，给后面几段补的话。
#
# 为什么必须有它：每段是**独立生成**的 10 秒片子，如果几段收到同一句
# 「片头：废墟城市 + 快速剪辑 + 丧尸手伸出…」，那每段都会把整条片子的画面重演一遍
# —— 用户要 30 秒，拿到的却是同一段内容放三遍。模型给不出分镜时，这里至少推它一把。
# 用中文：与用户 / 模型写的提示词同一种语言 —— 工具说明、分镜规范、这些锚点是同一条链路上
# 的东西，不该只有锚点是英文（一屏两种语言，模型读起来要来回切，指令容易被稀释）。
CONTINUATION_HINT = (
    "\n\n这是同一条连续片子的第 {index}/{count} 个镜头。接着上一个镜头往下演："
    "保持同一个场景、同一批人物与同一种画面风格（这一段的首帧就是上一个镜头的末帧），"
    "把故事往前推进，不要重复上一个镜头里已经出现过的内容。"
)

# 「别换人、别换地方」这一段——用户反馈过「后面几段主角莫名其妙换脸了、背景也对不上」。
#
# 实情：接续只靠**一帧**做锚点（上一段末帧 → 下一段首帧），人物在 720P 里只占很小一块，
# 模型是拿这一帧重新"想象"人物的，脸和背景细节本来就会飘。我们改不了模型的conditioning，
# 但能把**文字锚点**补足：每段都明确点名同一个人/同一个场景，漂移会明显收敛。
# 更狠的一招是让调用方给 subject（主角与场景的固定措辞），那就每段原样带上。
IDENTITY_HINT = (
    "\n\n画面必须与上一个镜头完全一致：同一批人（脸、发型、年龄、体型与服装都一模一样）、"
    "同一个场景、同样的道具、光线与色调。不要引入新人物，不要更换场景，不要改变画面风格。"
)
SUBJECT_LINE = "\n\n固定的主体与场景（每一个镜头都必须与它完全相同）：{subject}"

# 把**前面几段演了什么**告诉这一段。
#
# 为什么必须有它：每段是独立生成的，视频模型只看得到「这一段的提示词 + 参考图」，
# 它并不知道前面几段发生了什么。光说「不要重复上一段」落不了地 —— 它得先知道
# 上一段演了什么，才知道该避开什么。实测用户反馈「下一段重复上一段的话和动作」，
# 根子就在这儿。摘要只取最近 3 段、每段截 120 字符，免得把提示词撑爆。
RECAP_LINE = (
    "\n\n前面几个镜头已经演过：{done}。"
    "\n这一个镜头紧接着那之后开始 —— 演接下来发生的事；"
    "不要把上面列出的动作、台词或镜头运动再演一遍。"
)
# 每段末尾的「衔接点 / 收尾」提示（规则见 services/skills/video_prompt.py）
# 没给分镜时（每段共用同一句提示词），用「叙事阶段」把几段岔开：
# 同一句话生成三次，出来的就是同一段画面放三遍。
PHASE_LINE = "\n\n故事位置：这是整条片子的{phase} —— 演出这个阶段的内容。"
PHASES = ("开头", "中间", "结尾")
RECAP_SNIPPET_CHARS = 120
RECAP_MAX_SHOTS = 3

# 追加锚点时的字符预算：用户的提示词本来就可能上千字符（实测 1031 字符的提示词供应商
# 是收的），拼完别把提示词撑得太夸张。优先级从高到低：
#   固定主角/场景 > **已演过摘要** > 叙事阶段 > 人物一致 > 接着演
# 预算不够先丢「接着演」那句 —— 它要说的「别重复」已经写在摘要那一句里了。
MAX_PROMPT_CHARS = 2500

# 长视频的两种接法（`continuity`）：
#
#   character（默认）：**参考图锚定**。第 1 段出片后从它身上采几帧当「人物 + 场景基准」，
#       之后每一段都用 reference 模式挂上这几帧 —— 人物与场景不乱变，段间是**硬切**。
#       为什么默认是它：实测（`data/artifacts/_eval/`）首尾帧链只把**一帧**传给下一段，
#       那一帧是背影/远景时下一段拿不到人脸信息、只能自己编（这就是「换脸」的成因）；
#       而每段都挂参考图时，人物的脸与场景在四段里都守住了。
#   seamless：首尾帧链（一镜到底），画面真连续，但人物靠单帧锚、容易飘。
CONTINUITY_CHARACTER = "character"
CONTINUITY_SEAMLESS = "seamless"
CONTINUITY_CHOICES = (CONTINUITY_CHARACTER, CONTINUITY_SEAMLESS)
# 供应商最多收 5 张参考图（见 video_client.generate）
MAX_REFERENCE_IMAGES = 5

# 关于 seed：接口收这个字段，但**实测不生效**（2026-09-23：同样的首帧 + 同样的提示词 +
# 同一个 seed 跑两次，画面差 15–20；换个 seed 反而只差 6–12 —— 与「同一段自己演几秒」的
# 变化量（约 21）同量级）。也就是说它既不能复现、也不能用来压跨段漂移，于是**不自动生成
# 种子**：调用方显式传就原样发过去（留个口子），不传就不塞，别让结果看起来像是被控住了。


def plan_segments(total: int) -> list[int]:
    """把总时长拆成若干段，每段都落在供应商允许的档位上。

    两条优先级，都是照着「用户等不起、额度也有限」定的：

    1. **段数尽量少** —— 每多一段就多等一两分钟、多花一次额度，还多一次衔接失真；
    2. 段数相同时**尽量精准**（少超时长的优先），最后才看均匀。

    所以 9 秒给 ``[10]``（一段，多 1 秒）而不是 ``[5, 4]``（两段、刚刚好）——
    为了省那 1 秒多跑一次生成不划算。只有一段塞不下（超过 12 秒）才往下分段。

    Examples:
        >>> plan_segments(5)
        [5]
        >>> plan_segments(9)
        [10]
        >>> plan_segments(13)
        [8, 5]
        >>> plan_segments(30)
        [10, 10, 10]
    """
    wanted = max(int(total or 0), 1)
    fewest = max(1, -(-wanted // MAX_SEGMENT))
    for count in range(fewest, wanted // MIN_SEGMENT + 1):
        # 同样的段数里，从「刚好等于」往上找，先撞上的就是超得最少的那种
        for candidate in range(wanted, wanted + MAX_SEGMENT + 1):
            plan = _split_exact(candidate, count)
            if plan:
                return plan
    return [MAX_SEGMENT] * fewest


def _split_exact(total: int, count: int) -> list[int]:
    """用 ``count`` 段（每段取自允许档位）**精确**凑出 ``total``；凑不出返回空。

    ``_pairs`` 按「档位差」从小到大给组合，所以第一个能凑出来的就是各段最均匀的。
    """
    for low, high in _pairs():
        rest = total - count * low
        step = high - low
        if step == 0:
            if rest == 0:
                return [high] * count
            continue
        if rest < 0 or rest % step:
            continue
        fills = rest // step
        if 0 <= fills <= count:
            return [high] * fills + [low] * (count - fills)
    return []


def _pairs() -> list[tuple[int, int]]:
    """所有 ``(低档, 高档)`` 组合（低 ≤ 高），用来解「a 个低档 + b 个高档 = 总时长」。

    ``SEGMENT_CHOICES`` 是降序的（长的优先），这里必须自己排序 —— 直接按它切片会
    把「低档」当成 12、把「高档」当成 4，于是 13 秒被拆成三段 5 秒而不是 [8, 5]。
    """
    values = sorted(SEGMENT_CHOICES)
    return [
        (low, high) for index, low in enumerate(values) for high in values[index:]
    ]


def segment_prompts(
    prompt: str,
    shots: Sequence[str] | None,
    count: int,
    subject: str = "",
    segment_seconds: Sequence[int] | None = None,
) -> list[str]:
    """每一段分别用哪条提示词（:func:`plan_prompts` 的薄包装，签名保持不变）。"""
    return plan_prompts(prompt, shots, count, subject, segment_seconds)[0]


def plan_prompts(
    prompt: str,
    shots: Sequence[str] | None,
    count: int,
    subject: str = "",
    segment_seconds: Sequence[int] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """每一段发什么提示词 + **这次是怎么来的**（结果备注要如实说，别悄悄丢内容）。

    核心是**时间线**（见 :mod:`paper_agent.services.skills.video_timeline`）：模型按整片
    时间轴写清每个时间点（``0-4秒：… 4-8秒：… 8-12秒：…``），这里按**实际分段**把它切开、
    把时间戳重算成「这一段自己的 0 起点」—— 模型不用猜我们分几段，每段也都明确写清
    「这几秒演什么」（实测视频模型会照时间线演，包括切换景别 / 机位）。

    三种来源：

    * ``timeline``：识别出整片时间线 → 逐段切片（每段只拿到属于自己那几秒的画面）；
    * ``shots``：老写法（每条一段，或每条都从 0 重新计时）→ 一一对应，**多出来的并进
      最后一段，不再丢掉**；
    * ``prompt``：什么都没给 → 整片复用同一条，给后面几段补「接着往下演、别重复」。

    ``≤12 秒`` 只有一段：**整条时间线原样发出去**（多条分镜并成一条），只补「没铺满」那句
    —— 用户实测过「一个字都不要改」的写法，我们不能在中间动他的时间线。
    """
    own = [str(item).strip() for item in (shots or []) if str(item).strip()]
    lengths = [int(item) for item in (segment_seconds or [])]
    total = float(sum(lengths))
    meta: dict[str, Any] = {
        "count": count,
        "shots": len(own),
        "merged": 0,
        "mode": "prompt",
        "timeline_end": 0.0,
        "total": total,
    }
    found = video_timeline.from_input(prompt, own, total)
    if found is not None:
        meta["timeline_end"] = video_timeline.timeline_end(found[1])
    elif not own:
        # 只有一条提示词时：即使没被当成整片时间线（只写了一个时间点），也记下它写到第几秒
        # —— 结果备注要能如实说「后面几秒没写画面，已补兜底」
        meta["timeline_end"] = video_timeline.timeline_end(video_timeline.parse(prompt)[1])

    if count <= 1:
        # 一次生成、一段片子：整条时间线原样发出去
        if own:
            meta["mode"] = "shots"
            meta["merged"] = max(0, len(own) - 1)
            text = _merge_texts(prompt, own)
        else:
            text = str(prompt or "")
        return [_append_single(text, total or (lengths[0] if lengths else 0))], meta

    windows = _windows(lengths, count, total)
    if found is not None:
        preamble, beats = found
        bodies = video_timeline.slice_windows(preamble, beats, windows)
        meta["mode"] = "timeline"
    elif own:
        bodies = _shot_bodies(own, count)
        meta["mode"] = "shots"
        meta["merged"] = max(0, len(own) - count)
    else:
        bodies = [str(prompt or "")] * count

    fixed = (subject or "").strip()
    fixed_block = SUBJECT_LINE.format(subject=fixed) if fixed else ""
    texts = [
        _append_within(
            body,
            index,
            count,
            fixed_block,
            bodies,
            has_shots=bool(own),
            seconds=lengths[index] if index < len(lengths) else 0,
        )
        for index, body in enumerate(bodies)
    ]
    return texts, meta


def _windows(lengths: Sequence[int], count: int, total: float) -> list[tuple[float, float]]:
    """每段的时间范围 ``[起, 止)``：按计划时长累加；缺计划时长就按总数均分。"""
    spans: list[tuple[float, float]] = []
    cursor = 0.0
    fallback = (total / count) if count > 0 and total > 0 else 0.0
    for index in range(count):
        length = float(lengths[index]) if index < len(lengths) and lengths[index] else fallback
        spans.append((cursor, cursor + length))
        cursor += length
    return spans


def _merge_texts(prompt: str, own: Sequence[str]) -> str:
    """把「整片提示词 + 多条分镜」并成一条 —— 一段片子只收一条提示词，但内容不能丢。"""
    parts: list[str] = []
    base = (prompt or "").strip()
    if base and not any(base in item or item in base for item in own):
        parts.append(base)
    for item in own:
        if item not in parts:
            parts.append(item)
    return "\n\n".join(parts)


def _shot_bodies(own: Sequence[str], count: int) -> list[str]:
    """老写法（每条一段）的对应关系：不够沿用最后一条，**多出来的并进最后一段**。

    以前这里是 ``texts[:count]`` —— 多出来的直接扔。用户真的踩到过：12 秒的分镜被拆成
    3 条交上来，只有第 1 条发出去，后 8 秒的画面整段消失（会话 ``5bb3daf924d8``）。
    """
    bodies = list(own[:count])
    while len(bodies) < count:
        bodies.append(bodies[-1] if bodies else "")
    if len(own) > count:
        bodies[-1] = "\n\n".join(own[count - 1 :])
    return bodies


def _append_single(text: str, seconds: float) -> str:
    """单段（≤12 秒）只补「没铺满」那句。

    一段片子没有「下一段」，所以不加衔接 / 摘要 / 人物一致这些锚点；但**「时间线没铺满
    这一段」的坑在单段一样存在**（写 0-7 秒却要 8 秒），以前这里直接 return 漏掉了。
    """
    return text + pad_block(text, seconds)


def _append_within(
    text: str,
    index: int,
    count: int,
    fixed_block: str,
    all_texts: Sequence[str],
    *,
    has_shots: bool,
    seconds: float = 0,
) -> str:
    """按优先级把锚点拼到提示词后面，超预算就先丢优先级低的。

    顺序是有讲究的：**「已演过什么」比「别换脸」更靠前** —— 后者丢了只是脸飘一点，
    前者丢了整段就是前面某段的重播，用户看到的是"内容没推进"。

    再往后是**衔接点**（见 :mod:`paper_agent.services.skills.video_prompt`）：
    每段收在一个明确的画面上，下一段从那儿接；最后一段换成"收尾"提示。
    """
    blocks = [fixed_block]
    # 分镜的时间戳没铺满这一段（模型常把时间戳当成整片时间轴）：补一句「往前演」
    # —— 剩下的时间没有指令时，视频模型最常见的补法就是原地重演刚才的画面
    blocks.append(pad_block(text, seconds))
    if index:
        recap = _recap_block(all_texts, index)
        if recap:
            blocks.append(recap)
    if index:
        blocks.append(IDENTITY_HINT)
    if count > 1:
        # 衔接点：有下一段就点名"下一段的画面"，没有就是收尾
        if index < count - 1:
            blocks.append(handoff_block(all_texts[index + 1] if has_shots else ""))
        else:
            blocks.append(closing_block())
    if not has_shots:      # 每段同一句话：靠叙事阶段把它们岔开
        blocks.append(PHASE_LINE.format(phase=_phase(index, count)))
    if index:
        blocks.append(CONTINUATION_HINT.format(index=index + 1, count=count))
    for block in blocks:
        if not block or len(text) + len(block) > MAX_PROMPT_CHARS:
            continue
        text += block
    return text


def _recap_block(texts: Sequence[str], index: int) -> str:
    """前面几段演了什么（最近 3 段，每段截 120 字符）。"""
    done = [
        (offset, str(item).strip())
        for offset, item in enumerate(texts[:index], start=1)
        if str(item).strip()
    ]
    if not done:
        return ""
    snippets = []
    for offset, item in done[-RECAP_MAX_SHOTS:]:
        short = item if len(item) <= RECAP_SNIPPET_CHARS else item[:RECAP_SNIPPET_CHARS] + "…"
        snippets.append(f"第 {offset} 段：{short}")
    return RECAP_LINE.format(done=" | ".join(snippets))


def _phase(index: int, count: int) -> str:
    """这一段在整个片子里的位置（开头 / 中间 / 收尾）。"""
    if index <= 0:
        return PHASES[0]
    if index >= count - 1:
        return PHASES[2]
    return PHASES[1]


def resolve_seed(seed: Any) -> int | None:
    """只做「显式传了就用」这一件事，**不自动生成种子**。

    曾试过「整条片子共用一颗随机种子」来压跨段漂移，实测供应商不认这个字段
    （见文件头注释里的数据），自动塞一颗只会给人「结果被控住了」的错觉。
    """
    try:
        return int(str(seed).strip())
    except (TypeError, ValueError):
        return None


def clamp_total(seconds: Any) -> int:
    """把请求时长收进可用范围：小于最小档就取最小档，超过上限取上限。"""
    try:
        value = int(float(str(seconds).strip()))
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    return max(MIN_SEGMENT, min(value, MAX_TOTAL_SECONDS))


def generate_segments(
    generator: Callable[..., list[dict[str, Any]]],
    prompt: str,
    *,
    seconds: int,
    aspect_ratio: str = "16:9",
    seed: int | None = None,
    images: Sequence[str] | None = None,
    first_frame: str = "",
    last_frame: str = "",
    progress: Any = None,
    on_segment: Callable[[dict[str, Any], int, int], None] | None = None,
    shots: Sequence[str] | None = None,
    subject: str = "",
    continuity: str = CONTINUITY_CHARACTER,
    anchor_frames: Sequence[str] | None = None,
    frame_reader: Callable[[str], str] | None = None,
    sampler: Callable[[str], list[str]] | None = None,
    is_stopped: Callable[[], bool] | None = None,
    merge: bool = True,
) -> dict[str, Any]:
    """分段续接出片，并把各段合成长视频。

    Args:
        generator: ``VideoClient.generate`` 形态的生成器（一段一次）。
        seconds: 想要的**总**时长（秒）；≤12 时就是一段，不做合成。
        progress: 进度接收方（形如 ``_VideoProgress``：有 ``label(text)`` 与
            ``report(percent)``）；传 None 就只走生成器自己的默认回调。
        on_segment: 每段一出片就回调 ``(item, 段号, 总段数)`` —— 界面据此立刻挂卡片。
        shots: 分镜：每段一条提示词（按顺序）。不传就整片复用同一条 —— 那会让每段
            把整片画面重演一遍，见 :func:`segment_prompts`。
        subject: 主角与场景的固定措辞，每段原样带上（收敛「后面几段换脸 / 换背景」）。
        continuity: ``character``（默认）每段挂参考图、人物与场景不乱变（段间硬切）；
            ``seamless`` 首尾帧链、画面真连续但人物靠单帧锚（容易飘）。
        anchor_frames: 现成的「人物 / 场景基准」图（data URL 列表）；不传则第 1 段出片后
            自动从它身上采几帧。
        seed: 只透传：调用方传了就发过去，不传就不塞。实测供应商不认这个字段
            （同一颗种子两次结果并不一致），所以别指望它来复现或压漂移。
        frame_reader: 取上一段末帧的实现（测试里替换掉，免得依赖 Qt 解码）。
        merge: 是否合成长视频（False 只出分段，测试用）。

    Returns:
        ``{"planned", "prompts", "segments", "merged", "merge_error", "stopped", "seconds"}``
    """
    requested = clamp_total(seconds) or MAX_SEGMENT
    # **一定**要过 plan_segments：供应商只认 4/5/6/8/10/12 这几档，把 11 秒原样发下去，
    # 客户端只能回落默认值 5 秒 —— 「要 11 秒却一直给 5 秒」就是这么来的（2026-09-24）。
    # plan_segments 会就近上调（7→8、9→10、11→12），片长只多不少。
    planned = plan_segments(requested)
    total = float(sum(planned))
    count = len(planned)
    # 按时间线（或分镜）决定每段发什么；``meta`` 里记着这次是怎么切的，
    # 结果备注要如实告诉用户 / 模型（见 video_skills 的备注）
    texts, meta = plan_prompts(prompt, shots, count, subject, segment_seconds=planned)
    # 记下「用户到底要了几秒」：档位被上调时，结果备注要如实说（不许悄悄改）
    meta["requested"] = requested
    seed_value = resolve_seed(seed)
    read_frame = frame_reader or last_frame_data_url
    read_samples = sampler or sample_frames
    anchored = (continuity or CONTINUITY_CHARACTER) != CONTINUITY_SEAMLESS
    # 「人物 / 场景基准」：调用方给的优先；没给就等第 1 段出片后从它身上采
    references = [str(item) for item in (anchor_frames or []) if item][:MAX_REFERENCE_IMAGES]
    attached = [str(item) for item in (images or []) if item]

    segments: list[dict[str, Any]] = []
    previous = ""
    merge_error = ""
    stopped = False

    for index, length in enumerate(planned, 1):
        if _stopped(is_stopped):
            stopped = True
            break

        if index == 1:
            head = (first_frame or "").strip()
        elif not anchored or not references:
            # 接不上就退回首尾帧链（采样失败 / 想要一镜到底时走这条）
            head = read_frame(previous) if previous else ""
            if not head:
                return _result(
                    planned,
                    segments,
                    None,
                    stopped=False,
                    merge_error="",
                    prompts=texts,
                    seed=seed_value,
                    continuity=continuity,
                    reference_count=len(references),
                    meta=meta,
                    extra={
                        "failed_at": index,
                        "error": (
                            f"没能从第 {index - 1} 段取到末帧，第 {index} 段接不上。"
                            "已生成的片段仍然可用。"
                        ),
                    },
                )
        else:
            head = ""      # 参考图锚定：不接帧，靠参考图把人物与场景钉住
        # 只有最后一段收在用户指定的尾帧上：中间几段的结尾由下一段的首帧决定
        tail = (last_frame or "").strip() if index == count else ""

        if hasattr(progress, "label"):
            progress.label(f"第 {index}/{count} 段")
        call: dict[str, Any] = {
            "seconds": str(length),
            "aspect_ratio": aspect_ratio,
            "seed": seed_value,
        }
        if head or tail:
            call["first_frame"] = head
            call["last_frame"] = tail
        else:
            refs = (references + attached)[:MAX_REFERENCE_IMAGES]
            if refs:
                call["images"] = refs      # 参考图模式（人物 / 场景基准）
        if progress is not None:
            call["on_progress"] = _segment_progress(progress, index, count)
        if is_stopped is not None:
            call["is_stopped"] = is_stopped

        try:
            produced = generator(texts[index - 1], **call)
        except Exception as exc:      # noqa: BLE001 - 中途失败要把已出的段交出去
            if _looks_stopped(exc):
                stopped = True
                break
            return _result(
                planned,
                segments,
                None,
                stopped=False,
                merge_error="",
                prompts=texts,
                seed=seed_value,
                continuity=continuity,
                reference_count=len(references),
                meta=meta,
                extra={"failed_at": index, "error": str(exc)},
            )

        for item in produced or []:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            segments.append(item)
            if on_segment:
                on_segment(item, index, count)
        if produced:
            previous = str((produced[-1] or {}).get("path", "")) or previous

        if anchored and not references and segments and index == 1:
            # 第 1 段出来了：从它身上采几帧当整条片子的「人物 / 场景基准」——
            # 用户什么都不用给，后面每一段都靠这几帧把人脸和场景钉住
            try:
                references = [
                    str(item) for item in read_samples(str(segments[0]["path"]))
                ][:MAX_REFERENCE_IMAGES]
            except Exception:      # noqa: BLE001 - 采样失败就退回首尾帧链
                references = []

    stopped = stopped or _stopped(is_stopped)

    merged: dict[str, Any] | None = None
    if merge and len(segments) >= 2:
        if hasattr(progress, "label"):
            progress.label(f"正在合成 {len(segments)} 段长视频…")
        try:
            merged = _merge(segments, prompt, sum(planned[: len(segments)]))
        except VideoMergeError as exc:
            merge_error = str(exc)
        except Exception as exc:      # noqa: BLE001 - 合成失败不该让分段一起没
            merge_error = f"视频合成失败：{exc}"
    if hasattr(progress, "label"):
        progress.label("")

    return _result(
        planned,
        segments,
        merged,
        stopped=stopped,
        merge_error=merge_error,
        prompts=texts,
        seed=seed_value,
        continuity=continuity,
        reference_count=len(references),
        meta=meta,
    )


def _result(
    planned: list[int],
    segments: list[dict[str, Any]],
    merged: dict[str, Any] | None,
    *,
    stopped: bool,
    merge_error: str,
    prompts: Sequence[str] | None = None,
    seed: int | None = None,
    continuity: str = CONTINUITY_CHARACTER,
    reference_count: int = 0,
    meta: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gone = sum(planned[: len(segments)]) if segments else 0
    payload: dict[str, Any] = {
        "planned": planned,
        "prompts": list(prompts or []),
        "seed": seed,
        "continuity": continuity,
        "reference_count": reference_count,
        "meta": dict(meta or {}),
        "segments": segments,
        "merged": merged,
        "merge_error": merge_error,
        "stopped": stopped,
        "seconds": gone,
    }
    payload.update(extra or {})
    return payload


def _merge(
    segments: Sequence[dict[str, Any]], prompt: str, seconds: int = 0
) -> dict[str, Any]:
    """把各段合成长视频，返回产物描述（命名规则与分段一致）。"""
    total = int(seconds or 0)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"长视频-{stamp}-{total}秒.mp4" if total else f"长视频-{stamp}.mp4"
    target = new_artifact_path(name)
    info = merge_videos([str(item["path"]) for item in segments], target)
    return {
        "name": target.name,
        "path": str(target),
        "kind": "video",
        "prompt": prompt,
        "seconds": int(round(info.get("duration") or total)),
        "merged": True,
    }


def _segment_progress(progress: Any, index: int, count: int) -> Callable[[int], None]:
    """把「第 index 段的段内百分比」换算成**整体**百分比后转出去。

    整体百分比跨段单调递增（第 2 段不会从 0 重来），进度条才不会看着倒退；
    段号由 ``progress.label`` 写在文案里，用户仍然知道在跑第几段。
    """

    def report(percent: int) -> None:
        value = int(percent)
        done = (index - 1) / count * 100
        overall = done if value < 0 else done + value / count
        progress.report(int(round(overall)))

    return report


def _stopped(is_stopped: Callable[[], bool] | None) -> bool:
    if is_stopped is None:
        return False
    try:
        return bool(is_stopped())
    except Exception:      # noqa: BLE001 - 取消信号异常不该影响出片
        return False


def _looks_stopped(exc: Exception) -> bool:
    """用户按 Esc 时生成器抛的哨兵异常（见 ``video_client``）。"""
    return "__stopped__" in str(exc)
