# app/tools/cache.py
import hashlib

TOOL_VERSIONS: dict[str, str] = {
    "style_agent": "1",
    "assemble_prompt": "1",
    "comfy_render": "1",
    "api_render": "1",
    "convert_dwg": "1",
    "inspect_dxf": "1",
    "parse_scene": "1",
    "build_white_model": "1",
}


def build_cache_key(tool: str, *parts: str | int | None) -> str:
    payload = "|".join(
        [tool, TOOL_VERSIONS.get(tool, "0")] + [str(p) for p in parts if p is not None]
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:20]
