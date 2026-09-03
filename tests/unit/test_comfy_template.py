# tests/unit/test_comfy_template.py
import json
from pathlib import Path

from app.engines.comfy_template import render_workflow

TEMPLATE = json.loads(Path("workflows/sdxl-control-v1.json").read_text(encoding="utf-8"))


def test_replaces_all_placeholders():
    out = render_workflow(TEMPLATE, {
        "__CHECKPOINT__": "sdxl_base.safetensors",
        "__CONTROLNET_DEPTH__": "depth_v2.safetensors",
        "__CONTROLNET_LINEART__": "lineart.safetensors",
        "__POSITIVE__": "a room", "__NEGATIVE__": "bad",
        "__DEPTH_IMAGE__": "d.png", "__LINEART_IMAGE__": "l.png",
        "__SEED__": 42,
    })
    text = json.dumps(out)
    assert "__" not in text
    assert out["11"]["inputs"]["seed"] == 42           # 整串占位符保留 int 类型
    assert out["1"]["inputs"]["ckpt_name"] == "sdxl_base.safetensors"


def test_partial_string_placeholder_substitution():
    out = render_workflow({"n": {"inputs": {"text": "prefix __POSITIVE__ suffix"}}},
                          {"__POSITIVE__": "X"})
    assert out["n"]["inputs"]["text"] == "prefix X suffix"
