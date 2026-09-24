"""占位推理引擎：在接入真实模型前用于 UI 联调。

替换真实模型时，只需保持 :class:`AgentService` 的信号协议不变，
把 ``MockEngine.build_reply`` 换成实际的网络/本地推理调用即可。
"""

from __future__ import annotations

import random

TEMPLATE_OUTLINE = """## 一、论文结构建议

根据你的选题，建议采用如下章节结构（以本科/硕士论文通用规范为基准）：

| 章节 | 建议篇幅 | 关键内容 |
| --- | --- | --- |
| 第一章 绪论 | 10% | 研究背景、问题陈述、研究意义 |
| 第二章 相关研究 | 20% | 国内外研究现状、研究空白 |
| 第三章 方法设计 | 25% | 总体架构、关键技术、实验设计 |
| 第四章 实验与分析 | 30% | 数据集、评价指标、结果对比 |
| 第五章 总结与展望 | 15% | 结论、不足、后续工作 |

## 二、写作要点

1. **问题定义要收敛**：绪论中明确"研究什么"和"不研究什么"，避免范围过大。
2. **相关工作要有批判性**：按研究路线分类，而非逐篇罗列文献。
3. **方法可复现**：交代数据来源、参数配置与实验环境。
4. **实验要回答研究问题**：每个实验对应绪论提出的一个研究问题。

> 提示：先写第三章方法与第四章实验，再回头补绪论，逻辑会更连贯。

## 三、第一章示例段落

近年来，随着深度学习在自然语言理解任务中的广泛应用，^{*}面向特定领域的
文本生成研究逐渐成为热点。然而，现有方法在**领域知识一致性**与
**事实准确性**方面仍存在不足，主要表现为：

- 生成内容与领域术语表不一致；
- 长文本生成时出现事实漂移；
- 缺乏可验证的引用来源。

针对上述问题，本文提出一种融合检索增强与结构化约束的生成方法。

## 四、进度安排

```python
schedule = [
    ("第 1-2 周", "文献调研与选题确认"),
    ("第 3-5 周", "方法设计与基线复现"),
    ("第 6-9 周", "实验与数据分析"),
    ("第 10-12 周", "论文撰写与查重"),
]
for period, task in schedule:
    print(f"{period}: {task}")
```

需要我继续展开某一章，或先帮你整理参考文献吗？
"""

TEMPLATE_REVIEW = """## 文献综述框架

下面按**研究路线**梳理该方向的主要工作，并标注各自局限。

### 1. 基于规则与统计的方法

早期研究主要依赖人工特征与统计模型，可解释性强，但泛化能力有限。

### 2. 基于深度学习的方法

- 代表工作：A 等（2020）提出端到端框架，在公开数据集上取得显著提升；
- 局限：对标注数据依赖严重，低资源场景效果下降明显。

### 3. 检索增强与大模型方法

| 方法 | 优势 | 局限 |
| --- | --- | --- |
| RAG | 可引入外部知识 | 检索噪声影响生成 |
| 微调 | 领域适配好 | 训练成本高 |
| 提示工程 | 零成本接入 | 稳定性不足 |

> 研究空白：现有工作多关注英文场景，中文学术写作场景下的术语一致性问题尚未系统研究。

```text
检索 → 重排 → 生成 → 事实校验
```

接下来可以帮你把这三类工作整理成完整的综述段落。
"""

TEMPLATE_DEFAULT = """我已收到你的需求，以下是针对性的写作建议。

## 分析

1. 需求定位：{prompt}
2. 使用模型：`{model}`
3. 推理强度：{effort}

## 建议

- 明确论文的研究问题与创新点，建议用一句话概括；
- 先搭建三级标题骨架，再逐节填充内容；
- 每段遵循"观点 → 论据 → 小结"的结构；
- 图表需编号并在正文中引用说明。

> 提示：把已有草稿或参考文献发给我，我可以按章节继续扩写。

```json
{{
  "next_step": "确认章节大纲",
  "estimated_words": 1200
}}
```

需要我直接开始写第一章吗？
"""

TEMPLATE_IMAGE = """已收到配图需求，我先把画面要点整理清楚，随后调用文生图模型出图。

**画面要点**

- 画面主体：{brief}
- 风格建议：学术论文配图风格，简洁、留白充足、无多余文字
- 用途：可直接插入正文对应小节

```image
{prompt}
```

图片生成后会展示在下方，可点击查看或另存为。
"""

KEYWORD_MAP = [
    (("大纲", "结构", "目录", "章节"), TEMPLATE_OUTLINE),
    (("综述", "文献", "related work"), TEMPLATE_REVIEW),
]

# 命中这些词时，内置模型会输出 ```image 代码块触发文生图
IMAGE_KEYWORDS = (
    "配图", "插图", "示意图", "封面图", "概念图", "画一张", "画个", "画一副",
    "生成图片", "生成一张图", "来张图", "出一张图",
)


def build_reply(prompt: str, model: str = "", effort: str = "medium") -> str:
    """生成模拟回复（含 Markdown，用于展示渲染效果）。"""
    text = prompt or ""
    flat = text.strip().replace("\n", " ")
    if any(keyword in flat for keyword in IMAGE_KEYWORDS):
        brief = flat[:40] + ("…" if len(flat) > 40 else "")
        return _with_flavor(
            TEMPLATE_IMAGE.format(
                brief=brief or "按用户描述生成", prompt=flat or "学术论文配图"
            ),
            model,
            effort,
        )
    for keywords, template in KEYWORD_MAP:
        if any(keyword in text for keyword in keywords):
            return _with_flavor(template, model, effort)

    summary = text.strip().replace("\n", " ")
    if len(summary) > 40:
        summary = summary[:40] + "…"
    return _with_flavor(
        TEMPLATE_DEFAULT.format(prompt=summary or "（空）", model=model or "未指定", effort=effort),
        model,
        effort,
    )


def _with_flavor(text: str, model: str, effort: str) -> str:
    if effort == "low":
        text = text.split("## 三、")[0] + "\n\n（低推理强度：已省略后续展开内容）"
    elif effort == "high":
        text += "\n\n### 补充：可进一步展开的三个方向\n\n1. 理论分析\n2. 消融实验设计\n3. 与最新工作的对比实验\n"
    if model:
        text += f"\n\n<sub>由 `{model}` 生成 · 示例内容</sub>\n"
    return text


def build_thinking(prompt: str, model: str = "") -> str:
    """示例推理过程（用于展示可折叠的"思考过程"区块）。"""
    topic = (prompt or "").strip().replace("\n", " ")
    if len(topic) > 30:
        topic = topic[:30] + "…"
    return (
        f"用户的需求是：{topic or '（未填写）'}\n"
        "先确认任务类型：属于论文写作类，需要给出结构化、可执行的输出。\n"
        "检查是否缺少关键信息——研究方向、字数要求、章节范围暂未提供，"
        "应先给出通用结构，同时提示用户补充。\n"
        "组织输出：先给章节结构与篇幅分配，再写写作要点，"
        "最后附一段可直接使用的示例正文。\n"
        "确认排版：使用 Markdown，表格呈现篇幅分配，代码块给出进度安排。"
        + (f"\n当前使用模型：{model}" if model else "")
    )


def random_thinking_lines(prompt: str) -> list[str]:
    """模拟"思考过程"文本。"""
    candidates = [
        "分析选题范围与研究可行性…",
        "检索相关文献与最新工作…",
        "梳理论文结构与章节逻辑…",
        "生成初稿并校对学术表达…",
    ]
    return candidates[: 2 + random.randint(0, 2)]
