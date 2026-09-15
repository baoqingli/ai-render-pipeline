# app/agents/vision/edit_compiler.py
"""编辑指令编译器：用户口语化/模糊/负向的局部编辑指令 → 可执行的编辑描述。

实测教训（2026-09-15，洗手池案例）：负向指令（"去掉左侧封闭部分"）三轮
全败——重生成模型默认忠实复刻原图，只说不要什么、不说变成什么样，模型无
从得知目标状态，输出 diff≈0。改写为正向终态描述后一次过检。

改写规则：
①负向表述 → 正向终态（去掉 X → X 处变为 Y 样子）；
②模糊指代 → 具体化（位置+目标物体+改后形态/材质/颜色）；
③完整物体删除（"删掉椅子"）保留原意；
④严格保留用户的位置与尺寸意图，不发明新要求。

与 desc_compiler 的关键区别：desc 编译器剥离布局类要求（整图生成的布局
由参考图决定）；编辑编译器恰恰要保留布局改动意图（"沙发往上移"是合法
编辑）。LLM 失败降级为返回原文（等价于现状行为）。
"""
from app.infra.llm import make_chat_model

_SYSTEM = (
    "你是室内局部编辑指令编译器。把用户的编辑要求改写成具体、可执行的"
    "正向终态描述，供图像编辑模型使用：\n"
    "①负向表述（去掉/删除某部分/某处不正确）→ 改写为编辑完成后的画面终态"
    "（那个位置变成什么样子）；\n"
    "②模糊指代 → 具体化：位置+目标物体+改后的形态/材质/颜色；\n"
    "③完整物体的删除（如'删掉椅子'）保留原意即可；\n"
    "④严格保留用户的位置与尺寸意图，不新增用户没提的要求；\n"
    "⑤只改用户点名要改的东西，其余一切严格保持原样。未被用户提及的固定"
    "设施（洗手池/台盆、马桶、门、窗、墙体）必须保持原位；用户明确要求"
    "改动某固定设施（如'墙改成玻璃门'）则照用户要求改，但其余未被提及"
    "的固定设施仍然不许动。\n"
    "用中文输出，不超过 100 字，只输出改写后的指令本身，不要解释。"
    "（实测：该编辑模型对具体化的中文指令服从度高于英文。）"
)


def compile_edit_instruction(instruction: str, *, model: str | None = None,
                             failure_reason: str | None = None) -> str:
    """编译编辑指令。LLM 失败时返回原文（降级=现状行为）。

    failure_reason：质检失败原因（重试时传入，使改写针对性强化）。
    同步实现；调用方若已在事件循环中请放线程执行。
    """
    text = instruction.strip()
    if not text:
        return text
    from app.core.config import get_settings

    s = get_settings()
    use_model = model or s.vision_model or s.llm_model
    client = make_chat_model(s.model_copy(update={"llm_model": use_model}),
                             temperature=0.3)
    user_content = f"用户编辑指令：{text}"
    if failure_reason:
        user_content += (f"\n上一轮编辑失败原因：{failure_reason}"
                         "\n请改写得更明确，确保改动在结果图里明显可见。")
    try:
        resp = client.invoke([
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_content},
        ])
    except Exception:  # noqa: BLE001 降级：LLM 不可用时用原文
        return text
    out = str(resp.content).strip().strip('"').strip()
    return out or text
