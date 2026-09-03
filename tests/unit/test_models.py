import pytest
from pydantic import ValidationError

from app.models.rendering import PromptPair, StyleParams


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
