# app/graph/nodes/__init__.py
"""图节点包：main graph（Task 4）组装时按 cad_branch/render_branch.<node> 精确名引用。"""
from app.graph.nodes.cad_branch import (
    convert_node,
    diagnose_node,
    ingest_node,
    inspect_node,
    is_failed,
    layout_stub_node,
    parse_node,
)
from app.graph.nodes.render_branch import (
    finalize_node,
    make_fan_out,
    plan_node,
    qa_node,
    render_item,
    style_node,
    white_model_node,
)

__all__ = ["convert_node", "diagnose_node", "finalize_node", "ingest_node",
           "inspect_node", "is_failed", "layout_stub_node", "make_fan_out",
           "parse_node", "plan_node", "qa_node", "render_item", "style_node",
           "white_model_node"]
