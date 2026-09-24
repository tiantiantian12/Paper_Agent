"""智能体服务：在工作线程中生成流式回复。

- 内置模型：使用 ``mock_engine`` 的示例内容（UI 演示）
- 自定义模型：走多智能体编排（Planner 拆解 → ReAct 调用文档 Skills → Writer 汇总），
  支持 function calling，产物以可点击卡片呈现在聊天界面
"""

from __future__ import annotations

import base64
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

from paper_agent.core.models import Attachment
from paper_agent.services.background_assets import background_generator_for
from paper_agent.services.agents import (
    build_orchestrator,
    extract_artifact_blocks,
    message_text,
)
from paper_agent.services import session_artifacts
from paper_agent.services.video_frames import image_data_url as compact_data_url
from paper_agent.services.image_client import (
    IMAGE_MIME,
    ImageClient,
    ImageGenerationError,
    image_data_url,
)
from paper_agent.services.video_client import VideoClient, VideoGenerationError
from paper_agent.services.mock_engine import build_reply, build_thinking
from paper_agent.services.openai_client import OpenAICompatClient

# ---------------------------------------------------------------- 文件名硬规则
# 2026-09-24 的真实事故：模型每轮都照「视频-20260924-HHMMSS.mp4」的格式**预测**一个
# 文件名写进正文，跟工具真正返回的名字对不上（真实是 111902，正文写 032357）。后果：
#   * 它自己以为「上一条没真调工具」，于是同一段反复重拍，白等一两分钟、白花额度；
#   * 用户拿着一个不存在的文件名去找文件；
#   * 让它把已有视频合成时，它回「工作区里没有任何视频文件，之前那些名字都是我编的」。
# 文件名里带的是**生成时刻**，模型不可能预知 —— 这条写进两套系统提示（文档 / 编程）。
FILE_NAME_RULE = (
    "\n**文件名一律不许猜**：生成出来的名字里带「生成时刻」的时间戳，你无法预知，所以：\n"
    "① 结果还没返回时**不要**先写文件名，也不要写「接下来会生成 视频-xxxx.mp4」这类推测；\n"
    "② 引用已有文件时，只能用**系统提示里的文件清单**或**工具返回原文**里的名字 —— "
    "照抄，不要改写、不要凭印象拼一个；\n"
    "③ 想知道本会话已经生成了什么，调用 `list_artifacts`（工程文件用 `list_files`）；"
    "**不要**凭记忆回答「有哪些文件」，更不要断言「什么都没有」；\n"
    "④ 只有工具真的返回成功才能说「已生成」；一时写不出准确文件名就用描述性说法"
    "（如「刚才那两段视频」）—— **宁可不说，也不要编一个像模像样的名字**。"
)

SYSTEM_PROMPT = (
    "你是「Paper Agent」——一位严谨的毕业论文写作助手。"
    "你擅长选题分析、章节大纲设计、文献综述梳理、正文写作与学术润色，"
    "也熟悉 GB/T 7714 参考文献规范。\n"
    "回答要求：使用 Markdown 排版；结构清晰、论证充分；"
    "语言规范、避免口语化；涉及数据与结论时给出依据；"
    "如信息不足，先提出关键澄清问题再写作。\n"
    "文件产出：需要交付 Word / PPT / PDF / CSV / Excel 时，优先调用对应工具；"
    "其中写论文（学位论文 / 开题报告 / 文献综述正文）时，create_docx 要传 doc_type=thesis，"
    "系统会按论文规范排版（宋体正文、黑体标题、1.5 倍行距、首行缩进）；"
    "普通文档、说明、记录用 doc_type=plain，不要用论文排版；"
    "论文正文里插图用独占一行的 Markdown 图片语法「![图 x-y 说明](配图文件名)」，"
    "会真正嵌入 Word 并自动加题注；表格题注写成「表 x-y 说明」单独一行放在表格上方；"
    "写了「# 目录」这一章时系统会自动插入可更新的 Word 目录域，不要手写目录条目和页码；"
    "**长文档必须分段写入**：单次 create_docx 的 markdown 控制在 6000 字以内，"
    "第一次调用不带 append 先把文件建出来，之后每写完一部分（比如一章 / 一节）"
    "再调用一次并带上 append=true 续写到同一个文件名；"
    "千万不要把整篇上万字论文塞进一次调用——单次输出长度有上限，"
    "超了会被截断，工具什么都不会生成；"
    "做汇报 PPT 用 create_pptx：`#` 一页、`- ` 要点（缩进两格降级）、"
    "`##` 是页内小节（不新开页）、表格与 `![图 x-y 说明](配图)` 都会自动排进页面；"
    "目录不要自己写：主标题 ≥5 个时系统自动生成目录页（只列一级标题，自动分页的续页不重复计数），"
    "要点超过 7 条会自动拆续页，所以「核心算法分多页展开」只需把内容写足；"
    "背景不要整份只用一张：create_pptx 的 backgrounds 可传多张（本地路径或画面描述），"
    "按章节顺序轮换；某节要单独换背景就在该节标题下写一行「[背景图: 文件名或描述]」，"
    "系统还会逐页变换背景处理方式（整页蒙版 / 左侧渐变 / 右侧竖条）与裁切焦点；"
    "缺素材先用 web_search(kind=image) 检索、再用 download_file 下载（比出图快且不耗额度），"
    "都没有才让系统按讲题自动生成（同描述命中缓存复用）；"
    "写论文引用资料、找数据时也可用 web_search(kind=web) 检索来源，不要编造；"
    "交付的文档一律用上述工具生成，不要用 execute_python 自己拼装 docx/pptx/pdf —— "
    "手拼的文档没有论文排版与目录页码，还需要用户返工；"
    "若工具不可用，则在回答里用 ```docx / ```pptx / ```pdf / ```csv / ```xlsx "
    "代码块输出内容（首行可写文件名），系统会自动落成可点击打开的文件。\n"
    "配图：需要插图、示意图、封面图或概念图时，调用 generate_image 工具；"
    "若工具不可用，则用 ```image 代码块输出**图片提示词**"
    "（中文描述画面主体、风格、构图与光线），系统会调用文生图模型出图并直接展示。"
    "**只有工具真的返回成功才能说「已生成」**：不要只描述出图过程就宣布完成，"
    "也不要凭空写文件名或本地路径（编出来的路径用户打不开）。\n"
    "系统任务：需要扫描磁盘、统计大文件、查找或清理临时/缓存文件时，"
    "调用 execute_python 编写并执行 Python 代码；删除操作仅对临时目录、"
    "浏览器缓存、回收站等白名单生效，其它位置一律拒绝，不要尝试绕过。"
    + FILE_NAME_RULE
)

# 编程模式：把角色从「论文助手」切到「工程助手」
CODE_SYSTEM_PROMPT = (
    "你是「Paper Agent」的编程模式——一位严谨的软件工程助手。"
    "你负责配置环境、编写代码、运行与调试程序。\n"
    "工作方式：\n"
    "1. 先了解现状：用 list_files 看工程目录结构、read_file 读关键文件，不要凭空假设；\n"
    "2. 写代码用 write_file（相对路径按工程目录算），改已有文件先读再写全量内容；\n"
    "3. **路径默认规则（重要）**：用户没有指定目录、或只给了文件名（例如「改一下 index.html」）"
    "时，一律按**工程目录内**的同名文件理解 —— 先在本轮附带的「代码工作区现有文件」清单里找，"
    "命中就直接用 read_file / write_file 操作；不要用 execute_python 全盘扫描、"
    "不要猜用户桌面 / 下载 / 系统目录里的同名文件；\n"
    "4. 清单里没有、用 list_files 在工程目录里也找不到，才考虑其它办法："
    "先用 web_search 找资料、或明确告诉用户「工程目录里没有这个文件」并请他提供路径 / 上传文件，"
    "不要擅自到工程目录之外的地方去读改写；\n"
    "5. 需要依赖就用 run_command 执行 pip install / npm install 等；\n"
    "6. 写完必须验证：用 run_command 跑脚本或测试，把报错修掉再交付；\n"
    "7. 复杂任务先用 execute_python 做原型验证也可以，但最终代码要落到工程文件里；\n"
    "8. 要跑「长期运行的服务」（前端 dev server、后端 API、python -m http.server 等）"
    "一律用 start_service：它后台启动、立刻返回，识别到访问地址后**应用会自动用"
    "浏览器打开**。绝对不要用 run_command 起常驻服务（会一直卡到超时）。\n"
    "9. 用户要「生成 / 画一张图」时（图标、示意图、占位图、插画、封面等）用 generate_image："
    "写清画面主体、风格、构图与光线，出图会直接显示在对话里并收进「我的论文空间」，"
    "需要落到工程目录时再把产物复制过去；"
    "不要回答「我没有图像生成能力」，也不要用 execute_python 去画"
    "（除非用户明确要的是数据图表）。"
    "**只有工具真的返回成功才能说「已生成」**：不要只写一段出图过程就宣布完成，"
    "也不要凭空编文件名或本地路径 —— 编出来的路径用户打不开，等于没生成。\n"
    "所有读写与命令执行都发生在**工程目录**内（默认在应用数据目录下的 "
    "``workspace/<会话 id>/generated``：你写的代码与生成的视频 / 文档都在这一层，"
    "用户上传的在旁边的 ``upload``；可用环境变量 PAPERAGENT_CODE_ROOT 指定已有项目），"
    "目录之外的写入会被拒绝。\n"
    "关于浏览器：你自己运行在无 GUI 的工具环境里，确实打不开浏览器，"
    "但也**不需要**打开 —— start_service 会由应用代开，你只要把访问地址写成裸链接"
    "（不要放进反引号或代码块）写进回复里，用户点一下就能进。"
    "不要在回复里说「请你自己在浏览器打开」或「我无法打开浏览器」。\n"
    "回答要求：用 Markdown；改了什么、为什么改、怎么验证的要讲清楚；"
    "贴代码时用带语言标注的代码块；不确定的地方先说明假设，不要编造 API。"
    + FILE_NAME_RULE
)

# 会话工作区布局（两种模式共用同一份目录，见 services/session_workspace.py）。
# 以前产物在 data/artifacts、附件在 data/attachments、编程模式的工程在 workspace/<会话 id>，
# 三处各管各的 —— 模型在工程目录里找不到刚生成的视频，于是断言「工作区里没有任何视频文件」
# （2026-09-24 真实事故）。把布局写进系统提示，它才知道该去哪找。
WORKSPACE_LAYOUT_TEMPLATE = (
    "**本会话工作区**（上传与生成都在这同一棵树里，编程 / 文档模式共用）：\n"
    "根目录：{root}\n"
    "- `{upload}/` —— 用户上传 / 粘贴的文件\n"
    "- `{generated}/` —— 你生成的配图 / 视频 / Word / PPT / 下载\n"
    "引用已有文件时写**文件名**就行（系统会在这两个目录里找）；"
    "要在代码或命令里写路径，就写成 `generated/文件名`、`upload/文件名`。"
)


def workspace_layout_hint(root: str) -> str:
    """工作区布局说明（随每轮请求注入，路径按会话而异）。"""
    from paper_agent.services import session_workspace

    return WORKSPACE_LAYOUT_TEMPLATE.format(
        root=root,
        upload=session_workspace.UPLOAD_DIR_NAME,
        generated=session_workspace.GENERATED_DIR_NAME,
    )


# 输入区「深度写作」开关：作为一条 system 消息注入本轮请求
DEEP_WRITING_HINT = (
    "本轮开启了「深度写作」：请把内容写足 —— 正文不少于 3000 字，"
    "每个小节展开成多段（观点 → 论据 → 例证），需要处给出数据、公式或引文；"
    "不要只给提纲或要点列表，也不要因为篇幅长就省略章节。"
)


# 图片扩展名 → MIME（多模态请求用）与 data URI 编码统一放在 image_client，
# 图生图的参考图也用它，避免两处各留一份实现
_IMAGE_MIME = IMAGE_MIME


def _image_data_urls(attachments, limit: int = 5) -> list[str]:
    """把本轮的图片附件内联成 ``data:`` 链接，供「图生视频 / 图生图」当参考图。

    接口只认图片地址，而桌面端的附件是本地文件，这里直接内联带上（Flash 最多 5 张）。
    没有图片时返回空列表 —— 那就是文生视频 / 文生图。
    大图先压到 1280 宽再内联（手机原图几 MB，原样塞进请求体既慢又容易超限）。
    """
    urls: list[str] = []
    for item in attachments or []:
        path = Path(getattr(item, "path", "") or "")
        if not path.is_file():
            continue
        mime = _IMAGE_MIME.get(path.suffix.lower())
        if not mime:
            continue
        inline = compact_data_url(path)
        if not inline:
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            inline = f"data:{mime};base64," + base64.b64encode(raw).decode()
        urls.append(inline)
        if len(urls) >= limit:
            break
    return urls


def build_user_content(
    user_text: str, attachments: list[Attachment] | None
) -> str | list[dict]:
    """构造 user 消息的 ``content``：带图片时返回多模态数组。

    OpenAI 兼容接口要求图片以 ``image_url``（data URI）形式随消息发送，
    只写文件名的话模型是收不到图的。没有可用图片时返回纯字符串。
    """
    images = [item for item in (attachments or []) if item.is_image]
    if not images:
        return user_text

    parts: list[dict] = [
        {"type": "text", "text": user_text or "（用户未输入文字，请参考附件）"}
    ]
    for attachment in images:
        url = image_data_url(attachment.path)
        if url:
            parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts if len(parts) > 1 else user_text


def build_chat_messages(
    history: list[dict],
    user_text: str,
    attachments: list[Attachment] | None = None,
    workspace: list[dict] | None = None,
    deep: bool = False,
    limit: int = 20,
    resume: str = "",
    mode: str = "doc",
    code_files: list[str] | None = None,
    code_root: str = "",
    workspace_root: str = "",
) -> list[dict]:
    """组装发送给模型的消息列表（图片走多模态 content）。

    Args:
        workspace: 本会话工作区文件清单（``workspace_snapshot()`` 的结果），
            注入后模型无需再调用 ``list_artifacts`` 扫磁盘就知道已有哪些文件。
        deep: 输入区的「深度写作」开关，本轮要求模型把篇幅写足。
        resume: 上一次失败尝试的进度摘要（工具调用记录 + 已产出文件 + 已写正文），
            重试时注入，模型就能接着做而不把做过的事重来一遍。
        mode: ``doc`` 文档模式（论文助手）/ ``code`` 编程模式（工程助手）。
        code_files: 编程模式的工程目录文件清单（``file_index()`` 的结果）。
            注入后模型不用先调 ``list_files`` 也知道这里有什么，
            并且**默认就认这些文件**（用户没给路径时不会跑到别处去找）。
        code_root: 工程目录绝对路径，随清单一并说明，方便模型拼相对路径。
        workspace_root: 会话工作区根目录（``upload`` / ``generated`` 在哪），
            两种模式都注入 —— 模型按名字找文件时才知道该看哪两个目录。
    """
    prompt = CODE_SYSTEM_PROMPT if mode == "code" else SYSTEM_PROMPT
    messages: list[dict] = [{"role": "system", "content": prompt}]

    if workspace_root:
        messages.append(
            {"role": "system", "content": workspace_layout_hint(workspace_root)}
        )

    if workspace:
        from paper_agent.services.paper_outline import format_workspace_file

        # **两种模式都要给清单**（以前编程模式只注入工程目录，于是模型看不到本会话生成的
        # 视频 / 文档，让它"把前面生成的视频合成一下"时会答「工作区里没有任何视频文件，
        # 之前那些名字都是我编的」——2026-09-24 真实事故）
        if mode == "code":
            lines = [
                "本会话已有的**产物**清单（视频 / 文档 / 配图等，就收在工作区的 generated 里，"
                "工程目录里也能看到）——"
                "**这些是真实存在的文件**，引用时照抄下面的文件名，不要自己猜文件名："
            ]
        else:
            lines = ["本会话工作区已有以下文件（可直接用 read_document 按路径读取，"
                     "不要再去磁盘搜索，也不要对已存在的文件重复生成）："]
        lines.extend(format_workspace_file(item) for item in workspace)
        if mode == "code":
            lines.append(
                "（要用它们就把**文件名**原样传给对应工具：生成视频时 anchor / continue_from "
                "给这个名字，拼接用 merge_videos，读文档用 read_document。）"
            )
        messages.append({"role": "system", "content": "\n".join(lines)})

    if code_files and mode == "code":
        # 编程模式：先把工程目录里有什么摆出来，模型才知道「默认动哪些文件」
        lines = [
            f"代码工作区（{code_root or '工程目录'}）里现有这些文件。用户没有指定路径 / "
            "只说了文件名时，**一律按这里的同名文件理解**，直接用 read_file / write_file "
            "操作，不要去磁盘其它位置搜索或凭空猜路径："
        ]
        lines.extend(code_files)
        messages.append({"role": "system", "content": "\n".join(lines)})

    if deep and mode != "code":
        messages.append({"role": "system", "content": DEEP_WRITING_HINT})

    if attachments:
        notes = ["用户附带了以下参考资料："]
        for attachment in attachments:
            if attachment.is_image:
                notes.append(
                    f"- {attachment.name}（图片，已随用户消息以图像形式提供，请直接看图）"
                )
                continue
            notes.append(f"- {attachment.name}（{attachment.kind}）")
            preview = _read_text_preview(attachment)
            if preview:
                notes.append(f"  内容摘录：\n{preview}")
        if mode == "code":
            # 附件落在工作区的 upload（工程目录默认就是旁边的 generated）：
            # 不给出真实路径的话，模型会拿着名字去磁盘别处找，然后回「找不到这个文件」
            paths = "、".join(
                str(item.path) for item in attachments if getattr(item, "path", "")
            )
            notes.append(
                "（以上附件已复制进本会话工作区的 upload 目录 —— 它在**工程目录**旁边，"
                + (f"引用时用这个路径：{paths}；" if paths else "")
                + "不要到别的目录去找）"
            )
        else:
            notes.append(
                "（以上附件就在本会话工作区的 upload 目录里，按文件名引用即可）"
            )
        messages.append({"role": "system", "content": "\n".join(notes)})

    messages.extend(history[-limit:])

    if resume:
        # 重试场合：把上一次尝试的进度摆在提问前面，模型就不会从头再来
        messages.append(
            {
                "role": "system",
                "content": (
                    "【接着上一次的进度继续】上一次尝试因接口错误中断，"
                    "以下记录可以直接沿用：\n"
                    f"{resume}\n\n"
                    "要求：已经成功过的工具不要重复调用；已经生成的文件直接沿用或继续修改，"
                    "不要重新生成；正文从上次的断点往后写。"
                ),
            }
        )

    messages.append({"role": "user", "content": build_user_content(user_text, attachments)})
    return messages


def _read_text_preview(attachment: Attachment, limit: int = 1500) -> str:
    """读取文本类附件的开头部分（用于给模型提供上下文）。"""
    if attachment.is_image:
        return ""
    try:
        with open(attachment.path, "r", encoding="utf-8", errors="ignore") as file:
            return file.read(limit)
    except OSError:
        return ""


# ---------------------------------------------------------------- 生产者
def _emit_chunks(
    emit: Callable[[str, str], None],
    text: str,
    kind: str,
    size: int,
    delay: int,
    is_stopped: Callable[[], bool] | None = None,
) -> None:
    """按小块推送文本（模拟流式输出的节奏）；被停止时立即中断。"""
    index = 0
    total = len(text)
    while index < total:
        if is_stopped is not None and is_stopped():
            return
        emit(kind, text[index : index + size])
        index += size
        QThread.msleep(delay)


def _mock_producer(
    reply: str,
    thinking: str,
    image_generator: Callable[[str], list[dict]] | None,
    is_stopped: Callable[[], bool] | None = None,
) -> Callable[[Callable[[str, str], None]], None]:
    """内置模型的分块输出：思考 → 正文 → 落产物（```image 会转成配图）。"""

    def produce(emit) -> None:
        if thinking:
            _emit_chunks(emit, thinking, "thinking", 4, 16, is_stopped)
        _emit_chunks(emit, reply, "content", 6, 12, is_stopped)
        if is_stopped is not None and is_stopped():
            return
        # 正文里可能含 ```docx / ```image 等代码块，落盘后用清洗后的正文替换显示
        cleaned, artifacts = extract_artifact_blocks(reply, image_generator)
        for artifact in artifacts:
            emit("artifact", json.dumps(artifact, ensure_ascii=False))
        if artifacts:
            emit("content_final", cleaned)

    return produce


def _image_producer(
    image_client: ImageClient,
    prompt: str,
    is_stopped: Callable[[], bool] | None = None,
) -> Callable[[Callable[[str, str], None]], None]:
    """直接选中文生图模型：把用户输入当作图片提示词出图。"""

    def produce(emit) -> None:
        emit("thinking", f"调用文生图模型 {image_client.model_id} 生成配图…\n")
        emit("thinking", f"图片提示词：{prompt}\n")
        try:
            items = image_client.generate(prompt)
        except ImageGenerationError as exc:
            raise ImageGenerationError(f"生成图片失败：{exc}") from exc

        summary = f"已按你的描述生成 {len(items)} 张配图：\n\n" + "\n".join(
            f"- {item['name']}" for item in items
        )
        _emit_chunks(emit, summary, "content", 6, 12, is_stopped)
        for item in items:
            emit("artifact", json.dumps(item, ensure_ascii=False))
        emit("content_final", summary)

    return produce


def _video_producer(
    video_client: VideoClient,
    prompt: str,
    params: dict | None = None,
    images: list[str] | None = None,
    is_stopped: Callable[[], bool] | None = None,
) -> Callable[[Callable[[str, str], None]], None]:
    """直接选中文生视频模型：把用户输入当作视频提示词出片。

    视频是「提交任务 → 轮询 → 下载」三步，出片要几十秒甚至几分钟，所以一路用
    ``note`` 把「排队中 / 生成中 / 下载中」推到界面状态栏，并用 ``progress``
    事件驱动进度条 —— 一个不动的界面会让用户以为卡死了。
    """
    options = params or {}

    def produce(emit) -> None:
        mode = "图生视频" if images else "文生视频"
        emit("thinking", f"调用视频模型 {video_client.model_id}（{mode}）…\n")
        emit("thinking", f"视频提示词：{prompt}\n")

        # percent < 0 = 还查不到进度（排队中）：界面用不确定进度条 + 这句文案
        state = {"percent": -1}

        def post(percent: int, text: str) -> None:
            state["percent"] = percent
            emit(
                "progress",
                json.dumps({"percent": percent, "text": text}, ensure_ascii=False),
            )

        post(-1, "视频任务已提交，正在排队…")

        def on_status(state_name: str) -> None:
            if state_name == "completed":
                post(100, "视频已出片，正在下载…")
            elif state_name == "retrying":
                # 供应商队列满了：客户端会自己等一会儿重投，这里说清楚，别让人以为卡死
                post(state["percent"], "服务繁忙（视频队列已满），等一会儿自动重试…")
            elif state_name == "pending":
                post(state["percent"], "视频生成中（出片要 1–3 分钟）…")

        def on_progress(percent: int) -> None:
            post(percent, f"视频生成中 {percent}%（出片要 1–3 分钟）…")

        try:
            items = video_client.generate(
                prompt,
                seconds=options.get("seconds") or "5",
                aspect_ratio=options.get("aspect_ratio") or "16:9",
                seed=options.get("seed"),
                images=images or None,
                on_status=on_status,
                on_progress=on_progress,
                is_stopped=is_stopped,
            )
        except VideoGenerationError as exc:
            if str(exc) == "__stopped__":
                return          # 用户按了停止：交给 worker 按「已停止」上报
            raise VideoGenerationError(f"生成视频失败：{exc}") from exc

        summary = f"已按你的描述生成 {len(items)} 段视频：\n\n" + "\n".join(
            f"- {item['name']}" for item in items
        )
        for item in items:
            emit(
                "artifact",
                json.dumps(
                    {"name": item["name"], "path": item["path"], "kind": "video"},
                    ensure_ascii=False,
                ),
            )
        _emit_chunks(emit, summary, "content", 6, 12, is_stopped)
        emit("content_final", summary)

    return produce


class _VideoProgress:
    """视频生成进度的「桥」。

    智能体调 ``generate_video`` 工具时，生成器是在 worker 线程里同步跑完的，
    而进度要变成信号推回界面。工具是在 worker 之前就装配好的（拿不到 emit），
    所以这里先给个空壳，``run()`` 起来后再把 emit 接上。
    """

    def __init__(self) -> None:
        self._emit: Callable[..., None] | None = None
        self._label = ""

    def bind(self, emit: Callable[..., None]) -> None:
        self._emit = emit

    def label(self, text: str) -> None:
        """设置进度文案前缀：长视频分段时写「第 2/3 段」。

        由长视频编排在 worker 线程里调用；只在文本框里加个前缀，不额外推事件。
        """
        self._label = (text or "").strip()

    def report(self, percent: int) -> None:
        """由生成器在 worker 线程里调用。"""
        if self._emit is not None:
            self._emit(int(percent), self._label)


class _StreamWorker(QObject):
    """分块产生回复内容的 worker（mock 或智能体编排）。"""

    token = Signal(str)          # 正式回答
    thinking = Signal(str)       # 推理过程
    plan = Signal(str)           # 任务计划（JSON）
    tool = Signal(str)           # 工具调用（JSON）
    artifact = Signal(str)       # 文件产物（JSON）
    content_final = Signal(str)  # 清洗后的最终正文（去掉已落成文件的代码块）
    limit = Signal(str)          # 达到工具调用轮次上限（JSON）
    note = Signal(str)           # 状态栏提示（重试倒计时等）
    draft = Signal(str)          # 正在写入的正文片段（JSON）
    open_url = Signal(str)       # 需要打开的地址（JSON：本地服务起来了）
    output = Signal(str)         # 终端任务实时输出（JSON）
    progress = Signal(str)       # 长任务进度（视频生成：百分比；空串 = 不确定进度）
    finished = Signal()
    failed = Signal(str)

    MOCK_CHUNK_SIZE = 6
    MOCK_CHUNK_DELAY_MS = 12

    def __init__(
        self,
        reply: str = "",
        producer: Callable[[Callable[[str, str], None]], None] | None = None,
        mock_thinking: str = "",
    ) -> None:
        super().__init__()
        self._reply = reply
        self._producer = producer
        self._mock_thinking = mock_thinking
        self._aborted = False
        # 供长任务工具（如扫盘用的代码执行）感知停止，及时杀掉子进程
        self.cancel_event = threading.Event()
        # 断开在途请求的钩子（用户点停止时用，避免卡在 socket 读上不动）
        self.abort_hook: Callable[[], None] | None = None
        # 智能体调 generate_video 工具时的进度桥（worker 起跑后接上 emit）
        self.video_progress: _VideoProgress | None = None
        # 这条消息属于哪个会话：产物按会话分目录（见 services/session_artifacts.py）
        self.session_id = ""

    def request_stop(self) -> None:
        self._aborted = True
        self.cancel_event.set()
        if self.abort_hook is not None:
            try:
                self.abort_hook()
            except Exception:      # noqa: BLE001 - 断开失败不该影响停止流程
                pass

    @property
    def aborted(self) -> bool:
        return self._aborted

    @Slot()
    def run(self) -> None:
        # 产物按会话分目录（见 services/session_artifacts.py）：图片 / 视频 / 文档 / 下载
        # 都落到这个会话自己的工作区子目录里，别的会话的同名产物不会被认错 —— 用户报的
        # 「视频串文件」就是平铺目录里扫到了别人的片子。producer 是阻塞式跑完的、工具都在
        # 本线程同步执行，所以这份上下文一路可见；没有 session_id（示例引擎）时退回旧行为。
        with session_artifacts.use(self.session_id):
            self._generate()

    def _generate(self) -> None:
        if self.video_progress is not None:
            self._bind_video_progress()
        try:
            if self._producer is not None:
                self._producer(self._emit)
                if self._aborted:
                    self.failed.emit("__aborted__")
                else:
                    self.finished.emit()
                return

            # 示例引擎：先"思考"，再输出正文
            if self._mock_thinking:
                self._emit_chunk(self._mock_thinking, self.thinking.emit, size=4, delay=16)
            self._emit_chunk(self._reply, self.token.emit)

            if self._aborted:
                self.failed.emit("__aborted__")
            else:
                self.finished.emit()
        except Exception as exc:  # pragma: no cover - 兜底
            # 用户主动停止时，断开请求会以异常形式冒出来：按「已停止」上报，
            # 否则界面会把「我按了停止」显示成一条报错
            self.failed.emit("__aborted__" if self._aborted else str(exc))

    # ------------------------------------------------------------------
    def _bind_video_progress(self) -> None:
        """把「智能体调 generate_video」的进度接到信号上。

        工具是在 worker 线程里同步跑完的（中途界面看不到任何动静），所以生成器
        包装层拿到的回调必须转成信号推回主线程。
        """

        def report(percent: int, label: str = "") -> None:
            value = int(percent)
            if value < 0:
                text = "视频任务已提交，正在排队…"
            else:
                text = f"视频生成中 {value}%…"
            if label:
                text = f"{label} · {text}"
            self.progress.emit(
                json.dumps({"percent": value, "text": text}, ensure_ascii=False)
            )

        self.video_progress.bind(report)

    def _emit(self, kind: str, text: str) -> None:
        """把不同来源的内容分发到对应信号。"""
        if kind == "thinking":
            self.thinking.emit(text)
        elif kind == "plan":
            self.plan.emit(text)
        elif kind == "tool":
            self.tool.emit(text)
        elif kind == "artifact":
            self.artifact.emit(text)
        elif kind == "content_final":
            self.content_final.emit(text)
        elif kind == "limit":
            self.limit.emit(text)
        elif kind == "note":
            self.note.emit(text)
        elif kind == "draft":
            self.draft.emit(text)
        elif kind == "open_url":
            self.open_url.emit(text)
        elif kind == "output":
            self.output.emit(text)
        elif kind == "progress":
            self.progress.emit(text)
        else:
            self.token.emit(text)

    def _emit_chunk(
        self, text: str, emit, size: int = MOCK_CHUNK_SIZE, delay: int = MOCK_CHUNK_DELAY_MS
    ) -> None:
        index = 0
        total = len(text)
        while index < total and not self._aborted:
            emit(text[index : index + size])
            index += size
            QThread.msleep(delay)


@dataclass
class _Run:
    """一个正在跑的生成任务（每个会话最多一个）。"""

    message_id: str
    session_id: str
    thread: QThread
    worker: _StreamWorker


class AgentService(QObject):
    """管理生成任务：**按会话并发**，总并发有上限。

    以前整个窗口只有一个 worker 线程：在 A 会话出视频（动辄十几分钟）的这段时间里，
    B 会话连问个问题都得等 —— 一提交就被「正在生成中」挡回来。现在每个会话各自一个
    worker，互不干扰：切会话不打断、流式输出各归各的消息（事件本来就按消息 ID 路由）。

    总并发用 ``max_parallel`` 兜住（默认 2）：免费档的视频 / 生图很容易撞速率限制，
    同时跑一堆只会一起卡 —— 上限到了就明确告诉用户「前面还有几个在跑」。
    """

    started = Signal(str)               # message id
    token = Signal(str, str)            # message id, delta（正式回答）
    thinking = Signal(str, str)         # message id, delta（推理过程）
    plan = Signal(str, str)             # message id, 计划 JSON
    tool = Signal(str, str)             # message id, 工具调用 JSON
    artifact = Signal(str, str)         # message id, 产物 JSON
    content_final = Signal(str, str)    # message id, 最终正文
    limit_hit = Signal(str, str)        # message id, 达到轮次上限的 JSON
    note = Signal(str, str)             # message id, 状态栏提示
    draft = Signal(str, str)            # message id, 正在写入的正文片段（JSON）
    open_url = Signal(str, str)         # message id, 要打开的地址（JSON）
    output = Signal(str, str)           # message id, 终端任务实时输出（JSON）
    progress = Signal(str, str)         # message id, 长任务进度（视频生成百分比）
    finished = Signal(str)              # message id
    failed = Signal(str, str)           # message id, error

    DEFAULT_MAX_PARALLEL = 2

    def __init__(self, parent: QObject | None = None, max_parallel: int = 0) -> None:
        super().__init__(parent)
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()
        try:
            limit = int(max_parallel)
        except (TypeError, ValueError):
            limit = 0
        self._max_parallel = max(1, limit or self.DEFAULT_MAX_PARALLEL)

    # ------------------------------------------------------------------ 状态
    @property
    def max_parallel(self) -> int:
        """最多同时跑几个任务。"""
        return self._max_parallel

    @property
    def running_count(self) -> int:
        return len(self._runs)

    @property
    def is_running(self) -> bool:
        """是否有**任意**任务在跑（整体状态；界面上的「这个会话在跑吗」用
        :meth:`is_session_running`）。"""
        return bool(self._runs)

    @property
    def has_capacity(self) -> bool:
        return len(self._runs) < self._max_parallel

    def is_message_running(self, message_id: str) -> bool:
        return bool(message_id) and message_id in self._runs

    def is_session_running(self, session_id: str) -> bool:
        return any(run.session_id == session_id for run in self._runs.values())

    def streaming_message_id(self, session_id: str) -> str:
        """该会话正在生成的那条助手消息；没在跑就返回空串。"""
        for run in self._runs.values():
            if run.session_id == session_id:
                return run.message_id
        return ""

    def running_sessions(self) -> set[str]:
        return {run.session_id for run in self._runs.values()}

    # ------------------------------------------------------------------ 控制
    def start(
        self,
        message_id: str,
        messages: list[dict],
        model: str = "",
        effort: str = "medium",
        endpoint: dict | None = None,
        attachments: list[Attachment] | None = None,
        image: dict | None = None,
        image_mode: bool = False,
        session_files: list[dict] | None = None,
        mode: str = "doc",
        code_root: str = "",
        video: dict | None = None,
        video_mode: bool = False,
        video_params: dict | None = None,
        session_id: str = "",
    ) -> None:
        """开始生成（每个会话最多一个，总数不超过 ``max_parallel``）。

        Args:
            message_id: 关联的助手消息 ID
            session_id: 这条消息所属会话（界面据此判断「这个会话在跑吗」、只停这一个）
            messages: 完整对话消息（system + history + 当前提问）
            model: 模型显示名（用于 mock 文案）
            effort: 推理强度
            endpoint: 自定义模型配置（base_url / api_key / model_id），
                为 None 时使用内置示例引擎
            attachments: 附件列表
            image: 文生图接口配置（base_url / api_key / model_id），
                任意模型在需要配图时都会用它出图
            image_mode: 当前选中的就是文生图模型，直接把用户输入当提示词出图
            session_files: 本会话工作区文件清单，供工具按会话范围列举产物
            mode: 工作模式：``doc`` 文档 / ``code`` 编程（决定工具集与沙箱策略）
            code_root: 编程模式下该会话专属的工程目录（会话之间互不干扰）
            video: 文生视频接口配置；为空表示当前没有出视频能力
            video_mode: 当前选中的就是文生视频模型，直接把用户输入当提示词出片
            video_params: 视频参数（seconds / aspect_ratio / seed），来自界面上的选择
        """
        if self.is_message_running(message_id):
            return          # 同一条消息不会起两次

        # 取消事件必须先于编排器构造：worker 内部要把它注入长任务工具
        # （否则按 Esc 时 execute_python 的子进程不会被杀掉）
        cancel_event = threading.Event()
        box: dict[str, Any] = {"worker": None}

        def is_stopped() -> bool:
            """**本任务**是否已被要求停止。

            必须读「这一个 worker」的状态：以前读的是 ``self._worker``，并发之后会读到
            别人的 worker —— A 会话一停，B 会话也跟着停。
            """
            worker = box["worker"]
            return worker is not None and worker.aborted

        worker = self._build_worker(
            messages, model, effort, endpoint, attachments, image, image_mode,
            session_files, cancel_event, is_stopped, mode, code_root, video, video_mode,
            video_params,
        )
        worker.cancel_event = cancel_event
        # 产物按会话分目录：worker 线程起跑时用它（见 _StreamWorker.run）
        worker.session_id = session_id
        box["worker"] = worker      # 起跑前填好，闭包即刻可用

        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.token.connect(lambda delta: self.token.emit(message_id, delta))
        worker.thinking.connect(lambda delta: self.thinking.emit(message_id, delta))
        worker.plan.connect(lambda payload: self.plan.emit(message_id, payload))
        worker.tool.connect(lambda payload: self.tool.emit(message_id, payload))
        worker.artifact.connect(lambda payload: self.artifact.emit(message_id, payload))
        worker.content_final.connect(
            lambda text: self.content_final.emit(message_id, text)
        )
        worker.limit.connect(lambda payload: self.limit_hit.emit(message_id, payload))
        worker.note.connect(lambda text: self.note.emit(message_id, text))
        worker.draft.connect(lambda text: self.draft.emit(message_id, text))
        worker.open_url.connect(lambda text: self.open_url.emit(message_id, text))
        worker.output.connect(lambda text: self.output.emit(message_id, text))
        worker.progress.connect(lambda text: self.progress.emit(message_id, text))
        worker.finished.connect(lambda _id=message_id: self._finish(_id, ""))
        worker.failed.connect(lambda error, _id=message_id: self._finish(_id, error))
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(lambda _t=thread, _w=worker: self._cleanup(_t, _w))

        with self._lock:
            self._runs[message_id] = _Run(message_id, session_id, thread, worker)
        thread.start()
        self.started.emit(message_id)

    def _build_worker(
        self,
        messages: list[dict],
        model: str,
        effort: str,
        endpoint: dict | None,
        attachments: list[Attachment] | None,
        image: dict | None = None,
        image_mode: bool = False,
        session_files: list[dict] | None = None,
        cancel_event: threading.Event | None = None,
        is_stopped: Callable[[], bool] | None = None,
        mode: str = "doc",
        code_root: str = "",
        video: dict | None = None,
        video_mode: bool = False,
        video_params: dict | None = None,
    ) -> _StreamWorker:
        prompt = message_text(messages[-1]) if messages else ""
        # 「本任务是否已被要求停止」的判据由调用方传进来（每个任务一个），
        # 用 self 上的字段会在并发时读串。
        stop_check = is_stopped or (lambda: False)
        image_client = ImageClient(
            base_url=(image or {}).get("base_url", ""),
            api_key=(image or {}).get("api_key", ""),
            model_id=(image or {}).get("model_id", ""),
        )
        generator = image_client.generate if image_client.available else None
        background_generator = background_generator_for(image_client)

        video_client = VideoClient(
            base_url=(video or {}).get("base_url", ""),
            api_key=(video or {}).get("api_key", ""),
            model_id=(video or {}).get("model_id", ""),
        )
        # 智能体自己决定出视频时（generate_video 工具），进度经这座桥推给界面
        video_progress = _VideoProgress()

        def video_generate(prompt: str, **kwargs):
            kwargs.setdefault("on_progress", video_progress.report)
            return video_client.generate(prompt, **kwargs)

        video_generator = video_generate if video_client.available else None

        def video_images() -> list[str]:
            """本轮的图片附件 → 图生视频的参考图（只有文字时返回空 = 文生视频）。"""
            return _image_data_urls(attachments) if video_generator else []

        def image_images() -> list[str]:
            """本轮的图片附件 → 图生图的参考图。

            和视频那条是同一批素材，区别只在**谁说了算**：图生视频是本轮有图就一律
            用上；图生图由工具按提示词判断（见 ``GenerateImageTool``）—— 提示词在说
            人物 / 形象 / 定妆时才挂，要画流程图 / 风景时不掺用户的照片。
            """
            return _image_data_urls(attachments) if generator else []

        # 1) 当前选中的就是文生图模型：用户的输入直接作为图片提示词
        if image_mode:
            if generator is None:
                return _StreamWorker(
                    reply="未配置文生图 API Key，暂时无法出图。请在设置中补全后重试。"
                )
            return _StreamWorker(
                producer=_image_producer(image_client, prompt, stop_check)
            )

        # 1.5) 当前选中的是文生视频模型：输入直接作为视频提示词（带图就是图生视频）
        if video_mode:
            if video_generator is None:
                return _StreamWorker(
                    reply="当前没有可用的文生视频模型。请在服务端看板里配置一个"
                    "「类型 = 文生视频」的模型并登录后重试。"
                )
            return _StreamWorker(
                producer=_video_producer(
                    video_client, prompt, video_params, video_images(), stop_check
                )
            )

        # 2) 内置模型：示例内容（其中可能带 ```image 配图块）
        if not endpoint:
            reply = build_reply(prompt, model, effort)
            mock_thinking = build_thinking(prompt, model)
            if generator is None:
                return _StreamWorker(reply=reply, mock_thinking=mock_thinking)
            return _StreamWorker(
                producer=_mock_producer(reply, mock_thinking, generator, stop_check)
            )

        # 3) 自定义模型：多智能体编排 + 文生图（工具调用 / ```image 兜底）
        client = OpenAICompatClient(
            base_url=endpoint.get("base_url", ""),
            api_key=endpoint.get("api_key", ""),
            model_id=endpoint.get("model_id", ""),
            api_keys=endpoint.get("api_keys"),      # 多个 Key 会轮换，降低 429 概率
            reasoning=endpoint.get("reasoning", "auto"),
            effort=effort,                          # 低/中/高 → 推理参数
            model_name=endpoint.get("name", ""),
        )
        # 注意：这里要传入本次新建的 cancel_event，不能读 self._worker —— 
        # 该方法在 self._worker = worker 之前执行，读到的会是上一轮（或 None），
        # 导致取消事件永远为空、Esc 无法终止 execute_python 的子进程。
        engine = build_orchestrator(
            client=client,
            effort=effort,
            messages=messages,
            is_stopped=stop_check,
            cancel=cancel_event,
            image_generator=generator,
            background_generator=background_generator,
            session_files=session_files,
            mode=mode,
            workspace=Path(code_root) if code_root else None,
            video_generator=video_generator,
            video_images_provider=video_images if video_generator else None,
            # 长视频分段生成时，进度文案里要写「第 i/N 段」（跨段走整体百分比）
            video_progress=video_progress,
            # 传了照片又在做「定妆图 / 人物形象」时兜底成图生图（否则会按文字另画一张脸）
            image_images_provider=image_images if generator else None,
        )
        worker = _StreamWorker(producer=engine.run)
        worker.abort_hook = client.abort      # Esc 时立刻断掉在途请求
        worker.video_progress = video_progress
        return worker

    def stop(self, message_id: str = "") -> None:
        """请求停止：给了 ``message_id`` 只停那一个任务，不给就全停。

        刻意不在这里 ``wait()``：``run()`` 是阻塞式 producer，``quit()`` 对它无效，
        而在主线程 wait 会把界面冻住最长 2 秒。收尾由 worker 的
        finished/failed 信号驱动（用户主动停止会按 ``__aborted__`` 上报）。
        """
        for run in self._targets(message_id):
            run.worker.request_stop()
            if run.thread.isRunning():
                run.thread.quit()

    def terminate(self, message_id: str = "") -> None:
        """最后的手段：协作式停止没用时强行杀掉工作线程。

        只在用户明确确认「强制结束」后才调用——``terminate()`` 会在任意指令处
        中断线程，可能导致正在写的文档是半成品，或某些资源没来得及释放。
        """
        for run in self._targets(message_id):
            try:
                run.thread.terminate()
                run.thread.wait(2000)
            except RuntimeError:      # C++ 对象已销毁：无所谓，本来就是要停掉
                pass

    def _targets(self, message_id: str = "") -> list[_Run]:
        if message_id:
            run = self._runs.get(message_id)
            return [run] if run is not None else []
        return list(self._runs.values())

    # ------------------------------------------------------------------ 内部
    def _finish(self, message_id: str, error: str) -> None:
        """一个任务跑到尽头：先把它摘掉再回报界面。

        先摘掉的用意：界面收到 finished / failed 时会去查「这个会话还在跑吗」，
        要是那时任务还挂在表里，输入区就不会回到可发送状态。
        """
        with self._lock:
            run = self._runs.pop(message_id, None)
        if run is None:
            return          # finished 与 failed 只会来一个，来第二次就忽略
        if error:
            self.failed.emit(message_id, error)
        else:
            self.finished.emit(message_id)

    @Slot()
    def _cleanup(self, thread: QThread, worker: _StreamWorker) -> None:
        """线程收尾：把这一套 thread / worker 交给 Qt 释放。

        按对象引用来清理（而不是查 ``self._runs``）：任务已经从表里摘掉了，
        而且并发时不能误删别人的 thread。
        """
        worker.deleteLater()
        thread.deleteLater()
