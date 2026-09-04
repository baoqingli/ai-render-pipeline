"""Blender 进程内白模构建脚本（薄壳：几何全部在上游 geom 层完成）。

用法（由 app/tools/blender/runner.py 子进程调用，也可手动）:
  blender --background --factory-startup --python blender/scene_builder.py -- \
      --plan <build_plan.json>

产出: <output_dir>/{view_id}_{depth|lineart|white}.png   （单位: 上游 mm → Blender m ×0.001）
"""
import argparse
import json
import os
import sys

import bpy
from mathutils import Vector

S = 0.001  # mm → m


def add_boxes(boxes):
    mat_wall = bpy.data.materials.new("wall")
    mat_wall.diffuse_color = (0.8, 0.8, 0.8, 1.0)
    mat_furn = bpy.data.materials.new("furniture")
    mat_furn.diffuse_color = (0.6, 0.6, 0.65, 1.0)
    for i, b in enumerate(boxes):
        bpy.ops.mesh.primitive_cube_add(size=1.0)
        o = bpy.context.active_object
        o.name = f"{b['kind']}_{i:03d}"
        sx, sy, sz = (d * S for d in b["size"])
        o.dimensions = (abs(sx), abs(sy), abs(sz))
        cx, cy, cz = (c * S for c in b["center"])
        o.location = (cx, cy, cz)
        o.rotation_euler[2] = b.get("rot_z", 0.0)
        mat = mat_wall if b["kind"] != "furniture" else mat_furn
        o.data.materials.append(mat)


def add_camera(cam):
    if "ARP_CAM" not in bpy.data.objects:
        cam_data = bpy.data.cameras.new("ARP_CAM")
        obj = bpy.data.objects.new("ARP_CAM", cam_data)
        bpy.context.collection.objects.link(obj)
    obj = bpy.data.objects["ARP_CAM"]
    obj.location = Vector([c * S for c in cam["position"]])
    direction = Vector([t * S for t in cam["target"]]) - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = obj
    return obj


def _setup_world(scene):
    scene.world = scene.world or bpy.data.worlds.new("ARP_WORLD")
    scene.world.use_nodes = True
    bg = scene.world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.9, 0.9, 0.9, 1.0)
        bg.inputs[1].default_value = 1.0


def _setup_depth_output(scene, path):
    scene.view_layers[0].use_pass_mist = True
    diag = _scene_diag(scene)
    ms = scene.world.mist_settings
    ms.start, ms.depth, ms.falloff = 0.0, diag * 1.5, "LINEAR"
    scene.use_nodes = True
    nt = scene.node_tree
    nt.nodes.clear()
    rl = nt.nodes.new("CompositorNodeRLayers")
    fo = nt.nodes.new("CompositorNodeOutputFile")
    fo.format.file_format, fo.format.color_mode = "PNG", "BW"
    fo.base_path = path
    nt.links.new(rl.outputs["Mist"], fo.inputs["Image"])
    return fo


def _scene_diag(scene) -> float:
    xs = [o.location.x for o in scene.objects]
    ys = [o.location.y for o in scene.objects]
    if not xs:
        return 10.0
    return max(max(xs) - min(xs), max(ys) - min(ys), 1.0) * 1.5


def _setup_freestyle(scene):
    scene.render.use_freestyle = True
    vl = scene.view_layers[0]
    if "ARP" not in [ls.name for ls in vl.freestyle_settings.linesets]:
        ls = vl.freestyle_settings.linesets.new("ARP")
        ls.linestyle = bpy.data.linestyles.new("ARP_LINE")
        ls.linestyle.color = (0.0, 0.0, 0.0)


def render_pass(scene, cam, pass_name, out_dir):
    out = os.path.join(out_dir, f"{cam['view_id']}_{pass_name}.png")
    scene.render.filepath = out
    scene.render.image_settings.file_format = "PNG"
    scene.render.resolution_x, scene.render.resolution_y = 1024, 768
    fo = None
    scene.render.use_freestyle = False
    scene.use_nodes = False
    scene.view_layers[0].use_pass_mist = False
    if pass_name == "depth":
        fo = _setup_depth_output(scene, out)
    elif pass_name == "lineart":
        _setup_freestyle(scene)
    try:
        bpy.ops.render.render(write_still=(pass_name != "depth"))
    finally:
        if fo is not None:
            scene.use_nodes = False
        scene.view_layers[0].use_pass_mist = False
        scene.render.use_freestyle = False
    return out


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    args = ap.parse_args(argv)
    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    out_dir = plan["output_dir"]
    os.makedirs(out_dir, exist_ok=True)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 16
    scene.cycles.device = "CPU"
    _setup_world(scene)
    add_boxes(plan["boxes"])
    for cam in plan["cameras"]:
        add_camera(cam)
        for pass_name in plan.get("passes", ["depth", "lineart", "white"]):
            render_pass(scene, cam, pass_name, out_dir)
    print(f"ARP_DONE views={len(plan['cameras'])}")


if __name__ == "__main__":
    main()
