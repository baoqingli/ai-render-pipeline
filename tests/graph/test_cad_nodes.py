# tests/graph/test_cad_nodes.py
from pathlib import Path
from types import SimpleNamespace

from app.graph.nodes.cad_branch import (
    convert_node,
    diagnose_node,
    ingest_node,
    inspect_node,
    is_failed,
    layout_stub_node,
    parse_node,
)
from app.graph.state import initial_state
from app.models.cad_report import CadReport
from app.models.scene import Furniture, SceneJSON
from app.models.tooling import ToolError, ToolResult


def _state(cad="uploads/a.dxf", **kw):
    return initial_state("p1", 1, cad, "描述") | kw


def _deps(**tools):
    """Task 4 注入口径：inspect/parse 工具经 keyword-only deps 注入节点，
    单测用 SimpleNamespace 鸭子类型（tests 不进 mypy 门禁）。"""
    return SimpleNamespace(**tools)


async def test_ingest_accepts_dxf_and_rejects_other(tmp_path):
    ok = await ingest_node(_state())
    assert ok["stage"].value == "ingested" and ok.get("errors") is None
    bad = await ingest_node(_state(cad="uploads/a.pdf"))
    assert is_failed(_state() | bad) and bad["errors"][0].code == "INPUT_INVALID"


async def test_ingest_accepts_dwg(tmp_path):
    # 新增（brief 外）：.dwg 同为合法入口，与 .dxf 对称
    ok = await ingest_node(_state(cad="uploads/a.dwg"))
    assert ok["stage"].value == "ingested" and ok.get("errors") is None


async def test_convert_passthrough_dxf(tmp_path):
    out = await convert_node(_state() | {"stage": None})
    # 修（brief 原文 == 字符串比较）：实现语义 dxf_key = str(Path(key))，
    # Windows 下分隔符归一为反斜杠，按 posix 口径比较
    assert Path(out["dxf_key"]).as_posix() == "uploads/a.dxf"
    assert out["stage"].value == "converted"


async def test_convert_dwg_via_tool(tmp_path, monkeypatch):
    # 新增（brief 外）：.dwg 走 convert_dwg 工具封装
    async def fake_convert(src, out_dir):
        return ToolResult(ok=True, data=tmp_path / "a.dxf", cache_key="c1")

    monkeypatch.setattr("app.graph.nodes.cad_branch.convert_dwg", fake_convert)
    out = await convert_node(_state(cad="uploads/a.dwg"))
    assert Path(out["dxf_key"]).name == "a.dxf" and out["stage"].value == "converted"


async def test_convert_failure_fails_soft(tmp_path, monkeypatch):
    # 新增（brief 外）：convert 失败 → errors + stage=failed（不可恢复，条件边走 finalize）
    async def fake_convert(src, out_dir):
        return ToolResult(ok=False, error=ToolError(code="ODA_MISSING", message="no exe"))

    monkeypatch.setattr("app.graph.nodes.cad_branch.convert_dwg", fake_convert)
    out = await convert_node(_state(cad="uploads/a.dwg"))
    assert is_failed(_state() | out) and out["errors"][0].code == "ODA_MISSING"


async def test_inspect_and_diagnose_stub(tmp_path):
    fake = CadReport(confidence=0.5, proxy_entity_count=3)
    out = await inspect_node(_state() | {"dxf_key": "x.dxf"},
                             deps=_deps(inspect_dxf=lambda p: fake))
    assert out["confidence"]["parse"] == 0.5
    diag = await diagnose_node(_state() | {"cad_report": fake, "confidence": {"parse": 0.5}})
    assert diag["fallback_log"][0].stage == "diagnose"
    assert diag["parse_strategy"] is None                 # 规则桩：策略 None


async def test_inspect_exception_degrades(tmp_path):
    # 新增（brief 外）：勘察异常 → 记 errors + 置信 0，但不 fail（降级继续）
    def boom(p):
        raise RuntimeError("ezdxf blew up")

    out = await inspect_node(_state() | {"dxf_key": "x.dxf"}, deps=_deps(inspect_dxf=boom))
    assert out["errors"][0].code == "INSPECT_FAILED"
    assert out["confidence"]["parse"] == 0.0 and out["stage"].value == "inspected"
    assert not is_failed(_state() | out)


async def test_diagnose_clean_report_no_fallback(tmp_path):
    # 新增（brief 外）：高置信无 proxy → 无降级记录
    clean = CadReport(confidence=0.9, proxy_entity_count=0)
    diag = await diagnose_node(_state() | {"cad_report": clean})
    assert not diag["fallback_log"] and diag["parse_strategy"] is None


async def test_parse_node_uses_tool_result(tmp_path):
    scene = SceneJSON()
    tr = ToolResult(ok=True, data=scene, cache_key="k123", error=None)
    out = await parse_node(_state() | {"dxf_key": "x.dxf"},
                           deps=_deps(parse_scene=lambda p, r=None: tr))
    assert out["scene_cache_key"] == "k123" and out["scene_json"] == scene


async def test_parse_failure_fails(tmp_path):
    # 新增（brief 外）：parse 失败 → fail（errors + stage=failed）
    tr = ToolResult(ok=False, error=ToolError(code="PARSE_FAILED", message="no walls"))
    out = await parse_node(_state() | {"dxf_key": "x.dxf"},
                           deps=_deps(parse_scene=lambda p, r=None: tr))
    assert is_failed(_state() | out) and out["errors"][0].code == "PARSE_FAILED"


async def test_parse_low_confidence_logs_fallback(tmp_path):
    # 新增（brief 外）：ok=True 但带 PARSE_LOW_CONFIDENCE 提示 → fallback_log
    tr = ToolResult(ok=True, data=SceneJSON(), cache_key="k9", error=ToolError(
        code="PARSE_LOW_CONFIDENCE", message="floor_height -> 2800 默认"))
    out = await parse_node(_state() | {"dxf_key": "x.dxf"},
                           deps=_deps(parse_scene=lambda p, r=None: tr))
    assert out["fallback_log"][0].stage == "parse"
    assert "floor_height" in out["fallback_log"][0].detail


async def test_missing_dxf_key_degrades_or_fails(tmp_path):
    # 新增（brief 外）：dxf_key 缺失（图误接线，mypy 收窄分支）——
    # inspect 按降级不 fail；parse 按 fail 处理
    def _no_tool(*a, **k):
        raise AssertionError("dxf_key 缺失时不应触达工具")

    guard = _deps(inspect_dxf=_no_tool, parse_scene=_no_tool)
    out = await inspect_node(_state(), deps=guard)
    assert out["errors"][0].code == "DXF_MISSING"
    assert out["stage"].value == "inspected" and not is_failed(_state() | out)
    out2 = await parse_node(_state(), deps=guard)
    assert is_failed(_state() | out2) and out2["errors"][0].code == "DXF_MISSING"


async def test_layout_stub_records_fallback_when_empty():
    # 修（brief 原文用裸 _state()）：实现语义 scene_json=None 直通返回 {}，
    # 仅「有场景但 furniture 为空」才记 fallback，故显式给空场景
    out = await layout_stub_node(_state() | {"scene_json": SceneJSON()})
    assert out["fallback_log"][0].stage == "layout"
    out2 = await layout_stub_node(_state() | {"scene_json": SceneJSON(
        furniture=[Furniture(id="f", type="sofa", position=[0, 0],
                             size=[1, 1, 1], source="cad")])})
    assert "fallback_log" not in out2 or not out2.get("fallback_log")
