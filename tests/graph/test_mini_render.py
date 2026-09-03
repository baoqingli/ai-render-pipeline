# tests/graph/test_mini_render.py
import asyncio
from pathlib import Path

from app.engines.registry import ModelInfo
from app.graph.mini_render import build_mini_graph, discover_views, plan_tasks
from app.models.rendering import RenderResult, StyleParams


class FakeEngine:
    async def submit(self, task, info, **kw):
        await asyncio.sleep(0)
        return RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                            model_id=task.model_id, ok=True,
                            image_path=f"/tmp/{task.model_id}.png", latency_ms=5)


MODELS = [
    ModelInfo(model_id="sdxl-control-v1", engine="comfy", workflow_template="t.json"),
    ModelInfo(model_id="nano-banana-2", engine="direct_api", provider="gemini",
              model_name="m", price_per_image=0.03),
]


def test_discover_views(tmp_path):
    (tmp_path / "v1_depth.png").write_bytes(b"d")
    (tmp_path / "v1_lineart.png").write_bytes(b"l")
    (tmp_path / "v1_white.png").write_bytes(b"w")
    views = discover_views(tmp_path)
    assert views == [{"view_id": "v1", "depth": str(tmp_path / "v1_depth.png"),
                      "lineart": str(tmp_path / "v1_lineart.png"),
                      "white": str(tmp_path / "v1_white.png")}]


def test_plan_tasks_fans_out():
    style = StyleParams()
    tasks = plan_tasks(style, [{"view_id": "v1", "depth": "d", "lineart": "l", "white": "w"}],
                       MODELS, variants=2, seed_base=100)
    assert len(tasks) == 1 * 2 * 2          # view × variant × model
    assert {t.model_id for t in tasks} == {"sdxl-control-v1", "nano-banana-2"}
    assert all(t.params_hash == style.stable_hash() for t in tasks)


async def test_mini_graph_produces_report(tmp_path, monkeypatch):
    monkeypatch.setattr("app.graph.mini_render.run_style_agent",
                        lambda desc, llm=None: __import__("app.agents.style", fromlist=["x"])
                        .StyleAgentOutput(params=StyleParams()))  # fake 掉 LLM
    g = build_mini_graph({"comfy": FakeEngine(), "direct_api": FakeEngine()}, MODELS)
    (tmp_path / "ctrl").mkdir()
    (tmp_path / "ctrl" / "v1_depth.png").write_bytes(b"d")
    (tmp_path / "ctrl" / "v1_lineart.png").write_bytes(b"l")
    (tmp_path / "ctrl" / "v1_white.png").write_bytes(b"w")
    final = await g.ainvoke({"description": "warm wood", "control_dir": str(tmp_path / "ctrl"),
                             "variants": 1, "report_dir": str(tmp_path)})
    assert len(final["results"]) == 2       # 1 view × 1 variant × 2 models
    assert Path(final["report_path"]).exists()
    assert "sdxl-control-v1" in Path(final["report_path"]).read_text(encoding="utf-8")


async def test_mini_graph_fail_soft(tmp_path, monkeypatch):
    """新增（brief 外）：fail-soft 约束——单任务失败（引擎记 ok=False 或违约抛错）
    都不阻塞批次：结果照常聚合（ok=False），报告照常生成。"""
    monkeypatch.setattr("app.graph.mini_render.run_style_agent",
                        lambda desc, llm=None: __import__("app.agents.style", fromlist=["x"])
                        .StyleAgentOutput(params=StyleParams()))

    class BadEngine:  # 引擎返回失败结果（如 ComfyError 分支）
        async def submit(self, task, info, **kw):
            return RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                                model_id=task.model_id, ok=False, error_code="COMFY_ERROR")

    class RaisingEngine:  # 引擎违约直接抛错
        async def submit(self, task, info, **kw):
            raise RuntimeError("engine exploded")

    g = build_mini_graph({"comfy": BadEngine(), "direct_api": RaisingEngine()}, MODELS)
    (tmp_path / "ctrl").mkdir()
    (tmp_path / "ctrl" / "v1_depth.png").write_bytes(b"d")
    (tmp_path / "ctrl" / "v1_lineart.png").write_bytes(b"l")
    (tmp_path / "ctrl" / "v1_white.png").write_bytes(b"w")
    final = await g.ainvoke({"description": "warm wood", "control_dir": str(tmp_path / "ctrl"),
                             "variants": 1, "report_dir": str(tmp_path)})
    assert len(final["results"]) == 2 and not any(r.ok for r in final["results"])
    codes = {r.model_id: r.error_code for r in final["results"]}
    assert codes["sdxl-control-v1"] == "COMFY_ERROR"
    assert codes["nano-banana-2"].startswith("ENGINE_RAISED")
    report = Path(final["report_path"]).read_text(encoding="utf-8")
    assert "成功率 0/2" in report and "COMFY_ERROR" in report
