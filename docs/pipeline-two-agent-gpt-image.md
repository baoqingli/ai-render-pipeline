# 两 Agent + gpt-image 直出渲染方案

> 定版：2026-09-14。全链路当日实测打通并验证可复现。
> 路线：DWG/DXF → 识图 Agent → 验证 Agent → gpt-image-2.5-sunburst → 高保真 dollhouse 渲染图。

## 一、生成链路总览

```
fixtures/cad/01-平面系统图.dwg                    ← 原始输入（用户提供）
      │ ODA File Converter（项目外，此前已转）
      ▼
fixtures/cad/_converted/01-平面系统图.31cd742821d4c1dc.dxf
      │
      ▼  ①识图 Agent（qwen/qwen3.8-flash）
ElementRegistry（31墙/8房/4门/3窗/21家具）
      │
      ▼  ②验证 Agent（qwen/qwen3.8-flash，≤3轮）
output/render_qwen3/layout.png                   ← 关键中间产物：CAD 布局渲染图
output/render_qwen3/elements.json
      │
      ▼  ③生图 Agent（openai/gpt-image-2.5-sunburst）
output/render_gpt/gpt_render_<ts>_1.png          ← 最终效果图
```

## 二、用法

```bash
# 全链路第一段：识图 + 验证 + 出包（模型可切换）
uv run python scripts/two_agent.py \
    --input "fixtures/cad/_converted/01-平面系统图.31cd742821d4c1dc.dxf" \
    --out output/render_qwen3 \
    --model qwen/qwen3.8-flash

# 全链路第二段：生图（消费第一段产物）
uv run python scripts/gpt_image_render.py \
    --pipeline-out output/render_qwen3 \
    --out output/render_gpt \
    --n 1

# 对比实验开关（本次实测的落选路径，留作备查）
#   --reference semantic   语义配色重绘图作参考（失真传导，已弃用）
#   --prompt-mode file     逐房间方位长 prompt（与图冲突，已弃用）
```

## 三、输入内容及其来源

| 输入 | 来源 | 作用 |
|---|---|---|
| DXF 文件 | DWG 经 ODA 转换（`fixtures/cad/_converted/`） | 几何真值：墙体/房间/门窗/家具实体 |
| VLM 解读书 `DrawingUnderstanding` | qwen3.8-flash 现场生成（缓存优先：`experiments/data/model/p0_verify/drawing_understanding.json`） | 语义图层路由、房间分区、家具扫荡 |
| `layout.png` | `vision_agent` 用 `cad_render.render_sheet_view` 从 DXF 渲染（叠加 GND 地面材质图层，1600px） | 生图阶段的**唯一空间参考**（`input_references`） |
| 提示词 | `MIN_PROMPT_SUFFIX` 常量（`app/engines/gpt_image_agent.py`）：*"Convert this floor plan into a photorealistic 3D rendering."* | 激活参考图跟随；**不带任何布局/风格描述** |
| `validation.json` | 验证 Agent 逐轮输出 | 质量门（未通过时可 `--force`，仅 two_agent_comfy 路径） |

## 四、组件依赖清单

**Agent/管线层**
- `scripts/two_agent.py` — 识图+验证 CLI（`--model` 切换双 Agent 模型）
- `scripts/two_agent_comfy.py` — 识图+验证 + ComfyUI ControlNet 出图（并行路线）
- `scripts/gpt_image_render.py` — 生图 CLI 入口
- `app/engines/gpt_image_agent.py` — `GptImageAgent`：组装 `input_references` + prompt，调 OpenRouter Image API，解码 b64 落盘
- `app/agents/vision/two_agent_pipeline.py` — 识图→验证→出包编排
- `app/agents/vision/agent_v3.py` — `analyze_dwg`（VLM 解读书 + 视口对齐 + 解析）、`scene_to_registry`（shapely 墙碎片合并、bbox_pct 归一化，y-down 与图面同向）
- `app/agents/vision/agent.py` — `analyze_drawing`（多视图渲染 + VLM 读图）
- `app/tools/tri_validate.py` — `validate_registry`（清单 × 布局图 VLM 核对）
- `app/agents/vision/gated_analyze.py` — 确定性修正器（墙碎片合并/房间过数合并）
- `app/agents/vision/render_package.py` — `build_prompt`（逐房间方位描述，file 模式用）、`draw_semantic_reference`（语义图，实验用）

**CAD 工具层**
- `app/tools/cad/parse.py` — `parse_scene`（ezdxf 实体+HATCH+尺寸链提取）
- `app/tools/cad_sheets.py` — `pick_layout_view`（布置图视口选择、冻结图层）
- `app/tools/cad_render.py` — `Canvas`/`render_sheet_view`/`render_modelspace`（layout.png 绘制）

**基础设施层**
- `app/core/config.py` — `Settings`（`ARP_` 前缀环境变量，.env）
- `app/infra/llm.py` — `make_chat_model`（base_url 含 `/api/anthropic` 走 Anthropic 协议；否则 OpenAI 兼容。当前 .env 为 OpenRouter → 后者）
- `app/models/vision.py` / `app/models/tooling.py` — `ElementRegistry` / `ToolResult`

**Python 依赖**：ezdxf、shapely、httpx、langchain-openai、pydantic/pydantic-settings、Pillow（仅 semantic 实验路径）

## 五、外部服务依赖

| 服务 | 用途 | 端点 / 模型 |
|---|---|---|
| OpenRouter `https://openrouter.ai/api/v1` | 识图/验证 VLM 推理 | `/chat/completions`，`qwen/qwen3.8-flash` |
| OpenRouter（同一 key） | 生图 | **`/images` + `input_references`**（官方文档标准写法），`openai/gpt-image-2.5-sunburst` |
| 认证 | — | `ARP_LLM_API_KEY`（sk-or-v1-…，.env） |

本路线**不依赖**：ComfyUI / 本地 GPU / GLM 端点（`two_agent_comfy.py` 是并行的 ControlNet 路线，独立运行）。

## 六、OpenRouter 生图 API 实测口径（2026-09-14）

| 调用方式 | 结论 |
|---|---|
| `/images/edits`（OpenAI 官方 SDK `client.images.edit`） | ✗ OpenRouter **无此端点**（404）。网上样例多为 OpenAI 官方 API 写法，照抄到 OpenRouter 跑不通 |
| `/chat/completions` 生图模型 | ✗ 报错并提示转 `/api/v1/images`；且 `gpt-image-2.5-sunburst` 等专生生图模型**不进 `/models` 列表**（按列表搜会漏） |
| `/images/generations` + 非官方 `image` 单参数 | △ 能出图但结构服从度弱于标准写法 |
| **`/images` + `input_references` 数组（定版）** | ✓ 结构近乎 1:1 |

```json
POST https://openrouter.ai/api/v1/images
{
  "model": "openai/gpt-image-2.5-sunburst",
  "prompt": "Convert this floor plan into a photorealistic 3D rendering.",
  "n": 1,
  "input_references": [
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
  ]
}
```

其他实测事实：
- `prompt` 必填且 ≥1 非空白字符（空串/空格均被 Zod 校验拒绝）；
- prompt 是参考图跟随的**开关**：纯图 + 占位符 `"."` → 模型完全无视参考图，自由发挥；
- 参考图支持 HTTP(S) URL 或 base64 data URI。

## 七、关键实测经验（本路线的调参红线）

1. **参考图越原始越好**：直接喂 `cad_render` 产出的 CAD 布局图。曾改为语义配色重绘图（registry 外接矩形粗化），失真原样传导进生成图，已回退。
2. **文字越少越好**：只给转换指令。逐房间方位长 prompt 若与参考图方位冲突（如文字说卧室在右下、图里在右上），模型各听一半，结构反而变差。
3. **模型可正确解读 CAD 抽象符号**：三角/菱形→玻璃淋浴隔断、曲线形状→弧形椅、灶眼圆圈→灶台、圆心带点圆→地毯+落地灯，均原位渲染。
4. **两 Agent 模型可整体切换**：`--model` 一处参数同步控制识图与验证（`vision_agent` → `analyze_dwg(model=...)`，`validation_agent(model=...)`）。
5. **验证门口径**：仅墙结构 `under_count/fragmentation` 且 high severity 才硬失败；门/窗/家具计数差异不阻断（VLM 估计口径波动大）。

## 八、产物清单

```
output/render_qwen3/            # 第一段产物
├── layout.png                  # CAD 布局渲染图（生图参考图）
├── elements.json               # ElementRegistry（几何清单）
├── validation.json             # 验证逐轮记录
├── prompt.txt / prompt_zh.txt  # 提示词（file 模式 / 审阅用）
└── layout_control.png          # 线稿控制图（ComfyUI 路线用）

output/render_gpt/              # 第二段产物
└── gpt_render_<ts>_<i>.png     # dollhouse 俯视渲染图
```
