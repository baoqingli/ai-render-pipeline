from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.core.config import Settings, get_settings


def make_chat_model(settings: Settings | None = None, temperature: float = 0.2) -> ChatOpenAI:
    s = settings or get_settings()
    return ChatOpenAI(
        model=s.llm_model,
        base_url=s.llm_base_url,
        api_key=SecretStr("local"),  # vLLM 不校验，但客户端要求非空
        temperature=temperature,
    )
