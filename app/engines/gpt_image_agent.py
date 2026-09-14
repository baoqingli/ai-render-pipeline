# app/engines/gpt_image_agent.py
"""OpenRouter gpt-image 系列生图 Agent（2026-09-14 官方文档+实测校准）。

模型/端点事实（实测验证，勿凭旧经验推断）：
- openai/gpt-image-2.5-sunburst 等专生生图模型【不进 /models 列表】，
  【不走 /chat/completions】、【无 /images/edits 端点】（那是 OpenAI 官方
  API 的端点，OpenRouter 未实现）；
- OpenRouter 专用 Image API：POST /api/v1/images，
  参考图用 input_references 数组（官方文档标准写法）：
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
  实测 input_references 的结构服从度显著优于非官方的 image 单参数写法。

用法（独立）:
  from app.engines.gpt_image_agent import GptImageAgent
  agent = GptImageAgent(api_key="sk-or-...", out_dir=Path("output/render_gpt"))
  paths = asyncio.run(agent.run(pipeline_out_dir=Path("output/render_qwen3")))
"""
import asyncio
import base64
import mimetypes
import time
from pathlib import Path

import httpx

BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/gpt-image-2.5-sunburst"
# 参考图跟随的最低指令：不带布局/风格描述，解释权全交参考图。
# prompt 是必填项（API 强制 ≥1 非空白字符），也是激活参考图跟随的开关——
# 实测纯图 + 占位符 prompt 会被模型完全无视。
MIN_PROMPT_SUFFIX = "Convert this floor plan into a photorealistic 3D rendering."


def _data_uri(img: Path) -> str:
    mime = mimetypes.guess_type(img.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(img.read_bytes()).decode()}"


class GptImageAgent:
    def __init__(self, api_key: str, out_dir: Path,
                 model: str = DEFAULT_MODEL,
                 n: int = 1,
                 with_reference: bool = True,
                 prompt_mode: str = "minimal",   # minimal | file
                 timeout_s: int = 300) -> None:
        self.api_key = api_key
        self.out_dir = Path(out_dir)
        self.model = model
        self.n = n
        self.with_reference = with_reference
        self.prompt_mode = prompt_mode
        self.timeout_s = timeout_s

    async def run(self, pipeline_out_dir: Path,
                  reference_img: Path | None = None,
                  prompt: str | None = None) -> list[str]:
        """读取管线输出目录的 prompt.txt + 布局参考图，生成效果图。

        实测结论（2026-09-14，三组对照）：
        - 参考图用原始 layout.png（CAD 渲染）而非语义重绘图——原始图几何
          最准，模型在 input_references 正确路由下读得很好；语义图是外接
          矩形粗化重绘，失真会原样传导进生成图。
        - prompt 用最小指令——逐房间方位描述若与参考图方位冲突，模型
          各听一半，结构反而变差。
        prompt_mode="minimal" 只给转换指令；"file" 读 prompt.txt（可含
          build_prompt 的逐房间描述，供实验对比）。
        返回保存到 out_dir 的图片路径列表。
        """
        pipeline_out_dir = Path(pipeline_out_dir)
        if prompt is None and self.prompt_mode == "file":
            prompt = (pipeline_out_dir / "prompt.txt").read_text(
                encoding="utf-8").strip()
        prompt = (prompt or MIN_PROMPT_SUFFIX).strip()
        if self.with_reference and "floor plan" not in prompt.lower():
            prompt = f"{prompt}\n{MIN_PROMPT_SUFFIX}"

        if self.with_reference:
            layout_img = Path(reference_img) if reference_img \
                else pipeline_out_dir / "layout.png"
            if not layout_img.exists():
                raise FileNotFoundError(f"找不到布局参考图: {layout_img}")

        self.out_dir.mkdir(parents=True, exist_ok=True)

        body: dict = {"model": self.model, "prompt": prompt, "n": self.n}
        if self.with_reference:
            body["input_references"] = [
                {"type": "image_url", "image_url": {"url": _data_uri(layout_img)}}
            ]

        async with httpx.AsyncClient(timeout=self.timeout_s) as http:
            # 瞬时错误重试（502/503/429/超时）：生图在链路末端，白跑代价大
            delays = (5, 15)  # 两次重试，递增间隔
            for attempt in range(len(delays) + 1):
                resp = await http.post(
                    f"{BASE_URL}/images",
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json"},
                    json=body,
                )
                if resp.status_code in (429, 502, 503, 504) and attempt < len(delays):
                    wait = delays[attempt]
                    print(f"  瞬时错误 {resp.status_code}，{wait}s 后重试"
                          f"（{attempt + 1}/{len(delays)}）…")
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                return self._save_results(resp.json())

    def _save_results(self, resp_json: dict) -> list[str]:
        items = resp_json.get("data", [])
        saved = []
        ts = int(time.time())
        for i, item in enumerate(items):
            b64 = item.get("b64_json") or ""
            if not b64:
                url = item.get("url", "")
                print(f"  图{i+1} 返回 url（需手动下载）: {url}")
                continue
            out = self.out_dir / f"gpt_render_{ts}_{i+1}.png"
            out.write_bytes(base64.b64decode(b64))
            print(f"  已保存: {out.name}")
            saved.append(str(out))
        return saved
