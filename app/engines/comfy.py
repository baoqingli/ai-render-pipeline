# app/engines/comfy.py
import json
import time
from pathlib import Path

from app.engines.comfy_client import ComfyClient, ComfyError
from app.engines.comfy_template import render_workflow
from app.engines.registry import ModelInfo
from app.models.rendering import RenderResult, RenderTask

# SDXL 家族默认组合（终审 minor：原 SD1.5 ControlNet 与 SDXL checkpoint
# 不匹配）。实际文件名以操作者下载为准，可用 CLI flag 覆盖。
DEFAULT_CHECKPOINT = "sd_xl_base_1.0.safetensors"
DEFAULT_CONTROLNET_DEPTH = "xinsir/controlnet-depth-sdxl-1.0.safetensors"
DEFAULT_CONTROLNET_LINEART = "xinsir/controlnet-lineart-sdxl-1.0.safetensors"


class ComfyEngine:
    def __init__(self, client: ComfyClient, template_dir: Path, out_dir: Path,
                 checkpoint: str = DEFAULT_CHECKPOINT,
                 controlnet_depth: str = DEFAULT_CONTROLNET_DEPTH,
                 controlnet_lineart: str = DEFAULT_CONTROLNET_LINEART) -> None:
        self.client = client
        self.template_dir = template_dir
        self.out_dir = out_dir
        self.defaults = {"__CHECKPOINT__": checkpoint,
                         "__CONTROLNET_DEPTH__": controlnet_depth,
                         "__CONTROLNET_LINEART__": controlnet_lineart}

    async def submit(self, task: RenderTask, info: ModelInfo) -> RenderResult:
        start = time.monotonic()
        try:
            template = json.loads(
                (self.template_dir / (info.workflow_template or "")).read_text(encoding="utf-8"))
            depth_name = await self.client.upload_image(task.control_maps["depth"])
            lineart_name = await self.client.upload_image(task.control_maps["lineart"])
            # 显式标注：mypy 否则会把 str 与 int 的 join 推断为 dict[str, object]
            injections: dict[str, str | int] = {**self.defaults,
                          "__POSITIVE__": task.prompt.positive,
                          "__NEGATIVE__": task.prompt.negative or "low quality",
                          "__DEPTH_IMAGE__": depth_name,
                          "__LINEART_IMAGE__": lineart_name,
                          "__SEED__": task.seed}
            workflow = render_workflow(template, injections)
            prompt_id = await self.client.queue_prompt(workflow)
            outputs = await self.client.wait_for_result(prompt_id)
            # Adapted from the brief (outputs["images"][0]): Task 8's
            # wait_for_result returns the full node-id -> outputs mapping.
            img = next(node["images"][0] for node in outputs.values() if "images" in node)
            data = await self.client.fetch_image(img["filename"], img["subfolder"], img["type"])
            self.out_dir.mkdir(parents=True, exist_ok=True)
            name = f"{task.view_id}_{task.variant_id}_{task.model_id}_{task.seed}.png"
            path = self.out_dir / name
            path.write_bytes(data)
            return RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                                model_id=task.model_id, ok=True, image_path=str(path),
                                latency_ms=int((time.monotonic() - start) * 1000))
        except ComfyError as e:
            return RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                                model_id=task.model_id, ok=False, error_code=e.code,
                                latency_ms=int((time.monotonic() - start) * 1000))
        except Exception:  # noqa: BLE001 — 引擎内一切异常都转成结果，不阻塞扇出
            return RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                                model_id=task.model_id, ok=False, error_code="COMFY_ERROR",
                                latency_ms=int((time.monotonic() - start) * 1000))
