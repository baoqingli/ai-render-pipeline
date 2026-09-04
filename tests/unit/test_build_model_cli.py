# tests/unit/test_build_model_cli.py
import hashlib
from pathlib import Path

import pytest

from app.models.scene import Room, SceneJSON, Wall
from scripts.build_model import default_out_dir, ensure_scene


def _write_scene(path: Path, scene: SceneJSON) -> Path:
    path.write_text(scene.model_dump_json(), encoding="utf-8")
    return path


def _wall(wid: str) -> Wall:
    return Wall(id=wid, polygon=[[0, 0], [1, 0], [1, 1], [0, 1]])


def test_ensure_scene_passes_valid(tmp_path, capsys):
    p = tmp_path / "s.json"
    p.write_text(SceneJSON(walls=[Wall(id="w", polygon=[[0, 0], [1, 0], [1, 1], [0, 1]])])
                 .model_dump_json(), encoding="utf-8")
    ensure_scene(p)                                    # 不抛即通过


def test_ensure_scene_rejects_missing(tmp_path, capsys):
    with pytest.raises(SystemExit) as ei:
        ensure_scene(tmp_path / "nope.json")
    assert ei.value.code == 2


def test_ensure_scene_rejects_corrupt(tmp_path, capsys):
    p = tmp_path / "bad.json"
    p.write_text("{oops", encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        ensure_scene(p)
    assert ei.value.code == 2


def test_ensure_scene_rejects_empty(tmp_path, capsys):
    p = _write_scene(tmp_path / "empty.json", SceneJSON())  # walls/rooms 均空
    with pytest.raises(SystemExit) as ei:
        ensure_scene(p)
    assert ei.value.code == 2
    assert "scene 无可建模几何（walls/rooms 均空）" in capsys.readouterr().err


def test_ensure_scene_accepts_rooms_only(tmp_path, capsys):
    # 空 scene 守卫只看 walls/rooms：有 rooms 无 walls 仍可建模（地板 pass）
    p = _write_scene(tmp_path / "rooms.json",
                     SceneJSON(rooms=[Room(id="r", polygon=[[0, 0], [1, 0], [1, 1], [0, 1]])]))
    ensure_scene(p)  # 不抛即通过


def test_default_out_dir_same_content_same_dir(tmp_path):
    src = _write_scene(tmp_path / "plan.json", SceneJSON(walls=[_wall("w")]))
    copy = tmp_path / "copy" / "plan.json"
    copy.parent.mkdir()
    copy.write_bytes(src.read_bytes())  # 字节级同内容、同 stem
    assert default_out_dir(copy) == default_out_dir(src)
    d = default_out_dir(src)
    assert d.parent == Path("experiments/model")
    assert d.name == f"plan-{hashlib.sha256(src.read_bytes()).hexdigest()[:8]}"


def test_default_out_dir_diff_content_diff_dir(tmp_path):
    old = _write_scene(tmp_path / "plan.json", SceneJSON(walls=[_wall("w")]))
    d_old = default_out_dir(old)
    new = _write_scene(tmp_path / "plan.json",
                       SceneJSON(walls=[_wall("w"), _wall("w2")]))  # 同 stem 异内容
    d_new = default_out_dir(new)
    assert d_new != d_old
    assert d_new.parent == d_old.parent == Path("experiments/model")
    assert d_old.name != d_new.name
