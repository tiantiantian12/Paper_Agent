"""自动重试策略：限流要等得更久、试得更多；普通抖动快速重试。"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QLabel

from paper_agent.core.models import Message
from paper_agent.ui.chat.message_widget import MessageWidget
from paper_agent.ui.main_window import (
    DEFAULT_RETRY_DELAYS_MS,
    RATE_LIMIT_RETRY_DELAYS_MS,
    retry_plan,
)


def test_rate_limit_plan_waits_longer():
    limit, delays = retry_plan("HTTP 429 · inference exceeds tpm/rpm limit")
    assert limit == 3
    assert delays == RATE_LIMIT_RETRY_DELAYS_MS
    assert delays[0] >= 20_000, "限流必须等额度窗口，1.5 秒重试必然再撞"


def test_default_plan_retries_quickly():
    limit, delays = retry_plan("无法连接服务：timed out")
    assert (limit, delays) == (2, DEFAULT_RETRY_DELAYS_MS)
    assert delays[0] < 5_000


@pytest.mark.parametrize(
    "error",
    ["HTTP 500 · server error", "流式中断：Connection reset by peer", "HTTP 401 · bad key"],
)
def test_non_rate_limit_errors_use_default_plan(error):
    assert retry_plan(error) == (2, DEFAULT_RETRY_DELAYS_MS)


# ---------------------------------------------------------------- 状态呈现
def _assistant_widget(qapp) -> MessageWidget:
    return MessageWidget(Message(role="assistant", content="写了一半的正文"))


def test_error_status_is_marked_and_colored(qapp):
    widget = _assistant_widget(qapp)
    widget.finish_stream("error", "HTTP 429 · inference exceeds tpm/rpm limit")

    assert "429" in widget.status_label.text()
    assert widget.status_label.property("state") == "error", "失败不能用「还在写」的样式"


def test_note_uses_waiting_state(qapp):
    widget = _assistant_widget(qapp)
    widget.finish_stream("error", "HTTP 429 · 限流")
    widget.mark_note("接口限流，20 秒后自动重试（第 1/3 次）")

    assert "20 秒后自动重试" in widget.status_label.text()
    assert widget.status_label.property("state") == "waiting"


def test_note_is_cleared_on_next_stream(qapp):
    widget = _assistant_widget(qapp)
    widget.mark_note("接口限流，20 秒后自动重试（第 1/3 次）")
    widget.finish_stream("streaming")          # 新的一轮开始

    assert "自动重试" not in widget.status_label.text(), "上一轮的倒计时不该带到新一轮"


def test_status_label_object_name_for_qss(qapp):
    widget = _assistant_widget(qapp)
    assert isinstance(widget.status_label, QLabel)
    assert widget.status_label.objectName() == "statusLabel"
