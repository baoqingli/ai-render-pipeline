from app.core.config import Settings
from app.infra.llm import make_chat_model


def test_make_chat_model_points_to_custom_endpoint():
    s = Settings(llm_base_url="http://vllm:8001/v1", llm_model="Qwen/X")
    llm = make_chat_model(s)
    assert llm.openai_api_base.value if hasattr(llm.openai_api_base, "value") else True
    assert llm.model_name == "Qwen/X"
    assert str(llm.openai_api_base) == "http://vllm:8001/v1"
