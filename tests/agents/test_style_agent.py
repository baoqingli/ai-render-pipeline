# tests/agents/test_style_agent.py
from app.agents.style import run_style_agent, sanitize_params
from app.models.rendering import StyleParams


class FakeStructuredModel:
    """模拟 with_structured_output：返回预设 dict 或抛错"""
    def __init__(self, result: dict | Exception):
        self.result = result

    def with_structured_output(self, schema):
        async def ainvoke(inp):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result
        # brief 原文为 {"ainvoke": ainvoke}，但函数挂成类属性会被绑定成方法（多传一个
        # self），任何正确实现都会 TypeError。staticmethod 修正为可用的假 Runnable。
        return type("Runnable", (), {"ainvoke": staticmethod(ainvoke)})()


def test_extract_maps_description():
    fake = FakeStructuredModel({"style": "wood", "light": "warm"})
    out = run_style_agent("温馨日式原木风，暖光", llm=fake)  # type: ignore[arg-type]
    assert out.params.style == "wood" and out.fallbacks == []


def test_unknown_enum_falls_back_with_log():
    fake = FakeStructuredModel({"style": "太空舱风格"})
    out = run_style_agent("随便", llm=fake)  # type: ignore[arg-type]
    assert out.params.style == "modern_minimal"
    assert out.fallbacks == ["style=太空舱风格 -> modern_minimal"]


def test_llm_failure_returns_defaults():
    fake = FakeStructuredModel(RuntimeError("vllm down"))
    out = run_style_agent("x", llm=fake)  # type: ignore[arg-type]
    assert out.params.stable_hash() == StyleParams().stable_hash()
    assert out.fallbacks and out.fallbacks[0].startswith("llm_error")


async def test_run_style_agent_inside_running_event_loop():
    """新增（brief 外）：Task 11 的 async langgraph 节点里同步调用 run_style_agent，
    不得因嵌套 asyncio.run 崩溃（RuntimeError: cannot be called from a running loop）。"""
    fake = FakeStructuredModel({"style": "cream"})
    out = run_style_agent("奶油风", llm=fake)  # type: ignore[arg-type]
    assert out.params.style == "cream"
    assert out.fallbacks == []


def test_sanitize_params_direct():
    """新增（brief 外）：直接钉住 sanitize_params 接口——合法值透传；
    枚举外值/非字符串落默认并记录；schema 外字段忽略。"""
    params, fallbacks = sanitize_params(
        {"style": "wabi_sabi", "floor": "大理石", "wall": None, "light": 3, "unknown": "x"}
    )
    assert params.style == "wabi_sabi"   # 合法值透传
    assert params.floor == "wood_floor"  # 枚举外 → 默认
    assert params.wall == "white"        # 非字符串 → 默认
    assert params.light == "warm"
    assert fallbacks == [
        "floor=大理石 -> wood_floor",
        "wall=None -> white",
        "light=3 -> warm",
    ]
