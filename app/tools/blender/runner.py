"""build_white_model 工具：把 geom 层 BuildPlan 经多个独立 Blender 子进程渲染成白模多 pass PNG。
每个 camera×pass 组合独立启动 Blender，避免合成器跨 pass 污染。
"""
import hashlib
import subprocess
from pathlib import Path

from pydantic import ValidationError

from app.models.scene import SceneJSON
from app.models.tooling import Metrics, ToolError, ToolResult
from app.tools.cache import build_cache_key

SCENE_BUILDER = Path(__file__).resolve().parents[3] / "blender" / "scene_builder.py"
TIMEOUT_S = 600


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


async def build_white_model(scene_json_path: str | Path, out_dir: str | Path,
                            blender_exe: str | None = None) -> ToolResult[Path]:
    from app.core.config import get_settings
    from app.tools.blender.geom import build_plan

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
    # 内视候选相机：深度校验前先入列，校验后择优渲染 white/lineart
    from app.models.build_plan import PlanCamera
    from app.tools.blender.geom import plan_interior_candidates
    extra = [PlanCamera(view_id=cp.view_id, position=list(cp.position),
                        target=list(cp.target))
             for cp in plan_interior_candidates(scene)]
    plan = plan.model_copy(update={"cameras": list(plan.cameras) + extra})
    passes = plan.passes or ["depth", "lineart", "white"]
    key = build_cache_key("build_white_model", _sha(src), out.name, str(len(plan.cameras)))

    out.mkdir(parents=True, exist_ok=True)
    plan_path = out / "build_plan.json"
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")
    exe = blender_exe or get_settings().blender_exe
    calls = 0

    def _render(cam_idx: int, pass_name: str) -> ToolError | None:
        """渲染单相机单 pass；文件已存在则跳过。返回 None 表示成功。"""
        nonlocal calls
        cam = plan.cameras[cam_idx]
        out_file = out / f"{cam.view_id}_{pass_name}.png"
        if out_file.exists():
            return None
        calls += 1
        try:
            proc = subprocess.run(  # 渲染为串行阻塞设计
                [exe, "--background", "--factory-startup",
                 "--python", str(SCENE_BUILDER),
                 "--", "--plan", str(plan_path),
                 "--cam", str(cam_idx),
                 "--pass", pass_name],
                capture_output=True, timeout=TIMEOUT_S)
        except FileNotFoundError:
            return ToolError(code="BLENDER_MISSING",
                             message=f"blender not found: {exe}", retryable=False)
        except subprocess.TimeoutExpired:
            return ToolError(code="BLENDER_TIMEOUT",
                             message=f"blender timed out (> {TIMEOUT_S}s)", retryable=True)
        if proc.returncode != 0 or not out_file.exists():
            def _tail(x: object) -> bytes:
                if isinstance(x, str):
                    return x[-300:].encode("utf-8", "replace")
                return x[-300:] if isinstance(x, bytes) else b""
            detail = (_tail(getattr(proc, "stdout", b"")) + b" | "
                      + _tail(getattr(proc, "stderr", b"")))
            return ToolError(code="BLENDER_CRASH",
                             message=(f"blender rc={proc.returncode} "
                                      f"cam={cam.view_id} pass={pass_name}; {detail!r}"),
                             retryable=True)
        return None

    # 1) depth 全候选先行（内视机位校验依据）
    if "depth" in passes:
        for cam_idx in range(len(plan.cameras)):
            err = _render(cam_idx, "depth")
            if err is not None:
                return ToolResult(ok=False, error=err)

    # 2) 深度校验选优：iso 恒保留；内视候选按深度方差降序保留达标者（兜底保留最优）
    from app.tools.blender.imgstat import gray_stats
    scored: list[tuple[float, int]] = []
    for i in range(1, len(plan.cameras)):
        try:
            st = gray_stats(out / f"{plan.cameras[i].view_id}_depth.png")
        except Exception:  # noqa: BLE001 统计失败按 0 分处理
            st = {"std": 0.0}
        scored.append((float(st["std"]), i))
    scored.sort(reverse=True)
    keep = [0]
    for stdv, i in scored:
        if len(keep) >= 3:
            break
        if stdv >= 8.0 and i not in keep:
            keep.append(i)
    if len(keep) == 1 and scored:
        keep.append(scored[0][1])            # 兜底：宁可有图，质量由 qa 层标记

    # 3) white/lineart 仅渲保留机位
    for cam_idx in keep:
        for pass_name in passes:
            if pass_name == "depth":
                continue
            err = _render(cam_idx, pass_name)
            if err is not None:
                return ToolResult(ok=False, error=err)

    return ToolResult(ok=True, data=out, cache_key=key, cache_hit=(calls == 0),
                      metrics=Metrics())
