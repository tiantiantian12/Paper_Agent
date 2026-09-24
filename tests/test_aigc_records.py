"""AIGC 检测记录：保存、回看、导出、删除与上限裁剪。"""

from __future__ import annotations

import os

from PySide6.QtWidgets import QMessageBox

from paper_agent.services.aigc import analyze_text, export_report_pdf
from paper_agent.services.aigc import records

SAMPLE = (
    "综上所述，随着人工智能技术的不断发展，本研究针对当前制造业转型升级过程中存在的实际问题，"
    "提出了一种基于深度学习的智能优化方法。\n\n"
    "首先，本文对相关领域的研究现状进行了系统梳理，明确了现有方法在精度与效率方面存在的不足。"
    "其次，在此基础上构建了融合多源数据的预测模型，并通过对比实验验证了所提方法的有效性。\n\n"
    "我在车间蹲了两个月才想明白这件事。师傅姓王，干了二十多年钳工，他跟我说，机器好不好用，"
    "看的是换模时要不要骂人。数据不漂亮，样本也少，只有十七组，但我敢拿它去跟车间主任掰扯。"
)


def _store(tmp_path) -> records.AigcRecordStore:
    return records.AigcRecordStore(tmp_path / "aigc")


# ---------------------------------------------------------------- 存取
def test_save_report_returns_index_record(tmp_path):
    store = _store(tmp_path)
    report = analyze_text(SAMPLE, source="论文.docx")
    record = store.save_report(report)

    assert record.source == "论文.docx"
    assert record.sentence_count == report.sentence_count
    assert abs(record.aigc_rate - report.aigc_rate) < 0.05
    assert record.counts == report.counts()


def test_load_report_round_trips_sentences_and_layout(tmp_path):
    """回看记录时要能重新导出，所以逐句标注与原文段落流必须完整保留。"""
    store = _store(tmp_path)
    report = analyze_text(SAMPLE, source="论文.docx")
    record = store.save_report(report)

    loaded = store.load_report(record.id)
    assert loaded is not None
    assert [item.text for item in loaded.sentences] == [item.text for item in report.sentences]
    assert [item.level for item in loaded.sentences] == [item.level for item in report.sentences]
    assert [(block.text, block.kind) for block in loaded.layout] == [
        (block.text, block.kind) for block in report.layout
    ]
    assert loaded.reference_paragraphs == report.reference_paragraphs


def test_reloaded_report_can_be_exported(tmp_path):
    store = _store(tmp_path)
    record = store.save_report(analyze_text(SAMPLE, source="论文.docx"))
    target = tmp_path / "再次导出.pdf"
    export_report_pdf(store.load_report(record.id), target)
    assert target.is_file() and target.stat().st_size > 1000


def test_load_records_newest_first(tmp_path):
    store = _store(tmp_path)
    for index in range(3):
        report = analyze_text(SAMPLE, source=f"论文{index}.txt")
        report.created_at = 1000.0 + index
        store.save_report(report)
    records_list = store.load_records()
    assert [item.created_at for item in records_list] == [1002.0, 1001.0, 1000.0]


def test_broken_file_is_skipped(tmp_path):
    store = _store(tmp_path)
    store.save_report(analyze_text(SAMPLE, source="好的.txt"))
    (store.directory / "bad.json").write_text("{ 不是 json", encoding="utf-8")
    assert len(store.load_records()) == 1


# ---------------------------------------------------------------- 删除
def test_delete_and_clear(tmp_path):
    store = _store(tmp_path)
    first = store.save_report(analyze_text(SAMPLE, source="一.txt"))
    store.save_report(analyze_text(SAMPLE, source="二.txt"))

    store.delete(first.id)
    assert [item.source for item in store.load_records()] == ["二.txt"]
    assert store.load_report(first.id) is None

    assert store.clear() == 1
    assert store.load_records() == []


# ---------------------------------------------------------------- 上限
def test_prune_keeps_only_recent_records(tmp_path, monkeypatch):
    monkeypatch.setattr(records, "AIGC_RECORD_LIMIT", 3)
    store = _store(tmp_path)
    for index in range(5):
        report = analyze_text(SAMPLE, source=f"论文{index}.txt")
        report.created_at = 1000.0 + index
        payload = records.report_to_dict(report)
        # 直接落盘（绕过保存时的裁剪），再把 mtime 对齐到检测时间，顺序才可预期
        store._write(str(payload["id"]), payload)
        os.utime(store.directory / f"{payload['id']}.json", (report.created_at,) * 2)

    store._prune()
    remaining = store.load_records()
    assert len(remaining) == 3
    assert [item.created_at for item in remaining] == [1004.0, 1003.0, 1002.0]


# ---------------------------------------------------------------- 界面
def _history(tmp_path):
    from paper_agent.ui.dialogs.aigc_history_dialog import AigcHistoryDialog

    return AigcHistoryDialog(store=_store(tmp_path))


def test_history_dialog_empty_state(qapp, tmp_path):
    dialog = _history(tmp_path)
    assert dialog.record_list.count() == 0
    assert "还没有检测记录" in dialog.empty_label.text()
    assert dialog.export_button.isEnabled() is False
    assert dialog.clear_button.isEnabled() is False
    assert "暂无记录" in dialog.subtitle.text()


def test_history_dialog_shows_and_loads_record(qapp, tmp_path):
    store = _store(tmp_path)
    store.save_report(analyze_text(SAMPLE, source="论文.txt"))
    dialog = _history(tmp_path)

    assert dialog.record_list.count() == 1
    assert dialog.result_view.report is not None, "打开记录就该看到当时的逐句标注"
    assert dialog.result_view.report.source == "论文.txt"
    assert dialog.export_button.isEnabled()
    assert "共 1 条记录" in dialog.subtitle.text()


def test_history_dialog_delete_current(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    store = _store(tmp_path)
    store.save_report(analyze_text(SAMPLE, source="论文.txt"))
    dialog = _history(tmp_path)

    dialog._delete_current()
    assert store.load_records() == []
    assert dialog.record_list.count() == 0
    assert dialog.export_button.isEnabled() is False


def test_history_dialog_clear_all(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    store = _store(tmp_path)
    store.save_report(analyze_text(SAMPLE, source="一.txt"))
    store.save_report(analyze_text(SAMPLE, source="二.txt"))
    dialog = _history(tmp_path)

    dialog._clear_all()
    assert store.load_records() == []
    assert dialog.record_list.count() == 0
