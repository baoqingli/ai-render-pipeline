import pytest
from pydantic import ValidationError

from app.models.rendering import PromptPair, RenderResult, StyleParams


def test_style_params_defaults():
    p = StyleParams()
    assert p.style == "modern_minimal" and p.budget == "mid"


def test_style_params_rejects_unknown_enum():
    with pytest.raises(ValidationError):
        StyleParams(style="baroque")


def test_stable_hash_deterministic():
    a = StyleParams(style="wood", light="warm").stable_hash()
    b = StyleParams(light="warm", style="wood").stable_hash()  # 字段顺序无关
    assert a == b and len(a) == 16


def test_prompt_pair_holds_strings():
    pair = PromptPair(positive="p", negative="n")
    assert pair.positive == "p"


def test_render_result_defaults():
    r = RenderResult(view_id="v1", variant_id="var0", model_id="m", ok=True)
    assert r.ok and r.image_path is None
    assert r.error_code is None and r.latency_ms == 0 and r.cost_usd == 0.0


def test_render_result_failure_fields():
    r = RenderResult(view_id="v1", variant_id="var0", model_id="m", ok=False,
                     error_code="COMFY_TIMEOUT", latency_ms=12)
    assert not r.ok and r.error_code == "COMFY_TIMEOUT" and r.latency_ms == 12
