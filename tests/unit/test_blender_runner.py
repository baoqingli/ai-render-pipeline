# tests/unit/test_blender_runner.py
import json
from pathlib import Path

from app.models.scene import SceneJSON, Wall
from app.tools.blender import runner


def _write_scene(p: Path) -> Path:
    scene = SceneJSON(walls=[Wall(id="w1", polygon=[[0.0, 100.0], [6000.0, 100.0],
                                                    [6000.0, -100.0], [0.0, -100.0]])])
    p.write_text(scene.model_dump_json(), encoding="utf-8")
    return p


def _fake_blender_ok(plan_path_arg="--plan"):
    def fake_run(cmd, **kw):
        idx = cmd.index("--plan") + 1 if "--plan" in cmd else None
        plan = json.loads(Path(cmd[idx]).read_text(encoding="utf-8"))
        out = Path(plan["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        for cam in plan["cameras"]:
            for ps in plan["passes"]:
                (out / f"{cam['view_id']}_{ps}.png").write_bytes(b"PNG")
        return type("R", (), {"returncode": 0})()
    return fake_run


async def test_build_success(tmp_path, monkeypatch):
    scene = _write_scene(tmp_path / "s.json")
    monkeypatch.setattr(runner.subprocess, "run", _fake_blender_ok())
    result = await runner.build_white_model(scene, tmp_path / "model",
                                            blender_exe="blender")
    assert result.ok and result.data is not None
    # 新机位：view_iso（等轴测）和可能有view_interior
    assert any((result.data / f"{vid}_white.png").exists()
               for vid in ["view_iso", "view_interior", "view_01"])


async def test_cache_hit_second_call(tmp_path, monkeypatch):
    scene = _write_scene(tmp_path / "s.json")
    runs = []

    def fake_run(cmd, **kw):
        runs.append(1)
        return _fake_blender_ok()(cmd, **kw)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    a = await runner.build_white_model(scene, tmp_path / "model", blender_exe="blender")
    b = await runner.build_white_model(scene, tmp_path / "model", blender_exe="blender")
    assert a.ok and b.ok and b.cache_hit and len(runs) == 1


async def test_blender_missing_not_retryable(tmp_path, monkeypatch):
    scene = _write_scene(tmp_path / "s.json")

    def boom(cmd, **kw):
        raise FileNotFoundError("no blender")

    monkeypatch.setattr(runner.subprocess, "run", boom)
    r = await runner.build_white_model(scene, tmp_path / "model", blender_exe="blender")
    assert not r.ok and r.error.code == "BLENDER_MISSING" and not r.error.retryable


async def test_blender_timeout_retryable(tmp_path, monkeypatch):
    import subprocess

    scene = _write_scene(tmp_path / "s.json")

    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd="blender", timeout=1800)

    monkeypatch.setattr(runner.subprocess, "run", hang)
    r = await runner.build_white_model(scene, tmp_path / "model", blender_exe="blender")
    assert not r.ok and r.error.code == "BLENDER_TIMEOUT" and r.error.retryable


async def test_blender_crash_on_missing_artifacts(tmp_path, monkeypatch):
    scene = _write_scene(tmp_path / "s.json")

    def rc1_no_output(cmd, **kw):
        return type("R", (), {"returncode": 0})()          # rc 0 但什么都没写

    monkeypatch.setattr(runner.subprocess, "run", rc1_no_output)
    r = await runner.build_white_model(scene, tmp_path / "model", blender_exe="blender")
    assert not r.ok and r.error.code == "BLENDER_CRASH" and r.error.retryable


async def test_invalid_scene_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    r = await runner.build_white_model(p, tmp_path / "model", blender_exe="blender")
    assert not r.ok and r.error.code == "INPUT_INVALID"
    missing = await runner.build_white_model(tmp_path / "nope.json", tmp_path / "m",
                                             blender_exe="blender")
    assert not missing.ok and missing.error.code == "INPUT_INVALID"
