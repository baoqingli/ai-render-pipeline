# AI Render Pipeline

CAD 图纸（DWG/DXF）或户型图片（PNG/JPG）→ 两 Agent 识图验证 → AI 生图 → dollhouse 俯视 3D 渲染图。

## 快速开始（一键渲染）

```bash
uv run python scripts/render_e2e.py \
    --input "d:/me_work/AI/project/ai-render-pipeline/fixtures/cad/01-平面系统图.dwg" --force
```

**目录约定**：产物落在 `<输出根>/<日期>/<运行时分秒>/`（默认根 `output/`），
最终成品固定命名 `final.png`，同目录还有 layout.png、elements.json、
validation.json、prompt.txt 和 renders/ 等全部中间产物；当天的所有运行
都在同一个日期目录下，互不覆盖。

- `--input` 支持 DWG / DXF / PNG / JPG（DWG 自动经 ODA 转 DXF；路径用正斜杠）
- `--out` 是最终渲染图的保存路径（中间产物 layout.png / elements.json / validation.json 写在其父目录）
- `--force` 验证未通过时仍继续生图（VLM 计数判定有波动，建议常开）
- `--desc` 自然语言生图描述（自由输入：风格/材质/配色/光照/氛围/家具偏好/夜景等，自动识别生图相关内容并译为英文拼入 prompt）：

```bash
uv run python scripts/render_e2e.py \
    --input "d:/me_work/AI/project/ai-render-pipeline/fixtures/cad/01-平面系统图.dwg" \
    --force \
    --desc "渲染风格使用日式原木风，榻榻米元素，暖黄色灯光"
```

  描述与布局互不干扰。三类内容自动识别：
  - 效果描述（风格/材质/光照/氛围/家具）→ 保留
  - 空间状态陈述（"玄关上面是淋浴间"）→ 保留，帮助模型正确解读参考图
  - 布局改动要求（"改成三室""卧室放大"）与无关闲聊 → 剥离，布局由参考图决定
  纯改动输入则回落默认渲染。
- `--edit` 出图后的局部编辑指令（可多次传入串行执行），只改目标区域、
  其余像素严格不变（定位→重生成→遮罩合成回贴→质检，详见
  [局部重绘设计](docs/local-edit-agent-plan-2026-09.md)）：

```bash
uv run python scripts/render_e2e.py \
    --input "d:/me_work/AI/project/ai-render-pipeline/fixtures/cad/01-平面系统图.dwg" \
    --force \
    --desc "现代简约风，浅色木地板，暖色灯光" \
    --edit "把地毯上的圆形茶几换成方形黑色茶几" \
    --edit "删掉中央的布艺沙发"
```

  也可对任意已有渲染图单独做局部编辑（产物默认落在被编辑图片的同目录）：
  `scripts/local_edit.py --image <图> --instruction <指令>`。
  指令会自动编译为正向终态描述（模糊/负向说法也能用，如"去掉封闭部分"会
  被改写成具体的样子描述）；`--no-compile` 可跳过。质检未过自动带失败原因
  改写指令重试。

**推荐工作流（人在回路）**：出图后先人工检查，发现问题再逐处局部修——

```bash
# 1. 出图（不带 --edit）→ output/<日期>/<时分秒>/final.png
uv run python scripts/render_e2e.py --input "xxx.dwg" --force
# 2. 看图后局部修（产物默认与被编辑图同目录，路径会打印，链式迭代即可）
uv run python scripts/local_edit.py \
    --image output/2026-09-15/175252/final.png \
    --instruction "沙发离墙太远，往上移"
# 3. 看同目录下 edited_*.png，继续修下一处
```
- `--n 4` 一次生成多张；`--model` 切换识图+验证模型（默认 `qwen/qwen3.8-flash`）

渲染图输出示例：`output/test1.png`，全部产物在 `output/renders/`。

## 前置条件

1. `.env` 配置（参考现有条目）：
   - `ARP_LLM_BASE_URL=https://openrouter.ai/api/v1`
   - `ARP_LLM_API_KEY=<OpenRouter key>`
   - `ARP_VISION_MODEL=qwen/qwen3.8-flash`
2. DWG 输入需安装 [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter)（`ARP_ODA_EXE`，默认在 PATH）

## 更多文档

- [两Agent + gpt-image 直出渲染方案](docs/pipeline-two-agent-gpt-image.md) — 完整链路依赖、OpenRouter 生图 API 实测口径、调参红线
- [ComfyUI ControlNet 工作流](docs/ComfyUI室内设计工作流总结.md) — 并行的像素级结构锁定路线（`scripts/two_agent_comfy.py`）
