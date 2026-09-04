# tests/unit/test_scene_builder_static.py
import ast
from pathlib import Path

SRC = Path("blender/scene_builder.py")


def test_scene_builder_compiles():
    compile(SRC.read_text(encoding="utf-8"), str(SRC), "exec")


def test_scene_builder_marks():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert {"main", "add_boxes", "add_camera", "render_pass",
            "_settle_depth_output"} <= funcs
    text = SRC.read_text(encoding="utf-8")
    for marker in ["use_pass_mist", "use_freestyle", "to_track_quat",
                   "0.001", "--plan", "depth", "lineart", "white",
                   "_settle_depth_output", "os.replace"]:
        assert marker in text, f"missing marker: {marker}"
