# app/engines/comfy_client.py
import asyncio
import time

import httpx


class ComfyError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _execution_error(history: dict) -> tuple[str, str] | None:
    """从 history JSON 提取执行错误（坏权重/OOM 等）。

    ComfyUI 把它放在 status.messages 的 ["execution_error",
    {node_type, exception_message, ...}] 里，status_str 含 execution_error
    （或 "error"）时同样视为终态错误。返回 (node_type, exception_message)。
    """
    status = history.get("status", {})
    for msg in status.get("messages", []) or []:
        if (isinstance(msg, (list, tuple)) and len(msg) >= 2
                and msg[0] == "execution_error"):
            data = msg[1] if isinstance(msg[1], dict) else {}
            return (str(data.get("node_type", "unknown")),
                    str(data.get("exception_message", "execution error")))
    status_str = str(status.get("status_str", ""))
    if "execution_error" in status_str.replace(" ", "_"):
        return ("unknown", status_str)
    if status_str == "error":
        return ("unknown", "execution error (no detail in history)")
    return None


class ComfyClient:
    def __init__(self, base_url: str, timeout_s: int = 300) -> None:
        self._timeout = timeout_s
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def upload_image(self, path: str) -> str:
        # 控制图一次性小文件上传，Phase 1 不引入线程池开销
        with open(path, "rb") as fh:  # noqa: ASYNC230
            resp = await self._client.post(
                "/upload/image", files={"image": fh}, data={"overwrite": "true"})
        resp.raise_for_status()
        return resp.json()["name"]

    async def queue_prompt(self, workflow: dict) -> str:
        resp = await self._client.post("/prompt", json={"prompt": workflow, "client_id": "arp"})
        if resp.status_code != 200:
            raise ComfyError("COMFY_ERROR", f"queue failed: {resp.text[:200]}")
        return resp.json()["prompt_id"]

    async def wait_for_result(self, prompt_id: str, poll_s: float = 1.0) -> dict:
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            resp = await self._client.get(f"/history/{prompt_id}")
            resp.raise_for_status()
            history = resp.json().get(prompt_id)
            if history:
                # 终审 Important #4：执行错误（坏权重/OOM 等）立即失败，
                # 不轮询到 300s 超时后误报 COMFY_TIMEOUT
                err = _execution_error(history)
                if err is not None:
                    raise ComfyError("COMFY_ERROR", f"{err[0]}: {err[1]}")
                if history.get("status", {}).get("completed"):
                    # Adapted from the brief: the brief's test indexes the result by
                    # node id (outputs["13"]["images"]), so we return the whole node
                    # mapping {"<node_id>": {"images": [...]}} rather than the first
                    # image-bearing node dict. We still wait until some node has
                    # images before returning.
                    outputs = history["outputs"]
                    if any("images" in node_out for node_out in outputs.values()):
                        return outputs
                    # completed 即终态：无图继续轮询只会空转到超时，同样按
                    # 执行错误立即上报
                    raise ComfyError(
                        "COMFY_ERROR",
                        f"prompt {prompt_id} completed without image outputs")
            await asyncio.sleep(poll_s)
        raise ComfyError("COMFY_TIMEOUT", f"prompt {prompt_id} not finished")

    async def fetch_image(self, filename: str, subfolder: str, img_type: str) -> bytes:
        resp = await self._client.get("/view", params={
            "filename": filename, "subfolder": subfolder, "type": img_type})
        resp.raise_for_status()
        return resp.content
