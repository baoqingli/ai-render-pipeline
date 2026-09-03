# tests/unit/test_comfy_client.py
import json
import pytest
import httpx

from app.engines.comfy_client import ComfyClient, ComfyError


def make_client(handler) -> ComfyClient:
    transport = httpx.MockTransport(handler)
    c = ComfyClient.__new__(ComfyClient)
    c._client = httpx.AsyncClient(transport=transport, base_url="http://comfy")
    c._timeout = 5
    return c


async def test_upload_queue_wait_fetch_roundtrip():
    state = {"history_ready": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": "uploaded.png"})
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "pid-1"})
        if request.url.path == "/history/pid-1":
            if not state["history_ready"]:
                state["history_ready"] = True
                return httpx.Response(200, json={})   # 第一次未完成
            return httpx.Response(200, json={
                "pid-1": {"status": {"completed": True},
                          "outputs": {"13": {"images": [
                              {"filename": "out.png", "subfolder": "", "type": "output"}]}}}})
        if request.url.path == "/view":
            return httpx.Response(200, content=b"PNGDATA")
        return httpx.Response(404)

    c = make_client(handler)
    assert await c.upload_image("local.png") == "uploaded.png"
    assert await c.queue_prompt({}) == "pid-1"
    outputs = await c.wait_for_result("pid-1", poll_s=0.01)
    assert outputs["13"]["images"][0]["filename"] == "out.png"
    assert await c.fetch_image("out.png", "", "output") == b"PNGDATA"


async def test_timeout_raises_comfy_error():
    def handler(request): return httpx.Response(200, json={})
    c = make_client(handler)
    c._timeout = 0.05
    with pytest.raises(ComfyError):
        await c.wait_for_result("never", poll_s=0.02)
