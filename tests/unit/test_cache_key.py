from app.tools.cache import TOOL_VERSIONS, build_cache_key


def test_cache_key_changes_with_tool_version():
    old = TOOL_VERSIONS["comfy_render"]
    before = build_cache_key("comfy_render", "a")
    TOOL_VERSIONS["comfy_render"] = "9"
    try:
        assert build_cache_key("comfy_render", "a") != before
    finally:
        TOOL_VERSIONS["comfy_render"] = old


def test_cache_key_stable_and_ignores_none():
    assert build_cache_key("t", "x", None) == build_cache_key("t", "x")
    assert build_cache_key("t", "x") != build_cache_key("t", "y")
