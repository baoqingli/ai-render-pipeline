import sys

sys.path.insert(0, "D:/me_work/AI/project/ai-render-pipeline")
# 加载渲染后的 blend 文件（如果有）或直接运行 scene_builder
import json

import bpy

plan = json.load(open("experiments/model/scene-4967ced2/build_plan.json", encoding="utf-8"))

# 检查场景中的材质
print("\n=== Materials in scene ===")
for mat in bpy.data.materials:
    print(f"\nMaterial: {mat.name}")
    print(f"  use_nodes: {mat.use_nodes}")
    if mat.use_nodes:
        for node in mat.node_tree.nodes:
            print(f"  Node: {node.name} (type={node.type})")
            if node.type == 'BSDF_PRINCIPLED':
                bc = node.inputs.get("Base Color")
                if bc:
                    print(f"    Base Color: {bc.default_value[:]}")

# 检查物体材质分配
print("\n=== Objects and their materials ===")
furn_count = 0
for obj in bpy.data.objects:
    if obj.type == 'MESH' and 'furniture' in obj.name:
        furn_count += 1
        if furn_count <= 3:
            mats = [slot.material.name if slot.material else None
                   for slot in obj.material_slots]
            print(f"{obj.name}: materials={mats}")
print(f"Total furniture objects: {furn_count}")
