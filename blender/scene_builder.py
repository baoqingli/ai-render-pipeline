"""Blender 进程内白模构建脚本。

用法:
  blender --background --factory-startup --python blender/scene_builder.py -- \
      --plan <build_plan.json> [--pass white|depth|lineart]

每个 pass 单独调用，无跨 pass 合成器污染。
对照验收基准（docs/white-model-fix-plan-2026-09.md §5）：
太阳光+环境光柔和阴影、正交俯视相机、家具按类型低模套件、窗棂、全局 Bevel。
"""
import argparse
import contextlib
import json
import math
import os
import sys

import bpy
from mathutils import Vector

S = 0.001  # mm → m


# ── 材质 ──────────────────────────────────────────────────────────────────────

def _mat(name: str, rgb: tuple, roughness: float = 0.7, emissive: bool = False):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    mat.use_backface_culling = False  # 双面渲染
    nt = mat.node_tree
    nodes, links = nt.nodes, nt.links
    nodes.clear()
    out = nodes.new("ShaderNodeOutputMaterial")
    if emissive:
        # white pass 用无光 clay 着色：确定性零光照伪影（5.2 Cycles 对退化共面几何
        # 存在光照死角），ControlNet 控制图只需要结构可见性
        emi = nodes.new("ShaderNodeEmission")
        emi.inputs["Color"].default_value = (*rgb, 1.0)
        links.new(emi.outputs["Emission"], out.inputs["Surface"])
    else:
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
        bsdf.inputs["Roughness"].default_value = roughness
        links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    return mat


def _bevel(o, width: float = 0.006):
    """全局倒角：消灭纯直角体块的生硬光影（AI调研对话 硬伤一）。"""
    if os.environ.get("ARP_NO_BEVEL"):
        return
    m = o.modifiers.new("Bevel", "BEVEL")
    m.width = width
    m.segments = 2
    m.limit_method = "ANGLE"
    m.angle_limit = math.radians(50.0)


EMISSIVE = {"on": False}   # white pass 置 True：clay 无光着色


# ── 世界背景与灯光 ─────────────────────────────────────────────────────────────

def setup_world(scene):
    """白环境光（配合太阳光做柔和定向阴影）。"""
    w = scene.world
    nt = w.node_tree
    nodes, links = nt.nodes, nt.links
    nodes.clear()
    bg = nodes.new("ShaderNodeBackground")
    bg.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    bg.inputs["Strength"].default_value = 1.0
    # Blender 5.x Background 节点新增 Weight 输入，默认 0.0 → 世界光完全不生效，
    # 场景只剩太阳光、所有阴影纯黑（白模黑斑问题的根因）
    if "Weight" in bg.inputs:
        bg.inputs["Weight"].default_value = 1.0
    out = nodes.new("ShaderNodeOutputWorld")
    links.new(bg.outputs["Background"], out.inputs["Surface"])


def setup_lights(scene):
    """单一太阳光：柔和大角度，斜射入场景形成基准图同款定向阴影。
    ARP_NO_SUN=1 时跳过（诊断用：纯环境光）。"""
    if os.environ.get("ARP_NO_SUN"):
        return
    data = bpy.data.lights.new("ARP_SUN", type="SUN")
    data.energy = 1.2
    data.angle = math.radians(25.0)          # 大角度软阴影
    sun = bpy.data.objects.new("ARP_SUN", data)
    bpy.context.collection.objects.link(sun)
    direction = Vector((0.25, -0.2, -1.0)).normalized()   # 近垂直俯照，阴影贴墙根
    sun.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


# ── 几何体 ───────────────────────────────────────────────────────────────────

def _place(center: tuple, lx: float, ly: float, lz: float, rot: float) -> tuple:
    c, s = math.cos(rot), math.sin(rot)
    return (center[0] + lx * c - ly * s, center[1] + lx * s + ly * c, center[2] + lz)


def _box_obj(name, size, loc, rot_z, mat):
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    o = bpy.context.active_object
    o.name = name
    o.dimensions = (max(size[0], 2e-3), max(size[1], 2e-3), max(size[2], 2e-3))
    bpy.ops.object.transform_apply(scale=True)
    # 5.2 后台模式 primitive_*_add(location=) 不可靠，必须显式赋值（家具堆原点教训）
    o.location = loc
    o.rotation_euler[2] = rot_z
    o.data.materials.append(mat)
    _bevel(o)
    return o


def _cyl_obj(name, radius, depth, loc, mat):
    bpy.ops.mesh.primitive_cylinder_add(radius=max(radius, 5e-3), depth=max(depth, 2e-3))
    o = bpy.context.active_object
    o.name = name
    o.location = loc
    o.data.materials.append(mat)
    _bevel(o)
    return o


def _sphere_obj(name, radius, loc, mat):
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2, radius=max(radius, 5e-3))
    o = bpy.context.active_object
    o.name = name
    o.location = loc
    o.data.materials.append(mat)
    return o


def _furniture_kit(b, idx: int, mats: dict):
    """按规范类型参数化低模套件（局部坐标 x=宽 y=进深 z=向上，单位 m）。

    mats: {"default": mat, "bed": mat, "sanitary": mat, "wood": mat, "upholstery": mat}
    """
    label = b.get("label", "")
    rot = b.get("rot_z", 0.0)
    cx, cy = b["center"][0] * S, b["center"][1] * S
    sx = max(b["size"][0] * S, 0.02)
    sy = max(b["size"][1] * S, 0.02)
    sz = max(b["size"][2] * S, 0.02)
    n = [0]

    # 按类型选材质
    if label in ("bed",):
        type_mat = mats["bed"]
    elif label in ("toilet", "sink", "shower", "bathtub"):
        type_mat = mats["sanitary"]
    elif label in ("wardrobe", "cabinet"):
        type_mat = mats["wood"]
    elif label in ("sofa", "armchair", "chair"):
        type_mat = mats["upholstery"]
    else:
        type_mat = mats["default"]

    def part(name, size, lx, ly, lz):
        n[0] += 1
        return _box_obj(f"furn_{idx:03d}_{label}_{n[0]}", size,
                        _place((cx, cy, 0.0), lx, ly, lz, rot), rot, type_mat)

    if label == "bed":
        part("base", (sx, sy, 0.25), 0, 0, 0.125)
        part("mattress", (sx - 0.08, sy - 0.35, 0.22), 0, 0.12, 0.36)
        part("pillow_l", (sx * 0.38, 0.30, 0.10), -sx * 0.22, -sy / 2 + 0.28, 0.52)
        part("pillow_r", (sx * 0.38, 0.30, 0.10), sx * 0.22, -sy / 2 + 0.28, 0.52)
        part("headboard", (sx, 0.07, 0.75), 0, -sy / 2 + 0.035, 0.375)
    elif label == "sofa":
        part("base", (sx, sy, 0.32), 0, 0, 0.16)
        part("back", (sx, 0.18, 0.45), 0, -sy / 2 + 0.09, 0.32 + 0.225)
        part("arm_l", (0.14, sy, 0.42), -sx / 2 + 0.07, 0, 0.32 + 0.21)
        part("arm_r", (0.14, sy, 0.42), sx / 2 - 0.07, 0, 0.32 + 0.21)
        n_seat = max(2, int(sx / 0.8))
        for k in range(n_seat):
            lx = -sx / 2 + 0.14 + (sx - 0.28) * (k + 0.5) / n_seat
            part(f"seat{k}", ((sx - 0.28) / n_seat - 0.03, sy * 0.7, 0.12),
                 lx, 0.05, 0.32 + 0.06)
    elif label == "table":
        if min(sx, sy) < 0.5:
            _cyl_obj(f"furn_{idx:03d}_table_top", sx / 2, 0.045,
                     _place((cx, cy, 0.0), 0, 0, 0.72, rot), type_mat)
            _cyl_obj(f"furn_{idx:03d}_table_leg", 0.05, 0.70,
                     _place((cx, cy, 0.0), 0, 0, 0.35, rot), type_mat)
        else:
            part("top", (sx, sy, 0.045), 0, 0, 0.72)
            for dx in (-1, 1):
                for dy in (-1, 1):
                    part(f"leg{dx}{dy}", (0.05, 0.05, 0.70),
                         dx * (sx / 2 - 0.05), dy * (sy / 2 - 0.05), 0.35)
    elif label == "chair":
        part("seat", (sx, sy, 0.07), 0, 0, 0.44)
        part("back", (sx, 0.05, 0.42), 0, -sy / 2 + 0.03, 0.44 + 0.21)
        for dx in (-1, 1):
            for dy in (-1, 1):
                part(f"leg{dx}{dy}", (0.03, 0.03, 0.44),
                     dx * (sx / 2 - 0.04), dy * (sy / 2 - 0.04), 0.22)
    elif label == "toilet":
        part("tank", (sx * 0.85, sy * 0.30, 0.72), 0, -sy * 0.30, 0.36)
        part("bowl", (sx, sy * 0.62, 0.38), 0, sy * 0.15, 0.19)
        _cyl_obj(f"furn_{idx:03d}_toilet_seat", min(sx, sy * 0.6) * 0.42, 0.05,
                 _place((cx, cy, 0.0), 0, sy * 0.15, 0.42, rot), type_mat)
    elif label == "sink":
        part("counter", (sx, sy, sz), 0, 0, sz / 2)
        _cyl_obj(f"furn_{idx:03d}_sink_basin", min(sx, sy) * 0.30, 0.08,
                 _place((cx, cy, 0.0), 0, 0, sz + 0.04, rot), type_mat)
    elif label == "plant":
        _cyl_obj(f"furn_{idx:03d}_pot", min(sx, sy) * 0.40, 0.35,
                 _place((cx, cy, 0.0), 0, 0, 0.175, rot), type_mat)
        _sphere_obj(f"furn_{idx:03d}_leaf", max(sx, sy) * 0.55,
                    _place((cx, cy, 0.0), 0, 0, 0.35 + sz * 0.4, rot), type_mat)
    elif label == "tv":
        part("console", (sx, 0.40, 0.45), 0, 0, 0.225)
        part("panel", (sx, 0.06, min(sz * 0.9, 0.75)), 0, -0.10, 0.45 + min(sz * 0.45, 0.38))
    else:
        # cabinet/wardrobe/other：整体箱体
        part("body", (sx, sy, sz), 0, 0, sz / 2)


def add_boxes(boxes):
    hide = set(filter(None, os.environ.get("ARP_HIDE", "").split(",")))
    em = EMISSIVE["on"]
    # ARP_SEMANTIC_COLORS=1：语义高对比配色（校验专用）——
    # 地板纯白/墙纯黑/每类家具独立饱和色，像素级确定性判别+VLM 识别都受益
    if os.environ.get('ARP_SEMANTIC_COLORS'):
        mat_wall = _mat('sv_wall', (0.0, 0.0, 0.0), emissive=True)
        mat_floor = _mat('sv_floor', (1.0, 1.0, 1.0), emissive=True)
        mats = {
            'default': _mat('sv_furn', (1.0, 0.0, 1.0), emissive=True),   # 品红
            'bed': _mat('sv_bed', (1.0, 0.0, 0.0), emissive=True),         # 红
            'sanitary': _mat('sv_san', (0.0, 1.0, 0.0), emissive=True),    # 绿
            'wood': _mat('sv_wood', (0.0, 0.0, 1.0), emissive=True),       # 蓝
            'upholstery': _mat('sv_uph', (1.0, 1.0, 0.0), emissive=True),  # 黄
            'door': _mat('sv_door', (1.0, 0.5, 0.0), emissive=True),        # 橙
        }
        hide = set(filter(None, os.environ.get("ARP_HIDE", "").split(",")))
        for i, b in enumerate(boxes):
            if b['kind'] in hide:
                continue
            if b['kind'] == 'furniture':
                _furniture_kit(b, i, mats)
                continue
            # 校验模式跳过门过梁/窗台矮段（z<1200）：俯视时它们盖住门洞
            # 地面标记（label=door 橙色条），导致门在俯视图不可见
            if b['kind'] == "wall" and b['size'][2] < 1200                     and not b.get('label'):
                continue
            bpy.ops.mesh.primitive_cube_add(size=1.0)
            o = bpy.context.active_object
            o.name = f"{b['kind']}_{i:03d}"
            sx = max(abs(b['size'][0]) * S, 1e-4)
            sy = max(abs(b['size'][1]) * S, 1e-4)
            sz = max(abs(b['size'][2]) * S, 1e-4)
            o.dimensions = (sx, sy, sz)
            bpy.ops.object.transform_apply(scale=True)
            o.location = (b['center'][0]*S, b['center'][1]*S, b['center'][2]*S)
            o.rotation_euler[2] = b.get('rot_z', 0.0)
            if b.get('label') == 'door':
                m = mats['door']
            else:
                m = mat_wall if b['kind'] == 'wall' else mat_floor
            o.data.materials.clear()
            o.data.materials.append(m)
            _bevel(o)
        return

    # 制图惯例配色：墙深灰、地面浅、家具中灰（对齐 Image#8 阅读习惯）
    mat_wall = _mat("mat_wall", (0.30, 0.30, 0.30), roughness=0.7, emissive=em)
    mat_furn = _mat("mat_furn", (0.85, 0.80, 0.72), roughness=0.5, emissive=em)  # 木质暖色
    mat_bed = _mat("mat_bed", (0.92, 0.90, 0.88), roughness=0.6, emissive=em)  # 床垫白
    mat_upholstery = _mat("mat_uph", (0.75, 0.72, 0.68), roughness=0.7, emissive=em)  # 沙发/椅子布艺
    mat_sanitary = _mat("mat_sanitary", (0.95, 0.95, 0.97), roughness=0.2, emissive=em)  # 洁具白
    mat_wood = _mat("mat_wood", (0.65, 0.50, 0.35), roughness=0.5, emissive=em)  # 木饰面
    mat_floor = _mat("mat_floor", (0.88, 0.88, 0.88), roughness=0.8, emissive=em)
    mat_frame = _mat("mat_frame", (0.20, 0.22, 0.28), roughness=0.35, emissive=em)
    mat_glass = _mat("mat_glass", (0.40, 0.48, 0.60), roughness=0.2, emissive=em)
    for i, b in enumerate(boxes):
        kind = b["kind"]
        if kind in hide:
            continue
        if kind == "furniture":
            furn_mats = {"default": mat_furn, "bed": mat_bed,
                         "sanitary": mat_sanitary, "wood": mat_wood,
                         "upholstery": mat_upholstery}
            _furniture_kit(b, i, furn_mats)
            continue
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0))
        o = bpy.context.active_object
        o.name = f"{kind}_{i:03d}"
        sx = max(abs(b["size"][0]) * S, 1e-4)
        sy = max(abs(b["size"][1]) * S, 1e-4)
        sz = max(abs(b["size"][2]) * S, 1e-4)
        o.dimensions = (sx, sy, sz)
        bpy.ops.object.transform_apply(scale=True)
        o.location = (b["center"][0]*S, b["center"][1]*S, b["center"][2]*S)
        o.rotation_euler[2] = b.get("rot_z", 0.0)
        if b.get("label") == "glass":
            mat = mat_glass
        else:
            mat = {"wall": mat_wall, "frame": mat_frame, "floor": mat_floor}.get(kind, mat_furn)
        o.data.materials.clear()
        o.data.materials.append(mat)
        _bevel(o)


def add_floor(scene):
    meshes = [o for o in scene.objects
              if o.type == "MESH" and not o.name.startswith("ARP_")]
    if not meshes:
        return
    xs = [o.location.x for o in meshes]
    ys = [o.location.y for o in meshes]
    cx, cy = (min(xs)+max(xs))/2, (min(ys)+max(ys))/2
    sw = (max(xs)-min(xs)) * 1.4 + 1.0
    sh = (max(ys)-min(ys)) * 1.4 + 1.0
    bpy.ops.mesh.primitive_plane_add(size=1.0, location=(cx, cy, -0.001))
    o = bpy.context.active_object
    o.name = "ARP_FLOOR"
    o.dimensions = (sw, sh, 0.0)
    bpy.ops.object.transform_apply(scale=True)
    mat = _mat("mat_ground", (0.40, 0.40, 0.40), roughness=0.9, emissive=EMISSIVE["on"])
    o.data.materials.clear()
    o.data.materials.append(mat)


# ── 相机 ─────────────────────────────────────────────────────────────────────

def add_camera(scene, cam):
    cd = bpy.data.cameras.new("ARP_CAM")
    co = bpy.data.objects.new("ARP_CAM", cd)
    bpy.context.collection.objects.link(co)
    if cam.get("ortho"):
        cd.type = "ORTHO"
        # ortho_scale 契约按 mm 存储（与 position/target 一致），此处换算到场景单位 m
        cd.ortho_scale = float(cam.get("ortho_scale") or 10000.0) * S
    pos = Vector([c * S for c in cam["position"]])
    tgt = Vector([c * S for c in cam["target"]])
    co.location = pos
    direction = tgt - pos
    if abs(direction.z) > direction.length * 0.98:
        # 正下方俯视：to_track_quat 方向退化会产生任意滚转（镜像 bug 根因），
        # 固定旋转 (0,0,0) = 相机 -Z 朝下、+Y 朝北（与 CAD 图面方向一致）
        co.rotation_euler = (0.0, 0.0, 0.0)
    else:
        up = "Y" if abs(direction.z) > direction.length * 0.5 else "Z"
        co.rotation_euler = direction.to_track_quat("-Z", up).to_euler()
    # 内视透视图用广角（24mm）：室内视野需要 ~74° 水平 FOV
    if "int" in cam.get("view_id", "") and not cam.get("ortho"):
        cd.lens = 24.0
    scene.camera = co


def render_pass(scene, cam, out_dir, pass_name: str):
    """渲染分发：white / depth / lineart 三个 pass 独立出图。"""
    if pass_name == "white":
        render_white(scene, cam, out_dir)
    elif pass_name == "depth":
        render_depth(scene, cam, out_dir)
    elif pass_name == "lineart":
        render_lineart(scene, cam, out_dir)
    else:
        raise ValueError(f"unknown pass: {pass_name}")


# ── 渲染：white pass ──────────────────────────────────────────────────────────

def render_white(scene, cam, out_dir):
    out = os.path.join(out_dir, f"{cam['view_id']}_white.png")
    scene.render.engine  = "CYCLES"
    scene.cycles.samples = 128  # 更多采样减少噪点
    scene.cycles.device  = "CPU"
    scene.render.resolution_x = 1200
    scene.render.resolution_y = 900
    scene.render.filepath = out
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode  = "RGB"
    # Standard：线性映射，无色调压缩
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0
    with contextlib.suppress(Exception):
        scene.compositing_node_group = None
    scene.view_layers[0].use_pass_mist = False
    scene.render.use_freestyle = False
    bpy.ops.render.render(write_still=True)
    print(f"ARP_WHITE_SAVED={out}")


# ── 渲染：depth pass ──────────────────────────────────────────────────────────

def render_depth(scene, cam, out_dir):
    out = os.path.join(out_dir, f"{cam['view_id']}_depth.png")
    scene.render.engine  = "CYCLES"
    scene.cycles.samples = 4
    scene.cycles.device  = "CPU"
    scene.render.resolution_x = 1200
    scene.render.resolution_y = 900
    scene.render.filepath = out
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode  = "BW"
    scene.view_layers[0].use_pass_mist = True
    xs = [o.location.x for o in scene.objects]
    ys = [o.location.y for o in scene.objects]
    diag = max(max(xs)-min(xs), max(ys)-min(ys), 1.0) * 2.0 if xs else 10.0
    ms = scene.world.mist_settings
    ms.start, ms.depth, ms.falloff = 0.0, diag, "LINEAR"
    # 合成器：Mist → 输出
    nt = bpy.data.node_groups.new("ARP_COMP", "CompositorNodeTree")
    scene.compositing_node_group = nt
    nt.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    rl = nt.nodes.new("CompositorNodeRLayers")
    go = nt.nodes.new("NodeGroupOutput")
    nt.links.new(rl.outputs["Mist"], go.inputs["Image"])
    bpy.ops.render.render(write_still=True)
    print(f"ARP_DEPTH_SAVED={out}")


# ── 渲染：lineart pass ────────────────────────────────────────────────────────

def render_lineart(scene, cam, out_dir):
    out = os.path.join(out_dir, f"{cam['view_id']}_lineart.png")
    scene.render.engine  = "CYCLES"
    scene.cycles.samples = 4
    scene.cycles.device  = "CPU"
    scene.render.resolution_x = 1200
    scene.render.resolution_y = 900
    scene.render.filepath = out
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode  = "RGB"
    scene.render.use_freestyle = True
    vl = scene.view_layers[0]
    ls = vl.freestyle_settings.linesets.new("ARP")
    ls.linestyle = bpy.data.linestyles.new("ARP_LINE")
    ls.linestyle.color = (0.0, 0.0, 0.0)
    ls.linestyle.thickness = 1.5
    with contextlib.suppress(Exception):
        scene.compositing_node_group = None
    scene.view_layers[0].use_pass_mist = False
    bpy.ops.render.render(write_still=True)
    print(f"ARP_LINEART_SAVED={out}")


# ── 公共场景构建 ──────────────────────────────────────────────────────────────

def build_scene(plan, pass_name):
    scene = bpy.context.scene
    # white/lineart 用 clay 无光底图（lineart 只取 Freestyle 边缘，底图须无光照伪影）
    EMISSIVE["on"] = pass_name == "lineart" and not os.environ.get("ARP_EMISSIVE_OFF")
    setup_world(scene)
    setup_lights(scene)
    add_boxes(plan["boxes"])
    add_floor(scene)
    cam = plan["cameras"][0]          # 每次只渲一个相机
    if "_cam" in plan:
        cam = plan["_cam"]
    add_camera(scene, cam)
    out_dir = plan["output_dir"]
    render_pass(scene, cam, out_dir, pass_name)


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan",    required=True)
    ap.add_argument("--cam",     default="0")
    ap.add_argument("--pass",    dest="pass_name", default="white")
    args = ap.parse_args(argv)
    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    cam_idx = int(args.cam)
    plan["_cam"] = plan["cameras"][cam_idx]
    build_scene(plan, args.pass_name)
    print(f"ARP_DONE pass={args.pass_name}")


if __name__ == "__main__":
    main()
