# 局部重绘 Agent 设计方案（local-edit agent）

> 设计稿：2026-09-15。目标：对 `scripts/render_e2e.py` 已生成的渲染图做**局部调整**，
> 其余区域严格保持不变。本文档只含方案，实现待后续提交。

## 一、问题定义

- 输入：已生成的渲染图（如 `output/test1.png`）+ 自然语言局部调整指令
  （如"把淋浴间隔断改成实心墙""把中间的圆形茶几换成方形储物凳"）
- 输出：调整后的图，**指令区域外像素严格不变**
- 约束：继续走 OpenRouter 生态（gpt-image-2.5-sunburst / qwen3.8-flash）

## 二、核心难点与总对策

生图模型是"全图重画"的：直接发原图 + "只改 X，其他不变"，模型会重新生成
整张图——光照、纹理、小物件全局漂移（2026-09-14 实测：文字与图冲突时模型
各听一半，"保持不变"这类约束文字上说了不算）。

**对策：遮罩（mask）机制**。只允许遮罩区域内像素变化，区域外用原图像素
合成回来——"严格不变"由像素操作构造性保证，不依赖模型自觉。

## 三、实施前的两个技术探测（各一次便宜调用）

| # | 探测项 | 影响 |
|---|---|---|
| ① | qwen3.8-flash 能否输出目标物体 bbox（视觉定位/grounding） | 决定"文字描述→遮罩"能否自动化 |
| ② | OpenRouter `/api/v1/images` 是否支持 `mask` 参数透传 | 决定能否走标准 inpaint 路线 A |

两项都有兜底，探测失败不阻塞架构。

## 四、Agent 架构：三个子模块

```
原图 + 自然语言指令（"把淋浴间隔断改成实心墙"）
      │
      ▼  ①MaskAgent 遮罩生成
用户给了 mask 文件？──是──→ 直接用
      │否
      ▼
VLM 视觉定位（原图+指令 → 目标物体 bbox）→ 羽化遮罩（高斯羽化 15-30px）
      │
      ▼  ②EditAgent 局部重绘（三路线降级）
路线A: /api/v1/images + image + mask + 指令（标准 inpaint，探测②通过才走）
路线B: 全图重生成 + 遮罩合成回贴（主力路线）
       out = 原图×(1-mask) + 新图×mask   ← 区域外像素 100% 来自原图
       接缝处理：羽化 alpha + 颜色统计匹配
路线C: ComfyUI 本地 inpaint（SetLatentNoiseMask + SDXL，终极兜底）
      │
      ▼  ③VerifyAgent 质检（复用 tri_validate 的循环模式）
VLM 对比原图/新图："是否只有 X 区域变化且达成指令？"
  通过 → 交付；未通过 → 调整指令重试（≤2 轮）
```

### 路线取舍

- **路线 B 是主力**：不依赖探测②，"区域外严格不变"是像素级保证。代价是
  遮罩边缘可能有细微色差（羽化+颜色匹配压制）。
- **路线 A 质量更高**：遮罩内由模型按上下文重画、外不动由 API 保证；
  探测②通过则优先走。
- **路线 C** 仅当 B 接缝不可接受时启用（风格衔接风险最大，本地 SDXL 与
  gpt-image 质感有差异）。

## 五、交互设计

```bash
# 纯文字定位（MaskAgent 自动找目标）
uv run python scripts/local_edit.py \
    --image output/test1.png \
    --instruction "把中间的圆形茶几换成方形储物凳" \
    --out output/test1_edited.png

# 手动画了遮罩就优先用（最精准兜底，PS/画图涂白区域即可）
uv run python scripts/local_edit.py \
    --image output/test1.png --mask output/mask.png \
    --instruction "这个区域改成书架"

# 集成模式（后期）：render_e2e 出图后直接接局部调整（Stage 4）
uv run python scripts/render_e2e.py --input xxx.dwg --out output/test1.png \
    --edit "把淋浴间隔断改成实心墙"
```

产物目录（可审计、可重跑）：

```
output/edit_<ts>/
├── mask.png          # 遮罩（不满意可手改后 --mask 重跑）
├── edited.png        # 结果
└── edit_report.json  # 走的路线、定位 bbox、质检轮次结论
```

多次调整 = 串行迭代（上次输出作为下次输入），每次独立遮罩。

## 六、关键工程细节

1. **bbox 够不够**：VLM 定位给矩形框，框内含物体周边背景；inpaint 重画框内
   时背景可能微变——遮罩**外**严格不变的保证不受影响，框内背景微变是可
   接受代价（探测①若能输出多边形/点位可再收紧）。
2. **多处指令**："沙发和茶几都换" → grounding 返回多个 bbox → 并集遮罩。
3. **歧义消解**：描述匹配多个物体时，VLM 报告全部候选让用户选，或指令加
   方位词自动挑选。
4. **接缝质量**：路线 B 合成在浮点空间进行 + 颜色直方图匹配；仍可见则升级
   路线 C。
5. **VerifyAgent 判据**：与 `_verdict_ok` 同思路——只硬性要求"指令达成 +
   区域外无可见结构变化"，光照级微差不阻断。

## 七、风险登记

| 风险 | 缓解 |
|---|---|
| qwen3.8-flash grounding 精度未知 | 先探测；失败则用户提供 mask 文件兜底 |
| OpenRouter mask 参数不支持 | 路线 B 不依赖它 |
| 路线 B 接缝可见 | 羽化 + 颜色匹配；再不行升级路线 C |
| 模型忽略"只改遮罩区"指令（路线 A/B 共同） | VerifyAgent 循环 + mask 合成保证外边界 |

## 八、实施顺序（探测先行，每步独立可交付）

1. 探测：grounding 能力 + mask 参数（2 次调用）
2. MaskAgent + 路线 B（探测结果如何都能用，核心交付）
3. 路线 A（若探测②通过，遮罩内质量更好）
4. VerifyAgent 循环 + CLI（`scripts/local_edit.py`）
5. 集成进 `render_e2e`（`--edit`，Stage 4）

代码量预估：MaskAgent ~100 行、EditAgent ~150、VerifyAgent ~50、CLI ~60；
新增 `app/engines/local_edit_agent.py` 与 `scripts/local_edit.py` 两个文件。
