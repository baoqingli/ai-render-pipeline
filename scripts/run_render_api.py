# scripts/run_render_api.py
"""生成+局部编辑 API 独立轻量入口（不依赖 PG/Valkey）。

用法（仓库根）: uv run python scripts/run_render_api.py [端口]
产物根目录: ARP_OUTPUT_ROOT（默认 output/），产物经 /files/<相对路径> 访问。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    import uvicorn

    from app.api.render_edit import create_render_edit_app
    from app.core.logging import setup_logging

    # Windows 控制台/重定向默认 GBK：模型输出含非 GBK 字符时 print 会崩，
    # 统一强制 UTF-8（errors=replace 双保险）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    setup_logging()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8100
    uvicorn.run(create_render_edit_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
