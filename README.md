# AI Render Pipeline

CAD 图纸（DWG/DXF）或户型图片（PNG/JPG）→ 两 Agent 识图验证 → AI 生图 → dollhouse 俯视 3D 渲染图，支持自然语言局部编辑精修。

## 快速开始（一键渲染）

```bash
uv run python scripts/render_e2e.py \
    --input "d:/me_work/AI/project/ai-render-pipeline/fixtures/cad/01-平面系统图.dwg" --force
```

**目录约定**：产物落在 `<输出根>/<日期>/<运行时分秒>/`（默认根 `output/`），
最终成品固定命名 `final.png`，同目录还有 layout.png、elements.json、
validation.json、prompt.txt 和 renders/ 等全部中间产物；当天的所有运行
都在同一个日期目录下，互不覆盖。

### 常用参数

- `--input` 支持 DWG / DXF / PNG / JPG（DWG 自动经 ODA 转 DXF；路径用正斜杠）
- `--force` 验证未通过时仍继续生图（VLM 计数判定有波动，建议常开）
- `--desc` 自然语言生图描述（自由输入：风格/材质/配色/光照/氛围/家具偏好/夜景等）：

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

### 局部编辑

**方式一：出图时一并下编辑指令**（`--edit` 可多次传入，串行执行）：

```bash
uv run python scripts/render_e2e.py \
    --input "d:/me_work/AI/project/ai-render-pipeline/fixtures/cad/01-平面系统图.dwg" \
    --force \
    --desc "现代简约风，浅色木地板，暖色灯光" \
    --edit "把地毯上的圆形茶几换成方形黑色茶几" \
    --edit "删掉中央的布艺沙发"
```

**方式二：对已有渲染图单独编辑**（人在回路推荐，产物 `update_*.png`
默认落在被编辑图片的同目录）：

```bash
uv run python scripts/local_edit.py \
    --image "output/2026-09-15/175252/final.png" \
    --instruction "把中央的布艺沙发换成深绿色布艺沙发"
```

指令会自动编译为正向终态描述（模糊/负向说法也能用，如"去掉封闭部分"会
被改写成具体的样子描述）；`--no-compile` 可跳过。质检未过自动带失败原因
改写指令重试。**指令写法建议**：描述改完后的样子（正向终态）>
"去掉某物"式负向说法；实在描述不清用 `--mask` 涂白区域兜底。

**推荐工作流（人在回路）**：出图后先人工检查，发现问题再逐处局部修——

```bash
# 1. 出图（不带 --edit）→ output/<日期>/<时分秒>/final.png
uv run python scripts/render_e2e.py --input "xxx.dwg" --force
# 2. 看图后局部修（产物与被编辑图同目录，路径会打印，链式迭代即可）
uv run python scripts/local_edit.py \
    --image output/2026-09-15/175252/final.png \
    --instruction "沙发离墙太远，往上移"
# 3. 看同目录下 update_*.png，继续修下一处
```

- `--n 4` 一次生成多张；`--model` 切换识图+验证模型（默认 `qwen/qwen3.8-flash`）

## HTTP API（生成 + 局部编辑）

### 启动服务

```bash
uv run python scripts/run_render_api.py        # 默认 8100 端口
uv run python scripts/run_render_api.py 9000   # 自定义端口
```

独立轻量服务，不依赖 PG/Valkey；产物经 `/files/<相对路径>` 下载。

### 端点一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/renders` | 提交生成任务（multipart 上传图纸） |
| POST | `/api/v1/edits` | 提交局部编辑任务 |
| GET | `/api/v1/dirs` | 列运行子目录（`<日期>/<时分秒>`，升序） |
| GET | `/api/v1/dirs/{path}` | 列目录下图片文件（`[{name,url}]`，仅顶层） |
| GET | `/api/v1/jobs/{job_id}` | 查询任务状态与结果 |
| GET | `/api/v1/jobs?jtype=render&status=succeeded` | 任务列表（可过滤） |
| GET | `/files/{path}` | 产物下载（相对 output 根） |
| GET | `/api/v1/healthz` | 健康检查 |

### 完整流程示例

**1. 提交生成任务**（202 返回 job_id）：

```bash
curl -X POST http://localhost:8100/api/v1/renders \
  -F "file=@/tmp/plan.dxf" \
  -F "desc=现代简约风，浅色木地板，暖色灯光" \
  -F "force=true" \
  -F "edit=把圆形茶几换成方形黑色茶几"
```

支持的表单字段：`file`（.dwg/.dxf/.png/.jpg 必填）、`desc`、`edit`
（可重复）、`force`（默认 true）、`model`、`gpt_model`、`n`、`max_iters`。

**2. 提交局部编辑**（两种图片来源二选一）：

```bash
# 方式 A：引用服务端已有产物（相对 output 根，如之前生成的 final.png）
curl -X POST http://localhost:8100/api/v1/edits \
  -F "image_path=2026-09-16/114228/final.png" \
  -F "instruction=把阳台上的圆形木桌换成方形黑色茶几"

# 方式 B：直接上传图片
curl -X POST http://localhost:8100/api/v1/edits \
  -F "image=@/tmp/room.png" \
  -F "instruction=删掉绿色扶手椅"
```

可选字段：`mask` / `mask_path`（白=可编辑区，描述不清时的兜底）、
`no_compile`、`max_retries`、`edit_model`、`vlm_model`。

**3. 轮询任务**（建议每 2~5 秒一次，整体 30 秒 ~ 5 分钟）：

```bash
curl http://localhost:8100/api/v1/jobs/r-20260916-114228-34b4
```

```json
{
  "job_id": "r-20260916-114228-34b4",
  "type": "render",
  "status": "succeeded",
  "stage": "done",
  "error": null,
  "result": {
    "final": "/files/2026-09-16/114228/final.png",
    "renders": ["/files/2026-09-16/114228/renders/gpt_render_...png"],
    "validation_passed": true,
    "out_dir": "/files/2026-09-16/114228"
  }
}
```

- `status`：`pending` → `running` → `succeeded` / `failed`
- `stage`（running 时）：`vision`（识图验证）→ `generate`（生图）→
  `edit`（局部编辑）→ `done`
- `succeeded` 后用 result 里的 `/files/...` URL 直接下载图：

```bash
curl -o final.png http://localhost:8100/files/2026-09-16/114228/final.png
```

### 注意事项

- DWG 输入要求 API **服务进程**可找到 ODA File Converter（PATH 或
  `ARP_ODA_EXE`）；DXF/PNG/JPG 无此要求
- 上传文件的路径建议用 ASCII 文件名（部分 curl 版本对中文路径的
  multipart 处理不稳）
- 任务列表在服务重启后清空（产物文件不受影响，可按 output 目录对账）
- 产物根目录由 `ARP_OUTPUT_ROOT` 控制（默认 `output/`），与 CLI 共用
- 详见 [HTTP API 设计](docs/http-api-design-2026-09.md)

## 前置条件

1. `.env` 配置（参考现有条目）：
   - `ARP_LLM_BASE_URL=https://openrouter.ai/api/v1`
   - `ARP_LLM_API_KEY=<OpenRouter key>`
   - `ARP_VISION_MODEL=qwen/qwen3.8-flash`
2. DWG 输入需安装 [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter)（`ARP_ODA_EXE`，默认在 PATH）

## 更多文档

- [两Agent + gpt-image 直出渲染方案](docs/pipeline-two-agent-gpt-image.md) — 完整链路依赖、OpenRouter 生图 API 实测口径、调参红线
- [局部重绘 Agent 设计](docs/local-edit-agent-plan-2026-09.md) — 定位/遮罩合成/质检循环与实测边界
- [HTTP API 设计](docs/http-api-design-2026-09.md) — 异步作业模型、接口契约
- [ComfyUI ControlNet 工作流](docs/ComfyUI室内设计工作流总结.md) — 并行的像素级结构锁定路线（`scripts/two_agent_comfy.py`）
