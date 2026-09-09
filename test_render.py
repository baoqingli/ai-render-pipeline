import bpy

# 清理场景
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete()

# 创建材质
mat = bpy.data.materials.new("test_mat")
mat.use_nodes = True
bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
if bsdf:
    bsdf.inputs["Base Color"].default_value = (0.8, 0.3, 0.3, 1.0)  # 红色
    bsdf.inputs["Roughness"].default_value = 0.5

# 创建立方体
bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0, 0, 1))
cube = bpy.context.active_object
cube.data.materials.append(mat)

# 设置世界背景
bpy.context.scene.world.use_nodes = True
bg = bpy.context.scene.world.node_tree.nodes.get("Background")
if bg:
    bg.inputs[0].default_value = (0.9, 0.9, 0.9, 1.0)
    bg.inputs[1].default_value = 1.0

# 添加强光源
bpy.ops.object.light_add(type='SUN', location=(5, 5, 10))
sun = bpy.context.active_object
sun.data.energy = 3.0

# 设置相机
bpy.ops.object.camera_add(location=(7, -7, 5))
cam = bpy.context.active_object
cam.rotation_euler = (1.1, 0, 0.785)
bpy.context.scene.camera = cam

# Cycles 设置
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.samples = 32
scene.cycles.device = 'CPU'
scene.render.resolution_x = 512
scene.render.resolution_y = 512
scene.render.filepath = "D:/me_work/AI/project/ai-render-pipeline/test_output.png"
scene.render.image_settings.file_format = 'PNG'

# 渲染
bpy.ops.render.render(write_still=True)
print("Test render complete")
