# app/engines/local_edit_agent.py
"""局部重绘 Agent（2026-09-15 设计，docs/local-edit-agent-plan-2026-09.md）。

三子模块：MaskAgent（定位+遮罩）→ EditAgent（重生成+合成回贴）→ VerifyAgent。

实测定版事实（勿凭旧经验推断）：
- qwen3.8-flash grounding 可用，但输出【0-1000 归一化坐标】（Qwen-VL 惯例），
  须按图像尺寸反归一化到像素；
- OpenRouter /api/v1/images 接受 mask 参数但【不像素级遵守】（区域外仅 ~16%
  像素不变，整图全局漂移）——故走路线 B：模型重生成负责"改对"，像素合成
  负责"区域外严格不变"，两者各司其职。

保证：合成后遮罩外像素与原图逐位一致（构造性保证，不依赖模型自觉）。
"""
import base64
import json
import time
from pathlib import Path

import httpx
import numpy as np
from PIL import Image, ImageFilter

BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_EDIT_MODEL = "openai/gpt-image-2.5-sunburst"
DEFAULT_VLM_MODEL = "qwen/qwen3.8-flash"

_GROUND_PROMPT = (
    "这张图将要进行如下局部编辑：{instruction}\n"
    "框出编辑会触及的目标（被替换/修改的物体本身，不是它的新样子）。"
    "只输出 JSON 数组，格式："
    '[{{"label":"...","bbox_2d":[x1,y1,x2,y2]}}]，坐标为 0-1000 归一化值'
    "（原点左上）。找不到则输出 []。"
)

_EDIT_PROMPT = (
    "Apply this exact modification to the interior rendering: "
    "{instruction}. Render the modified object realistically, consistent "
    "with the scene's perspective and lighting. Keep everything else "
    "identical: room layout, camera angle, walls, flooring, and all other "
    "furniture and objects."
)


def _data_uri(img_path: Path) -> str:
    b64 = base64.b64encode(img_path.read_bytes()).decode()
    return f"data:image/png;base64,{b64}"


class LocalEditAgent:
    def __init__(self, api_key: str, out_dir: Path, *,
                 edit_model: str = DEFAULT_EDIT_MODEL,
                 vlm_model: str = DEFAULT_VLM_MODEL,
                 feather_px: int = 20,
                 max_retries: int = 2,
                 timeout_s: int = 300) -> None:
        self.api_key = api_key
        self.out_dir = Path(out_dir)
        self.edit_model = edit_model
        self.vlm_model = vlm_model
        self.feather_px = feather_px
        self.max_retries = max_retries
        self.timeout_s = timeout_s

    # ── MaskAgent：VLM grounding → 像素 bbox → 羽化遮罩 ───────────────────
    async def ground_region(self, http: httpx.AsyncClient,
                            image_path: Path, target: str) -> list[dict]:
        """返回 [{"label", "bbox_norm", "bbox_px"}]，bbox_px 为像素坐标。"""
        msg = _GROUND_PROMPT.format(instruction=target)
        resp = await http.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json={"model": self.vlm_model, "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": _data_uri(image_path)}},
                    {"type": "text", "text": msg},
                ]}]},
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        start = content.find("[")
        if start < 0:
            return []
        # 容错解析：JSON 可能被 ```json 包裹或尾部有杂文
        try:
            items, _ = json.JSONDecoder().raw_decode(content[start:])
        except json.JSONDecodeError:
            return []
        img = Image.open(image_path)
        w, h = img.size
        out = []
        for it in items if isinstance(items, list) else []:
            bb = it.get("bbox_2d")
            if not bb or len(bb) != 4:
                continue
            x1, y1, x2, y2 = bb
            # 0-1000 归一化 → 像素（若已是像素级小图也会被 clamp 保护）
            px = [max(0, int(x1 / 1000 * w)), max(0, int(y1 / 1000 * h)),
                  min(w, int(x2 / 1000 * w)), min(h, int(y2 / 1000 * h))]
            out.append({"label": str(it.get("label", target)),
                        "bbox_norm": bb, "bbox_px": px})
        return out

    def build_mask(self, size: tuple[int, int],
                   bboxes_px: list[list[int]]) -> Image.Image:
        """bbox 并集 → L 模式遮罩（255=可编辑），高斯羽化边缘。"""
        m = Image.new("L", size, 0)
        px = m.load()
        for x1, y1, x2, y2 in bboxes_px:
            for y in range(y1, min(y2, size[1])):
                for x in range(x1, min(x2, size[0])):
                    px[x, y] = 255
        if self.feather_px > 0:
            m = m.filter(ImageFilter.GaussianBlur(self.feather_px))
        return m

    # ── EditAgent：重生成 + 颜色匹配 + 遮罩合成回贴 ───────────────────────
    async def regenerate(self, http: httpx.AsyncClient,
                         image_path: Path, instruction: str) -> Image.Image:
        resp = await http.post(
            f"{BASE_URL}/images",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json={"model": self.edit_model, "n": 1,
                  "prompt": _EDIT_PROMPT.format(instruction=instruction),
                  "input_references": [
                      {"type": "image_url",
                       "image_url": {"url": _data_uri(image_path)}}]},
        )
        resp.raise_for_status()
        import base64 as _b64
        raw = _b64.b64decode(resp.json()["data"][0]["b64_json"])
        import io
        return Image.open(io.BytesIO(raw)).convert("RGB")

    @staticmethod
    def _match_ring(edited: Image.Image, original: Image.Image,
                    mask: Image.Image, ring_px: int = 25) -> Image.Image:
        """按遮罩外环带做逐通道均值/方差匹配，压制全局光照漂移接缝。"""
        o = np.asarray(original, dtype=np.float32)
        e = np.asarray(edited.resize(original.size), dtype=np.float32)
        m = np.asarray(mask, dtype=np.float32) / 255.0
        # 环带 = 遮罩膨胀区 - 遮罩本体（两侧内容应一致，用于统计对齐）
        k = ring_px * 2 + 1
        dil = np.asarray(
            mask.filter(ImageFilter.MaxFilter(k)), dtype=np.float32) / 255.0
        ring = (dil > 0.5) & (m < 0.5)
        if ring.sum() < 100:   # 环带过小（贴边遮罩）则退化为全图匹配
            ring = np.ones_like(m, dtype=bool)
        out = e.copy()
        for c in range(3):
            mo, so = o[..., c][ring].mean(), o[..., c][ring].std() + 1e-6
            me, se = e[..., c][ring].mean(), e[..., c][ring].std() + 1e-6
            out[..., c] = (e[..., c] - me) / se * so + mo
        # 仅遮罩内应用校色（环带本身保持 edited 原值以减伪影）
        apply = np.clip(m, 0, 1)[..., None]
        return Image.fromarray(
            np.clip(apply * out + (1 - apply) * e, 0, 255).astype(np.uint8))

    def composite(self, original: Image.Image, edited: Image.Image,
                  mask: Image.Image) -> Image.Image:
        """out = 原图×(1-mask) + 校色后新图×mask —— 遮罩外逐位等于原图。"""
        e = self._match_ring(edited, original, mask)
        o = np.asarray(original.convert("RGB"), dtype=np.float32)
        e = np.asarray(e, dtype=np.float32)
        a = (np.asarray(mask, dtype=np.float32) / 255.0)[..., None]
        out = np.clip(o * (1 - a) + e * a, 0, 255).astype(np.uint8)
        return Image.fromarray(out)

    # ── VerifyAgent：指令达成 + 区域外无可见结构变化 ──────────────────────
    async def verify(self, http: httpx.AsyncClient, original: Path,
                     edited: Path, instruction: str) -> dict:
        prompt = (
            "图1是原图，图2是局部编辑后的图。编辑指令：" f"{instruction}\n"
            "检查两点：1) 指令是否已达成；2) 指令目标之外的区域（墙体布局、"
            "其他家具、门窗位置）是否保持不变（光照细微差异不算）。"
            '只输出 JSON：{"instruction_fulfilled": true/false, '
            '"outside_changed": true/false, "reason": "一句话"}'
        )
        resp = await http.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json={"model": self.vlm_model, "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": _data_uri(original)}},
                    {"type": "image_url",
                     "image_url": {"url": _data_uri(edited)}},
                    {"type": "text", "text": prompt},
                ]}]},
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        try:
            start = content.find("{")
            v, _ = json.JSONDecoder().raw_decode(content[start:])
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            pass
        return {"instruction_fulfilled": None, "outside_changed": None,
                "reason": f"verify 输出不可解析: {content[:120]}"}

    # ── 主流程 ────────────────────────────────────────────────────────────
    async def run(self, image_path: Path, instruction: str, *,
                  mask_path: Path | None = None) -> dict:
        image_path = Path(image_path)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        original = Image.open(image_path).convert("RGB")

        async with httpx.AsyncClient(timeout=self.timeout_s) as http:
            # ① 遮罩：用户文件优先，否则 VLM grounding
            if mask_path:
                mask = Image.open(mask_path).convert("L").resize(original.size)
                grounding: list[dict] = []
            else:
                grounding = await self.ground_region(
                    http, image_path, instruction)
                if not grounding:
                    raise RuntimeError(
                        f"未能定位目标（指令: {instruction}）；"
                        "请提供 --mask 遮罩文件或补充方位词")
                mask = self.build_mask(original.size,
                                       [g["bbox_px"] for g in grounding])
            mask_path_out = self.out_dir / f"mask_{ts}.png"
            mask.save(mask_path_out)

            # ②③ 重生成 → 合成 → 质检，未过重试
            report: dict = {"instruction": instruction,
                            "grounding": grounding,
                            "mask": str(mask_path_out), "retries": []}
            edited_path = self.out_dir / f"edited_{ts}.png"
            for attempt in range(self.max_retries + 1):
                regen = await self.regenerate(http, image_path, instruction)
                composed = self.composite(original, regen, mask)
                composed.save(edited_path)
                v = await self.verify(http, image_path, edited_path,
                                      instruction)
                report["verify"] = v
                ok = (v.get("instruction_fulfilled") is True
                      and v.get("outside_changed") is not True)
                report["attempts"] = attempt + 1
                if ok or attempt == self.max_retries:
                    break
                report["retries"].append({"attempt": attempt + 1,
                                          "verify": v})
                print(f"  质检未过（{v.get('reason')}），重试 "
                      f"{attempt + 1}/{self.max_retries}…")

            report["edited"] = str(edited_path)
            report_path = self.out_dir / f"edit_report_{ts}.json"
            report_path.write_text(json.dumps(
                report, ensure_ascii=False, indent=1), encoding="utf-8")
            return report
