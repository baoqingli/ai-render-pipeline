# AI 渲染流水线 Agent 平台 — 系统设计文档

> 日期：2026-09-02
> 状态：设计定稿（已经用户逐节确认）
> 上游文档：[requirement.md](../../requirement.md)（需求源头）、
> [cad-to-render-pipeline-v2.md](../../cad-to-render-pipeline-v2.md)（可实施方案 V2）
> 本文档定义系统的**开发设计**：如何用 LangChain + LangGraph 把 V2 的模块方案实现为
> 生产级 AI agent 平台。实施计划（按 Phase 切分）由后续 writing-plans 产出。

---

## 目录

1. [目标与范围](#1-目标与范围)
2. [关键决策记录](#2-关键决策记录)
3. [总体架构](#3-总体架构)
4. [Pipeline State Schema](#4-pipeline-state-schema)
5. [主图设计](#5-主图设计)
6. [Agent 子图设计](#6-agent-子图设计)
7. [工具层规范](#7-工具层规范)
8. [RenderEngine 抽象与 ModelRegistry](#8-renderengine-抽象与-modelregistry)
9. [API 契约与运行拓扑](#9-api-契约与运行拓扑)
10. [错误处理与降级](#10-错误处理与降级)
11. [项目目录结构](#11-项目目录结构)
12. [可观测性与测试](#12-可观测性与测试)
13. [部署](#13-部署)
14. [Phase 映射](#14-phase-映射)
15. [风险与开放问题](#15-风险与开放问题)

---

## 1. 目标与范围

**目标**：将 V2 定义的"CAD → 白模 → AI 渲染"全自动流水线实现为一个
**LangGraph 编排的 AI agent 系统**：

- 主流程用 LangGraph StateGraph 编排，LangChain 工具调用全部第三方能力
- 需要推理/异常处理的环节由专职 agent 子图承担（多 agent）
- 生产级：持久化、崩溃恢复、降级兜底、可观测、可测试、成本计量

**范围**：后端 agent 系统 + API 契约。前端 UI 不在本文档范围（仅定义 API）。

**继承的硬约束**（来自 requirement.md，本文档不得违反）：

- 交互模型：输入层（CAD + 文字描述 + 参考图）→ 全自动流水线 → 结果层反馈闭环；
  **中途无任何用户确认环节**
- 结构由 CAD/白模保证，AI 渲染只做风格化；布局优先级 CAD 布置 > AI 补全（白模层）
- 解析失败用默认值兜底，流程不中断
- 增量重生成：改描述/换参考图只重跑渲染阶段

---

## 2. 关键决策记录

| # | 决策 | 备选与取舍 |
| --- | --- | --- |
| D1 | **LangGraph 统一编排，不引入 Celery**。重工具（Blender/ComfyUI/API）为异步节点，内部提交专用进程池排队；工具靠缓存键幂等，重放安全 | 备选 LangGraph 大脑 + Celery 手脚：调度能力强但两套状态系统一致性复杂、调试链路长。当前规模一套状态系统更优；任务量上来后再演进 |
| D2 | **固定 DAG 主图 + agent 节点**。主干确定性（同输入同路径、可重放、可精确测试），agent 只在低置信诊断、布局补全、QA 灰区三处介入 | 备选 supervisor 动态路由多 agent：最灵活但控制流不确定、难回归测试、LLM 成本高，与"全自动可重放"的定位冲突 |
| D3 | **完整系统开发文档**（本文档），writing-plans 按 Phase 切实施计划 | 备选只写框架层或只写 Phase 2：会在开发中频繁回跳 V2 文档或后期补文档 |
| D4 | LLM 运行时（Phase 1 起）：**智谱 GLM API（OpenAI 兼容端点 `https://open.bigmodel.cn/api/paas/v4`，模型 `glm-5.3`）**，LangChain `ChatOpenAI(base_url=..., api_key=Settings.llm_api_key)` 接入；四个 agent 共用，可按 agent 独立换模型。**自部署 Qwen（vLLM）降级为切换项**（成本敏感或数据不出内网需求出现时切换，接口不变） | GPU 只需服务 ComfyUI 渲染，免去 vLLM 显存与运维；密钥只进 `.env`（gitignored），仓库内仅占位符 |
| D5 | 观测选型：**Langfuse（自部署）+ OpenTelemetry/Jaeger** | LangSmith 为商业 SaaS，与开源优先不符 |
| D6 | 反馈闭环 = **新 iteration（新 thread）**，上游阶段靠缓存键瞬时命中 | 备选在同 thread 里回跳：状态复杂、缓存复用不直观 |

---

## 3. 总体架构

```
                        ┌────────────────────────────────────┐
用户 ──HTTP──▶ FastAPI ──┤ 创建项目 / 进度(SSE) / 结果 / 重生成 │
                        └───────────────┬────────────────────┘
                                        │ 启动信号（Valkey 队列）
                                        ▼
                ┌───────────────────────────────────────┐
                │  graph-runner（独立服务）               │
                │  LangGraph 主图（AsyncStateGraph）      │
                │  持久化: AsyncPostgresSaver             │
                │  thread_id = {project_id}:{iteration}  │
                └───────┬───────────────────────────────┘
                        │ 异步工具调用（全部 LangChain tool）
        ┌───────────────┼───────────────────┬─────────────────┐
        ▼               ▼                   ▼                 ▼
   CAD 工具组       Blender 进程池       ComfyUI 池(按卡)   直连 API 引擎
   ezdxf/ODA        (CPU, 信号量限流)    (GPU, 每卡一实例)  (令牌桶限流)
        │               │                   │                 │
        └───────────────┴───────┬───────────┴─────────────────┘
                                ▼
              PostgreSQL(元数据+检查点) + MinIO(全部产物)
              LLM: GLM API(OpenAI 兼容, Phase 1) / vLLM Qwen(切换项) → 4 个 agent 共用
              Langfuse(LLM 轨迹) + Jaeger(工具 span)
```

组件职责：

| 组件 | 职责 |
| --- | --- |
| **FastAPI (api)** | 无状态业务接口：上传、进度 SSE、结果、重生成、反馈；投递启动信号 |
| **graph-runner** | 独立服务，消费启动信号执行主图；重启时扫描 in-flight 检查点自动恢复 |
| **主图** | 固定 DAG 编排：解析 → 结构化 → 白模 → 渲染 → QA；含条件边与并行分支 |
| **agent 子图 ×4** | style / cad_diagnosis / layout / render_qa，LLM 推理只发生在这里 |
| **工具层** | 全部第三方能力封装为 LangChain tool，统一信封/幂等/池限流 |
| **进程池** | blender 池（CPU 信号量）、comfy 池（每 GPU 一客户端）、api 池（令牌桶） |
| **存储** | PG：元数据 + 检查点 + ModelRegistry + 计量汇总；MinIO：全部产物（DXF/JSON/blend/控制图/渲染图）；Valkey：启动队列 + pub/sub + 可选热缓存 |

三条贯穿性设计原则：

1. **重放安全**：所有工具幂等（缓存键 = hash(上游 key + 参数 + 工具版本)），
   崩溃恢复从检查点重放时缓存命中即返回，不重复消耗 GPU/API
2. **产物不入 State**：graph state 只放决策数据与对象键，产物本体在 MinIO，
   避免检查点膨胀
3. **agent 收口**：LLM 调用只存在于四个 agent 子图内部；主图节点是纯函数 +
   工具调用，保证主干可精确测试

---

## 4. Pipeline State Schema

```python
import operator
from typing import Annotated, TypedDict

class PipelineState(TypedDict):
    # ── 输入 ──
    project_id: str
    iteration: int                        # 反馈轮次，thread_id 组成部分
    cad_file_key: str                     # MinIO 对象键
    text_description: str | None
    reference_image_keys: Annotated[list[str], operator.add]

    # ── CAD 分支产物 ──
    dxf_key: str | None                   # DWG 转换后的统一 DXF 键
    cad_report: CadReport                 # 图层统计/块清单/代理实体/文字标注/置信度
    parse_strategy: ParseStrategy | None  # 诊断 agent 产出（墙图层/门窗块模式/单位/层高来源）
    scene_json: SceneJSON                 # V2 模块3 结构，含 furniture(source=cad|ai_supplement)
    scene_cache_key: str

    # ── 白模分支产物 ──
    views: list[ViewPlan]                 # 机位（含评分与相机参数）
    control_maps: dict[str, dict[str, str]]  # {view_id: {depth,lineart,normal,white}} → MinIO 键
    blend_cache_key: str

    # ── 风格分支产物 ──
    style_params: StyleParams | None      # 稳定序列化（字段排序后 hash 进缓存键）

    # ── 渲染 ──
    render_tasks: list[RenderTask]        # view × variant × model_id
    render_results: Annotated[list[RenderResult], operator.add]   # Map/Send 扇出并发汇聚
    qa_actions: Annotated[list[QaAction], operator.add]

    # ── 控制与诊断 ──
    confidence: dict[str, float]          # parse / layout / render 各环节置信度
    fallback_log: Annotated[list[FallbackEvent], operator.add]    # 默认值兜底记录（内部诊断）
    errors: Annotated[list[NodeError], operator.add]
    stage: PipelineStage                  # 进度推送用
```

Schema 约定：

- 领域模型（`CadReport`/`SceneJSON`/`StyleParams`/`ViewPlan`/`RenderTask`/`RenderResult`
  等）全部为 `app/models/` 下的 Pydantic 模型，与 MinIO 中的 JSON 产物一一对应
- `StyleParams` 必须实现 `stable_hash()`：字段排序序列化后哈希——保证同一描述
  提取结果相同 hash，渲染缓存才能命中（V2 模块 8 的既有要求）
- `SceneJSON` 即 V2 模块 3 的结构化中间层，字段定义以 V2 为准，本设计不重复定义

---

## 5. 主图设计

### 5.1 节点与边

```
ingest ──▶ convert_dwg ──▶ inspect_cad ──[低置信/proxy?]──是──▶ cad_diagnosis_agent ─┐
 (校验/入库/      (ODA, DXF      (ezdxf 图层/块/                          │解析策略   │
  算缓存键)        则跳过)        代理实体/标注)    否                     ▼          │
                                                      └──────────▶ parse_scene ◀──────┘
                                                                     │
                                                          [家具为空且有房间?]
                                                                     │是
                                                                     ▼
                                                              layout_agent
                                                                     │
    ┌────────────────── join ───────────────────┐                   │
    │                                           ▼                   ▼
extract_style  ═══并行══▶  build_white_model ◀───────(scene_json 汇聚)
(风格 agent)                (Blender 池: 白模+机位+控制图)
    │                                           │
    └────────────────── join ───────────────────┘
                        ▼
              plan_render_tasks（视角 × 变体 × 模型，确定性）
                        ▼
              render ──Map/Send 扇出──▶ RenderEngine 路由
                        ▼               (ComfyEngine | DirectAPIEngine)
              qa_filter（确定性指标 → 灰区给 qa_agent，重试 ≤1）
                        ▼
                    finalize（汇总 / 计量 / 通知 / 更新项目状态）
```

### 5.2 节点清单

| 节点 | 类型 | 输入 → 输出 | 说明 |
| --- | --- | --- | --- |
| `ingest` | 工具 | 文件 → 对象键 + 缓存键 | 校验格式/大小，上传 MinIO，计算内容哈希 |
| `convert_dwg` | 工具 | DWG → DXF | ODA 子进程；输入已是 DXF 则条件边跳过 |
| `inspect_cad` | 工具 | DXF → `CadReport` | ezdxf：图层统计、块清单、**proxy 实体检测**、层高文字标注、单位推断、置信度评分 |
| `cad_diagnosis_agent` | agent | `CadReport` → `ParseStrategy` | 见 §6.2；仅低置信/proxy 时进入 |
| `parse_scene` | 工具 | DXF + 策略 → `SceneJSON` | 确定性解析（清洗/墙体/门窗/房间/布置图元），低置信部分按策略默认值兜底并记 fallback_log |
| `layout_agent` | agent | rooms → furniture | 见 §6.3；仅家具为空且有房间时进入 |
| `build_white_model` | 工具 | `SceneJSON` → views + 控制图 | Blender 池：建模、机位规划（V2 模块 5 规则）、渲 Depth/Lineart/Normal/白模 |
| `extract_style` | agent | 文字描述 → `StyleParams` | 见 §6.1 |
| `plan_render_tasks` | 纯函数 | views × 风格 × registry → `render_tasks` | 展开 视角 × 变体 × 启用模型 |
| `render` | 工具(扇出) | 每个 `RenderTask` → `RenderResult` | LangGraph Map/Send 按任务并行；按 model_id 路由引擎；单任务失败不阻塞批次 |
| `qa_filter` | 工具+agent | `RenderResult` 批 → 过滤决策 | 确定性指标（黑图/清晰度/CLIP）先分流；灰区给 qa_agent（§6.4）；重试上限 1 |
| `finalize` | 纯函数 | 全部 → 汇总 | 结果清单、成本汇总入计量表、SSE 完成事件、更新项目状态 |

### 5.3 条件边与并行

- **条件边**：`convert_dwg` 跳过（已是 DXF）；`inspect_cad → cad_diagnosis`
  （置信度 < 阈值或 proxy 实体）；`parse_scene → layout_agent`（furniture 为空且有房间）
- **并行**：`extract_style` 与 CAD 分支无数据依赖，ingest 后即扇出并行，
  在 `plan_render_tasks` 前汇合
- **Map/Send**：`render` 节点对 `render_tasks` 扇出，`render_results` 用 append
  reducer 汇聚
- **无用户中断点**：全图不使用 interrupt；进度通过 stream 事件外发

### 5.4 持久化与恢复

- `AsyncPostgresSaver` 检查点，`thread_id = f"{project_id}:{iteration}"`
- graph-runner 重启 → 扫描 `stage != finalized` 的线程 → 从最后检查点恢复执行
- 重放的节点工具调用先查缓存键，命中即秒回（成本与 GPU 时间不重复消耗）

---

## 6. Agent 子图设计

统一骨架：每个 agent 固定五要素——**输入 schema / 工具白名单 / 模型配置（可独立
换模型）/ 最大迭代数 / 输出 schema**。全部位于 `app/agents/<name>/`，含
`graph.py`（子图）、`prompts.py`、`schemas.py`。输出必须过 Pydantic 校验，
校验失败按各 agent 的兜底规则处理，绝不向上抛裸文本。

### 6.1 style_agent（风格提取）

- **形态**：两步结构化，无循环。LLM `with_structured_output(StyleParamsSchema)`
  提取 → 确定性代码校验补默认（枚举外取 default + fallback_log）
- **模型**：Qwen-7B 级（vLLM 端点）
- **输入**：`text_description` + 参考图有无（参考图本身不进 LLM，进 IP-Adapter）
- **输出**：`StyleParams`（V2 模块 6 的风格参数体系：style/floor/wall/light/budget…）
- **兜底**：描述为空或不可识别 → 全默认风格（现代简约）+ fallback_log
- **关键约束**：输出实现 `stable_hash()`（排序序列化），保证同描述同 hash

### 6.2 cad_diagnosis_agent（CAD 诊断）

- **形态**：受限 ReAct，≤5 轮
- **触发**：`inspect_cad` 置信度 < 阈值 或 检测到天正 proxy 实体
- **工具白名单（全部只读探测）**：`list_layers` / `layer_stats(layer)` /
  `sample_entities(layer, n)` / `detect_blocks` / `search_text_annotations(pattern)`
- **任务**：判定解析策略 → `ParseStrategy{wall_layers[], door_block_patterns[],
  window_block_patterns[], unit, floor_height_source}`
- **退出条件**：产出合法 `ParseStrategy`；或 5 轮后判定 unparseable → 输出
  **降级策略**（全默认值：墙厚 200 / 层高 2800 / 无洞口 + 标记），主流程继续
- **约束**：绝不写用户文件；工具仅只读；天正 proxy 判定不可解析时不猜测几何

### 6.3 layout_agent（布局补全）

- **形态**：结构化生成 + 校验循环，≤3 轮
- **输入**：rooms（polygon + 功能标注；无标注用面积/位置启发式分类，分类器为
  独立小 LLM 调用或规则）
- **循环**：生成 `FurniturePlan` → `validate_layout` 确定性规则校验
  （体块不重叠/不穿墙/不堵门窗洞口/动线间距）→ 有违规则带违规清单重新生成
- **兜底**：3 轮仍不过 → 缩减家具集（只留床/桌等核心件）或空布置 + fallback_log
- **关键约束**（V2 已定）：布局只依赖房间几何 + 功能类型，**不依赖风格参数**；
  产出 furniture 带 `source="ai_supplement"`

### 6.4 render_qa_agent（渲染质检）

- **形态**：单次结构化判定，仅**灰区案例**触发（确定性指标 pass/fail 之间）
- **输入**：渲染图 + 对应控制图 + 指标（CLIP score / 清晰度 / 主体区域）
- **决策**：`pass` / `retry(control_weight+0.1)` / `flag`（交付但标记）
- **约束**：重试上限 1 次（成本控制）；重试任务带新参数 → 新缓存键，独立可复算
- **MVP 简化**：Phase 2 用纯规则版（指标阈值直接分档），agent 版 Phase 3 上线，
  接口不变

---

## 7. 工具层规范

每个第三方能力封装为一个 LangChain tool，遵守六条规范：

```python
@tool
async def build_white_model(scene_key: str, params: WhiteModelParams)
    -> ToolResult[WhiteModelOutputs]:
    """统一信封：
    ok / data / error{code, message, retryable}
    cache{key, hit} / metrics{latency_ms, cost_usd, pool}
    """
```

| # | 规范 | 实现要点 |
| --- | --- | --- |
| 1 | 类型化边界 | 入参/出参 Pydantic；`ToolResult` 泛型信封 |
| 2 | 幂等 + 缓存前置 | 缓存键 = hash(上游 key + 参数 + 工具版本号)；命中直接返回信封（`cache.hit=true`） |
| 3 | 资源池标签 | 工具声明 pool：`blender` / `comfy` / `image_api` / `cpu`；infra 调度器按池施加信号量或令牌桶 |
| 4 | 超时 + 分类重试 | 声明 timeout；仅 `retryable=true` 的错误指数退避重试（默认 3 次） |
| 5 | 错误编码化 | 见下方错误码表；条件边与降级逻辑按码分支 |
| 6 | 自动观测 | LangChain callbacks 统一上报 Langfuse（LLM/agent）与 OTel span（工具） |

**错误码表**（初版，随实现扩充）：

| code | retryable | 语义 |
| --- | --- | --- |
| `INPUT_INVALID` | 否 | 文件损坏/格式非法（DXF 解析器直接报错） |
| `PROXY_ENTITY` | 否 | 检测到天正等代理实体 |
| `PARSE_LOW_CONFIDENCE` | 否 | 置信度低于阈值（触发诊断 agent，不是失败） |
| `BLENDER_CRASH` / `BLENDER_TIMEOUT` | 是 | Blender 子进程异常 |
| `COMFY_TIMEOUT` / `COMFY_ERROR` | 是 | ComfyUI 执行失败 |
| `API_RATE_LIMIT` | 是 | 外部 API 限流（退避 + 令牌桶自适应） |
| `API_QUOTA` | 否 | 配额耗尽（切换引擎或标记 missing） |
| `STORAGE_ERROR` | 是 | MinIO/PG 瞬时故障 |

**工具清单**：

| 工具 | 第三方 | 池 |
| --- | --- | --- |
| `convert_dwg` | ODA File Converter（子进程） | cpu |
| `inspect_dxf` / `parse_scene` | ezdxf + shapely + networkx | cpu |
| `build_white_model` | Blender headless（子进程，执行 `blender/generate_scene.py`） | blender |
| `run_comfy_workflow` | ComfyUI API | comfy |
| `call_image_api` | Gemini / OpenAI / 火山引擎适配器 | image_api |
| `clip_score` / `image_metrics` | open_clip（本地） | cpu |
| `validate_layout` | shapely（本地规则） | cpu |
| `storage_put` / `storage_get` | MinIO | cpu |
| `cache_get` / `cache_put` | PG（+可选 Valkey 热层） | cpu |

---

## 8. RenderEngine 抽象与 ModelRegistry

```python
class RenderEngine(Protocol):
    async def submit(self, task: RenderTask) -> RenderResult: ...

class ComfyEngine(RenderEngine):     # 本地：加载 workflows/{model_id}.json 模板，
    ...                              # 注入控制图/prompt/seed，调 ComfyUI API
class DirectAPIEngine(RenderEngine): # 远程：按 provider 适配器调 Gemini/OpenAI/火山，
    ...                              # 白模图/线稿作为图生图/编辑参考输入
```

**ModelRegistry**（PG 表，热更新）：

| 字段 | 说明 |
| --- | --- |
| `model_id` | 如 `sdxl-control-v1` / `nano-banana-2` / `seedream-5-pro` |
| `engine` | `comfy` \| `direct_api` |
| `workflow_template` / `provider` | ComfyUI 模板文件名 或 API provider+模型名 |
| `prompt_variant` | prompt 模板变体（如 API 模型无长负面词） |
| `max_concurrency` | 引擎内并发上限 |
| `price_per_image` | 计量用（本地引擎为 0） |
| `enabled` | 上下架开关 |

- **Prompt 拼装是确定性代码**：`StyleParams` → 模板渲染 → 正/负面 prompt；
  LLM 不写最终 prompt（V2 既有决策）
- **路由零耦合**：`plan_render_tasks` 产出带 `model_id` 的任务，`render` 节点查
  registry 分发；**新模型上线 = registry 加一行 + 模板/适配器，主图零改动**
- **计量内建**：每次 `ToolResult.metrics.cost` 累计，`finalize` 写入项目计量表

---

## 9. API 契约与运行拓扑

### 9.1 REST 契约

```
POST /api/projects                    # multipart: cad_file + description? + ref_images[]
                                       # → 202 {project_id, iteration: 1}
GET  /api/projects/{id}                # 项目状态 + 结果索引
GET  /api/projects/{id}/events         # SSE 进度流（stage 级事件）
GET  /api/projects/{id}/results        # 结果列表（图 URL + 指标 + model_id + 收藏态）
POST /api/projects/{id}/regenerate     # {description?, ref_images?} → 202 {iteration: n+1}
POST /api/results/{rid}/feedback       # 收藏 / 打分
GET  /api/results/{rid}/image          # 图片（302 到 MinIO 预签名 URL）
# 内部（仅内网鉴权）
GET  /api/admin/diagnosis/{id}         # 置信度 / fallback_log / 检查点状态
POST /api/admin/registry               # ModelRegistry 管理
```

### 9.2 运行拓扑

```
FastAPI ──发布 start 信号──▶ Valkey 队列 ──▶ graph-runner(≥1 副本)
graph-runner ──进度事件──▶ Valkey pub/sub ──▶ FastAPI SSE 下发
graph-runner 重启 ──▶ 扫描 stage != finalized 的线程 ──▶ 检查点恢复执行
```

- API 无状态可多副本；发版不杀运行中的图任务
- SSE 事件模型：`{project_id, iteration, stage, status, detail, ts}`；
  stage 枚举与 `PipelineStage` 一致
- 反馈闭环（D6）：`regenerate` 创建新 iteration = 新 thread；上游工具缓存键
  未变 → 命中秒回 → 实际只执行渲染及以后阶段。**增量重生成不需要特殊代码，
  是缓存键设计的自然结果**

---

## 10. 错误处理与降级

| 层 | 策略 |
| --- | --- |
| 工具层 | §7：timeout / 分类重试 / 错误码 |
| 节点层 | **能降级就降级，不中断**：低置信 → 诊断 agent → 默认值策略；布局校验不过 → 缩减家具；单张渲染失败 → 标记 missing 不阻塞批次；某视角全部失败 → 该视角整体重试一轮 |
| 图层 | 不可恢复错误（`INPUT_INVALID` 等）→ `finalize` 产出失败报告（错误码 + 人话原因，如"图纸包含天正代理实体，请导出前分解"）。注意：这是**结果层反馈**，不违反"无中途确认" |
| 进程层 | runner 崩溃 → 重启 → 扫描 in-flight → 检查点恢复 → 工具幂等快速追平 |

总原则：**能出图就出图，出不了图给明确原因**。所有降级动作记 `fallback_log`，
供内部诊断视图（`/api/admin/diagnosis`）。

---

## 11. 项目目录结构

```
ai-render-pipeline/
├── app/
│   ├── api/                    # FastAPI：routes / sse / deps / 错误处理
│   ├── core/                   # config(pydantic-settings) / logging(structlog) / 错误码
│   ├── graph/                  # 主图
│   │   ├── state.py            # PipelineState + reducers
│   │   ├── pipeline.py         # 图构建（节点/条件边/Map-Send）
│   │   └── nodes/              # 各节点实现（调 tools / agents）
│   ├── agents/
│   │   ├── style/              # graph.py / prompts.py / schemas.py
│   │   ├── cad_diagnosis/
│   │   ├── layout/
│   │   └── render_qa/
│   ├── tools/                  # 工具实现（每第三方一模块）
│   │   ├── cad/                # convert_dwg / inspect / parse
│   │   ├── blender/            # build_white_model（子进程封装）
│   │   ├── comfy/              # run_comfy_workflow
│   │   ├── image_api/          # Gemini / OpenAI / 火山适配器
│   │   ├── vision/             # clip_score / image_metrics
│   │   ├── storage/            # MinIO
│   │   └── cache/              # 缓存键计算与读写
│   ├── engines/                # RenderEngine 接口 + 两实现 + registry
│   ├── models/                 # 领域 schema：SceneJSON / StyleParams / ViewPlan ...
│   ├── infra/                  # 进程池(信号量/令牌桶) / pg / valkey / minio / llm client
│   └── workers/                # graph-runner 独立入口
├── blender/                    # generate_scene.py / view_planner.py（Blender 进程内执行）
├── workflows/                  # ComfyUI 工作流 JSON 模板（按 model_id 组织）
├── tests/
│   ├── unit/                   # 工具单元（fixtures 回放）
│   ├── graph/                  # 图测试（fake tools，状态转移快照断言）
│   ├── agents/                 # golden sets 评测
│   └── e2e/                    # 标准户型全链（GPU tag 可选）
├── fixtures/                   # 样例 DXF 语料（含天正、乱图层反例）
├── deploy/                     # docker-compose（开发）/ k8s（生产）
├── docs/
└── pyproject.toml              # uv 管理；ruff + mypy + pytest
```

---

## 12. 可观测性与测试

### 12.1 可观测性

| 维度 | 工具 | 接入点 |
| --- | --- | --- |
| LLM/agent 轨迹 | Langfuse（自部署） | LangChain callbacks |
| 工具/节点 span | OpenTelemetry → Jaeger | 工具信封统一埋点 |
| 业务指标 | PG 计量汇总表 | finalize 写入：时长 / 缓存命中率 / 兜底次数 / 成本 |
| 日志 | structlog | `project_id:iteration` 贯穿全链 |

### 12.2 测试策略（四层）

1. **单元**：工具函数级；Blender/ComfyUI 用小样本 fixtures 与录制回放，
   CI 无 GPU 也可跑
2. **图测试**：fake tools 注入主图，**断言状态转移快照**——确定性 DAG 的红利：
   条件边路径（低置信→诊断、无家具→补全、DWG→转换）可精确覆盖与回归
3. **agent 评测**：golden sets——风格提取（N 组描述 → 期望参数）、CAD 诊断
   （N 份图纸报告 → 期望策略）、布局（产出必须全过 validate_layout）
4. **e2e**：一个标准户型 DXF 全链，断言产物数量/结构/计量记录（GPU tag，可选）

---

## 13. 部署

**开发（docker-compose）**：api、graph-runner、postgres、valkey、minio、
vllm(Qwen)、comfyui(GPU)、blender-runner、langfuse、jaeger。

**生产（k8s）**：

- GPU 节点池：vllm + comfyui（按卡调度）
- CPU 节点：api（多副本无状态）、graph-runner（≥1，可横向扩）、blender-runner
  （按 CPU 副本扩）
- ModelRegistry / 配置存 PG，热更新无需发版
- 镜像注意：ODA 二进制需手工放置（EULA 限制不能包管理器安装）；
  Blender 镜像含 headless 依赖

---

## 14. Phase 映射

| Phase（V2 口径） | 本设计交付 | 说明 |
| --- | --- | --- |
| **1**：验证 AI 渲染（3-5 天） | `engines/`（ComfyEngine + DirectAPIEngine + registry）+ style_agent + `workflows/` 模板 + **mini 验证图**（手工控制图 → 多引擎 A/B） | 主图暂不建；但 state/工具规范/引擎接口**按本设计骨架落位**，非抛弃式代码 |
| **2**：全自动主链（2 周） | 主图全链 + CAD 工具组 + Blender 工具 + 缓存 + 增量重生成 + SSE；QA 用纯规则版 | 一个标准演示户型跑通（V2 工期口径） |
| **3**：解析强化（1-2 月） | cad_diagnosis_agent 完整化 + layout_agent 上线 + render_qa_agent 灰区版 + fixtures 语料扩充 | 自动解析准确率 = 产品生命线（requirement 交互模型结论） |
| **4**：产品化 | 计量账单 / 看板 / 多租户 / registry 管理界面 | 观测与计量数据 Phase 1 起就在积累 |

---

## 15. 风险与开放问题

| # | 风险/问题 | 对策 |
| --- | --- | --- |
| R1 | 渲染质量上限需实证（V2 可行性结论） | Phase 1 首位任务，多引擎 A/B |
| R2 | LangGraph 长时节点（渲染分钟级）内的检查点粒度——节点内崩溃恢复到节点起点 | 工具幂等 + 缓存前置：重放节点成本≈0；render 节点按任务粒度写产物键，重放只补缺失任务 |
| R3 | vLLM 与 ComfyUI 争抢同卡显存 | 部署上分卡或分时；小 Qwen(7B) 占用可控，生产建议 Qwen 独占一卡 |
| R4 | 天正图纸不可解析的占比未知 | Phase 2 起统计 fixtures 与真实样本的 proxy 检出率，决定是否做"导出引导"产品化 |
| R5 | Map/Send 扇出的并发写与检查点体积 | render_results 只存键与指标（图片本体在 MinIO），已在 schema 约束 |
| R6 | API 模型价格波动 | registry 价格字段热更新；finalize 计量对账 |

**开放问题**（实施期决定，不阻塞设计）：

- O1：SSE 事件粒度（stage 级 vs 节点级）——Phase 2 联调时定
- O2：Valkey 热缓存层是否启用（先只用 PG，命中率低再加）
- O3：qa_agent 的灰区阈值初值——Phase 1 A/B 数据定标
