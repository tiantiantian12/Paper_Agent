"""文生图客户端：请求体与供应商文档对齐、响应形态兼容。

供应商文档给的调用形态是::

    {"model": "sensenova-u1.5-lite", "prompt": "...", "n": 1, "size": "1024x1024",
     "output_format": "png", "response_format": "url", "watermark": true}
    # watermark=false：公测期间免费开放去水印

回归的坑：请求体里 ``watermark`` 以前默认 true，用户拿到的图右下角一直挂着平台
水印；另外只认 ``b64_json``，遇到只回图片地址的响应会直接报「未返回图片数据」。
"""

from __future__ import annotations

import base64

import pytest

from paper_agent.services import image_client as image_module
from paper_agent.services.image_client import ImageClient, ImageGenerationError

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake" * 8
PNG_B64 = base64.b64encode(PNG_BYTES).decode()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """产物落到临时目录，并拦掉真正的网络请求。"""
    monkeypatch.setattr(image_module, "ARTIFACTS_DIR", tmp_path)
    return ImageClient(base_url="https://token.sensenova.cn/v1", api_key="sk-x")


def _capture(monkeypatch, response: dict) -> list[dict]:
    sent: list[dict] = []

    def fake_post(payload):      # noqa: ANN001
        sent.append(payload)
        return response

    monkeypatch.setattr(ImageClient, "_post", lambda self, payload: fake_post(payload))
    return sent


# ---------------------------------------------------------------- 请求体
def test_payload_matches_vendor_spec(client, monkeypatch):
    sent = _capture(monkeypatch, {"data": [{"b64_json": PNG_B64}]})

    client.generate("一只海豹", size="1024x1024")
    payload = sent[0]

    assert payload["model"] == "sensenova-u1.5-lite"
    assert payload["prompt"] == "一只海豹"
    assert payload["size"] == "1024x1024"
    assert payload["output_format"] == "png"
    assert payload["response_format"] == "b64_json"
    assert set(payload) == {
        "model", "prompt", "n", "size", "output_format", "response_format", "watermark",
    }


def test_watermark_is_off_by_default(client, monkeypatch):
    """公测去水印免费，默认就该关掉（带水印的图不适合直接交付）。"""
    sent = _capture(monkeypatch, {"data": [{"b64_json": PNG_B64}]})

    client.generate("一只海豹")

    assert sent[0]["watermark"] is False


def test_watermark_can_still_be_requested(client, monkeypatch):
    sent = _capture(monkeypatch, {"data": [{"b64_json": PNG_B64}]})

    client.generate("一只海豹", watermark=True)

    assert sent[0]["watermark"] is True


def test_unsupported_size_falls_back(client, monkeypatch):
    sent = _capture(monkeypatch, {"data": [{"b64_json": PNG_B64}]})

    client.generate("一只海豹", size="9999x9999")

    assert sent[0]["size"] == "1024x1024"


def test_number_is_capped(client, monkeypatch):
    sent = _capture(monkeypatch, {"data": [{"b64_json": PNG_B64}]})

    client.generate("一只海豹", number=99)

    assert sent[0]["n"] <= 4


# ---------------------------------------------------------------- 响应形态
def test_base64_response_is_saved(client, monkeypatch):
    _capture(monkeypatch, {"data": [{"b64_json": PNG_B64}]})

    results = client.generate("一只海豹")

    assert len(results) == 1
    assert results[0]["kind"] == "image"
    assert results[0]["name"].endswith(".png")
    with open(results[0]["path"], "rb") as file:
        assert file.read() == PNG_BYTES


def test_url_response_is_downloaded(client, monkeypatch):
    """供应商文档的示例就是只回 url：要能自己下回来落盘，不能报「未返回图片数据」。"""
    _capture(monkeypatch, {"data": [{"url": "https://cdn.example.com/a.png"}]})
    downloaded: list[str] = []

    def fake_download(self, url):      # noqa: ANN001
        downloaded.append(url)
        return PNG_BYTES

    monkeypatch.setattr(ImageClient, "_download", fake_download)

    results = client.generate("一只海豹")

    assert downloaded == ["https://cdn.example.com/a.png"]
    with open(results[0]["path"], "rb") as file:
        assert file.read() == PNG_BYTES


def test_empty_data_raises(client, monkeypatch):
    _capture(monkeypatch, {"data": []})

    with pytest.raises(ImageGenerationError, match="未返回图片数据"):
        client.generate("一只海豹")


def test_blank_prompt_rejected(client):
    with pytest.raises(ImageGenerationError, match="提示词为空"):
        client.generate("   ")


# ---------------------------------------------------------------- 退避重试
def test_queue_full_is_retried(client, monkeypatch):
    """实测供应商会回「503 生图队列已满，请稍后重试」：那只是忙，等一会儿再来。"""
    monkeypatch.setattr(image_module, "RETRY_DELAYS", (0.0, 0.0))     # 测试里别真等
    attempts: list[int] = []

    def fake_post_once(self, payload):      # noqa: ANN001 - 挂在类上，第一个参数是 self
        attempts.append(1)
        if len(attempts) == 1:
            raise ImageGenerationError(
                'HTTP 503 · {"error":{"message":"生图队列已满，请稍后重试"}}'
            )
        return {"data": [{"b64_json": PNG_B64}]}

    monkeypatch.setattr(ImageClient, "_post_once", fake_post_once)

    results = client.generate("一只海豹")

    assert len(attempts) == 2, "队列满要重试一次"
    assert len(results) == 1


def test_content_error_is_not_retried(client, monkeypatch):
    """字段写错这类错误（400）重试多少次都一样，立刻失败并说清楚。"""
    monkeypatch.setattr(image_module, "RETRY_DELAYS", (0.0, 0.0))
    attempts: list[int] = []

    def fake_post_once(self, payload):      # noqa: ANN001
        attempts.append(1)
        raise ImageGenerationError(
            'HTTP 400 · {"error":{"message":"output_format 不是文生图队列支持的字段"}}'
        )

    monkeypatch.setattr(ImageClient, "_post_once", fake_post_once)

    with pytest.raises(ImageGenerationError, match="output_format"):
        client.generate("一只海豹")

    assert len(attempts) == 1


def test_missing_key_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(image_module, "ARTIFACTS_DIR", tmp_path)
    bare = ImageClient(base_url="https://token.sensenova.cn/v1", api_key="")

    assert bare.available is False
    with pytest.raises(ImageGenerationError, match="未配置文生图 API Key"):
        bare.generate("一只海豹")


# ---------------------------------------------------------------- Agnes 口味
# 实测（打真接口）：Agnes 不认 output_format（400），n 只能为 1（400），
# 返回格式与参考图都在 extra_body 里。所以请求体必须按供应商分开拼。
PNG_DATA_URL = "data:image/png;base64," + PNG_B64


@pytest.fixture
def agnes(tmp_path, monkeypatch):
    monkeypatch.setattr(image_module, "ARTIFACTS_DIR", tmp_path)
    return ImageClient(
        base_url="https://api.agnes-ai.cn/v1",
        api_key="sk-x",
        model_id="agnes-image-2.5-flash",
    )


def test_provider_is_detected_from_base_url(agnes, client):
    assert agnes.provider == "agnes"
    assert client.provider == "standard"


def test_provider_detected_behind_server_proxy(tmp_path, monkeypatch):
    """内置模型走服务端代理：地址是我们自己的，只能靠模型名认口味。"""
    monkeypatch.setattr(image_module, "ARTIFACTS_DIR", tmp_path)
    proxied = ImageClient(
        base_url="http://127.0.0.1:8000/api/llm/v1",
        api_key="pk-user",
        model_id="agnes-image-2.5-flash",
    )

    assert proxied.provider == "agnes"

    sent = _capture(monkeypatch, {"data": [{"b64_json": PNG_B64}]})
    proxied.generate("一座浮空城")
    assert set(sent[0]) == {"model", "prompt", "size", "extra_body"}


def test_agnes_payload_is_minimal(agnes, monkeypatch):
    """只发 model / prompt / size / extra_body —— 多一个字段就被 400 打回。"""
    sent = _capture(monkeypatch, {"data": [{"b64_json": PNG_B64}]})

    agnes.generate("一座浮空城", size="1024x768")
    payload = sent[0]

    assert set(payload) == {"model", "prompt", "size", "extra_body"}
    assert payload["model"] == "agnes-image-2.5-flash"
    assert payload["size"] == "1024x768"
    assert payload["extra_body"] == {"response_format": "url"}


def test_agnes_reference_image_goes_into_extra_body(agnes, monkeypatch):
    """图生图：参考图放 extra_body.image（官方形态）。"""
    sent = _capture(monkeypatch, {"data": [{"b64_json": PNG_B64}]})

    agnes.generate("改成赛博朋克夜景，保持构图", reference_images=[PNG_DATA_URL])

    assert sent[0]["extra_body"]["image"] == [PNG_DATA_URL]
    assert sent[0]["extra_body"]["response_format"] == "url"
    assert "image" not in sent[0], "别在顶层再放一个 image"


def test_agnes_splits_multiple_images_into_requests(agnes, monkeypatch):
    """Agnes 一次只出 1 张：要多张就多发几次，而不是塞 n（会 400）。"""
    sent = _capture(monkeypatch, {"data": [{"b64_json": PNG_B64}]})

    results = agnes.generate("一只海豹", number=3)

    assert len(sent) == 3
    assert all("n" not in payload for payload in sent)
    assert len(results) == 3
    assert len({item["name"] for item in results}) == 3, "落盘文件名不能撞车"


def test_agnes_number_is_capped(agnes, monkeypatch):
    sent = _capture(monkeypatch, {"data": [{"b64_json": PNG_B64}]})

    agnes.generate("一只海豹", number=99)

    assert len(sent) <= 4


def test_standard_flavor_keeps_n_and_options(client, monkeypatch):
    """标准口味不受影响：还是一次请求出多张。"""
    sent = _capture(monkeypatch, {"data": [{"b64_json": PNG_B64}]})

    client.generate("一只海豹", number=2)

    assert len(sent) == 1
    assert sent[0]["n"] == 2
    assert "extra_body" not in sent[0]


def test_standard_reference_goes_to_top_level_image(client, monkeypatch):
    sent = _capture(monkeypatch, {"data": [{"b64_json": PNG_B64}]})

    client.generate("改成夜景", reference_images=[PNG_DATA_URL])

    assert sent[0]["image"] == [PNG_DATA_URL]


def test_agnes_url_response_is_downloaded(agnes, monkeypatch):
    """Agnes 回的 data[0] 同时带 url 与 b64_json；只给 url 时也要能下回来。"""
    _capture(monkeypatch, {"data": [{"url": "https://cdn.example.com/a.jpg"}]})
    monkeypatch.setattr(ImageClient, "_download", lambda self, url: PNG_BYTES)

    results = agnes.generate("一只海豹")

    assert len(results) == 1
    with open(results[0]["path"], "rb") as file:
        assert file.read() == PNG_BYTES


# ---------------------------------------------------------------- 地址工具
def test_image_data_url_encodes_local_file(tmp_path):
    from paper_agent.services.image_client import image_data_url

    target = tmp_path / "图.png"
    target.write_bytes(PNG_BYTES)

    assert image_data_url(target) == PNG_DATA_URL


def test_image_data_url_handles_missing_file(tmp_path):
    from paper_agent.services.image_client import image_data_url

    assert image_data_url(tmp_path / "没有.png") == ""
