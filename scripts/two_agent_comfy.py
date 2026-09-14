# scripts/two_agent_comfy.py
"""两 Agent 识图 → ComfyUI 渲染 一键流程。

流程：
  1. 用 qwen/qwen3.8-flash 跑两 Agent 管线（识图+验证+提示词）
  2. 上传 layout_control.png 到 ComfyUI，注入提示词，提交工作流
  3. 等待完成，保存渲染图到输出目录

用法:
  uv run python scripts/two_agent_comfy.py \\
      --input fixtures/cad/01-平面系统图.dwg \\
      --out output/render_test

可选:
  --model        识图+验证模型（默认 qwen/qwen3.8-flash）
  --comfy-url    ComfyUI 地址（默认 http://localhost:8188）
  --seed         随机种子（默认 42）
  --checkpoint   SDXL checkpoint 名（默认 interiorSceneXL_v1.safetensors）
  --max-iters    验证最大轮数（默认 3）
"""
import argparse
import asyncio
import copy
import functools
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.vision.two_agent_pipeline import run as pipeline_run
from app.engines.comfy_client import ComfyClient

WORKFLOW_PATH = Path("workflows/interior-design-v1-comfyui.json")
DEFAULT_MODEL = "qwen/qwen3.8-flash"
DEFAULT_COMFY_URL = "http://localhost:8188"
DEFAULT_CHECKPOINT = "interiorSceneXL_v1.safetensors"

# 通用负向提示词
NEGATIVE_PROMPT = (
    "easynegative, low quality, blurry, distorted, extra furniture, "
    "floating objects, wrong perspective, anatomically incorrect"
)


def _patch_workflow(template: dict, image_name: str, positive: str,
                    negative: str, checkpoint: str, seed: int) -> dict:
    """深拷贝工作流并注入运行时参数。"""
    wf = copy.deepcopy(template)
    for node in wf.values():
        cls = node.get("class_type", "")
        inp = node.get("inputs", {})
        if cls == "CheckpointLoaderSimple":
            inp["ckpt_name"] = checkpoint
        elif cls == "LoadImage":
            inp["image"] = image_name
        elif cls == "CLIPTextEncode":
            # 用节点原文内容区分正/负：正向文本通常更长，且不含 easynegative
            if "easynegative" in inp.get("text", ""):
                inp["text"] = negative
            else:
                inp["text"] = positive
        elif cls == "KSampler":
            inp["seed"] = seed
    return wf


async def submit_to_comfy(out_dir: Path, comfy_url: str, checkpoint: str,
                          seed: int) -> str:
    """上传控制图、注入提示词、提交工作流，返回输出图片路径。"""
    control_img = out_dir / "layout_control.png"
    layout_img = out_dir / "layout.png"
    prompt_path = out_dir / "prompt.txt"

    upload_src = layout_img
    if not upload_src.exists():
        raise FileNotFoundError(f"找不到布局图: {layout_img}")

    positive = prompt_path.read_text(encoding="utf-8").strip()
    template = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))

    client = ComfyClient(comfy_url)
    print(f"  上传控制图: {upload_src.name}")
    image_name = await client.upload_image(str(upload_src))

    wf = _patch_workflow(template, image_name, positive, NEGATIVE_PROMPT,
                         checkpoint, seed)
    print("  提交工作流到 ComfyUI …")
    prompt_id = await client.queue_prompt(wf)
    print(f"  prompt_id: {prompt_id}，等待渲染完成（最长 300s）…")

    outputs = await client.wait_for_result(prompt_id)
    all_images = [
        img
        for node in outputs.values() if "images" in node
        for img in node["images"]
        if img.get("type") == "output"   # 排除 PreviewImage 的 temp 图
    ]
    saved = []
    for i, img_info in enumerate(all_images):
        data = await client.fetch_image(
            img_info["filename"], img_info["subfolder"], img_info["type"])
        out_img = out_dir / f"render_{seed}_{i+1}.png"
        out_img.write_bytes(data)
        saved.append(str(out_img))
        print(f"  已保存: {out_img.name}")
    return saved


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="DWG/DXF/图片路径")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"识图+验证模型（默认 {DEFAULT_MODEL}）")
    ap.add_argument("--comfy-url", default=DEFAULT_COMFY_URL)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--checkpoint", default=DEFAULT_CHECKPOINT)
    ap.add_argument("--max-iters", type=int, default=3)
    ap.add_argument("--force", action="store_true",
                    help="验证未通过时仍继续出提示词并提交 ComfyUI")
    args = ap.parse_args()

    out_dir = Path(args.out)

    # ── Step 1: 两 Agent 管线（同步，内部有 asyncio.run，放线程避免嵌套冲突）──
    print(f"\n[1/2] 两 Agent 管线（模型: {args.model}）")
    t0 = time.monotonic()
    loop = asyncio.get_event_loop()
    r = await loop.run_in_executor(
        None,
        functools.partial(pipeline_run, args.input, out_dir,
                          vlm_model=args.model, max_iters=args.max_iters))
    elapsed = time.monotonic() - t0

    if not r.ok or r.data is None:
        msg = r.error.message if r.error else "?"
        if args.force and r.error and r.error.code == "VALIDATION_FAILED":
            print(f"  验证未通过（--force 继续）: {msg}")
            # 手动构造 data，从输出目录读已有的清单
            import json as _json
            from app.agents.vision.render_package import build_prompt, layout_description_zh
            from app.models.vision import ElementRegistry
            registry = ElementRegistry.model_validate_json(
                (out_dir / "elements.json").read_text(encoding="utf-8"))
            _style = ("modern cozy hotel interior, warm wood flooring, "
                      "white walls, soft natural lighting, "
                      "photorealistic, architecturally accurate")
            prompt_en = build_prompt(registry, _style)
            (out_dir / "prompt.txt").write_text(prompt_en, encoding="utf-8")
            (out_dir / "prompt_zh.txt").write_text(
                layout_description_zh(registry), encoding="utf-8")
            r_data = {"prompt": prompt_en, "out_dir": str(out_dir),
                      "validation": _json.loads(
                          (out_dir / "validation.json").read_text(encoding="utf-8"))}
        else:
            print(f"  FAILED: {msg}")
            raise SystemExit(1)
    else:
        r_data = r.data

    val = r_data["validation"]
    for rd in val["rounds"]:
        print(f"  验证轮{rd['iter']}: {rd['checks']}  修正={rd['fixes']}")
    print(f"  验证: {'通过' if val['passed'] else '未通过'}  耗时: {elapsed:.1f}s")
    print(f"  Prompt: {r_data['prompt'][:100]}…")

    # ── Step 2: ComfyUI 渲染 ──────────────────────────────────────────────
    print(f"\n[2/2] ComfyUI 渲染（{args.comfy_url}）")
    t1 = time.monotonic()
    try:
        img_paths = await submit_to_comfy(
            out_dir, args.comfy_url, args.checkpoint, args.seed)
    except Exception as e:
        print(f"  FAILED: {e}")
        raise SystemExit(1)

    print(f"  渲染完成 {len(img_paths)} 张  耗时: {time.monotonic() - t1:.1f}s")
    print(f"\n输出目录: {out_dir.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
