# tests/unit/test_prompt.py
from app.engines.prompt import assemble_prompt
from app.models.rendering import StyleParams


def test_prompt_is_deterministic_and_contains_phrases():
    p = StyleParams(style="wood", light="warm")
    a = assemble_prompt(p)
    b = assemble_prompt(p)
    assert a == b
    assert "japanese-style warm wood aesthetic" in a.positive  # wood 风格短语
    assert "warm lighting" in a.positive
    assert a.negative  # 负面词非空


def test_api_variant_keeps_positive_shorter():
    p = StyleParams()
    default = assemble_prompt(p, "default")
    api = assemble_prompt(p, "api")
    assert api.positive.startswith(default.positive)
    assert "high quality render" in api.positive
