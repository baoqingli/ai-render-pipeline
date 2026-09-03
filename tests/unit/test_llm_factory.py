from app.core.config import Settings
from app.infra.llm import make_chat_model


def test_make_chat_model_points_to_custom_endpoint():
    s = Settings(_env_file=None, llm_base_url="http://vllm:8001/v1", llm_model="Qwen/X")
    llm = make_chat_model(s)
    assert llm.model_name == "Qwen/X"
    assert str(llm.openai_api_base) == "http://vllm:8001/v1"


def test_make_chat_model_carries_configured_api_key():
    """Spec D4：配置了 llm_api_key（GLM API 路径）时必须透传给 ChatOpenAI。"""
    s = Settings(_env_file=None, llm_api_key="sk-x")
    llm = make_chat_model(s)
    assert llm.openai_api_key.get_secret_value() == "sk-x"


def test_make_chat_model_defaults_to_local_without_key():
    """未配 key（自部署 vLLM 切换项路径）：占位 "local"，客户端要求非空。"""
    s = Settings(_env_file=None)
    assert s.llm_api_key is None
    llm = make_chat_model(s)
    assert llm.openai_api_key.get_secret_value() == "local"
