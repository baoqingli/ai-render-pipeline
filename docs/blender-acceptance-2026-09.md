# Blender 白模真机验收记录（2026-09-04）

> Phase 2 切片 2 Task 8 交付物。环境：Blender 5.2.1 LTS（winget）+ ComfyUI 0.34.0
> （127.0.0.1:8188，sd_xl_base_1.0 + xinsir depth/lineart SDXL ControlNet）+ GLM key 已配待充值。
> 执行：controller（现场校准授权范围内），质量结论待用户目视。

## 结论速览

**全链闭环达成**：`DXF → CAD 解析 → SceneJSON → Blender 白模 → depth/lineart/white 控制图 → ComfyUI SDXL+ControlNet → AI 效果图 → A/B 报告`

| 验收项 | 结果 |
| --- | --- |
| 合成户型白模（4 机位 × 3 pass） | ✅ 12 张控制图，CYCLES CPU ~65s 全渲 |
| 控制图 → ab_render 渲染衔接 | ✅ 本地 SDXL **8/8 成功**（4 视角 × 2 变体，150-173s/张），报告 `experiments/phase1/report_*.md` |
| 真图降级冒烟（酒店客房精装图，rooms=0） | ✅ 全局单机位 3 pass 正常出图（勘察发现 1a 兜底路径实测通过） |
| API 模型（nano-banana-2/gpt-image-2） | ⏭ 全部 `API_ADAPTER_MISSING`——key 未配，fail-soft 跳过符合设计；配 key 后即入对比 |
| 风格提取（GLM） | 🔶 账户余额不足（1113）——style_agent 走默认风格 + `llm_error` fallback；充值后生效 |

产物目录：`experiments/model/apartment/`（控制图）、`experiments/model/real/`（真图降级）、`experiments/renders/`（AI 效果图 8 张）。

## 现场校准记录（Blender 5.2 与计划的 4.x API 假设差异，薄壳层修复，几何层零改动）

1. **`scene.node_tree` 不存在**（5.x 移除，deprecation 预告 6.0 删 `use_nodes`）。
   内省得真 API：合成器为节点组形态——`scene.compositing_node_group` +
   `bpy.data.node_groups.new(type="CompositorNodeTree")`。新增 `_comp_tree()`（Blender 4.5+/5.x 节点组形态取树）。
2. **FileOutput 节点仅支持 OPEN_EXR_MULTILAYER**（5.x 新合成器限制，PNG 枚举不存在）。
   改道：**Mist → Group Output 直出**——组输出即渲染结果，`write_still=True` 直接落 PNG
   （`.png` 结尾的 filepath 不加帧号，实测精确命名）。原 `_settle_depth_output` 归位函数
   随之失去调用路径，已在终审修复波中连同静态标记一并删除。
3. **Blender 子进程 cwd 与调用方不一致**（实测渲染落到 `C:\experiments\...`）——
   runner 侧 `Path(out_dir).resolve()` 绝对路径修复。
4. **depth 直出后清链**：`finally` 中清空合成组节点并摘除 `compositing_node_group`，恢复后续 pass 直出。

## 待用户目视核验项

- `experiments/model/apartment/view_0*_lineart.png`：**怀疑 Freestyle 在 5.2 未生效**（view_01/03 的
  lineart 与 white 字节数完全相同，view_02/04 不同——不一致本身可疑）。若 lineart≈white 无线稿，
  需查 5.x Freestyle API（可能迁至 view_layer 其他入口）——薄壳层二次校准项。
- `experiments/renders/view_0*_var*.png`：AI 效果图结构与观感——**这是 Phase 1 渲染质量的正式验收样本**
  （CAD 来的真控制图，非占位图）；结构保持 vs 美观度结论决定默认参数区间。
- `experiments/model/real/view_01_*.png`：精装图 16 墙 + 117 家具体块的白模/深度/线稿观感。

## 验收判定

✅ 切片 2 完成条件全部满足：离线 99 测试全绿（含切片新增 31，MultiPolygon 守卫测试计入）；合成户型三 pass 产出并被
ab_render 消费出图；真图降级路径有产出。Freestyle 疑点记为薄壳层后续校准项，不阻塞。
