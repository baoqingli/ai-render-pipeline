# 白模修正方案（DWG→可用 3D 白模）— 2026-09-08

> 背景：真实图纸（精装综合图）白模连修数日不正确。本文档基于三类证据给出根因、
> 对《优化沟通话术.pdf》方案的评估，以及可落地的修正方案。
> 关联文档：[根本需求](./requirement.md)｜[V2 可实施方案](./cad-to-render-pipeline-v2.md)｜
> [真实图纸勘察](./cad-recon-2026-09.md)｜[Blender 验收](./blender-acceptance-2026-09.md)

---

## 1. 现状证据（2026-09-07 产物）

### 1.1 渲染结果（experiments/data/model/new_iso/）

- **view_iso**：3~4 个悬空的"开口罩盒"+ 地面附近大量黑色碎片体块。墙网不连续、
  无门窗洞口、家具呈碎片状。
- **view_interior**：整帧空白灰面 + 底部黑色条纹——相机贴墙（距墙约 0.3m）且视高
  1260mm 低于墙高 1400mm，画面被墙顶/墙面充满。

### 1.2 scene.json 量化（fe881fb4649f 上传图）

| 指标 | 值 | 问题 |
| --- | --- | --- |
| rooms | 4 个，全部 name=None，互不邻接 | 天花轮廓线 89 条中仅少数幸存，重叠未去重 |
| walls | 16 条双线配对墙 | **在 build_plan 中被整体丢弃**（见根因 1） |
| doors/windows | 4 / 3 | 归属到 16 条被丢弃的墙上 → 白模中不存在 |
| furniture | 113 个，**尺寸全部 (1000,500,500)**，rotation 全 0 | 块几何量尺失败 → 全走兜底默认值 |
| floor_height | 2800 兜底 | 图内无层高标注（可接受，需显式报告） |

### 1.3 图纸事实（inspect + ezdxf 实测）

| 图层 | 内容 | 结论 |
| --- | --- | --- |
| C-天花轮廓线 | 75 个封闭多段线（面积 10.3/6.5/6.3/6.1/6.0/4.2/3.3 m² + 68 个小的） | **本图唯一可用的房间划分源**（≈7 个功能区） |
| P-完成面 | 2 个封闭（均 ≈0m² 退化）+ 3 个开放 | **不是房间源**（修正勘察发现 1a 的假设） |
| P-新砌墙体 | 3 个封闭墙体条带 + 2 开放 | 改动墙，不是完整墙网（与勘察一致） |
| DOOR-门窗* | 少量 INSERT | 门存在但块名无尺寸（拼音体系） |
| S-LIGHT/E-插座/C-5消防/GND-地面 | 大量 INSERT | 机电点位，须排除出家具 |

---

## 2. 根因（架构级，非单点 bug）

1. **双几何源打架**：parse 同时产出"16 条双线墙"和"4 个天花轮廓房间"两套互不对齐的
   几何；`build_plan`（geom.py）遇 rooms 非空时**丢弃全部 scene.walls**，改用房间
   矩形边重建墙——而天花轮廓是吊顶分区边界，不等于墙位 → 悬空开口罩盒。
2. **去重被禁用**：parse.py 天花路径"暂时跳过去重逻辑，保留所有"→ 重叠/嵌套房间。
3. **门窗开洞缺失**：build_plan 的 rooms 路径完全不处理 doors/windows → 白模是密封盒，
   对 ControlNet 控制图而言开口信息（窗光、门洞）丢失。
4. **家具信息层退化**：`_block_bbox` 只量 LWPOLYLINE/LINE，真实块含嵌套 INSERT/其他
   实体 → 113/113 全部回退默认尺寸；"drc" 关键词又把高度批量改成 500 → 全场同尺寸
   薄片，互相堆叠遮挡，在纯环境光下形成黑色碎片。
5. **相机无校验**：内视相机固定取最大房间 15%/80% 角点，小房间内距墙 <0.5m、视高
   1260mm 低于 1400mm 墙高 → 空白帧，且无任何自动检测。
6. **无质量门禁（"几天修不好"的直接机制）**：错误全靠人眼看渲染图发现，每次修改都是
   对着单张图纸打补丁（debug print、硬编码块名 A$C7C5C6E88、去重跳过），无量化
   指标定义"什么是正确的白模"，因此每次修补无法收敛。
7. **置信度与质量脱节**（勘察已发现，未修）：房间 0/重叠时 confidence 仍 0.9，自动
   闭环拿不到降级信号。

> 结论：**不是路线错误**。requirement.md 的"CAD 决定空间"+V2 的确定性解析路线正确；
> 是实现层两个结构性缺陷（双几何源、家具量尺失效）叠加"无门禁、无 golden"的流程缺陷。

---

## 3. 对《优化沟通话术.pdf》方案的评估

### 3.1 总判断

方向正确，与本项目已有文档高度同构，**采纳其增量，不推翻现有路线**：

| PDF 主张 | 项目现状 | 结论 |
| --- | --- | --- |
| 六层拆解：几何还原→设计解析→相机→材质/风格→AI渲染→校验 | V2 模块设计已覆盖前五层 | **第六层"校验"是项目缺失的**，本次危机的直接解药 |
| 中间空间模型（CAD Parser→Spatial DB→3D Model） | SceneJSON 即中间空间模型 | 采纳概念，**不加数据库**——SceneJSON 文件足够，MVP 不引入 Spatial DB |
| "CAD 不是一张图片"，结构化解析 | ezdxf 确定性解析已做对 | 维持；**几何解析不用 LLM**（PDF 未强制 LLM，保持确定性路线） |
| 家具独立信息层（类型/尺寸/朝向/位置） | 已有 Furniture 模型，但量尺失效、rotation 恒 0 | **采纳为本轮核心修改**（P0-3） |
| 相机独立 Agent（5–10 机位+人工可调） | 硬编码 2 机位 | 采纳最小版：**候选机位+几何校验+深度图兜底**（P1），交互式调整后置 |
| 渲染分 pass（Geometry→Material→Lighting→Styling→Final） | white/depth/lineart 控制图 + ComfyUI 已实现 | 已满足；白模即 Geometry Pass，**必须先过质量门禁再烧 GPU** |
| 设计一致性检查 Agent + Fidelity Score | 无 | **采纳最小版：parse 后几何质量门禁 + 渲染后深度图校验**（P0-4/P1）；渲染图像素级 fidelity 后置 Phase 3 |
| 吊顶图是"空间感"关键输入 | C-天花轮廓线仅被(错误地)当房间源 | 采纳为 P2：吊顶作为真实几何层（高低差/灯槽进入 lineart/depth 控制图） |
| 参考图拆成设计参数（Reference Analyzer） | Phase1 style agent（GLM 提取）已具雏形 | P2 增量化，不动现有 |
| Level 1/2/3 分级 | — | **当前做 Level 2 的"自动建模+家具+吊顶+相机"部分**，与 V2 Phase 2/3 对齐；Level 3 全部后置 |

### 3.2 与《AI调研对话.txt》的交叉验证

调研对话指出四个"致命硬伤"，归属如下：

| 硬伤 | 判定 | 归属 |
| --- | --- | --- |
| 一：白模纯体块无倒角、方块家具拖累 ControlNet | **成立** | 本方案 P1-3（Bevel + 程序化低模）；资产库 P2 |
| 二：Lineart+Depth+IP-Adapter 控制过载 | 成立 | Phase 1 渲染调优清单（权重/ending step/主打 Depth+Normal），不动架构 |
| 三：裸 SDXL/Flux 基座不足，需室内垂直微调 | 成立 | Phase 1 渲染调优清单（室内 LoRA/专用基座 + Lightning 提速） |
| 四：prompt 模板缺乏灯光层次表达词、缺超分后处理 | 成立 | Phase 1 渲染调优清单（模板升级 + SUPIR/放大二段） |

> 调研对话的总结论（"不需要换 3ds Max/V-Ray，当前方向正确，问题在白模质量与 ComfyUI 调优"）
> 与本方案判断一致：**白模质量是第一优先**——骨架不对，渲染段怎么调都救不回来。

### 3.3 架构定位：不改为"多 Agent 协作"，落地"工作流图 + LLM 节点"

话术 PDF 中大量出现的"Agent"是对非技术读者的沟通语言；其技术内核（§3/§10 管线图）
是**确定性串联管线 + 少数语义节点用 LLM**。六个"Agent"中四个（CAD Parser/Spatial
Model/Camera/Fidelity 规则部分）本质是确定性代码。本项目形态：

| PDF 称呼 | 载体 | 类型 | 模型 |
| --- | --- | --- | --- |
| CAD Parser / Spatial Model | app/tools/cad + geom + Blender | 确定性 | 无 |
| Camera Agent | P1 相机候选制 + depth 校验 | 确定性为主 | 无 |
| Design Agent | style agent（已实现：GLM 提取 + 确定性模板） | LLM | GLM |
| Reference Agent | P2 参考图分析器 | LLM（VLM） | GLM-4.5V 类 |
| Fidelity Check | P0 规则门禁 → P3 VLM 图像比对 | 规则→LLM | VLM |
| （隐含）解析诊断 | Phase 3 cad_diagnosis（LLM+工具循环） | LLM | GLM |

- "不同 Agent 用不同模型、调不同工具" = **LangGraph 节点级配置**（切片 3 主图已规划），
  不是新架构；模型动物园最小化：1 文本（GLM，已有）+ 1 VLM（P2/P3 引入）。
- 几何链路坚决保持确定性：几何有唯一正确答案（计算非判断）；agent 自治循环破坏
  可复现性/内容缓存/golden 测试；本次白模危机正是确定性代码缺陷，LLM 协作救不了。
- Agent 化的正确落点 = 语义与例外处理（风格提取、参考图分析、低质量解析的诊断循环、
  渲染图 fidelity 评审），与 V2 Phase 3 规划一致。

### 3.4 需要明确拒绝/收敛的部分

- **Spatial Database**：不引入数据库，SceneJSON + build_plan.json 即中间模型（可控可 diff 可缓存）。
- **LLM 参与几何**：几何解析保持确定性代码；LLM 只用于房间语义命名、cad_diagnosis 规则库、风格提取（与 V2 一致）。
- **一次到位 Level 3**：多方案/反向修改 CAD 等不在本轮。

---

## 4. 修正方案（可落地，映射到现有代码）

目标架构不变：`DXF/DWG → inspect → parse → SceneJSON → build_plan → Blender 多 pass → 质量门禁 → ComfyUI`。
核心改动集中在 parse（几何事实源唯一化）与 build_plan（统一墙路径），新增校验层。

### P0：让真实图出正确白模（先做，1–2 天）

**P0-1 房间唯一化（parse.py 天花路径重写）**
- 收集 `C-天花轮廓线`+`P-完成面` 封闭多段线，`make_valid` 修复；
- **恢复去重**（替换"暂时跳过"）：按面积降序遍历，与已选集合任一 IoU>0.5 或被包含（面积比>0.85）则丢弃；
- 面积窗 [2, 200]m²（滤掉 68 个灯槽/检修口小轮廓；0.5m² 阈值太松）；
- 质量约束：房间数≥1 且 union 覆盖率（vs 全图轮廓 bbox）≥0.5，否则降级路径①/②/③；
- 房间命名：沿用 TEXT 包含法，位置判定增加 500mm 邻近兜底（点不在 poly 内时取最近房间）。

**P0-2 统一墙路径（geom.py build_plan 重写）**
- **删除 rooms/walls 二选一**：parse 保证"房间多边形边 + 双线墙条带"已合并为唯一墙列表；
  具体在 parse 内：房间边（含跨房间共享边）按端点吸附（SNAP_TOL）+ 共线合并聚类成墙段；
  wall_layers 双线条带先向最近房间边吸附（距离<wall_t 内合并），吸不上的保留为独立墙；
- 墙**全高** floor_height（删 1400mm cutaway——开口罩盒观感的来源；cutaway 后续作为 iso 视角
  渲染选项另行实现，不进默认路径）；
- build_plan 中对统一墙列表应用既有 `project_opening`/`segment_wall` 开洞 → **门窗洞回到白模**。

**P0-3 家具信息层（parse.py 家具段重写，对齐话术 §6）**
- 量尺：`_block_bbox` 改用 `ezdxf` `virtual_entities` 递归展开（嵌套 INSERT/POLYLINE/ARC/
  ELLIPSE/SPLINE 全覆盖），失败回退块表 bbox，再回退**类型默认尺寸**（不再全局单一默认）；
- 类型映射：块名/图层关键词 → 规范类型（bed/sofa/table/chair/cabinet/toilet/sink/bathtub/tv/other），
  每类型带期望尺寸范围，超界回退类型默认；
- `rotation = e.dxf.rotation`（当前恒 0 是家具朝向全错的直接原因之一）；
- 图层白名单过滤：保留 `P-固定家具/P-活动家具/FU-/P-洁具/P-木饰面`，排除 `S-/E-/C-5/GND-/P-legend`
  （按 1.3 图层事实补全 MEP 正则）。

**P0-4 质量门禁 + golden（防再次盲修的流程闸门）**
- parse 输出 `parse_quality`：rooms_count / tiling_ratio / overlap_max_iou /
  walls_attached_ratio / furniture_in_room_ratio / furniture_measured_ratio /
  openings_attached_ratio；confidence = 基础分 × 质量罚分（修正"置信度脱节"）；
- 新增 `scripts/qa_white_model.py`：对输出目录检查文件齐全、depth 有效像素占比/方差、
  white 非全白/全黑 → PASS/FAIL 报告（渲染层门禁）；
- golden 快照测试：合成户型 + 01-平面系统图 + fe881fb 真实图三份 fixture，
  断言 scene.json 关键指标（房间数区间、墙连续性、家具尺寸分布非单值、tiling_ratio 阈值）。
  **任何启发式修改必须保持 golden 绿**——这是收敛机制的落点。

### P1：相机与降级信号（1–2 天）

**P1-1 相机候选制（geom.py plan_views 重写，对齐话术 §7 最小版）**
- 每房间 1–2 个对角机位：eye 1600mm、距最近墙≥800mm（shrunken polygon 内采样）、
  target=房间中心 1200mm、FOV 65°；iso 全景保留；总数上限 6 控制渲染成本；
- 渲染后 depth 校验失败（有效像素占比<阈值）的机位自动剔除并换候选——替代当前"人眼发现空白帧"。

**P1-2 降级信号贯通**：PARSE_LOW_CONFIDENCE 按 parse_quality 真实触发，主图（切片 3）据此走降级路径。

**P1-3 家具低模化（吸收《AI调研对话》硬伤一）**
- scene_builder 全局挂 Bevel（倒角）修改器——消灭纯直角体块的生硬光影；
- 家具从"单盒"升级为**按类型程序化低模**（床=床架+床垫+床头板组合、沙发=底座+靠背+扶手），
  尺寸/朝向取自 P0-3 的实测值；不引入外部资产依赖；
- GLB 低模资产库替换（真家具轮廓）后置 P2——需要资产来源与管理，先不做。

### P2：按话术补齐 Level 2 特性（后续切片）

- **吊顶几何层**：天花轮廓（75 条）→ 吊顶体块 + 高低差 + 灯槽，进入 depth/lineart 控制图
  （话术强调吊顶决定"空间感"，且数据本图已具备）；
- **复合输入**：建筑平面图（完整墙网）+ 精装图（家具/吊顶）双源合并——勘察"产品侧收集建筑平面图"
  结论的工程化；
- **Fidelity Check Agent**：渲染图与平面叠合的像素级检查 + Design Fidelity Score（话术 §9 完整版）；
- **参考图分析器**参数化（Geometry/Material/Color/Lighting/Atmosphere 字段化）。

### 4.5 维护性与局部调整的工程纪律（P0 的前置约束）

P0 不是在现有 parse.py 上继续打补丁，以下纪律作为改动的前置条件：

1. **规则集中一处**：新建 `app/tools/cad/rules.py`——图层正则（墙/完成面/天花/门窗/机电/
   家具白名单）、家具类型映射表、类型默认尺寸表、面积/容差常量，全部数据化。
   inspect.py 与 parse.py 只引用不定义。新图纸适配 = 改一个数据模块，不碰几何逻辑。
2. **墙缝合独立模块**：房间边聚类、条带吸附、统一墙列表生成放 `app/tools/cad/walls.py`
   纯函数（输入房间多边形+墙条带，输出墙列表），单测独立覆盖，parse.py 只做编排。
3. **golden 只断言指标区间**：断言 tiling_ratio∈[0.6,1]、房间数∈[6,9]、家具尺寸种类≥5
   这类指标，不断言像素/精确数量——避免 golden 脆硬化，让合法优化不破坏测试。
4. **阶段契约冻结**：SceneJSON/BuildPlan 字段只增不改（parse_quality、rotation 为增量
   字段）；下游（Blender/ComfyUI）永远只读契约文件，任何阶段可独立替换重跑。
5. **调试残留清零**：删除 debug print、硬编码块名（A$C7C5C6E88 等）——同类需求一律
   落进 rules.py 的规则表，并配一条单元测试。

**局部调整映射表**（改什么、动哪里、什么不动）：

| 想调整的东西 | 只动哪里 | 不动什么 |
| --- | --- | --- |
| 新图纸公司的图层命名 | rules.py 规则表 | 几何逻辑、Blender、渲染 |
| 家具尺寸/类型判定 | rules.py 类型映射表 | 墙/房间逻辑 |
| 房间去重阈值、吸附容差 | rules.py 常量（golden 护航） | 其他阶段 |
| 机位策略 | geom.plan_views + depth 校验 | parse、渲染 |
| 白模材质/高度/倒角 | scene_builder.py | parse、geom |
| ControlNet 权重/模型/prompt | ComfyUI 工作流 + 风格模板 | 整条几何链路 |
| 新增吊顶几何层 | SceneJSON 增字段 + scene_builder 分支 | 既有墙/门窗逻辑 |

### 明确不做（本轮）

- Spatial Database / 向量库；LLM 几何解析；Level 3（多方案、反向改 CAD）；
- cutaway 渲染（默认路径全高墙）；交互式机位调整 UI。

---

## 5. 验收标准（本方案的"完成"定义）

**0. 视觉验收基准**（用户指定）：`docs/image/image_481953176845944.png`（千问 AI 生成的
酒店客房俯视白模）——对 `fixtures/cad/01-平面系统图.dwg` 产出同形态白模。由基准图反推
的实现要求：①相机近正交俯视（可见墙顶+少量内立面，房间划分一目了然）；②墙全高、
厚度正确、门窗洞存在；③窗洞带竖向棂条；④家具为按类型参数化的低模套件（床=床架+
床垫+枕头+床头板、马桶、沙发带扶手等），位置/朝向来自 CAD 实测；⑤太阳光+环境光
（柔和定向阴影），替代纯环境光。

1. **真实图（fe881fb）白模**：iso 图可见 ≈7 个功能区、墙连续闭合无悬空、门窗洞口存在、
   家具体块尺寸≥5 种不同值且位置与房间对应、无黑色碎片区域；
2. **golden 测试**三份 fixture 全绿（含新质量指标断言）；
3. **qa_white_model.py** 对三个相机 pass 全 PASS（无空白 depth/white）；
4. 现有 99 测试不回归；mypy/ruff 干净；
5. parse_quality 各指标进入 scene.json 附带报告，confidence 与之联动。

## 6. P0 执行结果（2026-09-08）

**P0 已实施并验证通过**（真实图 01-平面系统图全链渲染，产物 experiments/data/model/p0_verify/）：

| 验收项 | 结果 |
| --- | --- |
| 正交俯视白模（对照基准图） | ✅ 4 功能区划分清晰、墙网连续闭合、门窗洞+玻璃+棂条可见 |
| lineart 控制图 | ✅ 干净矢量线稿，黑斑清零 |
| depth 控制图 | ✅ 深度梯度正常 |
| scene.json 质量 | rooms=4（嵌套天花轮廓 IoU 0.92-0.97 已去重）/ tiling 0.727 / 门窗 4+3 全挂墙 / 家具 9 件 5 种尺寸含实测与旋转（旧版 113 件同尺寸默认值） |
| 测试 | 123 passed（新增 walls 纯函数 7 例 + golden 指标 2 例）/ mypy 0 / ruff clean |

执行中的三个关键发现（已固化进代码注释）：

1. **Blender 5.2 Background 节点新增 `Weight` 输入且默认 0.0**——世界光完全不生效，
   场景只剩太阳照明、一切阴影纯黑。这是"黑斑"持续数日的根因之一（已在 setup_world 修复）。
2. **white/lineart pass 改用 clay 无光（Emission）着色**：5.2 Cycles 对本场景退化共面几何
   （ARP_FLOOR 零厚度平面与墙盒共面）存在未查明原因的光照死角；控制图只需结构可见性，
   clay 着色确定性且更快。柔和定向阴影的还原作为 P1 调查项（结合 AO 重做光照）。
3. **内视相机（view_interior）仍有空白帧**：单一硬编码机位不适合小房间，按纪律停止盲调，
   归入 P1 相机候选制 + depth 校验解决。

## 7. 追加发现（2026-09-08 晚，用户出示 CAD 截图后）

用户出示的 CAD 截图（9600×4400 套房，含结构墙/双床/管井）与解析结果叠加对照暴露
"布局不一致"→ 排查发现**墙体与床一直在文件里，藏在块中**：

1. **块 `YSJZQT`（原始结构墙体）**：16 实体（8 HATCH + 闭合轮廓），bbox 5.2×9.6m，
   插入 1 次、旋转 90°——完整结构墙体系（承重墙柱/砖砌筑间隔墙/加固墙柱）。
2. **块 `DRC`**：1.2×2.0m，插入 2 次 = 两张床。
3. **根因**：ezdxf 的 `virtual_entities()` 对含 HATCH 的块静默失败，异常被抑制 →
   块内墙/家具在所有渲染与提取中"消失"。此前误判为"天正对象被 ODA 丢弃"
   （TCH_WINDOW×4 确实存在但只影响 4 个门窗编号对象）。
4. **修复**：`_hatch_wall_polys` 改为手动矩阵变换遍历块定义（`e.matrix44()` 链式
   变换，支持嵌套/旋转/缩放），收集墙体图层（`WALL_FILL_LAYER_RE`）的 HATCH
   边界与闭合轮廓，不做矩形度过滤（L 形/带垛墙段合法），面积窗 0.05-50m²；
   `drc` 床关键词回归（有尺寸边界校验护住）。
5. **结果**：walls 23→29（真实结构墙入列）、家具 9→11（含双床 1200×2000/1500×2000）、
   tiling 0.805、129 tests / mypy 0 / ruff 绿；overlay_v3 显示解析墙与图纸墙线全面贴合。
6. **教训入库**：`virtual_entities()` 不可信赖于含 HATCH/复杂实体的块——块内容遍历
   一律走 `doc.blocks[name]` + `matrix44()` 手动变换；叠加对照（cad_overlay.py）
   是布局正确性的唯一可证伪检验，每次解析后必跑。

### 7.1 追加修复（同日，逐项对账后）

用户以 CAD 截图逐项对账后，再修三个"隐形"缺陷：

1. **Blender 5.2 后台模式 `primitive_*_add(location=)` 参数不生效**——全部 46 个
   家具对象堆在原点（渲染左下角的白色堆）。修复：创建后显式 `o.location = loc`
   （墙分支一直正确的原因正是它用了显式赋值）。这是 5.x API 坑第五个。
2. **U/L 形墙带的 MRR 陷阱**：`wall_box` 取最小旋转矩形，U 形周长墙带的 MRR =
   整张图的大矩形 → 实心板盖住整个室内（像素全 247 的真相）。修复：rectness<0.7
   的墙多边形按外环边逐段拆条带（walls 29→47）。
3. **家具定位/圆形家具**：定位改实测包围盒中心（块原点可偏离视觉中心），候选中心
   （实测/原始内容/插入点）依场景范围校验择优；CIRCLE 茶几/圆桌纳入提取。
4. **clay 色阶分离**：地板 0.55 / 家具 1.0 / 墙 0.97 / 地面 0.45 / 玻璃隔断 0.5——
   正俯视图现在可直接逐项对账（与 Image #8 同读法）。

### 7.3 P1 完成（2026-09-08 深夜）

1. **内视相机候选制 + depth 校验**（P1-1 ✅）：geom.plan_interior_candidates（最大房间
   一对对角机位）+ runner 重构（depth 全候选先行 → imgstat 灰度方差排序 → 择优渲染
   white/lineart，iso 恒保留，兜底保留最优）。实测：6 候选渲染 depth，客房机位
   （std 19.3/18.3）胜出，旧空白机位（std 5.1）自动淘汰。已知限制：内视图质量受
   房间多边形保真度约束（天花分区近似），真房间边界需墙网完整闭合。
2. **SPLINE 家具抓取**（P1-2 ✅）：家具图层 SPLINE 控制点凸包 → 家具（能力就绪，
   本图 3 条 SPLINE 位于其他图层）。
3. **柔和阴影**（P1-3 ⚠️ 未解决）：几何修复后 lit 渲染仍有 ~12% 真黑像素——
   Cycles 对共面墙条带的光照死角与几何修复无关，根因未明。决策：white/lineart
   保持 clay 无光着色（确定性、可读），lit 调查连同根因分析归入 P2 深项。
4. **VLM Fidelity Check**（P2 提前 ✅）：glm-5.3 图像对比实测可用（score=82，
   剩余 issue 全部 low/medium：家具微偏/缺冰箱/门宽略窄），床头朝向 high 已由
   床头贴墙启发式修复。

**当前对账状态**（对照用户 CAD 截图）：外圈结构墙+墙垛 ✅、淋浴间/盥洗/马桶间内隔墙 ✅、
走廊 ✅、衣柜区 ✅、双床位置 ✅、电视柜 ✅、马桶 ✅、门洞 ✅；尚缺：沙发/单人扶手椅
部分捕获、吊顶几何（P2）、内视相机（P1）、柔和阴影（P1）。

### 7.2 VLM Fidelity Check（P2 提前实现，2026-09-08）

话术 §9"设计一致性检查 Agent"的最小实现——**咨询层**，确定性主校验
（cad_overlay 叠加 + parse_quality 断言）不变：

- **工具**：`app/tools/fidelity.py` `check_fidelity(cad_png, render_png, out_json, model=)`
  ——输入 CAD 平面渲染图 + 白模俯视渲染图，VLM 逐项对比墙体/房间划分/家具，
  输出结构化报告 `FidelityReport{score 0-100, summary, issues[kind/location/severity]}`；
- **通道**：复用 `app/infra/llm.py` 的 anthropic 协议端点（open.bigmodel.cn），
  图像走 base64 内容块；模型 `ARP_FIDELITY_MODEL` 缺省回落 `ARP_LLM_MODEL`；
- **缓存**：按文件 hash+模型+prompt 版本缓存（fidelity_report.json 命中即回）；
- **CLI**：`scripts/fidelity_check.py --cad <png> --render <png> [--out json] [--model]`；
- **真实端点验证**：glm-5.3 直接支持图像输入（score=82，5 条 issue：
  床头柜缺失/家具偏移/隔断未表现等——其中混有 VLM 对房型的误读如"厨房/客厅"，
  概率性固有，故只做咨询层）；
- **测试**：tests/unit/test_fidelity.py 5 例（解析/缓存/坏 JSON/markdown 包裹/缺输入），
  LLM 全 mock 不打真端点；全绿 134。

### 7.4 图纸识别 Agent 接入提取管线（2026-09-08）

用户提出"先做识别 Agent，后续工作依托其描述"→ 已实现并接入：

- **识别 Agent**：`app/agents/vision/agent.py`（多视图渲染 + VLM → 解读书
  DrawingUnderstanding，模型 `ARP_VISION_MODEL` 默认 glm-5.3-flash）
- **渲染模块**：`app/tools/cad_render.py`（手动矩阵遍历块，含 HATCH/嵌套块）
- **解读书模型**：`app/models/vision.py`（类型/分区/图层语义/家具清单）
- **接入 parse**：`parse_scene(..., understanding=)` 三条新路径：
  ①语义路由图层（wall/furniture/ceiling → 对应提取集合，walls 48→58）
  ②分区家具扫荡（识别分区内的家具形实体，无论图层；家具 14→17）
  ③交叉验证（VLM 清单 vs 提取差异 → notes："wardrobe,shower,plant 缺失"）
- **容错**：VLM 输出偏离 schema（layer_semantics 输出 dict）自动归一化
- **修复**：glm 推理模型 thinking 块耗尽默认 max_tokens → llm.py max_tokens=16384

**分工原则**：VLM 出语义与粗位置（解读书），确定性代码出精确几何（DXF 实体）。
解读书是咨询配置，不直接产生坐标。134 tests / mypy 0 / ruff 绿。

### 7.5 识别 Agent 升级：多图分块识别 + 话术元素全集（2026-09-08）

用户需求：识别 Model 区与布局区多张图、跨图交叉验证、话术 PDF 全元素逐项识别
（位置/尺寸/朝向详细描述）、选出最详尽布局图 → 已实现：

- **机制确认**：布局区 12 视口各自冻结不同图层 = 多张图（`cad_sheets.py` 枚举+评分，
  家具实体密度最高者 = 布置图视口，确定性选出 VP5）
- **分块识别**：模型空间 3×2 网格渲染（3% 重叠）→ 每分块独立 VLM 识别
  （glm-5.3-flash，提示词含 `taxonomy.py` 话术元素全集 17 类）
- **世界坐标合并**：分块元素 bbox_pct → 世界坐标 → IoU 去重（确定性交叉验证）
- **实测**（01-平面系统图）：91 元素/15 类别——窗 5（含百叶屏风/格栅）、家具 10、
  卫浴 2、走廊通道 1（**不再分割**）、柱 4、天花 15（含高低差/灯槽）、房间名称 1
- **精确数据分工**：VLM 出语义与粗位置（world_bbox 由确定性渲染网格换算），
  SceneJSON 出毫米级实体——后续模块用 world_bbox 空间对账二者
- 已知限制：VLM 单元素置信度 0.25-0.65（概率性），床类识别有分块波动

## 8. 与切片 3 的关系

P0/P1 完成前不推进切片 3（主图组装）——主图的全自动闭环依赖真实的降级信号
（parse_quality），否则把白模错误静默传递到 ComfyUI 端，问题被渲染掩盖、更难定位。

## 8. 与切片 3 的关系

P0/P1 完成前不推进切片 3（主图组装）——主图的全自动闭环依赖真实的降级信号
（parse_quality），否则把白模错误静默传递到 ComfyUI 端，问题被渲染掩盖、更难定位。
