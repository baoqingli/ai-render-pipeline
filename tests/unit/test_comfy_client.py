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


async def test_execution_error_in_messages_fails_fast():
    """终审 Important #4：执行错误（坏权重/OOM）出现在 status.messages 时
    立即抛 COMFY_ERROR（携带 node_type/exception_message），不等满超时。"""
    def handler(request):
        return httpx.Response(200, json={"pid-err": {
            "status": {"completed": False, "status_str": "error", "messages": [
                ["execution_error", {
                    "node_type": "ControlNetLoader",
                    "exception_message": "controlnet weight shape mismatch",
                }]]},
            "outputs": {}}})

    c = make_client(handler)
    c._timeout = 5  # 若实现错误会空转 5s 后误报 COMFY_TIMEOUT
    with pytest.raises(ComfyError) as ei:
        await c.wait_for_result("pid-err", poll_s=0.01)
    assert ei.value.code == "COMFY_ERROR"
    assert "ControlNetLoader" in str(ei.value)
    assert "controlnet weight shape mismatch" in str(ei.value)


async def test_execution_error_in_status_str_fails_fast():
    def handler(request):
        return httpx.Response(200, json={"pid-err2": {
            "status": {"completed": False, "status_str": "execution error"},
            "outputs": {}}})

    c = make_client(handler)
    c._timeout = 5
    with pytest.raises(ComfyError) as ei:
        await c.wait_for_result("pid-err2", poll_s=0.01)
    assert ei.value.code == "COMFY_ERROR"


async def test_completed_without_images_raises_comfy_error_not_timeout():
    """completed 即终态：无图不应空转到超时误报 COMFY_TIMEOUT。"""
    def handler(request):
        return httpx.Response(200, json={"pid-empty": {
            "status": {"completed": True, "status_str": "executed"},
            "outputs": {"13": {}}}})

    c = make_client(handler)
    c._timeout = 5
    with pytest.raises(ComfyError) as ei:
        await c.wait_for_result("pid-empty", poll_s=0.01)
    assert ei.value.code == "COMFY_ERROR"
    assert "completed without image outputs" in str(ei.value)
