# app/engines/comfy_client.py
import asyncio
import time
from pathlib import Path

import httpx


class ComfyError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ComfyClient:
    def __init__(self, base_url: str, timeout_s: int = 300) -> None:
        self._timeout = timeout_s
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def upload_image(self, path: str) -> str:
        with open(path, "rb") as fh:
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
            if history and history.get("status", {}).get("completed"):
                # Adapted from the brief: the brief's test indexes the result by
                # node id (outputs["13"]["images"]), so we return the whole node
                # mapping {"<node_id>": {"images": [...]}} rather than the first
                # image-bearing node dict. We still wait until some node has
                # images before returning.
                outputs = history["outputs"]
                if any("images" in node_out for node_out in outputs.values()):
                    return outputs
            await asyncio.sleep(poll_s)
        raise ComfyError("COMFY_TIMEOUT", f"prompt {prompt_id} not finished")

    async def fetch_image(self, filename: str, subfolder: str, img_type: str) -> bytes:
        resp = await self._client.get("/view", params={
            "filename": filename, "subfolder": subfolder, "type": img_type})
        resp.raise_for_status()
        return resp.content
