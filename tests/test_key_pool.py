"""API Key 请求池：解析、轮换、限流冷却与共享。"""

from __future__ import annotations

import pytest

from paper_agent.services.key_pool import (
    ApiKeyPool,
    looks_rate_limited,
    mask_key,
    parse_api_keys,
    reset_shared_pools,
    shared_pool,
)


# ---------------------------------------------------------------- 限流识别
@pytest.mark.parametrize(
    "text",
    [
        "HTTP 429 · inference exceeds tpm/rpm limit",
        "HTTP 429",
        "Rate limit reached for requests",
        "Too Many Requests",
        "you exceeded your current quota",
        "接口限流，请稍后再试",
        "请求过于频繁",
        "当前并发数已达上限",
    ],
)
def test_looks_rate_limited_positive(text):
    assert looks_rate_limited(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "HTTP 401 · invalid api key",
        "HTTP 404 · model not found",
        "无法连接服务：timed out",
        "流式中断：Connection reset by peer",
        "",
    ],
)
def test_looks_rate_limited_negative(text):
    assert looks_rate_limited(text) is False


# ---------------------------------------------------------------- 解析与打码
def test_parse_api_keys_handles_common_separators():
    text = "sk-1, sk-2\nsk-3；sk-4；sk-5\t sk-1"
    assert parse_api_keys(text) == ["sk-1", "sk-2", "sk-3", "sk-4", "sk-5"]


def test_parse_api_keys_ignores_blank_and_duplicates():
    assert parse_api_keys("\n\n  \nsk-a\n\nsk-a\n") == ["sk-a"]
    assert parse_api_keys("") == []


def test_mask_key_never_leaks_full_secret():
    assert mask_key("") == ""
    assert mask_key("short") == "*****"
    masked = mask_key("sk-mqxi4DRpohd7TYn4ze9p9bZIftoDXXwk")
    assert masked.startswith("sk-m") and masked.endswith("XXwk")
    assert "mqxi4DRpohd7TYn4ze9p9" not in masked


# ---------------------------------------------------------------- 轮换
def test_pool_rotates_in_order():
    pool = ApiKeyPool(["a", "b", "c"])
    assert [pool.acquire() for _ in range(4)] == ["a", "b", "c", "a"]


def test_pool_dedupes_and_drops_blank():
    pool = ApiKeyPool(["a", " a ", "", "b", "a"])
    assert pool.keys == ("a", "b")
    assert pool.size == 2


def test_empty_pool_returns_blank():
    pool = ApiKeyPool([])
    assert pool.acquire() == ""
    assert pool.size == 0


def test_limited_key_is_skipped():
    clock = [1000.0]
    pool = ApiKeyPool(["a", "b", "c"], cooldown=60, clock=lambda: clock[0])

    assert pool.acquire() == "a"
    pool.report_limited("a")
    assert pool.available_count() == 2
    assert [pool.acquire() for _ in range(3)] == ["b", "c", "b"], "被限流的 Key 不再被选中"


def test_cooldown_expires_after_window():
    clock = [1000.0]
    pool = ApiKeyPool(["a", "b"], cooldown=60, clock=lambda: clock[0])
    pool.report_limited("a")
    assert pool.available_count() == 1

    clock[0] += 61
    assert pool.available_count() == 2
    assert "a" in {pool.acquire() for _ in range(4)}


def test_success_clears_cooldown():
    clock = [1000.0]
    pool = ApiKeyPool(["a", "b"], cooldown=600, clock=lambda: clock[0])
    pool.report_limited("a")
    assert pool.available_count() == 1
    pool.report_success("a")
    assert pool.available_count() == 2


def test_all_keys_limited_still_returns_earliest():
    clock = [1000.0]
    pool = ApiKeyPool(["a", "b"], cooldown=60, clock=lambda: clock[0])
    pool.report_limited("a")
    clock[0] += 45
    pool.report_limited("b")            # a 的恢复时间更早

    assert pool.available_count() == 0
    assert pool.acquire() == "a", "全都在冷却时选最早恢复的，避免直接失败"


def test_snapshot_masks_keys():
    pool = ApiKeyPool(["sk-aaaaaaaaaaaa", "sk-bbbbbbbbbbbb"])
    pool.report_limited("sk-bbbbbbbbbbbb")
    snapshot = pool.snapshot()
    assert [item["limited"] for item in snapshot] == [False, True]
    assert all("aaaaaaaaaaaa" not in item["key"] for item in snapshot)


# ---------------------------------------------------------------- 共享
@pytest.fixture(autouse=True)
def _clean_pools():
    reset_shared_pools()
    yield
    reset_shared_pools()


def test_shared_pool_reuses_same_model():
    first = shared_pool("https://api.x.com/v1", "m1", ["a", "b"])
    second = shared_pool("https://api.x.com/v1", "m1", ["a", "b"])
    assert first is second, "同一模型复用池，冷却状态才能跨请求保留"

    first.report_limited("a")
    assert second.available_count() == 1


def test_shared_pool_rebuilds_when_keys_change():
    first = shared_pool("https://api.x.com/v1", "m1", ["a"])
    second = shared_pool("https://api.x.com/v1", "m1", ["a", "b"])
    assert second is not first and second.size == 2


def test_shared_pool_separates_models():
    first = shared_pool("https://api.x.com/v1", "m1", ["a"])
    other = shared_pool("https://api.x.com/v1", "m2", ["a"])
    assert first is not other
