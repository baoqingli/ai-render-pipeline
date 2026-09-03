from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.core.config import Settings, get_settings


def make_chat_model(settings: Settings | None = None, temperature: float = 0.2) -> ChatOpenAI:
    s = settings or get_settings()
    # Spec D4：Phase 1 默认 GLM API（key 进 Settings）；未配 key 时视为
    # 自部署 vLLM 切换项路径，客户端要求非空故占位 "local"（vLLM 不校验）
    api_key = SecretStr(s.llm_api_key) if s.llm_api_key else SecretStr("local")
    return ChatOpenAI(
        model=s.llm_model,
        base_url=s.llm_base_url,
        api_key=api_key,
        temperature=temperature,
    )
