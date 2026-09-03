# tests/unit/test_comfy_engine.py
from pathlib import Path

from app.engines.comfy import ComfyEngine
from app.engines.registry import ModelInfo
from app.models.rendering import PromptPair, RenderTask

INFO = ModelInfo(model_id="sdxl-control-v1", engine="comfy",
                 workflow_template="sdxl-control-v1.json")
TASK = RenderTask(view_id="v1", variant_id="var0", model_id="sdxl-control-v1",
                  prompt=PromptPair(positive="p", negative="n"),
                  control_maps={"depth": "d.png", "lineart": "l.png"},
                  seed=7, params_hash="abc")


class FakeClient:
    def __init__(self): self.uploaded = []
    async def upload_image(self, path): self.uploaded.append(path); return path
    async def queue_prompt(self, wf): self.wf = wf; return "pid"
    # Adapted from the brief: Task 8's ComfyClient.wait_for_result returns the
    # full node-id -> outputs mapping, so the fake mirrors {"13": {"images": [...]}}.
    async def wait_for_result(self, pid, poll_s=1.0):
        return {"13": {"images": [{"filename": "o.png", "subfolder": "", "type": "output"}]}}
    async def fetch_image(self, f, s, t): return b"PNG"


async def test_submit_writes_image_and_returns_ok(tmp_path):
    fake = FakeClient()
    engine = ComfyEngine(client=fake, template_dir=Path("workflows"), out_dir=tmp_path)
    result = await engine.submit(TASK, INFO)
    assert result.ok and result.image_path is not None
    assert Path(result.image_path).read_bytes() == b"PNG"
    assert Path(result.image_path).name == "v1_var0_sdxl-control-v1_7.png"
    assert fake.uploaded == ["d.png", "l.png"]
    assert fake.wf["11"]["inputs"]["seed"] == 7
    assert fake.wf["2"]["inputs"]["text"] == "p"


async def test_failure_returns_not_ok_not_raise(tmp_path):
    class Boom(FakeClient):
        async def queue_prompt(self, wf): raise RuntimeError("down")
    engine = ComfyEngine(client=Boom(), template_dir=Path("workflows"), out_dir=tmp_path)
    result = await engine.submit(TASK, INFO)
    # Adapted from the brief (result.error -> result.error_code): RenderResult
    # carries the failure as error_code, per the model definition.
    assert not result.ok and result.error_code is not None
