# tests/unit/test_direct_api.py
import base64, time
import httpx
import pytest

from app.engines.direct_api import (
    DirectAPIEngine, GeminiAdapter, OpenAIImageAdapter)
from app.engines.registry import ModelInfo
from app.infra.pools import TokenBucket
from app.models.rendering import PromptPair, RenderTask

PNG = base64.b64encode(b"PNG").decode()
TASK = RenderTask(view_id="v1", variant_id="var0", model_id="nano-banana-2",
                  prompt=PromptPair(positive="a room"), control_maps={},
                  seed=1, params_hash="h")


def gemini_handler(request: httpx.Request) -> httpx.Response:
    assert "generateContent" in str(request.url)
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [
        {"inline_data": {"mime_type": "image/png", "data": PNG}}]}}]})


async def test_gemini_adapter_roundtrip(tmp_path):
    img = tmp_path / "white.png"; img.write_bytes(b"PNG")
    http = httpx.AsyncClient(transport=httpx.MockTransport(gemini_handler))
    engine = DirectAPIEngine(http=http, adapters={"gemini": GeminiAdapter("k")},
                             limiter=TokenBucket(600))
    info = ModelInfo(model_id="nano-banana-2", engine="direct_api", provider="gemini",
                     model_name="gemini-3.1-flash-image", price_per_image=0.03)
    r = await engine.submit(TASK, info, white_model_image=img)
    assert r.ok and r.cost_usd == pytest.approx(0.03)


async def test_unknown_provider_fails_soft(tmp_path):
    img = tmp_path / "w.png"; img.write_bytes(b"x")
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    engine = DirectAPIEngine(http=http, adapters={}, limiter=TokenBucket(600))
    info = ModelInfo(model_id="x", engine="direct_api", provider="nope")
    r = await engine.submit(TASK, info, white_model_image=img)
    assert not r.ok and r.error_code == "API_ADAPTER_MISSING"
