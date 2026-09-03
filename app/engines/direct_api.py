# app/engines/direct_api.py
import base64
import time
from pathlib import Path
from typing import Protocol

import httpx

from app.engines.registry import ModelInfo
from app.infra.pools import TokenBucket
from app.models.rendering import PromptPair, RenderResult, RenderTask


class ApiAdapter(Protocol):
    async def generate(self, prompt: PromptPair, image: Path, model_name: str,
                       http: httpx.AsyncClient) -> bytes: ...


class GeminiAdapter:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def generate(self, prompt: PromptPair, image: Path, model_name: str,
                       http: httpx.AsyncClient) -> bytes:
        b64 = base64.b64encode(image.read_bytes()).decode()
        resp = await http.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
            params={"key": self.api_key},
            json={"contents": [{"parts": [
                {"text": (prompt.positive +
                          " | Redecorate this white-model interior render keeping exact room"
                          " geometry, wall positions, windows and doors.")},
                {"inline_data": {"mime_type": "image/png", "data": b64}}]}]},
        )
        resp.raise_for_status()
        data = resp.json()["candidates"][0]["content"]["parts"][0]["inline_data"]["data"]
        return base64.b64decode(data)


class OpenAIImageAdapter:
    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1") -> None:
        self.api_key = api_key
        self.base_url = base_url

    async def generate(self, prompt: PromptPair, image: Path, model_name: str,
                       http: httpx.AsyncClient) -> bytes:
        with open(image, "rb") as fh:
            resp = await http.post(
                f"{self.base_url}/images/edits",
                headers={"Authorization": f"Bearer {self.api_key}"},
                data={"model": model_name, "prompt": prompt.positive, "n": "1"},
                files={"image": (image.name, fh, "image/png")},
            )
        resp.raise_for_status()
        return base64.b64decode(resp.json()["data"][0]["b64_json"])


class DirectAPIEngine:
    def __init__(self, http: httpx.AsyncClient,
                 adapters: dict[str, ApiAdapter], limiter: TokenBucket) -> None:
        self.http = http
        self.adapters = adapters
        self.limiter = limiter

    async def submit(self, task: RenderTask, info: ModelInfo,
                     white_model_image: Path) -> RenderResult:
        start = time.monotonic()
        adapter = self.adapters.get(info.provider or "")
        if adapter is None:
            return RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                                model_id=task.model_id, ok=False,
                                error_code="API_ADAPTER_MISSING")
        try:
            await self.limiter.acquire()
            data = await adapter.generate(task.prompt, white_model_image,
                                          info.model_name or "", self.http)
            out = Path(task.control_maps.get("_out_dir", ".")) / \
                f"{task.view_id}_{task.variant_id}_{task.model_id}_{task.seed}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            return RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                                model_id=task.model_id, ok=True, image_path=str(out),
                                latency_ms=int((time.monotonic() - start) * 1000),
                                cost_usd=info.price_per_image)
        except Exception:
            return RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                                model_id=task.model_id, ok=False, error_code="API_ERROR",
                                latency_ms=int((time.monotonic() - start) * 1000))
