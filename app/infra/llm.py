from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.core.config import Settings, get_settings


def make_chat_model(settings: Settings | None = None,
                    temperature: float = 0.2) -> BaseChatModel:
    s = settings or get_settings()
    if "/api/anthropic" in s.llm_base_url:
        # GLM 套餐协议端点（Anthropic 协议）：走 ChatAnthropic。key 显式占位
        # "not-set"——传 None 会回落读宿主机 ANTHROPIC_API_KEY，行为不可控。
        from langchain_anthropic import ChatAnthropic
        # timeout/stop 显式传 None（= 类默认）：pydantic DataclassTransform 让
        # mypy 把这两个别名字段视为必填 named arg，缺省会 call-arg 报错
        return ChatAnthropic(
            model_name=s.llm_model,
            api_key=SecretStr(s.llm_api_key or "not-set"),
            base_url=s.llm_base_url,
            temperature=temperature,
            max_retries=2,
            timeout=None,
            stop=None,
            # glm-5.3/flash 为推理模型：默认 max_tokens 会被思考块耗尽导致正文为空
            max_tokens=16384,  # type: ignore[call-arg]  # langchain-anthropic 版本差异
        )
    # OpenAI 兼容（vLLM 自部署 / GLM v4 等按量付费路径）。
    # Spec D4：Phase 1 默认 GLM API（key 进 Settings）；未配 key 时视为
    # 自部署 vLLM 切换项路径，客户端要求非空故占位 "local"（vLLM 不校验）
    api_key = SecretStr(s.llm_api_key) if s.llm_api_key else SecretStr("local")
    return ChatOpenAI(
        model=s.llm_model,
        base_url=s.llm_base_url,
        api_key=api_key,
        temperature=temperature,
    )
