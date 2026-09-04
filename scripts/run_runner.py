"""graph-runner 入口（e2e 手册 docs/e2e-2026-09.md）。

用法（仓库根）: uv run python scripts/run_runner.py
仅做 sys.path bootstrap 后调用 app.workers.graph_runner.main()
（Valkey BLPOP 消费 + PG 状态 + 检查点恢复；PG saver 生产升级见手册）。

导入安全：graph_runner 模块顶层同样零副作用（重连接作都在其 main() 内），
本脚本因此可被 tests/unit/test_e2e_stack.py 直接 import 冒烟。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.workers.graph_runner import main

if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
