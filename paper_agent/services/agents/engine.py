"""多智能体编排引擎。

融合三种范式：

- **Plan-and-Execute**：先由 Planner 角色拆解任务为 todo 列表，再逐步执行；
- **ReAct**：执行者以「思考 → 调用工具 → 观察结果」循环推进，直到产出答案；
- **Multi-Agent**：Planner / Executor / Writer 三种角色分工协作。

工具调用优先使用 function calling；若模型不支持，则降级解析回答中的
`` ```docx / ```pptx / ```pdf / ```csv / ```xlsx `` 代码块并生成真实文件，
`` ```image `` 代码块则交给文生图模型出图。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from paper_agent.services.key_pool import looks_rate_limited
from paper_agent.services.skills.base import ToolRegistry
from paper_agent.services.stream_draft import DraftExtractor
from paper_agent.services.skills.document_skills import (
    _content_name,
    _safe_name,
    _unique_path,
    build_csv,
    build_docx,
    build_pdf,
    build_pptx,
    build_xlsx,
)

# 事件类型（与 _StreamWorker 的 emit 协议一致）
THINKING = "thinking"
CONTENT = "content"
CONTENT_FINAL = "content_final"
PLAN = "plan"
TOOL = "tool"
ARTIFACT = "artifact"
LIMIT = "limit"              # 达到工具调用轮次上限
NOTE = "note"                # 状态栏提示（如「接口中断，N 秒后重试本轮」）
OPEN = "open_url"            # 需要在用户浏览器里打开的地址（刚启动的本地服务）
OUTPUT = "output"            # 终端任务的实时输出片段（命令跑着就能看到进度）

# 终端输出合并：命令可能每秒刷几百行，一行一个事件会把界面的事件队列灌满
OUTPUT_FLUSH_INTERVAL = 0.12
OUTPUT_FLUSH_CHARS = 1200
DRAFT = "draft"              # 正在写入的正文（工具参数的实时解码结果）

# 参数里带长正文的工具：它们的参数流值得实时展示给用户
DRAFT_TOOLS = {"create_docx", "create_pptx", "create_pdf", "create_csv", "create_xlsx"}

# ---------------------------------------------------------------- 图片事实校验
# 模型（尤其小参数的 flash 模型）有时会「演」一遍调用过程：只写正文说
# 「出好了 ✅ 图片已生成」，还顺手编一个配图-日期-序号.png 的路径，实际根本没调
# generate_image。下面两个常量用来兜住这种情况。
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
# 「生成 / 画 / 出」的完成语
_IMAGE_DONE = r"(?:生成|画|出)(?:了|好|好了|完成|完毕|成功)"
# 「一张 / 2 张 / 三张」都算
_IMAGE_COUNT = r"(?:\d+|[一二三四五六七八九十两]+)\s*张"
IMAGE_CLAIM_RE = re.compile(
    # 我们自己的出图命名（模型会照着上一次工具返回的格式编一个出来）
    r"配图-\d{8}-\d{6}-\d+\.(?:png|jpe?g|webp)"
    # 「图片已生成」（带「已」时完成语可省）／「配图都已经生成好了」
    r"|(?:图片|配图|插图|图像|海报|封面)\s*(?:都|均|全部)?\s*(?:已经|已)\s*(?:生成|画|出)"
    r"(?:了|好|好了|完成|完毕|成功)?"
    r"|(?:图片|配图|插图|图像|海报|封面)\s*" + _IMAGE_DONE
    # 「已生成图片 / 已生成 2 张配图」；必须有「已」，否则「会生成图片」这类
    # 流程描述会被误判成完成汇报
    + r"|(?:已经|已)\s*(?:生成|画|出)(?:了|好|好了|完成|完毕|成功)?\s*"
    + _IMAGE_COUNT + r"?\s*(?:图片|配图|插图|图像)"
    # 口语化的「出好了」「出图完成」
    r"|出好了|(?:出图|生图)\s*(?:完成|好了|成功)"
)
IMAGE_RETRY_HINT = (
    "【事实校验】你上一条回复声称「图片已生成」，但本轮并没有真正调用 generate_image，"
    "也没有产出任何图片文件 —— 文中的文件名/路径是编的。\n"
    "现在请真正完成这件事：调用 generate_image，把画面描述写进 prompt 参数。"
    "只有工具返回成功才能说「已生成」；如果调用失败、或你无法完成，"
    "就直接说明失败原因，**不要再给出任何文件名或本地路径**。"
)
IMAGE_MISSING_WARNING = (
    "---\n\n"
    "> ⚠️ **事实校验**：以上回复声称图片已生成，但本轮实际没有产出任何图片 —— "
    "模型只是描述了过程，并没有真正调用生图工具（文中文件名是虚构的）。\n"
    "> 请再发一次「生成图片」，或把提示词写得更简单直接。"
)

# ---------------------------------------------------------------- 视频事实校验
# 和图片同一个毛病，但**后果更重**：视频一次要等一两分钟、还花额度。小模型会照着
# 历史里 `已生成 1 段视频（文生视频）：视频-20260922-011406.mp4` 这种工具返回格式，
# 凭空写一段「视频已生成」的完成汇报，实际一次 generate_video 都没调 ——
# 用户以为片子好了、去产物目录找，什么都没有。
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
# 完成语。**「跑完 / 生成完 / 拍完了」都要认**：2026-09-24 的真实会话里，模型编的是
# 「5 段全部跑完，现在合成 60 秒长片」——旧表只有「生成了 / 生成好」，一句都没接住，
# 于是这条虚构的完成汇报被原样发给了用户。
_VIDEO_DONE = r"(?:生成|拍|出|合成|跑|拼接|导出)(?:了|好|好了|完|完了|完成|完毕|成功|结束)"
# 「一段 / 1 段 / 两条」都算
_VIDEO_COUNT = r"(?:\d+|[一二三四五六七八九十两]+)\s*(?:段|条)"
# 「5 段**已全部**跑完」：名词与完成语之间允许叠着好几个填充词
_VIDEO_FILLER = r"(?:\s*(?:都|均|全部|全|已经|已|也))*\s*"
# 后面跟着这些词说明是在如实报错（「3 段出了点问题」），不是完成汇报
_VIDEO_NOT_DONE = r"(?!.{0,4}(?:问题|错误|失败|故障|异常|不了))"
VIDEO_CLAIM_RE = re.compile(
    # 我们自己的命名（模型经常照抄一段编出来）
    r"(?:长)?视频-\d{8}-\d{6}(?:-\d+秒)?\.(?:mp4|mov)"
    # 「视频生成完 / 长片已经合成好了 / 视频跑完了」
    r"|(?:视频|短片|片子|成片|长视频|长片|视频片段)" + _VIDEO_FILLER + _VIDEO_DONE
    + _VIDEO_NOT_DONE
    # 「视频已生成 / 两段视频都已经生成好了」：带「已」时完成语可以省
    # （不能把「已」也省了，否则「视频生成中，请稍等」会被误判成完成汇报）
    + r"|(?:视频|短片|片子|成片|长视频|长片|视频片段)"
    + r"\s*(?:都|均|全部|也)*\s*(?:已经|已)\s*"
    + r"(?:生成|拍|出|合成|拼接|导出)(?:了|好|好了|完|完了|完成|完毕|成功|结束)?"
    + _VIDEO_NOT_DONE
    # 「已生成视频 / 已生成 1 段视频 / 已合成一条长视频」；必须有「已」，
    # 否则「会生成视频」这类流程描述会被误判
    + r"|(?:已经|已)" + _VIDEO_FILLER
    + r"(?:生成|拍|出|合成|拼接|导出)(?:了|好|好了|完|完了|完成|完毕|成功)?\s*"
    + _VIDEO_COUNT + r"?\s*(?:视频|短片|长视频|长片|成片|片子)"
    # 「5 段全部跑完 / 两段已经拍完了 / 我把 5 段跑完了」——分段收工最常见的说法
    + r"|" + _VIDEO_COUNT + _VIDEO_FILLER + _VIDEO_DONE + _VIDEO_NOT_DONE
    # 「已合成完毕 / 已合成《X.mp4》」：合成、拼接本身就是视频动作，不必带名词；
    # 但要排掉「已合成背景图 / 音频」这类别的合成
    + r"|(?:已经|已)\s*(?:合成|拼接)(?:了|好|好了|完|完了|完成|完毕|成功)?"
    + r"(?!\s*[^\n]{0,10}(?:图|音频|音乐|字幕|封面))"
    # 口语化的「出片完成」
    + r"|出片\s*(?:完成|好了|成功|了)"
)
VIDEO_RETRY_HINT = (
    "【事实校验】你上一条回复声称「视频已生成 / 已合成」，但本轮产出的视频文件对不上"
    "（要么一次 generate_video 都没调，要么调了但没成功）—— 文中的文件名是编的。\n"
    "现在请真正完成这件事：\n"
    "1) 还缺哪几段就调用 generate_video 补哪几段（画面描述写进 prompt 参数；要长视频就把 "
    "seconds 写大，分段、取样、合成由客户端自动做）；\n"
    "2) 确认每段都真的生成好之后，再用 merge_videos 按顺序拼成一条长片；\n"
    "3) 只有工具真的返回成功才能说「已生成 / 已合成」；如果调用失败、或你无法完成，"
    "就直接说明失败原因，**不要再给出任何文件名或本地路径**。"
)
VIDEO_MISSING_WARNING = (
    "---\n\n"
    "> ⚠️ **事实校验**：以上回复声称视频已生成 / 已合成，但本轮实际没有产出任何视频 —— "
    "模型只是描述了过程，并没有真正生成成功（文中文件名是虚构的）。\n"
    "> 请再发一次，或把提示词写得更简单直接。"
)

# 单次请求的失败重试：能在这一层救回来，就不必让上层把整轮清空重来。
# 限流要等额度窗口，比网络抖动允许更多次。
# 支持「先落文件、再分段追加」的产物工具（输出预算不够时的正解）
SPLITTABLE_TOOLS = {"create_docx", "create_xlsx", "create_csv", "create_pdf"}

STEP_RETRY = 3               # 网络 / 超时类：最多再试 2 次
RATE_LIMIT_RETRY = 4         # 限流类：最多再试 3 次
STEP_RETRY_DELAYS = (5.0, 10.0, 30.0, 60.0)   # 每次重试前的等待（秒）

# 值得重试的错误文案特征：连接 / 超时 / 服务端异常类
TRANSIENT_HINTS = (
    "无法连接", "连接被重置", "远程主机", "连接中止", "超时", "流式中断", "flow failed",
    "timed out", "timeout", "connection", "reset by peer", "500", "502", "503", "504",
)

ARTIFACT_LANGS = ("docx", "pptx", "pdf", "csv", "xlsx", "image")
IMAGE_LANG = "image"
_ARTIFACT_BLOCK = re.compile(
    r"```(docx|pptx|pdf|csv|xlsx|image)[ \t]*([^\n`]*)\n(.*?)```", re.DOTALL
)


# 模型偶尔会陷进死循环：同一句话反复输出（"Writing form styles..." × 上万次），
# 既烧额度又永远不产出。这里对输出尾部做重复检测，发现就掐断本轮。
DEGENERATE_TAIL = 1500        # 只看尾部这么长
DEGENERATE_REPEATS = 10       # 同一小段重复这么多次就判定死循环
DEGENERATE_MAX_UNIT = 90      # 被重复的单元最长多少字符


def looks_degenerate(text: str) -> bool:
    """输出尾部是否在重复同一段内容（模型死循环的典型形态）。"""
    tail = (text or "")[-DEGENERATE_TAIL:]
    for unit in range(2, DEGENERATE_MAX_UNIT + 1):
        span = unit * DEGENERATE_REPEATS
        if len(tail) < span:
            break
        block = tail[-unit:]
        if block.strip() and tail[-span:] == block * DEGENERATE_REPEATS:
            return True
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    if len(lines) >= DEGENERATE_REPEATS:
        recent = lines[-DEGENERATE_REPEATS:]
        if len(set(recent)) == 1:
            return True
    return False


def message_text(message: dict) -> str:
    """取消息的纯文本内容。

    多模态消息的 ``content`` 是 ``text`` / ``image_url`` 混排的数组，
    这里只取文本部分，供 Planner 目标、示例文案等只认字符串的地方使用。
    """
    content = message.get("content", "") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


# ---------------------------------------------------------------- 降级解析
def _replace_image_block(
    body: str,
    image_generator: Callable[[str], list[dict]] | None,
    artifacts: list[dict],
) -> str:
    """把 ```image 代码块的内容当提示词，调用文生图模型出图。

    提示词由模型在回答过程中给出（也可以直接是用户输入），生成成功后
    由调用方通过 ARTIFACT 事件推到界面上内联展示。
    """
    prompt = body.strip()
    if not prompt:
        return ""
    if image_generator is None:
        return "\n\n> 未配置文生图模型，暂无法生成配图。\n"
    try:
        for item in image_generator(prompt):
            artifacts.append(
                {
                    "name": item.get("name", ""),
                    "path": item.get("path", ""),
                    "kind": "image",
                }
            )
    except Exception as exc:      # noqa: BLE001 - 出图失败时回显原因
        return f"\n\n> 生成配图失败：{exc}\n"
    return ""


def extract_artifact_blocks(
    markdown: str,
    image_generator: Callable[[str], list[dict]] | None = None,
) -> tuple[str, list[dict]]:
    """把特殊代码块转成真实文件（function calling 不可用时的兜底）。

    Args:
        markdown: 模型输出的 Markdown
        image_generator: 文生图生成器；``image`` 代码块会用它的内容作为提示词出图

    Returns:
        ``(清洗后的 Markdown, 产物列表)``，产物含 ``name`` / ``path``（图片另带 ``kind``）。
    """
    artifacts: list[dict] = []

    def _replace(match: re.Match) -> str:
        lang, filename, body = match.group(1), match.group(2).strip(), match.group(3)
        if lang == IMAGE_LANG:
            return _replace_image_block(body, image_generator, artifacts)
        try:
            path = _unique_path(_safe_name(filename, f".{lang}", _content_name(body)))
            if lang == "docx":
                build_docx(body, path)
            elif lang == "pptx":
                build_pptx(body, path)
            elif lang == "pdf":
                build_pdf(body, path)
            elif lang == "csv":
                build_csv(body, path)
            elif lang == "xlsx":
                build_xlsx(body, path)
            artifacts.append({"name": path.name, "path": str(path)})
            return ""
        except Exception as exc:      # pragma: no cover - 生成失败时回显错误
            return f"\n\n> 生成 {lang} 文件失败：{exc}\n"

    cleaned = _ARTIFACT_BLOCK.sub(_replace, markdown or "")
    return cleaned, artifacts


# ---------------------------------------------------------------- Planner
PLANNER_SYSTEM = (
    "你是论文写作任务的规划专家（Planner）。请把用户任务拆解为 3-6 个可执行步骤，"
    "按先后顺序排列。只输出 JSON 数组，每项形如 {\"title\": \"步骤名\", \"detail\": \"简要说明\"}，"
    "不要输出任何解释性文字或代码块标记。"
)


def plan_steps(client, goal: str) -> list[dict]:
    """调用 Planner 角色生成任务计划；失败时返回空列表（不阻塞主流程）。"""
    try:
        raw = client.chat(
            [
                {"role": "system", "content": PLANNER_SYSTEM},
                {"role": "user", "content": (goal or "")[:2000]},
            ],
            temperature=0.3,
            max_tokens=800,
        )
    except Exception:                 # pragma: no cover - 规划失败则跳过
        return []

    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []

    steps: list[dict] = []
    for item in data[:8]:
        if isinstance(item, dict) and item.get("title"):
            steps.append(
                {
                    "title": str(item["title"])[:120],
                    "detail": str(item.get("detail", ""))[:300],
                    "status": "pending",
                }
            )
    return steps


# ---------------------------------------------------------------- 编排器
EXECUTOR_HINT = (
    "你是论文写作执行者（Executor）。请结合计划逐步完成；"
    "若需要产出文件（Word/PPT/PDF/CSV/Excel），调用对应工具，不要只描述文件内容。"
)

WRITER_HINT = (
    "最后以「写作者（Writer）」身份给出面向用户的总结：说明做了什么、产出了哪些文件、"
    "下一步建议。使用 Markdown 排版，简洁清晰。"
)


class _OutputPump:
    """把子进程输出合并成少数几个事件。

    命令可能每秒刷几百行（装依赖、编译），一行一个事件会把界面事件队列灌满；
    这里按「时间 + 字符数」合并后再发，界面那边再按 100ms 节流渲染。

    工具调用结束后必须 :meth:`close`：后台服务（start_service）的读取线程会一直
    活着，不关掉的话它会继续往一个已经结束的回合里推事件。
    """

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._buffer: list[str] = []
        self._size = 0
        self._last = time.monotonic()
        self._closed = False
        self._lock = threading.Lock()

    def feed(self, chunk: str) -> None:
        if not chunk or self._closed:
            return
        with self._lock:
            self._buffer.append(chunk)
            self._size += len(chunk)
            due = (
                self._size >= OUTPUT_FLUSH_CHARS
                or time.monotonic() - self._last >= OUTPUT_FLUSH_INTERVAL
            )
        if due:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            if self._closed:
                return
            text = "".join(self._buffer)
            self._buffer.clear()
            self._size = 0
            self._last = time.monotonic()
        if text:
            self._emit(text)

    def close(self) -> None:
        self.flush()
        self._closed = True


class AgentOrchestrator:
    """Plan-and-Execute + ReAct + 多角色协作的编排器。"""

    # ReAct 最大轮次（防止无限循环）。复杂任务（建文档 + 配图 + 反复修正）
    # 常常需要十几轮工具调用，因此按推理强度分档，默认给足。
    MAX_STEPS = 40
    STEP_LIMITS = {"none": 8, "low": 24, "medium": 40, "high": 60, "max": 80}
    # 档位对应的采样温度：越高越发散。注意这控制的是发散度，
    # 不是思考深度（深度由发给服务端的 reasoning_effort 决定）。
    EFFORT_TEMPERATURES = {"none": 0.2, "low": 0.3, "medium": 0.7, "high": 0.9, "max": 0.9}

    def __init__(
        self,
        client,
        registry: ToolRegistry,
        effort: str = "medium",
        use_tools: bool = True,
        use_plan: bool = True,
        is_stopped: Callable[[], bool] | None = None,
        image_generator: Callable[[str], list[dict]] | None = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._effort = effort
        self._use_tools = use_tools
        self._use_plan = use_plan
        self._is_stopped = is_stopped or (lambda: False)
        self._image_generator = image_generator
        self._noted_drop = False      # 是否已提示「推理参数被服务端拒绝」
        self._emit: Callable[[str, str], None] = lambda *_: None

    # ---------------------------------------------------------------- 入口
    def run(self, emit: Callable[[str, str], None]) -> None:
        """作为 worker 的 producer 执行（签名与 ``_StreamWorker`` 一致）。"""
        self._emit = emit
        self._produced = []          # 本轮真正产出的文件：(名称, 路径)
        self._degenerate_round = False   # 上一轮是否因重复输出被中止
        temperature = self.EFFORT_TEMPERATURES.get(self._effort, 0.7)
        return self._execute(temperature)

    # ---------------------------------------------------------------- 流程
    def _execute(self, temperature: float) -> None:
        messages: list[dict] = getattr(self, "_messages", [])
        if not messages:
            return

        # 把取消信号注入支持的长任务工具（如代码执行），使 Esc 能真正终止
 
        for tool_name in self._registry.names():
            tool = self._registry.get(tool_name)
            if tool is not None:
                tool.set_cancel(getattr(self, "_cancel", None))

        goal = message_text(messages[-1]) if messages else ""

        # 1) Planner：拆任务
        steps: list[dict] = []
        if self._use_plan:
            steps = plan_steps(self._client, goal)
            if steps:
                self._emit(PLAN, json.dumps({"steps": steps}, ensure_ascii=False))

        # 2) Executor：带计划上下文执行
        working = list(messages)
        if steps:
            outline = "\n".join(f"{i + 1}. {s['title']}" for i, s in enumerate(steps))
            working = [
                working[0],
                {"role": "system", "content": f"{EXECUTOR_HINT}\n\n执行计划：\n{outline}"},
                *working[1:],
            ]

        tools = self._registry.specs() if self._use_tools else None
        final = self._react(working, tools, temperature)

        # 3) 降级解析：把 ```docx 等代码块落成真实文件，```image 块转成配图
        cleaned, artifacts = extract_artifact_blocks(final, self._image_generator)
        for artifact in artifacts:
            self._remember_artifact(artifact.get("name", ""), artifact.get("path", ""))
            self._emit(ARTIFACT, json.dumps(artifact, ensure_ascii=False))
        if artifacts:
            self._emit(CONTENT_FINAL, cleaned)

        # 3.5) 图片事实校验：模型有时只「演」一遍出图 —— 正文写着「出好了 ✅ 图片已生成」
        #      并编了个配图-xxx.png 的路径，实际既没调工具也没落文件。这里补一轮
        #      让它真去调；还是不行就在正文末尾挂一句醒目的说明，别让用户白找。
        final, cleaned, artifacts = self._verify_image_claim(
            final, cleaned, artifacts, working, tools, temperature
        )
        # 3.6) 视频事实校验：同一个毛病，但视频花时间又花额度，更要拦住
        final, cleaned, artifacts = self._verify_video_claim(
            final, cleaned, artifacts, working, tools, temperature
        )

        # 4) 正文兜底：模型有时把结论全放进推理过程，正文一个字都不吐，
        #    界面上就只剩一个「已思考」的空消息。这里补一次请求 / 补一份文件清单。
        final = self._ensure_answer(final, working, temperature, cleaned if artifacts else "")
        if not (final or "").strip():
            return

    def _remember_artifact(self, name: str, path: str) -> None:
        """记录本轮真正产出的文件，供正文兜底时列清单。"""
        if not path:
            return
        produced = getattr(self, "_produced", None)
        if produced is None:
            produced = self._produced = []
        if not any(item[1] == path for item in produced):
            produced.append((name or Path(path).name, path))

    def _live_artifact(self, item: Any, published: set[str]) -> None:
        """工具「边跑边挂」的入口：记下已挂过的路径，收尾时就不必再挂一遍。"""
        if isinstance(item, dict):
            path = str(item.get("path") or "")
            if path:
                published.add(path)
        self._emit_artifact(item)

    def _emit_artifact(self, item: Any) -> None:
        """把一件产物挂到当前消息上（界面立刻出一张卡片）。

        长任务（分段出片）会自己回调到这里：一段出了就挂一段，不用等整轮结束。
        同一路径重复挂也会被消息层按路径去重，但我们自己先去重（见 ``published``），
        免得同一条事件发两遍。
        """
        payload = item if isinstance(item, dict) else {}
        path = str(payload.get("path") or "")
        if not path:
            return
        name = str(payload.get("name") or Path(path).name)
        self._remember_artifact(name, path)
        event = {"name": name, "path": path}
        kind = str(payload.get("kind") or "")
        if kind:
            event["kind"] = kind
        self._emit(ARTIFACT, json.dumps(event, ensure_ascii=False))

    def _produced_images(self) -> list[tuple[str, str]]:
        """本轮**真正**产出的图片文件（按扩展名判断，不看模型怎么说）。"""
        return self._produced_with(IMAGE_SUFFIXES)

    def _produced_videos(self) -> list[tuple[str, str]]:
        """本轮**真正**产出的视频文件（按扩展名判断，不信模型的口头汇报）。"""
        return self._produced_with(VIDEO_SUFFIXES)

    def _produced_with(self, suffixes: set[str]) -> list[tuple[str, str]]:
        return [
            item
            for item in getattr(self, "_produced", [])
            if Path(item[1]).suffix.lower() in suffixes
        ]

    def _verify_image_claim(
        self,
        final: str,
        cleaned: str,
        artifacts: list[dict],
        working: list[dict],
        tools: list[dict] | None,
        temperature: float,
    ) -> tuple[str, str, list[dict]]:
        """模型声称出了图、实际一张都没有时纠偏（见 :meth:`_verify_claim`）。"""
        return self._verify_claim(
            final, cleaned, artifacts, working, tools, temperature,
            enabled=self._image_generator is not None,
            produced=self._produced_images,
            claim_re=IMAGE_CLAIM_RE,
            retry_hint=IMAGE_RETRY_HINT,
            warning=IMAGE_MISSING_WARNING,
        )

    def _verify_video_claim(
        self,
        final: str,
        cleaned: str,
        artifacts: list[dict],
        working: list[dict],
        tools: list[dict] | None,
        temperature: float,
    ) -> tuple[str, str, list[dict]]:
        """模型声称出了视频、实际一段都没有时纠偏。

        和图片同一个毛病（小模型照抄历史里工具返回的格式编汇报），但代价更重：
        视频一次要等一两分钟、还花额度，用户以为好了就不再去发。
        """
        return self._verify_claim(
            final, cleaned, artifacts, working, tools, temperature,
            enabled=self._has_video_tool(),
            produced=self._produced_videos,
            claim_re=VIDEO_CLAIM_RE,
            retry_hint=VIDEO_RETRY_HINT,
            warning=VIDEO_MISSING_WARNING,
        )

    def _has_video_tool(self) -> bool:
        """本轮有没有 ``generate_video`` 可用；没配视频模型就别折腾。"""
        try:
            return self._registry.get("generate_video") is not None
        except Exception:      # noqa: BLE001 - 拿不到就当没有
            return False

    def _verify_claim(
        self,
        final: str,
        cleaned: str,
        artifacts: list[dict],
        working: list[dict],
        tools: list[dict] | None,
        temperature: float,
        *,
        enabled: bool,
        produced: Callable[[], list[tuple[str, str]]],
        claim_re: Any,
        retry_hint: str,
        warning: str,
    ) -> tuple[str, str, list[dict]]:
        """「声称做了、实际没做」的通用纠偏（图片与视频各调一次）。

        小模型很容易「演」一遍调用过程：正文写「出好了 ✅ 已生成」，顺手编一个
        ``配图-20260922-004457-1.png`` / ``视频-20260922-011406.mp4`` 的路径
        （照抄工具返回的格式），实际一次工具都没调 —— 用户按路径去找，文件根本不存在。

        这里先补一轮，明确要求它真去调工具；还是没有产物就在正文末尾挂一句
        「以上是模型口述、实际没生成」，别让人白等白找。

        Returns:
            更新后的 ``(原始正文, 展示用正文, 产物列表)``。
        """
        if not enabled or produced():
            return final, cleaned, artifacts
        if not claim_re.search(final or ""):
            return final, cleaned, artifacts

        retry = self._react(
            [*working, {"role": "user", "content": retry_hint}], tools, temperature
        )
        retry_cleaned, retry_artifacts = extract_artifact_blocks(
            retry or "", self._image_generator
        )
        for artifact in retry_artifacts:
            self._remember_artifact(artifact.get("name", ""), artifact.get("path", ""))
            self._emit(ARTIFACT, json.dumps(artifact, ensure_ascii=False))
        if (retry or "").strip():
            final, cleaned, artifacts = retry, retry_cleaned, retry_artifacts
            if retry_artifacts:
                self._emit(CONTENT_FINAL, retry_cleaned)

        if not produced():
            cleaned = f"{cleaned.rstrip()}\n\n{warning}"
            self._emit(CONTENT_FINAL, cleaned)
            final = cleaned
        return final, cleaned, artifacts

    def _ensure_answer(
        self,
        final: str,
        working: list[dict],
        temperature: float,
        extra: str = "",
    ) -> str:
        """正文为空时的兜底：先让模型把结论写进正文，再退化为文件清单。"""
        if (final or "").strip():
            return final

        nudge = (
            "你上一轮只输出了推理过程，面向用户的正文是空的。"
            "请用 Markdown 简要说明这一轮做了什么、产出了哪些文件、下一步建议；"
            "不要再调用工具，也不要把内容只写在推理里。"
        )
        if getattr(self, "_degenerate_round", False):
            nudge += (
                " 另外：上一轮你陷入了重复输出（同一句话反复出现），"
                "这次不要重复任何句子，也不要长时间空想，直接给结论或直接调用工具。"
            )
        try:
            for event in self._stream_step(
                [*working, {"role": "user", "content": nudge}],
                tools=None,
                temperature=temperature,
            ):
                if self._is_stopped():
                    break
                if event.get("type") == CONTENT and event.get("text"):
                    text = event["text"]
                    self._emit(CONTENT, text)
                    final = (final or "") + text
        except Exception:      # noqa: BLE001 - 兜底失败不能影响已有产物
            pass
        if (final or "").strip():
            return final

        if extra:
            answer = extra
        elif self._produced:
            lines = "\n".join(f"- {name}（{path}）" for name, path in self._produced)
            answer = (
                "本轮模型只给出了推理过程，没有输出正文。已产出的文件如下，"
                f"可直接打开查看：\n\n{lines}"
            )
        else:
            answer = "（本轮模型只给出了推理过程，没有输出正文；回复「继续」可以让它接着说明。）"
        self._emit(CONTENT, answer)
        return answer

    def _emit_draft(self, drafts: dict, event: dict) -> None:
        """把工具参数的原始分片解码成正文增量，实时推给界面。

        模型写论文时正文塞在 ``create_docx`` 的 ``markdown`` 参数里，
        参数写完（也就是工具开始执行）之前界面完全没有内容，用户只能干等。
        """
        name = event.get("name", "")
        if name not in DRAFT_TOOLS:
            return
        index = event.get("index", 0)
        entry = drafts.get(index)
        if entry is None or entry[0] != name:
            entry = (name, DraftExtractor())
            drafts[index] = entry
        text = entry[1].feed(event.get("text", ""))
        if text:
            self._emit(DRAFT, json.dumps({"name": name, "text": text}, ensure_ascii=False))

    # ---------------------------------------------------------------- 重试
    def _required_params(self, name: str) -> list[str]:
        """该工具的必填参数名（未注册的工具返回空）。"""
        tool = self._registry.get(name)
        if tool is None:
            return []
        return [param.name for param in tool.parameters if param.required]

    def _parse_args(self, name: str, raw_args: str) -> tuple[dict, str]:
        """解析工具调用参数，返回 ``(参数, 错误说明)``。

        错误说明非空时**不要执行**工具：参数为空字符串（常见于单次输出被长度
        截断，参数一个字都没到达）或不是合法 JSON 时，静默按默认值执行只会留下
        垃圾产物。空对象 ``{}`` 不在此列——它是合法调用，必填项由工具注册表校验。
        """
        text = (raw_args or "").strip()
        if not text:
            # 参数一片空白：全可选入参的工具（list_files / list_artifacts）本来就
            # 允许不传参数，照常执行；有必填项的工具才当成「参数没到达」——
            # 那通常意味着上一次输出被长度截断，按默认值跑只会产出垃圾文件。
            if self._required_params(name):
                return {}, "参数是空的（可能上一轮输出被长度截断）"
            return {}, ""
        try:
            data = json.loads(text)
        except ValueError as exc:
            return {}, f"参数不是合法 JSON（{exc}）"
        if not isinstance(data, dict):
            return {}, "参数必须是 JSON 对象"
        # 注意：空对象 ``{}`` 是**合法**调用 —— ``list_files`` / ``list_artifacts``
        # 这类工具的入参全是可选的，传空对象就是「用默认值」。必填校验交给
        # ``ToolRegistry.execute``（它按 schema 逐项判断）。之前在这里一律拒掉，
        # 会把合法调用也砍掉：模型第一次调 list_files 就收到「调用被跳过」，
        # 弱模型会据此认定「这些工具用不了 / 我写不了文件」，随后整轮跑偏。
        return data, ""

    def _arg_error(self, name: str, parse_error: str) -> str:
        """拼给模型的「这次调用为什么没执行」说明。

        参数为空往往不是模型忘了写，而是**单次输出预算被用完**
        （``finish_reason == "length"``）：整篇正文塞在一个参数里，写到一半被截断，
        服务端就只留一个空壳工具调用。这种情况必须指明「分几次写 + append 续写」，
        否则模型会原样重试，一次次空转。
        """
        if getattr(self._client, "last_finish_reason", "") == "length":
            note = (
                f"{name} 调用被跳过：上一次输出被单次长度上限截断，"
                "参数没能写完（不是你漏写参数）。"
            )
            self._emit(NOTE, "输出被长度限制截断，已让模型分段写入")
            if name in SPLITTABLE_TOOLS:
                return (
                    f"{note}\n"
                    "请改成**分段写入**：\n"
                    "1. 第一次调用正常生成文件（filename / markdown 都给全）。\n"
                    "2. 之后每写一部分就调用一次，带 append=true，markdown 只放这一段，"
                    "single 次控制在 6000 字以内。\n"
                    "不要在一次调用里塞整篇论文。"
                )
            return (
                f"{note}\n请把要传的内容拆小后重新调用：单次调用的内容控制在 6000 字以内。"
            )
        return (
            f"{name} 调用被跳过：{parse_error}。请重新发起这次调用并补全所有参数。"
        )

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """是否值得重试：限流、网络抖动、超时、服务端异常。

        认证失败（401 / 403）这类怎么重试都不会变，**不能**重试，
        否则本来立刻能报的错会被拖成几十秒超时。
        """
        text = str(exc)
        if looks_rate_limited(text):
            return True
        lowered = text.lower()
        return any(hint in lowered for hint in TRANSIENT_HINTS)

    def _wait(self, seconds: float) -> None:
        """可打断的等待（停止生成时立即返回）。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self._is_stopped():
            time.sleep(0.2)

    def _stream_step(
        self,
        working: list[dict],
        tools: list[dict] | None,
        temperature: float,
    ) -> Iterator[dict]:
        """带重试的单轮请求。

        只有「本次一个事件都还没吐出去」时才重试：已经开始流式输出后失败，
        再发一次会让已经显示的内容重复一遍，那种情况直接抛出，
        交给上层的整轮重试（它会清掉半成品重发，结果才是干净的）。
        """
        attempts = 0
        while True:
            attempts += 1
            yielded = False
            try:
                for event in self._client.stream_events(
                    working, tools=tools, temperature=temperature
                ):
                    yielded = True
                    yield event
                return
            except Exception as exc:      # noqa: BLE001 - 按文案判断是否重试
                if self._is_stopped() or yielded or not self._is_transient(exc):
                    raise
                limited = looks_rate_limited(str(exc))
                allowed = RATE_LIMIT_RETRY if limited else STEP_RETRY
                if attempts >= allowed:
                    raise
                delay = STEP_RETRY_DELAYS[min(attempts, len(STEP_RETRY_DELAYS)) - 1]
                reason = "接口限流" if limited else "连接中断"
                self._emit(
                    NOTE,
                    f"{reason}，{int(delay)} 秒后重试本轮"
                    f"（{attempts}/{allowed - 1}）",
                )
                self._wait(delay)
                if self._is_stopped():
                    raise
                self._emit(NOTE, "正在重试本轮…")

    # ---------------------------------------------------------------- ReAct
    def _step_limit(self) -> int:
        """按推理强度取 ReAct 轮次上限。"""
        return self.STEP_LIMITS.get(self._effort, self.MAX_STEPS)

    def _react(self, working: list[dict], tools: list[dict] | None, temperature: float) -> str:
        collected: list[str] = []
        finished = False
        limit = self._step_limit()

        for _ in range(limit):
            if self._is_stopped():
                return "\n\n".join(collected)

            content_parts: list[str] = []
            calls: list[dict] = []
            drafts: dict[int, tuple[str, DraftExtractor]] = {}
            recent = ""            # 输出尾部，用于死循环检测
            degenerate = False
            for event in self._stream_step(working, tools=tools, temperature=temperature):
                if self._is_stopped():
                    return "\n\n".join([*collected, "".join(content_parts)])
                etype = event.get("type")
                if etype == "tool_calls":
                    calls = event.get("calls", [])
                elif etype == "tool_args":
                    self._emit_draft(drafts, event)
                elif etype in (THINKING, CONTENT):
                    text = event.get("text", "")
                    if text:
                        self._emit(etype, text)
                        if etype == CONTENT:
                            content_parts.append(text)
                        recent = (recent + text)[-DEGENERATE_TAIL:]
                        if looks_degenerate(recent):
                            # 模型卡在重复输出里：继续读只会烧额度、永远不落文件，
                            # 直接断开这一轮（生成器关闭 → 连接关闭）
                            degenerate = True
                            break
            if degenerate:
                self._degenerate_round = True
                self._emit(
                    NOTE,
                    "模型陷入重复输出，已中止本轮（回复「继续」或换个说法可重试）",
                )
                self._emit(THINKING, "\n…（检测到重复输出，本轮已中止）\n")

            if getattr(self._client, "reasoning_dropped", False) and not self._noted_drop:
                # 服务端不认推理参数：撤掉了，得让用户知道「低/中/高」这次没生效
                self._noted_drop = True
                self._emit(
                    THINKING,
                    "（该服务端不接受推理强度参数，本次已自动撤掉；"
                    "可在「自定义模型 → 推理强度参数」里改成服务端支持的写法）\n",
                )

            content = "".join(content_parts)
            if content.strip():
                collected.append(content)
            if not calls:
                finished = True
                break

            # 回写本轮的 assistant tool_calls
            working.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": call.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": call.get("name", ""),
                                "arguments": call.get("arguments", ""),
                            },
                        }
                        for call in calls
                    ],
                }
            )

            # 执行工具并把结果作为观察回灌
            for call in calls:
                name = call.get("name", "")
                # 不要用 `or "{}"` 兜底：那会把「参数一个字都没到」伪装成
                # 「模型传了空对象」，两种情况要分开处理（见 _parse_args）
                raw_args = call.get("arguments") or ""
                args, parse_error = self._parse_args(name, raw_args)
                if parse_error:
                    # 参数坏了就别执行：拿默认值瞎跑只会产出垃圾文件，
                    # 还会让模型误以为调用成功。改成明确报错，它下一轮会补对参数。
                    message = self._arg_error(name, parse_error)
                    self._emit(
                        TOOL,
                        json.dumps(
                            {
                                "name": name,
                                "args": raw_args,
                                "result": message,
                                "status": "failed",
                            },
                            ensure_ascii=False,
                        ),
                    )
                    working.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", ""),
                            "name": name,
                            "content": message,
                        }
                    )
                    continue

                self._emit(
                    TOOL,
                    json.dumps(
                        {"name": name, "args": raw_args, "status": "running"},
                        ensure_ascii=False,
                    ),
                )
                # 跑命令 / 执行代码时把输出实时推给界面（终端任务卡片上能看到进度）
                pump = _OutputPump(
                    lambda text, _name=name, _args=raw_args: self._emit(
                        OUTPUT,
                        json.dumps(
                            {"name": _name, "args": _args, "chunk": text},
                            ensure_ascii=False,
                        ),
                    )
                )
                tool = self._registry.get(name)
                published: set[str] = set()
                if tool is not None:
                    tool.set_output_hook(pump.feed)
                    # 长任务可以边跑边挂产物（分段出片：出一段挂一段）
                    tool.set_artifact_hook(
                        lambda item, _seen=published: self._live_artifact(item, _seen)
                    )
                try:
                    result = self._registry.execute(name, args)
                finally:
                    pump.close()
                    if tool is not None:
                        # 收工：后台服务的读取线程还会活着，回调必须撤掉
                        tool.set_output_hook(None)
                        tool.set_artifact_hook(None)
                payload = {
                    "name": name,
                    "args": raw_args,
                    "result": result.for_model()[:600],
                    "status": "done" if result.success else "failed",
                }
                if result.open_url:
                    # 界面的任务卡片要用它渲染「打开」按钮（后台服务的访问地址）
                    payload["open_url"] = result.open_url
                self._emit(TOOL, json.dumps(payload, ensure_ascii=False))
                for path in result.artifact_paths:
                    if str(path) in published:
                        continue      # 刚才已经随回调实时挂过了，别再挂一次
                    self._emit_artifact({"path": str(path)})
                if result.open_url:
                    # 本地服务起来了：模型所在的工具环境没有 GUI，开不了浏览器，
                    # 交给界面用系统默认浏览器代开
                    self._emit(
                        OPEN,
                        json.dumps(
                            {"url": result.open_url, "name": name}, ensure_ascii=False
                        ),
                    )
                working.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "name": name,
                        "content": result.for_model(),
                    }
                )

        if finished or self._is_stopped():
            return "\n\n".join(collected)
        # 轮次用尽：告知用户并让模型给一个收尾总结，避免停在半句话
        return self._close_out(collected, working, limit, temperature)

    # ---------------------------------------------------------------- 收尾
    def _close_out(
        self,
        collected: list[str],
        working: list[dict],
        limit: int,
        temperature: float,
    ) -> str:
        """达到轮次上限：明确提示用户，并补一次不带工具的总结。"""
        notice = (
            f"> ⚠️ 已达到工具调用上限（{limit} 轮），任务可能尚未完成，"
            f"回复「继续」可以接着往下做。"
        )
        self._emit(LIMIT, json.dumps({"limit": limit}, ensure_ascii=False))
        self._emit(CONTENT, "\n\n---\n\n")

        summary = self._summarize(working, temperature)
        body = summary.strip()
        if body:
            self._emit(CONTENT, f"\n\n{notice}\n")
        else:
            self._emit(CONTENT, f"{notice}\n")

        parts = [*collected, "\n\n---\n\n"]
        if body:
            parts.append(body)
        parts.append(f"\n\n{notice}\n" if body else f"{notice}\n")
        return "".join(parts)

    def _summarize(self, working: list[dict], temperature: float) -> str:
        """轮次用尽后的收尾：去掉工具再请求一次，让模型总结进展。"""
        hint = (
            "你已达到本轮的工具调用上限，不能再调用任何工具。"
            "请直接用 Markdown 总结当前进展：已经完成了什么、产出了哪些文件（含文件名）、"
            "还有哪些没做完、下一步建议。不要输出工具调用或代码块。"
        )
        parts: list[str] = []
        try:
            for event in self._stream_step(
                [*working, {"role": "user", "content": hint}],
                tools=None,
                temperature=temperature,
            ):
                if self._is_stopped():
                    break
                etype = event.get("type")
                if etype == CONTENT:
                    text = event.get("text", "")
                    if text:
                        self._emit(CONTENT, text)
                        parts.append(text)
                elif etype == THINKING:
                    self._emit(THINKING, event.get("text", ""))
        except Exception:      # noqa: BLE001 - 收尾失败不影响已有内容
            return ""
        return "".join(parts)


def build_orchestrator(
    client,
    effort: str = "medium",
    messages: list[dict] | None = None,
    is_stopped: Callable[[], bool] | None = None,
    cancel: Any = None,
    image_generator: Callable[[str], list[dict]] | None = None,
    session_files: list[dict] | None = None,
    background_generator: Callable[[str], list[dict]] | None = None,
    mode: str = "doc",
    workspace: Path | None = None,
    video_generator: Callable[..., list[dict]] | None = None,
    video_images_provider: Callable[[], list[str]] | None = None,
    video_progress: Any = None,
    image_images_provider: Callable[[], list[str]] | None = None,
) -> AgentOrchestrator:
    """构造编排器（默认挂载全部文档 Skills 与文生图 / 文生视频 Skill）。

    Args:
        session_files: 本会话工作区文件清单，用于让 ``list_artifacts`` 只列本会话产物
        background_generator: 背景图专用生成器（横版、无水印）；不传则复用 image_generator
        mode: 工作模式：``doc`` 文档（默认）/ ``code`` 编程（换成工程工具集）
        workspace: 编程模式下该会话专属的工程目录（会话之间互不干扰）
        video_generator: 文生视频生成器；传入才有 ``generate_video`` 工具
        video_images_provider: 本轮的图片素材（图生视频用）
        video_progress: 视频进度桥；长视频分段生成时用它写「第 i/N 段」
        image_images_provider: 本轮的图片素材（图生图用：人物形象类兜底成图生图）
    """
    from paper_agent.services.skills import build_default_registry

    engine = AgentOrchestrator(
        client=client,
        registry=build_default_registry(
            image_generator,
            session_files,
            background_generator,
            mode=mode,
            workspace=workspace,
            video_generator=video_generator,
            video_images_provider=video_images_provider,
            video_progress=video_progress,
            image_images_provider=image_images_provider,
        ),
        effort=effort,
        is_stopped=is_stopped,
        image_generator=image_generator,
    )
    engine._messages = messages or []      # noqa: SLF001 - 供 run() 取输入
    engine._cancel = cancel                # noqa: SLF001 - 传给长任务工具
    return engine
