import json
import sys

import bpy

# 读取 plan
with open(sys.argv[sys.argv.index("--plan") + 1], encoding="utf-8") as f:
    plan = json.load(f)

# 清场
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete()

# 材质
def _cycles_mat(name, rgb, roughness=0.7):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
        bsdf.inputs["Roughness"].default_value = roughness
        print(f"[MAT] {name} Base Color={rgb} Roughness={roughness}")
    else:
        print(f"[MAT] WARNING: No BSDF_PRINCIPLED in {name}")
    return mat

mat_wall = _cycles_mat("wall", (0.85, 0.85, 0.85))
mat_furn = _cycles_mat("furniture", (0.75, 0.75, 0.78), 0.5)

# 验证材质节点
for mat in [mat_wall, mat_furn]:
    for node in mat.node_tree.nodes:
        print(f"[MAT] {mat.name} node: {node.name} type={node.type}")

S = 0.001
furn_count = 0
for i, b in enumerate(plan["boxes"]):
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    o = bpy.context.active_object
    o.scale = (b["size"][0]*S, b["size"][1]*S, b["size"][2]*S)
    o.location = (b["center"][0]*S, b["center"][1]*S, b["center"][2]*S)
    o.rotation_euler.z = b["rot_z"]
    bpy.ops.object.transform_apply(scale=True)
    mat = mat_furn if b["kind"] == "furniture" else mat_wall
    o.data.materials.clear()
    o.data.materials.append(mat)
    if b["kind"] == "furniture":
        furn_count += 1
        if furn_count <= 2:
            print(f"[OBJ] furniture_{i}: mat_slots={[s.material.name for s in o.material_slots]}")

print(f"[INFO] furniture objects: {furn_count}")

# 灯光
light_data = bpy.data.lights.new("SUN", type='SUN')
light_data.energy = 3.0
light_obj = bpy.data.objects.new("SUN", light_data)
bpy.context.collection.objects.link(light_obj)
light_obj.location = (10, -10, 15)
light_obj.rotation_euler = (0.8, 0.2, 0.5)

# 世界背景
bpy.context.scene.world.use_nodes = True
bg = bpy.context.scene.world.node_tree.nodes.get("Background")
if bg:
    bg.inputs[0].default_value = (0.9, 0.9, 0.9, 1.0)
    bg.inputs[1].default_value = 1.0

# 相机
bpy.ops.object.camera_add(location=(10, -10, 8))
cam = bpy.context.active_object
cam.rotation_euler = (1.1, 0, 0.785)
bpy.context.scene.camera = cam

# Cycles
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.samples = 32
scene.cycles.device = 'CPU'
scene.render.resolution_x = 800
scene.render.resolution_y = 600
scene.render.filepath = "D:/me_work/AI/project/ai-render-pipeline/debug_mat_test.png"
scene.render.image_settings.file_format = 'PNG'
bpy.ops.render.render(write_still=True)
print("[DONE] render complete")
