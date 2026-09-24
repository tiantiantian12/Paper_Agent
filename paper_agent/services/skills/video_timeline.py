"""按时间线划分画面：模型写「每个时间点演什么」，客户端保证视频模型照它演。

用户的原话：**「要模型划分好时间线，每个时间段是什么画面后，能够正常切换画面 —— 你告诉
视频模型哪几秒干什么，模型是会遵守的，有这个能力的。」**（实测：一条 12 秒的提示词里写
``0-4秒 / 4-8秒 / 8-12秒``，出来的片子三个时间点都照着演了。）

以前的毛病出在 ``shots`` 的语义上 —— 它被当成「每段一条提示词」，跟实际分段一一对应：

* **≤12 秒只有一段**：``texts[:1]`` 把后面的分镜**直接丢掉**。用户写了 ``0-4秒 / 4-8秒 /
  8-12秒`` 三段时间线、模型把它拆成 3 条 shots 交上来，结果只有前 4 秒的画面被发出去，
  后 8 秒整段消失（会话 ``5bb3daf924d8`` 里就撞上过）。
* **>12 秒**：模型预先不知道我们会怎么分段（30 秒可能是 10+10+10，也可能是 12+12+6），
  它按整片时间轴写的秒数落不到段上，只能靠「没铺满就补一句」兜着。

所以这里把**时间线当唯一事实**：

1. 模型按**整片时间轴**写清每个时间点：``0-4秒：… 4-8秒：… 8-12秒：…``，从 0 开始、
   连续铺满它想要的总时长（规则见 :mod:`paper_agent.services.skills.video_prompt`）；
2. 客户端按**实际分段**把时间线切开 —— :func:`slice_windows` 逐段取与该段时间范围相交的
   条目、把时间戳**重算成「这一段自己的 0 起点」**，这样每一段拿到的都是「这几秒演什么」；
3. 某段没被时间线覆盖到（用户只写到第 8 秒、这一段却有 10 秒）→ 那一段交给
   ``pad_block`` 补一句「把剩下的时间往下演」，而不是让视频模型原地重演。

模型因此**不需要猜我们怎么分段**，用户写的每个时间点也都会被送到对应那一段。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

_NUM = r"\d+(?:\.\d+)?"
_UNIT = r"(?:秒|s|S)"
# 一个时间点标记：`0-4秒：` / `0秒-4秒：` / `0-4s:` / `4秒：`（未写结束秒）
# 注意单位是**必须**的，否则「23:13」这种时刻也会被当成秒数标记。
MARKER_RE = re.compile(
    rf"(?P<start>{_NUM})\s*{_UNIT}?\s*"
    rf"(?:(?P<dash>[-~～—–－]|至|到)\s*(?P<end>{_NUM})\s*{_UNIT})?"
    rf"\s*(?P<colon>[:：])?"
)


@dataclass(frozen=True)
class Beat:
    """一个时间点：``start``–``end`` 秒之间演什么（``end`` 为 None 表示没写结束秒）。"""

    start: float
    end: float | None
    body: str

    def clipped(self, window_start: float, window_end: float) -> "Beat | None":
        """截到窗口内并**重算成相对该窗口的时间戳**（窗口外返回 None）。"""
        start = max(self.start, window_start)
        end = window_end if self.end is None else min(self.end, window_end)
        if end <= start:
            return None
        return Beat(start - window_start, end - window_start, self.body)

    def rendered(self) -> str:
        """恢复成 ``0-4秒：正文`` 的形式（整数就不带小数点）。"""
        def short(value: float) -> str:
            return str(int(value)) if float(value).is_integer() else f"{value:g}"

        if self.end is None:
            return f"{short(self.start)}秒：{self.body}"
        return f"{short(self.start)}-{short(self.end)}秒：{self.body}"


def parse(text: str) -> tuple[str, list[Beat]]:
    """把一段提示词拆成 ``(前言, 时间点条目)``；没写时间戳时条目为空。

    前言是第一个时间点之前的内容（整体风格 / 场景交代），切片时**每一段都带上** ——
    否则各段会各自发挥，画风与场景立刻分家。
    """
    raw = str(text or "")
    # 三道过滤缺一不可：① 是范围（`0-4秒`）或带冒号（`4秒：`）—— 光写「3 秒后」不算；
    # ② 这一段里**必须出现「秒 / s」单位** —— 否则「凌晨 23:13」的时刻也会被当成秒数标记。
    matches = [
        item
        for item in MARKER_RE.finditer(raw)
        if (item.group("colon") or item.group("end")) and re.search(_UNIT, item.group(0))
    ]
    if not matches:
        return raw.strip(), []

    preamble = raw[: matches[0].start()].strip()
    beats: list[Beat] = []
    for index, match in enumerate(matches):
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        # 只去空白，**保留作者写的标点**（「…动了一下；」的那个分号要留着 ——
        # 以前连标点一起剥掉，切片拼回来就成了「动了一下 5-10秒：」，两个时间点粘在一起）
        body = raw[match.end() : body_end].strip()
        if not body:
            continue
        start = float(match.group("start"))
        end = float(match.group("end")) if match.group("end") else None
        beats.append(Beat(start, end, body))

    # 没写结束秒的条目：到下一个条目的起点为止（「铺满」的常见写法）
    fixed: list[Beat] = []
    for index, beat in enumerate(beats):
        if beat.end is None and index + 1 < len(beats):
            beat = Beat(beat.start, beats[index + 1].start, beat.body)
        fixed.append(beat)
    return preamble, fixed


def _join_preambles(parts: Sequence[str]) -> str:
    """把「整片提示词 + 各条分镜自己的前言」并成一个前言（重复的只留最全那份）。"""
    kept: list[str] = []
    for raw in parts:
        text = str(raw or "").strip()
        if not text:
            continue
        for index, item in enumerate(list(kept)):
            if text in item:
                break                     # 已有更全的
            if item in text:
                kept[index] = text        # 新的更全：替换掉旧的
                break
        else:
            kept.append(text)
    return "\n".join(kept)


def timeline_end(beats: Sequence[Beat]) -> float:
    """时间线写到第几秒（没写结束秒的条目按起点算）。"""
    return max((beat.end or beat.start for beat in beats), default=0.0)


def from_input(prompt: str, shots: Sequence[str], total: float) -> tuple[str, list[Beat]] | None:
    """把模型的输入变成**整片时间线**；拿不到时间线时返回 None（调用方走老路子）。

    两种被认作时间线的情况：

    * 给了 ``shots``：**每条都带时间戳、且整体递增**（``0-4秒``→``4-8秒``→``8-12秒``）。
      每条都从 0 重新开始（``0-3秒`` 出现在每一条里）说明那是「每段自己的时间轴」，
      属于老写法，不按整片时间线处理；
    * 没给 ``shots``、但 ``prompt`` 里有 ≥2 个时间点 —— 用户/模型直接把时间线写进了提示词。
    """
    entries = [str(item).strip() for item in shots or [] if str(item).strip()]
    if entries:
        parsed = [parse(item) for item in entries]
        if not all(beats for _, beats in parsed):
            return None
        stamps = [beat.start for _, beats in parsed for beat in beats]
        if any(later < earlier for earlier, later in zip(stamps, stamps[1:])):
            return None                      # 每条都从 0 开始：那是「每段自己的时间轴」
        # 前言（风格 / 场景 / 人物固定描述）要把 **prompt 里那句一起带上**：
        # 模型常把风格写在 prompt、把时间线拆进 shots，只取 shots 的前言会让
        # 「冷青绿调、35mm 胶片颗粒」这类整片风格整个消失（切片后每段都没了）
        preamble = _join_preambles([prompt, *(item for item, _ in parsed)])
        beats = [beat for _, group in parsed for beat in group]
        return preamble, beats

    preamble, beats = parse(prompt)
    if len(beats) >= 2:
        return preamble, beats
    return None


def slice_windows(
    preamble: str,
    beats: Sequence[Beat],
    windows: Sequence[tuple[float, float]],
) -> list[str]:
    """按分段的实际时间范围切开时间线（时间戳重算成每段自己的 0 起点）。

    返回每段的正文；**某段没有被时间线覆盖到时返回空串** —— 由调用方决定怎么兜
    （一般是补一句「把剩下的时间往下演」，见 ``video_prompt.pad_block``）。
    """
    texts: list[str] = []
    for start, end in windows:
        parts = [
            piece.rendered()
            for piece in (beat.clipped(start, end) for beat in beats)
            if piece is not None
        ]
        # 每个时间点**单独一行**：视频模型读起来是「这几秒演什么」逐条列出的，
        # 比挤成一段更不容易漏掉后面几条
        body = "\n".join(parts)
        if preamble:
            body = f"{preamble}\n{body}" if body else preamble
        texts.append(body)
    return texts
