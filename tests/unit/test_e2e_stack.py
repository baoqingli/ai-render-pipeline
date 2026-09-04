# tests/unit/test_e2e_stack.py
"""e2e 栈静态冒烟（离线）：入口脚本可导入零副作用 + compose 拓扑钉住。

不连真 PG/Valkey、不 docker——run_api/run_runner 的导入冒烟之所以能证明
「顶层无副作用」，靠的是本套件运行环境没有这两个服务；再用 AST 结构检查
钉住「连接类调用不在模块顶层」，避免门禁机恰好在栈起着的时刻漏检。
"""
import ast
import importlib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "deploy" / "docker-compose.services.yml"

# 顶层禁止出现的调用名（连接/服务启动类）；sys.path.insert 是唯一豁免的
# bootstrap 调用，不在表内
FORBIDDEN_TOP_LEVEL_CALLS = {
    "make_api_deps", "init_pg", "uvicorn.run", "asyncio.run", "server.serve",
    "make_chat_model", "main",
}


def test_run_api_importable() -> None:
    mod = importlib.import_module("scripts.run_api")
    assert callable(mod.main)


def test_run_runner_reexports_graph_runner_main() -> None:
    from app.workers.graph_runner import main as runner_main

    mod = importlib.import_module("scripts.run_runner")
    # 钉住转发关系：入口只做 bootstrap，不复制实现
    assert mod.main is runner_main


def _is_dunder_main_guard(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, ast.If):
        return False
    return ast.unparse(stmt.test) in (
        "__name__ == '__main__'", '__name__ == "__main__"')


def test_entrypoint_top_level_zero_side_effects() -> None:
    for name in ("run_api", "run_runner"):
        tree = ast.parse((REPO / "scripts" / f"{name}.py").read_text(encoding="utf-8"))
        for stmt in tree.body:
            # 函数体与 __main__ guard 内的调用是设计允许的位置；只检查真正的
            # 模块顶层可执行语句（import / sys.path.insert bootstrap / 赋值）
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                continue
            if _is_dunder_main_guard(stmt):
                continue
            for node in ast.walk(stmt):
                if isinstance(node, ast.Call):
                    fn = ast.unparse(node.func)
                    assert fn not in FORBIDDEN_TOP_LEVEL_CALLS, (
                        f"scripts/{name}.py 顶层调用 {fn}()——连接须在 main()/"
                        "__main__ guard 内（导入冒烟的前提）")


def _services() -> dict:
    with COMPOSE.open(encoding="utf-8") as f:
        return yaml.safe_load(f)["services"]


def test_compose_postgres_shape() -> None:
    pg = _services()["postgres"]
    assert pg["image"] == "postgres:16"
    assert pg["environment"] == {"POSTGRES_USER": "arp",
                                 "POSTGRES_PASSWORD": "arp",
                                 "POSTGRES_DB": "arp"}
    assert "5432:5432" in pg["ports"]
    # healthcheck 必须探测 pg_isready（API/runner 起动前 PG 可用的判定依据）
    test_cmd = " ".join(pg["healthcheck"]["test"])
    assert "pg_isready" in test_cmd and "-U arp" in test_cmd


def test_compose_valkey_shape() -> None:
    vk = _services()["valkey"]
    assert vk["image"] == "valkey/valkey:8"
    assert "6379:6379" in vk["ports"]


def test_env_example_documents_stack_keys() -> None:
    # 手册 §1 依赖 .env/.env.example 有这两键——钉住模板不漂移
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    assert "ARP_PG_DSN=postgresql+psycopg://arp:arp@localhost:5432/arp" in text
    assert "ARP_VALKEY_URL=redis://localhost:6379/0" in text
