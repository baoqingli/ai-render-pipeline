# 识图/验证 Agent 优化方案 — 2026-09-10

> 用户判断：白模问题反复的根源在两个 Agent 能力不足。调研 + 方案 + 实施。
> 关联：[白模修正方案](./white-model-fix-plan-2026-09.md)｜[根本需求](./requirement.md)

---

## 1. 线上调研结论（2026-09-10）

| 发现 | 来源 | 对本项目的含义 |
| --- | --- | --- |
| **纯 VLM 做精确几何是行业公认弱项**——2025-26 趋势是"分割模型出几何 + LLM 出语义"的混合架构 | [arXiv 2409.12842](https://arxiv.org/html/2409.12842v1)、[r/computervision 实践讨论](https://www.reddit.com/r/computervision/comments/1qwghux/)、[2026 preprint](https://www.preprints.org/manuscript/202605.1893) | **证实用户判断**：我们让 glm-5.3-flash 直接输出坐标是弱项错配 |
| **CubiCasa5K**：开源预训练多任务模型，户型图→墙/门/窗/房间多边形（SVG 标注，80+ 类） | [GitHub CubiCasa5k](https://github.com/CubiCasa/CubiCasa5k)、[Vitalify 3D 生成实践](https://www.vitalify.asia/en/blog/generate-3d-model-from-floor-plan-cubicasa5k-machine-learning) | 无尺寸图路径的理想几何引擎；但需 PyTorch+MMDet（2GB+），本项目 Python 3.12 兼容性风险高 |
| **Raster-to-Vector（ICCV'17）**： junction 检测→矢量化 | [FloorplanTransformation](https://github.com/art-programmer/FloorplanTransformation) | 经典形态学/连接点方法——**不依赖深度学习也可提取墙线** |
| **结构化输出用 API 级 schema 约束**，prompt-only JSON 不可靠；mini 级模型会丢键 | [GPT-4o Vision 实践](https://getstream.io/blog/gpt-4o-vision-guide/)、[Gemini structured outputs](https://medium.com/google-cloud/structured-output-with-gemini-models-begging-borrowing-and-json-ing-f70ffd60eae6) | 我们已做 pydantic 校验+容错，方向正确 |
| **自验证 LLM Agent 框架**（提取→自检循环）正在兴起 | [SSRN 自验证框架](https://papers.ssrn.com/sol3/Delivery.cfm/1f6fdac7-4264-4a23-a8c7-3dd45165efff-MECA.pdf?abstractid=7349462) | 与我们的验证 Agent 设计同构，可借鉴其"提取结果反喂验证"模式 |

**结论**：用户的方案与行业最佳实践一致——
1. 带尺寸图 → 尺寸数字定标 → 精确坐标（已有 dimension_walls 基础）
2. 无尺寸图 → 像素坐标体系 → 元素位置/大小（新增能力）
3. VLM 只做语义（类别/房间名），几何交给确定性计算
4. 双 Agent 独立解耦，识图 Agent 输出可直接喂图像生成大模型

## 2. 架构设计

```
输入（DWG/DXF 或 图片）
   │
   ├─ 路径 A：矢量输入（DWG/DXF）
   │    实体提取（HATCH/多段线/块，已有）
   │    + 尺寸链 defpoints（已有，精确 mm）
   │    + VLM 语义标注（类别/房间名）
   │
   ├─ 路径 B：栅格输入（PNG/JPG 截图）——新增
   │    B1 尺寸定标：VLM 读图中尺寸文字（"9600"/"4400"）
   │       → 已知两点的像素距离 + 真实 mm → px/mm 比例尺
   │    B2 墙线提取：numpy 形态学（阈值→腐蚀膨胀→连通域→矩形拟合）
   │       ——不依赖深度学习，参考 ICCV'17 经典方法
   │    B3 元素检测：VLM 分块识别（已有 tiles）+ 像素 bbox → mm bbox
   │    B4 房间推导：墙线栅格 → flood fill 连通域 → 房间多边形
   │
   ▼
ElementRegistry（统一元素登记簿，mm 坐标）
   ├── 消费者1：白模管线（SceneJSON → Blender）
   ├── 消费者2：验证 Agent（登记簿 vs 渲染图 vs 标准图三向对账）
   └── 消费者3：AI 直出包（布局描述+控制图+prompt → gpt-image/SDXL）
```

## 3. 实施任务

### T1 栅格几何引擎 `raster_geometry.py`（核心新增，纯 numpy）
- `calibrate_scale(png, known_dims) -> px_per_mm`：尺寸文字定位+两点距离定标
- `extract_wall_mask(png) -> mask`：暗色粗线阈值 + 形态学闭运算（补门洞缝隙）+ 连通域过滤（细线/文字剔除）
- `mask_to_wall_rects(mask) -> [rect]`：连通域 → 最小外接矩形 → 墙段
- `rooms_from_walls(wall_rects) -> [polygon]`：墙网栅格 flood fill
- 单测：合成户型图（画墙→提取→断言墙数/位置）

### T2 识图 Agent v3 `agent_v3.py`（统一入口）
- `analyze(dwg_or_image) -> ElementRegistry`
- DWG → 路径 A；图片 → 路径 B
- VLM 职责收窄：只出语义（类别/名称/朝向/尺寸文字），不出几何坐标
- 几何全部来自确定性引擎（A: DXF+尺寸链；B: 栅格引擎）

### T3 验证 Agent 升级（三向对账）
- 输入：ElementRegistry + 渲染图 + 标准图
- 确定性对账优先（几何 IoU/存在性），VLM 视觉对比兜底
- 输出：结构化差异 + 修正建议（已有基础，接入 v3）

### T4 AI 直出包 `render_package.py`
- ElementRegistry → 完整图像生成输入包：
  - 布局控制图（俯视线稿，已有渲染能力）
  - 结构化布局描述（layout_prompt.py 增强）
  - 风格 prompt（style agent 衔接）
- 目标：可直喂 gpt-image / SDXL img2img + ControlNet

### T5 回归验证
- 真实 DWG（01-平面系统图）路径 A 全链
- 用户截图（无 DXF）路径 B 全链
- golden 测试更新

## 4. 边界与不做

- 不引入 PyTorch/MMDetection（CubiCasa5K）——依赖代价 > 收益；形态学路径覆盖 80% 场景
  （若后续无尺寸图量增大，再评估 CubiCasa5K 作为 B2/B3 的升级替换件）
- VLM 不再输出任何几何坐标（职责硬隔离）
- 3D 白模管线（Blender）不动——它是 ElementRegistry 的消费者之一
