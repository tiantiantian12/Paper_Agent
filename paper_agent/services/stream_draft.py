"""把工具调用参数里的长文本实时抽出来，供界面「边写边看」。

模型写论文时，正文不是以正文流的形式吐出来的，而是塞在 ``create_docx`` 的
``markdown`` 参数里。参数是**逐片到达**且经过 JSON 转义的，所以在参数还没写完、
JSON 也还不合法的情况下，只能做「增量解码」：

1. 在已到达的片段里定位 ``"markdown": "`` 的起始位置；
2. 从那里开始按 JSON 转义规则逐字符解码，遇到半个转义序列（``\\`` / ``\\u12``）
   就停下等待下一片；
3. 每次 ``feed()`` 只返回**新增**的那段明文，界面照抄即可。
"""

from __future__ import annotations

import json
import re

# 这些字段里装的才是正文（文件名、doc_type 之类的短参数没有实时展示价值）
DRAFT_KEYS = ("markdown", "content", "text")

# 字段名一定出现在参数最前面，只在开头一小段里找，避免每片都全量正则扫描
_LOCATE_WINDOW = 4096

# 单次实时草稿的展示上限，防止异常情况下把内存吃光
MAX_DRAFT_CHARS = 200_000


class DraftExtractor:
    """从流式到达的工具调用参数（JSON 文本）里增量提取长文本字段。"""

    def __init__(self, keys: tuple[str, ...] = DRAFT_KEYS) -> None:
        self._keys = keys
        self._raw = ""
        self._pos = -1        # 明文字符串在 raw 里的当前解码位置（-1 = 还没定位到）
        self._finished = False
        self.total = 0        # 已解出的明文字符数

    @property
    def finished(self) -> bool:
        """字符串已经收尾（遇到结束引号）或确定没有可提取的字段。"""
        return self._finished

    def feed(self, fragment: str) -> str:
        """喂入新到的一片原始参数，返回新解出的明文（可能为空）。"""
        if self._finished or not fragment:
            return ""
        self._raw += fragment

        if self._pos < 0 and not self._locate():
            return ""

        text = self._decode_available()
        if text:
            self.total += len(text)
            if self.total > MAX_DRAFT_CHARS:
                self._finished = True
        return text

    # ------------------------------------------------------------------ 内部
    def _locate(self) -> bool:
        """在参数开头找到长文本字段的起始位置。"""
        window = self._raw[:_LOCATE_WINDOW]
        for key in self._keys:
            match = re.search(r'"' + re.escape(key) + r'"\s*:\s*"', window)
            if match:
                self._pos = match.end()
                return True
            # 字段存在但值不是字符串（null / 数字 / 数组）：没什么可实时展示的，放弃。
            # 注意要等到冒号后面的字符真的到了才能下结论——分片可能正好停在
            # `"markdown":` 之后，那时看成「值不是字符串」就会把整篇正文丢掉。
            if re.search(r'"' + re.escape(key) + r'"\s*:\s*[^"\s]', window):
                self._finished = True
                return False
        if len(self._raw) >= _LOCATE_WINDOW:
            self._finished = True       # 扫了开头还没有，说明参数结构不是预期的
        return False

    def _decode_available(self) -> str:
        """把已经完整到达的部分解码出来（保留停在半个转义上的尾巴）。"""
        raw = self._raw
        size = len(raw)
        parts: list[str] = []

        while self._pos < size:
            char = raw[self._pos]
            if char == '"':                      # 明文字符串结束
                self._finished = True
                break
            if char != "\\":
                parts.append(char)
                self._pos += 1
                continue

            escape = raw[self._pos : self._pos + 2]
            if len(escape) < 2:
                break                            # 只有反斜杠，等下一片
            if escape[1] == "u":
                chunk = raw[self._pos : self._pos + 6]
                if len(chunk) < 6:
                    break                        # \uXXXX 还没到齐
                try:
                    parts.append(json.loads(f'"{chunk}"'))
                except ValueError:
                    self._finished = True
                    break
                self._pos += 6
                continue
            try:
                parts.append(json.loads(f'"{escape}"'))
            except ValueError:
                self._finished = True
                break
            self._pos += 2

        return "".join(parts)
