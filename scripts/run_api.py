"""API 服务入口（e2e 手册 docs/e2e-2026-09.md）。

用法（仓库根）: uv run python scripts/run_api.py
装配链: make_api_deps(get_settings()) → create_app(deps) → uvicorn 0.0.0.0:8000

导入安全：模块顶层零副作用（不连 PG/Valkey）——连接在 main() 内建立；
tests/unit/test_e2e_stack.py 的「导入冒烟 + AST 顶层无调用」双检依赖这一点。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main() -> None:
    import uvicorn

    from app.api.app import create_app
    from app.api.deps import make_api_deps
    from app.core.config import get_settings
    from app.core.logging import setup_logging

    setup_logging()
    # 连接建立在 main() 内（PG 建表 + valkey 客户端），不在模块顶层——
    # 顶层连库会让 `python -c "import scripts.run_api"` 之类的冒烟直接挂
    deps = await make_api_deps(get_settings())
    # 不用 uvicorn.run()：它内部自建事件循环，套在 asyncio.run 里会报
    # "cannot be called from a running event loop"；且 deps.engine（AsyncEngine
    # 连接池）绑定建池循环，跨循环复用会炸 "Future attached to a different
    # loop"——Server.serve() 在本循环内驱动，deps 与 app 共享同一循环
    server = uvicorn.Server(uvicorn.Config(
        create_app(deps), host="0.0.0.0", port=8000))
    await server.serve()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
