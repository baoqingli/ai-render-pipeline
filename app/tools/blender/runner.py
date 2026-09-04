"""build_white_model 工具：把 geom 层 BuildPlan 经 Blender 子进程渲染成白模多 pass PNG。"""
import hashlib
import subprocess
from pathlib import Path

from pydantic import ValidationError

from app.models.scene import SceneJSON
from app.models.tooling import Metrics, ToolError, ToolResult
from app.tools.cache import build_cache_key

SCENE_BUILDER = Path(__file__).resolve().parents[3] / "blender" / "scene_builder.py"
TIMEOUT_S = 1800


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


async def build_white_model(scene_json_path: str | Path, out_dir: str | Path,
                            blender_exe: str | None = None) -> ToolResult[Path]:
    from app.core.config import get_settings
    from app.tools.blender.geom import build_plan
    # 绝对路径：Blender 子进程 cwd 与调用方不一致
    src, out = Path(scene_json_path), Path(out_dir).resolve()  # noqa: ASYNC240
    if not src.exists():  # noqa: ASYNC240
        return ToolResult(ok=False, error=ToolError(code="INPUT_INVALID",
                                                    message=f"scene not found: {src}"))
    try:
        scene = SceneJSON.model_validate_json(src.read_text(encoding="utf-8"))  # noqa: ASYNC240
    except (ValueError, ValidationError) as e:
        return ToolResult(ok=False, error=ToolError(code="INPUT_INVALID",
                                                    message=f"bad scene json: {e}"))
    plan = build_plan(scene, str(out))
    expected = [f"{c.view_id}_{p}.png" for c in plan.cameras for p in plan.passes]
    key = build_cache_key("build_white_model", _sha(src), out.name)
    if all((out / n).exists() for n in expected):
        return ToolResult(ok=True, data=out, cache_key=key, cache_hit=True)
    out.mkdir(parents=True, exist_ok=True)
    plan_path = out / "build_plan.json"
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")
    exe = blender_exe or get_settings().blender_exe
    try:
        proc = subprocess.run(  # noqa: ASYNC221
            [exe, "--background", "--factory-startup", "--python", str(SCENE_BUILDER),
             "--", "--plan", str(plan_path)],
            capture_output=True, timeout=TIMEOUT_S)
    except FileNotFoundError:
        return ToolResult(ok=False, error=ToolError(
            code="BLENDER_MISSING", message=f"blender not found: {exe}", retryable=False))
    except subprocess.TimeoutExpired:
        return ToolResult(ok=False, error=ToolError(
            code="BLENDER_TIMEOUT", message=f"blender timed out (> {TIMEOUT_S}s)",
            retryable=True))
    missing = [n for n in expected if not (out / n).exists()]
    if proc.returncode != 0 or missing:
        stdout = getattr(proc, "stdout", None)
        stderr = getattr(proc, "stderr", None)

        def _tail(x: object) -> bytes:
            if isinstance(x, str):
                return x[-150:].encode("utf-8", "replace")
            return x[-150:] if isinstance(x, bytes) else b""

        detail = _tail(stdout) + b" | stderr: " + _tail(stderr)
        return ToolResult(ok=False, error=ToolError(
            code="BLENDER_CRASH",
            message=f"blender rc={proc.returncode}; artifacts_missing={missing}; {detail!r}",
            retryable=True))
    return ToolResult(ok=True, data=out, cache_key=key, metrics=Metrics())
