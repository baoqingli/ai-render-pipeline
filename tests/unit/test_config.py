from app.core.config import Settings, get_settings


def test_settings_defaults():
    # Hermetic：_env_file=None 阻断本机 .env（GPU 机器上存在真实 .env），
    # 只断言文档化的代码默认值
    s = Settings(_env_file=None)
    assert s.llm_base_url == "http://localhost:8001/v1"
    assert s.llm_model
    assert s.comfy_url == "http://localhost:8188"
    assert s.llm_api_key is None


def test_get_settings_cached():
    assert get_settings() is get_settings()
