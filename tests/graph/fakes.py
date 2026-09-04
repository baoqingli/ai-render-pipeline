# tests/graph/fakes.py
"""图测试确定型 fakes：全部工具/引擎可预测。

fake_deps 返回的 GraphDeps 附两个测试侧手柄（dataclass 非冻结，动态属性）：
- engine_ref：CountingEngine，submits 计扇出提交次数
- calls：{"inspect", "parse", "white_model"} 各工具的调用记录
style 不在此处（style_node 走模块级 run_style_agent，测试侧 monkeypatch 模块属性）。
"""
from pathlib import Path

from app.engines.registry import ModelInfo
from app.graph.pipeline import GraphDeps
from app.models.cad_report import CadReport
from app.models.rendering import RenderResult
from app.models.scene import SceneJSON, Wall
from app.models.tooling import ToolResult

PASSES = ("depth", "lineart", "white")


class CountingEngine:
    """确定型引擎：记录 submit 次数，落一张 ≥5KB 的 ok 图（过 qa 的 5KB 线）。
    落图位取 task.control_maps["_out_dir"]（render_item 的真实注入口径，
    comfy/direct_api 同体计数）。"""

    def __init__(self):
        self.submits = 0

    async def submit(self, task, info, **kw):
        self.submits += 1
        out_dir = Path(task.control_maps.get("_out_dir") or kw.get("out_dir") or ".")
        out = out_dir / f"{task.view_id}_{task.variant_id}_{task.model_id}_{task.seed}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"f" * 6000)
        return RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                            model_id=task.model_id, ok=True, image_path=str(out),
                            latency_ms=1)


def fake_deps(tmp_path: Path, confidence: float = 1.0, variants: int = 1) -> GraphDeps:
    engine = CountingEngine()
    calls: dict[str, list] = {"inspect": [], "parse": [], "white_model": []}

    def fake_inspect(p):
        calls["inspect"].append(str(p))
        return CadReport(confidence=confidence)

    def fake_parse(p, r=None):
        calls["parse"].append(str(p))
        return ToolResult(ok=True, data=SceneJSON(walls=[Wall(
            id="w1", polygon=[[0, 0], [6000, 0], [6000, 200], [0, 200]])]),
            cache_key="fake-scene-key")

    async def fake_build(scene_path, out_dir, blender_exe=None):
        calls["white_model"].append(str(out_dir))
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 —— fake 落小文件（同 mini_render 口径）
        for v in ("view_01",):
            for p in PASSES:
                (out / f"{v}_{p}.png").write_bytes(b"f" * 6000)
        return ToolResult(ok=True, data=out, cache_key="fake-blend-key")

    deps = GraphDeps(
        engines={"comfy": engine, "direct_api": engine},
        registry_models=[ModelInfo(model_id="fake-comfy", engine="comfy",
                                   workflow_template="x.json")],
        variants=variants, data_dir=tmp_path / "data",
        inspect_dxf=fake_inspect, parse_scene=fake_parse,
        build_white_model=fake_build)
    deps.engine_ref = engine  # type: ignore[attr-defined]  # 测试侧计数手柄
    deps.calls = calls  # type: ignore[attr-defined]  # 测试侧调用记录
    return deps
