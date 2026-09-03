# tests/unit/test_registry.py
from app.engines.registry import ModelRegistry

DB = "sqlite+aiosqlite:///:memory:"


async def test_setup_seeds_and_lists_enabled():
    reg = ModelRegistry(DB)
    await reg.setup()
    models = await reg.list_enabled()
    ids = [m.model_id for m in models]
    assert "sdxl-control-v1" in ids


async def test_get_returns_model_and_missing_is_none():
    reg = ModelRegistry(DB)
    await reg.setup()
    m = await reg.get("sdxl-control-v1")
    assert m is not None and m.engine == "comfy"
    assert await reg.get("nope") is None
