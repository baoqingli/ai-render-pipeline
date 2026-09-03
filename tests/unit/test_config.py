from app.core.config import Settings, get_settings


def test_settings_defaults():
    s = Settings()
    assert s.llm_base_url == "http://localhost:8001/v1"
    assert s.llm_model
    assert s.comfy_url == "http://localhost:8188"


def test_get_settings_cached():
    assert get_settings() is get_settings()
