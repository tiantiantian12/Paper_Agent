"""AIGC 检测记录：保存每次检测的结论与逐句标注，便于事后回看 / 重新导出。

一次检测一个 JSON 文件（放在数据目录的 ``aigc/`` 下），结构与 :class:`SessionStore`
一致：原子写入、坏文件只跳过不连坐。记录里保留完整的句子判定与原文段落流，
所以打开历史记录还能看到和当时一模一样的标注稿、也能重新导出 PDF。

只保留最近 ``AIGC_RECORD_LIMIT`` 条：每条都带全文逐句结果，留太多会占空间。
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from paper_agent.core.constants import AIGC_DIR, AIGC_RECORD_LIMIT
from paper_agent.core.models import new_id
from paper_agent.services.aigc.analyzer import (
    LEVEL_HUMAN,
    LEVEL_ORDER,
    AigcReport,
    LayoutParagraph,
    SentenceVerdict,
)


@dataclass
class AigcRecord:
    """一条检测记录的索引信息（列表展示用，不含逐句明细）。"""

    id: str = field(default_factory=new_id)
    source: str = ""                # 文件名
    path: str = ""                  # 检测时的文件路径
    aigc_rate: float = 0.0
    risk: str = "low"
    risk_label: str = ""
    sentence_count: int = 0
    char_count: int = 0
    sensitivity: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    reference_paragraphs: int = 0
    heading_paragraphs: int = 0
    uncounted_count: int = 0
    created_at: float = field(default_factory=time.time)

    @property
    def summary(self) -> str:
        parts = []
        for level in LEVEL_ORDER:
            count = self.counts.get(level, 0)
            if count:
                parts.append(f"{count} 句")
        return " · ".join(parts)


def _verdict_to_dict(item: SentenceVerdict) -> dict:
    return {
        "text": item.text,
        "index": item.index,
        "paragraph": item.paragraph,
        "start": item.start,
        "end": item.end,
        "score": item.score,
        "level": item.level,
        "counted": item.counted,
        "reasons": list(item.reasons),
    }


def _verdict_from_dict(data: dict) -> SentenceVerdict:
    return SentenceVerdict(
        text=str(data.get("text", "")),
        index=int(data.get("index", 0)),
        paragraph=int(data.get("paragraph", 0)),
        start=int(data.get("start", -1)),
        end=int(data.get("end", -1)),
        score=float(data.get("score", 0.0)),
        level=str(data.get("level", LEVEL_HUMAN)),
        counted=bool(data.get("counted", True)),
        reasons=[str(text) for text in data.get("reasons", [])],
    )


def report_to_dict(report: AigcReport) -> dict:
    """把一次检测序列化成可落盘的字典（含逐句标注与原文段落流）。"""
    return {
        "id": new_id(),
        "source": report.source,
        "path": report.path,
        "sensitivity": report.sensitivity,
        "aigc_rate": report.aigc_rate,
        "average_score": report.average_score,
        "risk": report.risk,
        "risk_label": report.risk_label,
        "paragraph_count": report.paragraph_count,
        "char_count": report.char_count,
        "reference_paragraphs": report.reference_paragraphs,
        "heading_paragraphs": report.heading_paragraphs,
        "uncounted_count": report.uncounted_count,
        "thresholds": list(report.thresholds),
        "created_at": report.created_at,
        "counts": report.counts(),
        "sentences": [_verdict_to_dict(item) for item in report.sentences],
        "layout": [{"text": item.text, "kind": item.kind} for item in report.layout],
    }


def report_from_dict(data: dict) -> AigcReport:
    """反序列化；字段缺失一律回默认值，坏数据不能让界面崩掉。"""
    return AigcReport(
        source=str(data.get("source", "")),
        path=str(data.get("path", "")),
        sensitivity=str(data.get("sensitivity", "")),
        aigc_rate=float(data.get("aigc_rate", 0.0)),
        average_score=float(data.get("average_score", 0.0)),
        risk=str(data.get("risk", "low")),
        risk_label=str(data.get("risk_label", "")),
        sentences=[_verdict_from_dict(item) for item in data.get("sentences", [])],
        layout=[
            LayoutParagraph(text=str(item.get("text", "")), kind=str(item.get("kind", "body")))
            for item in data.get("layout", [])
        ],
        paragraph_count=int(data.get("paragraph_count", 0)),
        char_count=int(data.get("char_count", 0)),
        reference_paragraphs=int(data.get("reference_paragraphs", 0)),
        heading_paragraphs=int(data.get("heading_paragraphs", 0)),
        thresholds=tuple(data.get("thresholds", ())),
        created_at=float(data.get("created_at", time.time())),
    )


def record_from_dict(data: dict) -> AigcRecord:
    return AigcRecord(
        id=str(data.get("id", "")),
        source=str(data.get("source", "")),
        path=str(data.get("path", "")),
        aigc_rate=float(data.get("aigc_rate", 0.0)),
        risk=str(data.get("risk", "low")),
        risk_label=str(data.get("risk_label", "")),
        sentence_count=len(data.get("sentences", [])),
        char_count=int(data.get("char_count", 0)),
        sensitivity=str(data.get("sensitivity", "")),
        counts={str(k): int(v) for k, v in data.get("counts", {}).items()},
        reference_paragraphs=int(data.get("reference_paragraphs", 0)),
        heading_paragraphs=int(data.get("heading_paragraphs", 0)),
        uncounted_count=int(data.get("uncounted_count", 0)),
        created_at=float(data.get("created_at", time.time())),
    )


class AigcRecordStore:
    """检测记录读写（一条记录一个 JSON 文件）。"""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory or AIGC_DIR)
        self.directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 写
    def save_report(self, report: AigcReport) -> AigcRecord:
        """存一次检测：写入记录并在超出上限时清掉最旧的。"""
        payload = report_to_dict(report)
        record_id = str(payload["id"])
        self._write(record_id, payload)
        self._prune()
        return record_from_dict(payload)

    def _write(self, record_id: str, payload: dict) -> None:
        path = self.directory / f"{record_id}.json"
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    def _prune(self) -> None:
        """只留最近 ``AIGC_RECORD_LIMIT`` 条。

        记录写完就不再改动，所以按文件 mtime 排序即可 —— 不必把每条记录里的
        逐句明细都反序列化一遍（大文档的记录能到几百 KB）。
        """
        files = sorted(
            self.directory.glob("*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for file in files[AIGC_RECORD_LIMIT:]:
            try:
                file.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------ 读
    def load_records(self) -> list[AigcRecord]:
        """按检测时间倒序返回全部记录；坏文件只跳过。"""
        records: list[AigcRecord] = []
        broken: list[str] = []
        for file in self.directory.glob("*.json"):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except Exception:        # noqa: BLE001 - 坏文件只跳过，绝不连坐
                broken.append(file.name)
                continue
            if isinstance(data, dict):
                records.append(record_from_dict(data))
        if broken:
            print(
                f"[AIGC] 跳过 {len(broken)} 个无法解析的记录：{'、'.join(broken)}",
                file=sys.stderr,
            )
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    def load_report(self, record_id: str) -> AigcReport | None:
        path = self.directory / f"{record_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:            # noqa: BLE001
            return None
        return report_from_dict(data) if isinstance(data, dict) else None

    # ------------------------------------------------------------------ 删
    def delete(self, record_id: str) -> None:
        for suffix in (".json", ".json.tmp"):
            try:
                (self.directory / f"{record_id}{suffix}").unlink(missing_ok=True)
            except OSError:
                pass

    def clear(self) -> int:
        removed = 0
        for record in self.load_records():
            self.delete(record.id)
            removed += 1
        return removed
