# app/agents/vision/desc_compiler.py
"""描述编译器：用户自然语言渲染要求 → 生图可用的英文渲染描述。

识别范围不限于风格——一切影响生图效果的描述都保留：设计风格、材质、
配色、光照、氛围、家具偏好、时间段/天气、视角等。

设计约束（2026-09-14 实测红线，docs/pipeline-two-agent-gpt-image.md §七）：
- 文字与参考图冲突时模型各听一半——用户混入的布局类要求（改房间/动墙体/
  方位描述）必须剥离，布局由 layout.png 决定；
- 与生图无关的内容（闲聊/提问）同样过滤；
- 输出须短、无方位词、英文（生图模型对英文 prompt 更稳）。

LLM 失败时降级为直接使用用户原文（去首尾空白），有总比失败好。
"""
from app.infra.llm import make_chat_model

_SYSTEM = (
    "你是室内渲染 prompt 编译器。从用户的自然语言中识别所有与生图效果相关的"
    "描述——包括但不限于设计风格、材质、配色、光照、氛围、家具偏好、"
    "时间段/天气、视角——翻译成简洁的英文渲染描述。"
    "忽略两类内容：①涉及空间布局、房间结构、墙体门窗改动、面积调整、"
    "方位位置的要求——布局由参考图决定，文字不得干预；"
    "②与生图无关的闲聊、提问、指令。"
    "输出不超过 60 个英文词，禁止方位词（upper-left/left/right 等），"
    "只输出描述本身，不要解释。"
    "若没有任何与生图相关的内容，输出 IGNORED。"
)

_IGNORED = "IGNORED"


def compile_desc(user_desc: str, *, model: str | None = None) -> str | None:
    """编译用户描述。返回 None 表示无可保留内容（纯布局类/无关内容）。

    同步实现（一次 LLM 调用）；调用方若已在事件循环中请放线程执行。
    """
    text = user_desc.strip()
    if not text:
        return None
    from app.core.config import get_settings

    s = get_settings()
    use_model = model or s.vision_model or s.llm_model
    client = make_chat_model(s.model_copy(update={"llm_model": use_model}),
                             temperature=0.3)
    try:
        resp = client.invoke([
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"用户描述：{text}"},
        ])
    except Exception:  # noqa: BLE001 降级：LLM 不可用时直接用原文
        return text
    out = str(resp.content).strip().strip('"').strip()
    if not out or out.upper().startswith(_IGNORED):
        return None
    return out
