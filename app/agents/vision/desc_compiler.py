# app/agents/vision/desc_compiler.py
"""描述编译器：用户自然语言渲染要求 → 生图可用的英文渲染描述。

三类可保留：效果描述（风格/材质/配色/光照/氛围/家具偏好/时段/视角）、
空间状态描述（"玄关上面是淋浴间"——现状陈述，帮助模型正确解读参考图中
模糊符号；识图/解读都可能出错，用户描述可消歧）、其他生图相关信息。
剥离：布局改动要求（含变更动词）与无关闲聊。

设计约束（2026-09-14 实测，docs/pipeline-two-agent-gpt-image.md §七）：
- 文字与参考图冲突时模型各听一半——布局【改动】必须剥离，布局由
  layout.png 决定；但空间【状态陈述】不改布局，保留利大于弊；
- 输出须短、英文（生图模型对英文 prompt 更稳）。

LLM 失败时降级为直接使用用户原文（去首尾空白），有总比失败好。
"""
from app.infra.llm import make_chat_model

_SYSTEM = (
    "你是室内渲染 prompt 编译器。从用户的自然语言中识别三类可保留的内容，"
    "翻译成简洁的英文渲染描述：\n"
    "①效果描述：设计风格、材质、配色、光照、氛围、家具偏好、时间段/天气、"
    "视角等；\n"
    "②空间状态描述：用户对现状的陈述（如'玄关上面是淋浴间''左边是厨房'）"
    "——保留，这能帮助模型正确解读参考图中模糊的符号；\n"
    "忽略两类内容：①布局改动要求（含'改/加/拆/扩/移/变大'等变更动词，"
    "如'改成三室''卧室放大'）——布局由参考图决定，文字不得干预；"
    "②与生图无关的闲聊、提问。\n"
    "空间状态描述只照实转述用户陈述，不要自行发明或改动方位信息。\n"
    "输出不超过 80 个英文词，只输出描述本身，不要解释。"
    "若没有任何可保留的内容，输出 IGNORED。"
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
