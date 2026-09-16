# HTTP API 方案设计：生成 + 局部编辑

> 设计稿：2026-09-15。为 `render_e2e`（生成）与 `local_edit`（局部编辑）
> 两个业务增加 HTTP API 访问能力。本文档只含方案，实现待后续提交。

## 一、现状与设计原则

| 现状 | 对设计的影响 |
|---|---|
| 业务已收敛为两个 async 单入口：`run_e2e()`（生成）、`LocalEditAgent.run()`（编辑） | API 层**只做编排**，业务函数零改动 |
| 单次时长 30 秒 ~ 5 分钟（识图+生图+编辑） | **必须异步作业模式**：提交即返回 202 + job_id，客户端轮询取结果 |
| 已有 FastAPI 栈（`app/api/`，uvicorn:8000）且有 `POST 202 受理 → GET 轮询 → SSE` 成熟模式 | 新端点作为新 router 挂进现有 `create_app`，风格对齐 |
| 产物全是磁盘文件（`output/<日期>/<运行>/final.png` 等） | 产物用 **URL 引用**而非 base64 内联（图大，几 MB/张），静态路由对外暴露 |
| 编辑的输入可能是"已有产物图" | 支持两种输入：multipart 上传 / 服务端文件相对路径引用 |

## 二、接口设计

```
POST  /api/v1/renders          提交生成任务
POST  /api/v1/edits            提交局部编辑任务
GET   /api/v1/jobs/{job_id}    查询任务状态与结果（生成/编辑统一作业模型）
GET   /files/{path}            产物静态访问（限定 output 根内，防穿越）
GET   /healthz                 健康检查
```

### ① 提交生成 `POST /api/v1/renders`（multipart/form-data）

```
file:       图纸文件（.dwg/.dxf/.png/.jpg，必填）
desc:       生图风格描述（可选，"现代简约风，暖色灯光"）
edit:       局部编辑指令（可重复字段，串行执行，可选）
force:      验证未过是否继续（默认 true）
model / gpt_model / n / max_iters   可选参数
```

→ `202 {"job_id": "r-20260915-181200-ab12", "status": "pending"}`

### ② 提交编辑 `POST /api/v1/edits`（二选一）

```
方式A（multipart）：image 文件 + instruction + mask(可选文件) + no_compile
方式B（JSON）：    {"image_path": "2026-09-15/180410/final.png",
                    "instruction": "...",
                    "mask_path": "..."}     ← 相对 output 根，防穿越校验
```

→ `202 {"job_id": "e-..."}`

### ③ 查询 `GET /api/v1/jobs/{job_id}`

```json
{
  "job_id": "r-20260915-181200-ab12",
  "type": "render",
  "status": "succeeded",
  "stage": "done",
  "error": null,
  "result": {
    "final": "/files/2026-09-15/181200/final.png",
    "renders": ["/files/2026-09-15/181200/renders/gpt_render_...png"],
    "edited": "/files/2026-09-15/181200/edits/update_...png",
    "validation_passed": true,
    "out_dir": "2026-09-15/181200"
  }
}
```

**客户端流程**：`POST 拿 job_id → 每 2s GET 轮询 → succeeded 后拿 /files
URL 直接下载图`。

- `status`：pending | running | succeeded | failed
- `stage`（running 时）：vision | validation | generate | edit | done

## 三、异步作业模型

- **作业注册表**：进程内存 `dict[job_id → Job]`（状态/stage/结果/错误/创建
  时间）。产物本身已持久化在磁盘，注册表丢失可按目录重建（v1 接受重启丢
  任务列表，文档注明；job_id 同时写入运行目录 `job.json` 便于事后对账）。
- **执行**：提交后 `asyncio.create_task` 后台跑业务函数。
- **并发闸门**：全局 `Semaphore(2)`——识图/生图/编辑都打 OpenRouter，限流
  保护（各环节已有 429/5xx 自动重试兜底）。
- **进度上报**：`run_e2e()` 加可选参数 `progress_cb: Callable[[str], None]`
  （侵入极小），API 传入回调更新 `job.stage`。
- **超时**：作业级超时（默认 15 分钟）防止挂死占坑。

## 四、安全与边界

| 项 | 措施 |
|---|---|
| 路径穿越 | `/files/{path}` 与 `image_path` 引用一律 resolve 后校验必须在 output 根内，越界 403 |
| 上传限制 | 文件大小上限（图纸 50MB / 图片 20MB）+ 扩展名白名单 |
| 鉴权 | v1 内网默认免鉴权；预留 `X-API-Key`（Settings 开关），后续可加 |
| 并发写冲突 | 每作业独立运行目录（现有日期/时分秒机制天然保证） |

## 五、模块布局（API 层薄，业务零改动）

```
app/api/
├── render_edit.py     # 新增 router：/renders /edits /jobs /files（~200 行）
├── jobs.py            # 新增：Job 注册表 + 后台执行 + 信号量（~80 行）
└── app.py             # 改：挂载新 router（现有 /api/projects 不动）
scripts/run_api.py     # 启动入口复用，无需改
```

CLI 与 API 共享同一业务入口（`run_e2e` / `LocalEditAgent`），功能演进
单一来源。

## 六、分期交付

1. **Phase 1**（核心）：`POST /renders` + `GET /jobs/{id}` + `/files` +
   并发闸门 + progress_cb
2. **Phase 2**：`POST /edits`（两种输入方式）+ `GET /jobs` 列表
3. **Phase 3**（按需）：SSE 进度流（对齐现有 `/events` 模式）、作业持久化、
   API Key 鉴权

预估总量 ~300 行新增，不动任何现有业务代码。
