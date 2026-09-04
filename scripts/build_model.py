"""SceneJSON → 白模控制图 CLI（内部工具）。

用法: uv run python scripts/build_model.py <scene.json> [--out experiments/model]
产出: --out 下 {view_id}_{depth|lineart|white}.png + build_plan.json
默认 out 目录内容寻址: experiments/model/<scene_stem>-<sha8>（sha8 = scene 文件
字节 sha256 前 8 hex）——同内容复跑命中缓存，内容变更换目录、不吃陈旧缓存。
后续: 控制图目录直接可作 Phase 1 渲染输入:
      uv run python scripts/ab_render.py --control-dir <out> --description "..."
"""
import argparse
import asyncio
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.logging import setup_logging
from app.models.scene import SceneJSON
from app.tools.blender.runner import build_white_model


def ensure_scene(path: Path) -> None:
    if not path.exists():
        print(f"[error] scene 不存在: {path}", file=sys.stderr)
        raise SystemExit(2)
    try:
        scene = SceneJSON.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[error] scene.json 无法解析: {e}", file=sys.stderr)
        raise SystemExit(2) from e
    if not scene.walls and not scene.rooms:
        print("[error] scene 无可建模几何（walls/rooms 均空）", file=sys.stderr)
        raise SystemExit(2)


def default_out_dir(scene: Path) -> Path:
    sha8 = hashlib.sha256(scene.read_bytes()).hexdigest()[:8]
    return Path("experiments/model") / f"{scene.stem}-{sha8}"


def main() -> None:
    ap = argparse.ArgumentParser(description="SceneJSON → 白模控制图（内部工具）")
    ap.add_argument("scene", type=Path)
    ap.add_argument("--out", type=Path, default=None,
                    help="输出目录（默认 experiments/model/<stem>-<sha8>，内容寻址）")
    args = ap.parse_args()
    setup_logging()
    ensure_scene(args.scene)
    out = args.out if args.out is not None else default_out_dir(args.scene)
    result = asyncio.run(build_white_model(args.scene, out))
    if not result.ok or result.data is None:
        code = result.error.code if result.error else "?"
        msg = result.error.message if result.error else ""
        print(f"[error] 白模构建失败: {code} {msg}", file=sys.stderr)
        raise SystemExit(2)
    print(f"控制图目录: {result.data}")
    print(f'下一步: uv run python scripts/ab_render.py --control-dir "{result.data}" '
          f'--description "风格描述"')


if __name__ == "__main__":
    main()
