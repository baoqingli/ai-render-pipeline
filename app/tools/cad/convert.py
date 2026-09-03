import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.core.config import get_settings
from app.models.tooling import Metrics, ToolError, ToolResult
from app.tools.cache import build_cache_key

# 输出版本/类型/递归/审计——装好 ODA 后用真实版本核对参数顺序
ODA_ARGS = ["ACAD2018", "DXF", "0", "1"]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


async def convert_dwg(dwg_path: str | Path, out_dir: str | Path) -> ToolResult[Path]:
    src, out = Path(dwg_path), Path(out_dir)
    if not src.exists() or src.stat().st_size == 0:  # noqa: ASYNC240
        return ToolResult(
            ok=False,
            error=ToolError(code="INPUT_INVALID", message=f"bad dwg: {src}"),
        )
    key = build_cache_key("convert_dwg", _sha(src), out.name)
    cached = out / (src.stem + ".dxf")
    if cached.exists():  # 简式缓存：产物存在即命中（键含内容 hash，上游换了文件则重建）
        return ToolResult(ok=True, data=cached, cache_key=key, cache_hit=True)
    exe = get_settings().oda_exe
    out.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    try:
        with tempfile.TemporaryDirectory() as td:
            tin = Path(td) / "in"
            tin.mkdir()
            shutil.copy2(src, tin / src.name)
            proc = subprocess.run(  # noqa: ASYNC221
                [exe, str(tin), str(out), *ODA_ARGS], capture_output=True, timeout=300
            )
    except FileNotFoundError:  # exe 不在（POSIX 与 WinError 2 同症）→ ODA_MISSING
        return ToolResult(
            ok=False,
            error=ToolError(code="ODA_MISSING", message=f"ODA File Converter not found: {exe}"),
        )
    if proc.returncode != 0 or not cached.exists():
        return ToolResult(
            ok=False,
            error=ToolError(
                code="INPUT_INVALID",
                message=f"ODA failed rc={proc.returncode}: {proc.stdout[:120]!r}",
            ),
        )
    return ToolResult(ok=True, data=cached, cache_key=key, metrics=Metrics())
