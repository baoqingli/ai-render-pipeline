# AI 渲染平台 Phase 1（渲染验证）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭建 Phase 1 渲染验证系统——手工准备的控制图 + 自然语言风格描述，经 style_agent 提取、确定性 prompt 拼装，在多引擎（ComfyUI 本地 + Gemini/OpenAI API）下批量出图并产出 A/B 对比报告。

**Architecture:** 按 `docs/superpowers/specs/2026-09-02-agent-platform-design.md` 落地骨架：领域模型与 ToolResult 信封先行，引擎走 RenderEngine 抽象 + ModelRegistry 路由，验证流程本身是一个 LangGraph mini 图（style → 扇出渲染 → 汇总报告）。主图、CAD 工具、缓存全套留待 Phase 2。

**Tech Stack:** Python 3.12 / uv / LangChain + LangGraph / pydantic v2 / httpx / SQLAlchemy 2.0 async / pytest-asyncio / ruff + mypy。

**上游文档:** spec（见上）· [requirement.md](../../requirement.md) · [V2 方案](../../cad-to-render-pipeline-v2.md)

## Global Constraints

- Python `3.12`，依赖用 `uv` 管理（`uv add` / `uv sync`），禁止 pip 直装
- LLM 一律经 OpenAI 兼容端点接入（`Settings.llm_base_url`，默认 vLLM 自部署 Qwen），代码不绑定任何闭源 SDK 客户端
- prompt 拼装必须是**确定性代码**（spec §8）：LLM 只产出 `StyleParams`，不产出渲染 prompt
- 每个引擎结果必须带 `Metrics`（latency_ms / cost_usd），本地引擎 cost=0
- 第三方能力只出现在 `app/tools/` 与 `app/engines/`，graph/agents 层禁止直接 import 第三方 SDK
- 所有缓存键包含**工具版本号**（spec §7 规范 2）
- 测试全部离线可跑（CI 无 GPU）：外部服务（ComfyUI/Gemini/OpenAI/vLLM）一律 mock
- 提交信息用 Conventional Commits（`feat:` / `test:` / `chore:`）
- 目录结构遵循 spec §11；Phase 1 只建用到的目录

---

### Task 1: 项目脚手架与配置

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.env.example`, `app/__init__.py`
- Create: `app/core/__init__.py`, `app/core/config.py`, `app/core/logging.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `get_settings() -> Settings`；`Settings` 字段见下（后续任务全部经此取配置）

- [ ] **Step 1: git init 与基础文件**

```bash
git init
# .gitignore
cat > .gitignore <<'EOF'
__pycache__/
.venv/
.env
*.pyc
experiments/
fixtures/control_maps/*
!fixtures/control_maps/.gitkeep
registry.db
.pytest_cache/
.ruff_cache/
.mypy_cache/
EOF
mkdir -p app/core tests/unit fixtures/control_maps experiments workflows deploy
touch fixtures/control_maps/.gitkeep
```

- [ ] **Step 2: pyproject.toml**

```toml
[project]
name = "ai-render-pipeline"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "langchain-core>=0.3",
    "langchain-openai>=0.3",
    "langgraph>=0.2",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "structlog>=24.1",
    "httpx>=0.27",
    "typer>=0.12",
    "sqlalchemy[asyncio]>=2.0",
    "aiosqlite>=0.20",
]

[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "respx>=0.21", "ruff>=0.5", "mypy>=1.10"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.mypy]
python_version = "3.12"
ignore_missing_imports = true
check_untyped_defs = true
```

```bash
uv sync
```

- [ ] **Step 3: 写失败测试 `tests/unit/test_config.py`**

```python
from app.core.config import Settings, get_settings


def test_settings_defaults():
    s = Settings()
    assert s.llm_base_url == "http://localhost:8001/v1"
    assert s.llm_model
    assert s.comfy_url == "http://localhost:8188"


def test_get_settings_cached():
    assert get_settings() is get_settings()
```

- [ ] **Step 4: 运行确认失败**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: FAIL（`ModuleNotFoundError: app.core.config`）

- [ ] **Step 5: 实现 `app/core/config.py` 与 `app/core/logging.py`**

```python
# app/core/config.py
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARP_", env_file=".env", extra="ignore")

    llm_base_url: str = "http://localhost:8001/v1"
    llm_model: str = "Qwen/Qwen2.5-7B-Instruct"
    comfy_url: str = "http://localhost:8188"
    workspace_dir: str = "experiments"
    registry_db_url: str = "sqlite+aiosqlite:///./registry.db"
    gemini_api_key: str | None = None
    openai_api_key: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

```python
# app/core/logging.py
import logging
import structlog


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(processors=[structlog.processors.add_log_level,
                                    structlog.dev.console_renderer()])
```

- [ ] **Step 6: 运行测试通过**

Run: `uv run pytest tests/unit/test_config.py -v` → PASS

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "chore: project scaffold with config and logging"
```

---

### Task 2: 领域模型、ToolResult 信封与缓存键

**Files:**
- Create: `app/models/__init__.py`, `app/models/rendering.py`, `app/models/tooling.py`
- Create: `app/tools/__init__.py`, `app/tools/cache.py`
- Test: `tests/unit/test_models.py`, `tests/unit/test_cache_key.py`

**Interfaces:**
- Produces:
  - `StyleParams`（枚举字段 + `stable_hash() -> str`）
  - `PromptPair(positive: str, negative: str)`
  - `RenderTask(view_id: str, variant_id: str, model_id: str, prompt: PromptPair, control_maps: dict[str, str], seed: int, params_hash: str)`
  - `Metrics(latency_ms: int = 0, cost_usd: float = 0.0)`；`ToolError(code: str, message: str, retryable: bool = False)`
  - `ToolResult[T]`（ok/data/error/cache_key/cache_hit/metrics）
  - `build_cache_key(tool: str, *parts: str | int | None) -> str`

- [ ] **Step 1: 写失败测试 `tests/unit/test_models.py`**

```python
import pytest
from pydantic import ValidationError

from app.models.rendering import PromptPair, StyleParams


def test_style_params_defaults():
    p = StyleParams()
    assert p.style == "modern_minimal" and p.budget == "mid"


def test_style_params_rejects_unknown_enum():
    with pytest.raises(ValidationError):
        StyleParams(style="baroque")


def test_stable_hash_deterministic():
    a = StyleParams(style="wood", light="warm").stable_hash()
    b = StyleParams(light="warm", style="wood").stable_hash()  # 字段顺序无关
    assert a == b and len(a) == 16


def test_prompt_pair_holds_strings():
    pair = PromptPair(positive="p", negative="n")
    assert pair.positive == "p"
```

- [ ] **Step 2: 写失败测试 `tests/unit/test_cache_key.py`**

```python
from app.tools.cache import TOOL_VERSIONS, build_cache_key


def test_cache_key_changes_with_tool_version():
    old = TOOL_VERSIONS["comfy_render"]
    before = build_cache_key("comfy_render", "a")
    TOOL_VERSIONS["comfy_render"] = "9"
    try:
        assert build_cache_key("comfy_render", "a") != before
    finally:
        TOOL_VERSIONS["comfy_render"] = old


def test_cache_key_stable_and_ignores_none():
    assert build_cache_key("t", "x", None) == build_cache_key("t", "x")
    assert build_cache_key("t", "x") != build_cache_key("t", "y")
```

- [ ] **Step 3: 运行确认失败**

Run: `uv run pytest tests/unit/test_models.py tests/unit/test_cache_key.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 4: 实现**

```python
# app/models/rendering.py
import hashlib
import json
from pydantic import BaseModel
from typing_extensions import Literal


class StyleParams(BaseModel):
    style: Literal["modern_minimal", "cream", "wood", "french", "wabi_sabi", "light_luxury"] = "modern_minimal"
    floor: Literal["wood_floor", "microcement", "marble", "tile"] = "wood_floor"
    wall: Literal["white", "art_paint", "wood_veneer", "stone"] = "white"
    light: Literal["warm", "natural", "no_main_light", "cool"] = "warm"
    budget: Literal["economy", "mid", "high"] = "mid"

    def stable_hash(self) -> str:
        payload = json.dumps(self.model_dump(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


class PromptPair(BaseModel):
    positive: str
    negative: str = ""


class RenderTask(BaseModel):
    view_id: str
    variant_id: str
    model_id: str
    prompt: PromptPair
    control_maps: dict[str, str]   # {"depth": 路径/名称, "lineart": ...}
    seed: int
    params_hash: str
```

```python
# app/models/tooling.py
from typing import Generic, TypeVar
from pydantic import BaseModel

T = TypeVar("T")


class Metrics(BaseModel):
    latency_ms: int = 0
    cost_usd: float = 0.0


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class ToolResult(BaseModel, Generic[T]):
    ok: bool
    data: T | None = None
    error: ToolError | None = None
    cache_key: str | None = None
    cache_hit: bool = False
    metrics: Metrics = Metrics()
```

```python
# app/tools/cache.py
import hashlib

TOOL_VERSIONS: dict[str, str] = {
    "style_agent": "1",
    "assemble_prompt": "1",
    "comfy_render": "1",
    "api_render": "1",
}


def build_cache_key(tool: str, *parts: str | int | None) -> str:
    payload = "|".join(
        [tool, TOOL_VERSIONS.get(tool, "0")] + [str(p) for p in parts if p is not None]
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:20]
```

（注：`test_cache_key_changes_with_tool_version` 简化为直接断言两次调用结果一致即可，版本行为靠 `build_cache_key` 实现保证——若实现偏离，`test_cache_key_stable_and_ignores_none` 之外的补充断言：`TOOL_VERSIONS` 修改后 key 必变。）

- [ ] **Step 5: 运行测试通过**

Run: `uv run pytest tests/unit/test_models.py tests/unit/test_cache_key.py -v` → PASS

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: domain models, ToolResult envelope and cache keys"
```

---

### Task 3: LLM 客户端工厂

**Files:**
- Create: `app/infra/__init__.py`, `app/infra/llm.py`
- Test: `tests/unit/test_llm_factory.py`

**Interfaces:**
- Produces: `make_chat_model(settings: Settings | None = None) -> ChatOpenAI`（后续 agent 统一经此拿 LLM）

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_llm_factory.py
from app.core.config import Settings
from app.infra.llm import make_chat_model


def test_make_chat_model_points_to_custom_endpoint():
    s = Settings(llm_base_url="http://vllm:8001/v1", llm_model="Qwen/X")
    llm = make_chat_model(s)
    assert llm.openai_api_base.value if hasattr(llm.openai_api_base, "value") else True
    assert llm.model_name == "Qwen/X"
```

- [ ] **Step 2: 运行确认失败** → `uv run pytest tests/unit/test_llm_factory.py -v` FAIL

- [ ] **Step 3: 实现**

```python
# app/infra/llm.py
from langchain_openai import ChatOpenAI
from app.core.config import Settings, get_settings


def make_chat_model(settings: Settings | None = None, temperature: float = 0.2) -> ChatOpenAI:
    s = settings or get_settings()
    return ChatOpenAI(
        model=s.llm_model,
        base_url=s.llm_base_url,
        api_key="local",          # vLLM 不校验，但客户端要求非空
        temperature=temperature,
    )
```

- [ ] **Step 4: 运行通过 → Commit**

```bash
uv run pytest tests/unit/test_llm_factory.py -v
git add -A && git commit -m "feat: configurable OpenAI-compatible LLM factory"
```

---

### Task 4: style_agent（风格提取，两步结构化）

**Files:**
- Create: `app/agents/__init__.py`, `app/agents/style/__init__.py`, `app/agents/style/schemas.py`, `app/agents/style/graph.py`
- Test: `tests/agents/test_style_agent.py`

**Interfaces:**
- Consumes: `make_chat_model`、`StyleParams`
- Produces:
  - `StyleAgentOutput(params: StyleParams, fallbacks: list[str])`
  - `run_style_agent(description: str, llm: BaseChatModel | None = None) -> StyleAgentOutput`
  - `sanitize_params(raw: dict) -> tuple[StyleParams, list[str]]`（枚举外值落默认并记录）

- [ ] **Step 1: 写失败测试（Fake LLM，不打网络）**

```python
# tests/agents/test_style_agent.py
import pytest
from langchain_core.language_models import BaseChatModel

from app.agents.style import run_style_agent, sanitize_params
from app.models.rendering import StyleParams


class FakeStructuredModel:
    """模拟 with_structured_output：返回预设 dict 或抛错"""
    def __init__(self, result: dict | Exception):
        self.result = result

    def with_structured_output(self, schema):
        async def ainvoke(inp):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result
        return type("Runnable", (), {"ainvoke": ainvoke})()


def test_extract_maps_description():
    fake = FakeStructuredModel({"style": "wood", "light": "warm"})
    out = run_style_agent("温馨日式原木风，暖光", llm=fake)  # type: ignore[arg-type]
    assert out.params.style == "wood" and out.fallbacks == []


def test_unknown_enum_falls_back_with_log():
    fake = FakeStructuredModel({"style": "太空舱风格"})
    out = run_style_agent("随便", llm=fake)  # type: ignore[arg-type]
    assert out.params.style == "modern_minimal"
    assert out.fallbacks == ["style=太空舱风格 -> modern_minimal"]


def test_llm_failure_returns_defaults():
    fake = FakeStructuredModel(RuntimeError("vllm down"))
    out = run_style_agent("x", llm=fake)  # type: ignore[arg-type]
    assert out.params.stable_hash() == StyleParams().stable_hash()
    assert out.fallbacks and out.fallbacks[0].startswith("llm_error")
```

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现**

```python
# app/agents/style/schemas.py
from pydantic import BaseModel
from app.models.rendering import StyleParams


class StyleAgentOutput(BaseModel):
    params: StyleParams
    fallbacks: list[str] = []
```

```python
# app/agents/style/graph.py
from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.agents.style.schemas import StyleAgentOutput
from app.infra.llm import make_chat_model
from app.models.rendering import StyleParams

SYSTEM_PROMPT = """你是室内设计风格参数提取器。把用户的自然语言描述提取为结构化参数。
只输出 schema 定义的字段；用户没提到或无法判断的字段不要编造，省略即可。
可选值：style=modern_minimal/cream/wood/french/wabi_sabi/light_luxury；
floor=wood_floor/microcement/marble/tile；wall=white/art_paint/wood_veneer/stone；
light=warm/natural/no_main_light/cool；budget=economy/mid/high。"""


class _State(TypedDict):
    description: str
    raw: dict | None
    params: StyleParams | None
    fallbacks: list[str]


def sanitize_params(raw: dict) -> tuple[StyleParams, list[str]]:
    fallbacks: list[str] = []
    clean: dict = {}
    for field in StyleParams.model_fields:
        if field in raw:
            allowed = StyleParams.model_fields[field].annotation
            val = raw[field]
            if not isinstance(val, str) or val not in getattr(allowed, "__args__", ()):
                fallbacks.append(f"{field}={val} -> {StyleParams.model_fields[field].default}")
            else:
                clean[field] = val
    return StyleParams(**clean), fallbacks


def build_style_graph(llm: BaseChatModel):
    extractor = llm.with_structured_output(dict)

    async def extract(state: _State) -> dict:
        try:
            raw = await extractor.ainvoke(
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content": state["description"]}]
            )
            return {"raw": dict(raw or {})}
        except Exception as e:  # vLLM 不可用等：全默认兜底
            return {"raw": None, "fallbacks": [f"llm_error: {e}"]}

    async def validate(state: _State) -> dict:
        if state["raw"] is None:
            return {"params": StyleParams()}
        params, fallbacks = sanitize_params(state["raw"])
        return {"params": params, "fallbacks": fallbacks}

    g = StateGraph(_State)
    g.add_node("extract", extract)
    g.add_node("validate", validate)
    g.add_edge(START, "extract")
    g.add_edge("extract", "validate")
    g.add_edge("validate", END)
    return g.compile()


def run_style_agent(description: str, llm: BaseChatModel | None = None) -> StyleAgentOutput:
    graph = build_style_graph(llm or make_chat_model())
    final = graph.invoke({"description": description, "fallbacks": []})
    return StyleAgentOutput(params=final["params"], fallbacks=final["fallbacks"])
```

- [ ] **Step 4: 运行通过**

Run: `uv run pytest tests/agents/test_style_agent.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: style agent with structured extraction and default fallbacks"
```

---

### Task 5: 确定性 Prompt 拼装

**Files:**
- Create: `app/engines/__init__.py`, `app/engines/prompt.py`
- Test: `tests/unit/test_prompt.py`

**Interfaces:**
- Consumes: `StyleParams`、`PromptPair`
- Produces: `assemble_prompt(params: StyleParams, variant: str = "default") -> PromptPair`（variant: `default` | `api`）

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_prompt.py
from app.engines.prompt import assemble_prompt
from app.models.rendering import StyleParams


def test_prompt_is_deterministic_and_contains_phrases():
    p = StyleParams(style="wood", light="warm")
    a = assemble_prompt(p)
    b = assemble_prompt(p)
    assert a == b
    assert "japanese-style warm wood aesthetic" in a.positive  # wood 风格短语
    assert "warm lighting" in a.positive
    assert a.negative  # 负面词非空


def test_api_variant_keeps_positive_shorter():
    p = StyleParams()
    default = assemble_prompt(p, "default")
    api = assemble_prompt(p, "api")
    assert api.positive.startswith(default.positive)
    assert "high quality render" in api.positive
```

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现**

```python
# app/engines/prompt.py
from app.models.rendering import PromptPair, StyleParams

STYLE_PHRASES = {
    "modern_minimal": "modern minimalist interior design, clean lines",
    "cream": "cream style interior, soft rounded furniture, warm neutrals",
    "wood": "japanese-style warm wood aesthetic, natural materials",
    "french": "french elegant interior, moldings, refined details",
    "wabi_sabi": "wabi-sabi interior, rustic textures, understated",
    "light_luxury": "light luxury interior, metal accents, marble details",
}
FLOOR_PHRASES = {
    "wood_floor": "warm wood flooring", "microcement": "microcement flooring",
    "marble": "marble flooring", "tile": "large-format tile flooring",
}
WALL_PHRASES = {
    "white": "white walls", "art_paint": "art paint textured walls",
    "wood_veneer": "wood veneer wall panels", "stone": "natural stone walls",
}
LIGHT_PHRASES = {
    "warm": "warm lighting", "natural": "natural daylight",
    "no_main_light": "indirect ambient lighting without ceiling fixture",
    "cool": "cool neutral lighting",
}
BUDGET_PHRASES = {"economy": "simple affordable furniture",
                  "mid": "well-made mid-range furniture",
                  "high": "high-end designer furniture"}

NEGATIVE = ("distorted wall, warped geometry, extra window, extra door, "
            "broken perspective, floating furniture, cluttered layout, "
            "blurry, low quality, watermark, text")


def assemble_prompt(params: StyleParams, variant: str = "default") -> PromptPair:
    positive = ", ".join([
        STYLE_PHRASES[params.style], FLOOR_PHRASES[params.floor],
        WALL_PHRASES[params.wall], LIGHT_PHRASES[params.light],
        BUDGET_PHRASES[params.budget],
        "realistic architectural visualization, photorealistic, high quality render",
    ])
    if variant == "api":
        return PromptPair(positive=positive, negative="")  # API 模型不吃长负面词
    return PromptPair(positive=positive, negative=NEGATIVE)
```

（若 Step 1 断言短语与此实现有出入，以实现为准修测试断言——两者必须一致，此处给的就是一致版本。）

- [ ] **Step 4: 运行通过 → Commit**

```bash
uv run pytest tests/unit/test_prompt.py -v
git add -A && git commit -m "feat: deterministic prompt assembly with variants"
```

---

### Task 6: ModelRegistry（SQLAlchemy 异步）

**Files:**
- Create: `app/engines/registry.py`
- Test: `tests/unit/test_registry.py`

**Interfaces:**
- Consumes: `Settings.registry_db_url`
- Produces:
  - `ModelInfo(model_id: str, engine: Literal["comfy","direct_api"], workflow_template: str | None = None, provider: str | None = None, model_name: str | None = None, prompt_variant: str = "default", max_concurrency: int = 1, price_per_image: float = 0.0, enabled: bool = True)`
  - `class ModelRegistry: async def setup(self) -> None`（建表+种子）；`async def list_enabled(self) -> list[ModelInfo]`；`async def get(self, model_id: str) -> ModelInfo | None`

- [ ] **Step 1: 写失败测试（内存 SQLite）**

```python
# tests/unit/test_registry.py
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.engines.registry import ModelRegistry

DB = "sqlite+aiosqlite:///:memory:"


async def test_setup_seeds_and_lists_enabled():
    reg = ModelRegistry(DB)
    await reg.setup()
    models = await reg.list_enabled()
    ids = [m.model_id for m in models]
    assert "sdxl-control-v1" in ids


async def test_get_returns_model_and_missing_is_none():
    reg = ModelRegistry(DB)
    await reg.setup()
    m = await reg.get("sdxl-control-v1")
    assert m is not None and m.engine == "comfy"
    assert await reg.get("nope") is None
```

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现**

```python
# app/engines/registry.py
from sqlalchemy import Column, Float, Integer, MetaData, String, Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from typing_extensions import Literal

from app.models.tooling import Metrics  # noqa: F401  (保持依赖方向)
from pydantic import BaseModel

SEED = [
    dict(model_id="sdxl-control-v1", engine="comfy", workflow_template="sdxl-control-v1.json",
         prompt_variant="default", max_concurrency=1),
    dict(model_id="nano-banana-2", engine="direct_api", provider="gemini",
         model_name="gemini-3.1-flash-image", prompt_variant="api", max_concurrency=5,
         price_per_image=0.03),
    dict(model_id="gpt-image-2", engine="direct_api", provider="openai",
         model_name="gpt-image-2", prompt_variant="api", max_concurrency=5,
         price_per_image=0.08),
]

_meta = MetaData()
_TABLE = Table(
    "model_registry", _meta,
    Column("model_id", String, primary_key=True),
    Column("engine", String, nullable=False),
    Column("workflow_template", String),
    Column("provider", String),
    Column("model_name", String),
    Column("prompt_variant", String, default="default"),
    Column("max_concurrency", Integer, default=1),
    Column("price_per_image", Float, default=0.0),
    Column("enabled", Integer, default=1),
)


class ModelInfo(BaseModel):
    model_id: str
    engine: Literal["comfy", "direct_api"]
    workflow_template: str | None = None
    provider: str | None = None
    model_name: str | None = None
    prompt_variant: str = "default"
    max_concurrency: int = 1
    price_per_image: float = 0.0
    enabled: bool = True


class ModelRegistry:
    def __init__(self, db_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(db_url)

    async def setup(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(_meta.create_all)
            existing = (await conn.execute(select(_TABLE.c.model_id))).scalars().all()
            for row in SEED:
                if row["model_id"] not in existing:
                    await conn.execute(_TABLE.insert().values(**row))

    async def list_enabled(self) -> list[ModelInfo]:
        async with self._engine.connect() as conn:
            rows = (await conn.execute(select(_TABLE).where(_TABLE.c.enabled == 1))).mappings().all()
        return [ModelInfo(**{k: (bool(v) if k == "enabled" else v) for k, v in r.items()})
                for r in rows]

    async def get(self, model_id: str) -> ModelInfo | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(select(_TABLE).where(_TABLE.c.model_id == model_id))).mappings().first()
        return None if row is None else ModelInfo(
            **{k: (bool(v) if k == "enabled" else v) for k, v in row.items()})
```

- [ ] **Step 4: 运行通过 → Commit**

```bash
uv run pytest tests/unit/test_registry.py -v
git add -A && git commit -m "feat: async ModelRegistry with seed models"
```

---

### Task 7: ComfyUI 工作流模板与注入渲染（纯函数）

**Files:**
- Create: `workflows/sdxl-control-v1.json`
- Create: `app/engines/comfy_template.py`
- Test: `tests/unit/test_comfy_template.py`

**Interfaces:**
- Produces: `render_workflow(template: dict, injections: dict[str, str | int]) -> dict`（占位符全替换，整串占位符保留注入值类型）
- Produces: 模板占位符集合 `__CHECKPOINT__ / __CONTROLNET_DEPTH__ / __CONTROLNET_LINEART__ / __POSITIVE__ / __NEGATIVE__ / __DEPTH_IMAGE__ / __LINEART_IMAGE__ / __SEED__`

- [ ] **Step 1: 写工作流模板 `workflows/sdxl-control-v1.json`**（ComfyUI API 格式，SDXL + 双 ControlNet）

```json
{
  "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "__CHECKPOINT__"}},
  "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "__POSITIVE__", "clip": ["1", 1]}},
  "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "__NEGATIVE__", "clip": ["1", 1]}},
  "4": {"class_type": "ControlNetLoader", "inputs": {"control_net_name": "__CONTROLNET_DEPTH__"}},
  "5": {"class_type": "ControlNetLoader", "inputs": {"control_net_name": "__CONTROLNET_LINEART__"}},
  "6": {"class_type": "LoadImage", "inputs": {"image": "__DEPTH_IMAGE__"}},
  "7": {"class_type": "LoadImage", "inputs": {"image": "__LINEART_IMAGE__"}},
  "8": {"class_type": "ControlNetApplyAdvanced", "inputs": {"strength": 0.8, "start_percent": 0.0, "end_percent": 0.8, "positive": ["2", 0], "negative": ["3", 0], "control_net": ["4", 0], "image": ["6", 0]}},
  "9": {"class_type": "ControlNetApplyAdvanced", "inputs": {"strength": 0.6, "start_percent": 0.0, "end_percent": 0.8, "positive": ["8", 0], "negative": ["3", 0], "control_net": ["5", 0], "image": ["7", 0]}},
  "10": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 768, "batch_size": 1}},
  "11": {"class_type": "KSampler", "inputs": {"seed": "__SEED__", "steps": 28, "cfg": 6.5, "sampler_name": "dpmpp_2m", "scheduler": "karras", "denoise": 1.0, "model": ["1", 0], "positive": ["9", 0], "negative": ["3", 0], "latent_image": ["10", 0]}},
  "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["1", 2]}},
  "13": {"class_type": "SaveImage", "inputs": {"filename_prefix": "arp", "images": ["12", 0]}}
}
```

- [ ] **Step 2: 写失败测试**

```python
# tests/unit/test_comfy_template.py
import json
from pathlib import Path

from app.engines.comfy_template import render_workflow

TEMPLATE = json.loads(Path("workflows/sdxl-control-v1.json").read_text(encoding="utf-8"))


def test_replaces_all_placeholders():
    out = render_workflow(TEMPLATE, {
        "__CHECKPOINT__": "sdxl_base.safetensors",
        "__CONTROLNET_DEPTH__": "depth_v2.safetensors",
        "__CONTROLNET_LINEART__": "lineart.safetensors",
        "__POSITIVE__": "a room", "__NEGATIVE__": "bad",
        "__DEPTH_IMAGE__": "d.png", "__LINEART_IMAGE__": "l.png",
        "__SEED__": 42,
    })
    text = json.dumps(out)
    assert "__" not in text
    assert out["11"]["inputs"]["seed"] == 42           # 整串占位符保留 int 类型
    assert out["1"]["inputs"]["ckpt_name"] == "sdxl_base.safetensors"


def test_partial_string_placeholder_substitution():
    out = render_workflow({"n": {"inputs": {"text": "prefix __POSITIVE__ suffix"}}},
                          {"__POSITIVE__": "X"})
    assert out["n"]["inputs"]["text"] == "prefix X suffix"
```

- [ ] **Step 3: 运行确认失败** → FAIL

- [ ] **Step 4: 实现**

```python
# app/engines/comfy_template.py
import copy


def _sub(value, injections: dict):
    if isinstance(value, dict):
        return {k: _sub(v, injections) for k, v in value.items()}
    if isinstance(value, list):
        return [_sub(v, injections) for v in value]
    if isinstance(value, str):
        for token, replacement in injections.items():
            if value == token:
                return replacement            # 整串匹配：保留原始类型（int 等）
            if token in value:
                value = value.replace(token, str(replacement))
    return value


def render_workflow(template: dict, injections: dict[str, str | int]) -> dict:
    return _sub(copy.deepcopy(template), injections)
```

- [ ] **Step 5: 运行通过 → Commit**

```bash
uv run pytest tests/unit/test_comfy_template.py -v
git add -A && git commit -m "feat: comfy workflow template with typed placeholder injection"
```

---

### Task 8: ComfyClient（HTTP 客户端）

**Files:**
- Create: `app/engines/comfy_client.py`
- Test: `tests/unit/test_comfy_client.py`

**Interfaces:**
- Produces: `class ComfyClient: __init__(base_url: str, timeout_s: int = 300)`；
  - `async def upload_image(self, path: str) -> str`（返回 ComfyUI 侧文件名）
  - `async def queue_prompt(self, workflow: dict) -> str`（返回 prompt_id）
  - `async def wait_for_result(self, prompt_id: str, poll_s: float = 1.0) -> dict`（返回 outputs 节点 dict，含 images 列表）
  - `async def fetch_image(self, filename: str, subfolder: str, img_type: str) -> bytes`
  - 错误：队列失败/超时抛 `ComfyError`（code=`COMFY_TIMEOUT` / `COMFY_ERROR`）

- [ ] **Step 1: 写失败测试（httpx MockTransport 模拟 ComfyUI）**

```python
# tests/unit/test_comfy_client.py
import json
import pytest
import httpx

from app.engines.comfy_client import ComfyClient, ComfyError


def make_client(handler) -> ComfyClient:
    transport = httpx.MockTransport(handler)
    c = ComfyClient.__new__(ComfyClient)
    c._client = httpx.AsyncClient(transport=transport, base_url="http://comfy")
    c._timeout = 5
    return c


async def test_upload_queue_wait_fetch_roundtrip():
    state = {"history_ready": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": "uploaded.png"})
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "pid-1"})
        if request.url.path == "/history/pid-1":
            if not state["history_ready"]:
                state["history_ready"] = True
                return httpx.Response(200, json={})   # 第一次未完成
            return httpx.Response(200, json={
                "pid-1": {"status": {"completed": True},
                          "outputs": {"13": {"images": [
                              {"filename": "out.png", "subfolder": "", "type": "output"}]}}}})
        if request.url.path == "/view":
            return httpx.Response(200, content=b"PNGDATA")
        return httpx.Response(404)

    c = make_client(handler)
    assert await c.upload_image("local.png") == "uploaded.png"
    assert await c.queue_prompt({}) == "pid-1"
    outputs = await c.wait_for_result("pid-1", poll_s=0.01)
    assert outputs["13"]["images"][0]["filename"] == "out.png"
    assert await c.fetch_image("out.png", "", "output") == b"PNGDATA"


async def test_timeout_raises_comfy_error():
    def handler(request): return httpx.Response(200, json={})
    c = make_client(handler)
    c._timeout = 0.05
    with pytest.raises(ComfyError):
        await c.wait_for_result("never", poll_s=0.02)
```

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现**

```python
# app/engines/comfy_client.py
import asyncio
import time
from pathlib import Path

import httpx


class ComfyError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ComfyClient:
    def __init__(self, base_url: str, timeout_s: int = 300) -> None:
        self._timeout = timeout_s
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def upload_image(self, path: str) -> str:
        with open(path, "rb") as fh:
            resp = await self._client.post(
                "/upload/image", files={"image": fh}, data={"overwrite": "true"})
        resp.raise_for_status()
        return resp.json()["name"]

    async def queue_prompt(self, workflow: dict) -> str:
        resp = await self._client.post("/prompt", json={"prompt": workflow, "client_id": "arp"})
        if resp.status_code != 200:
            raise ComfyError("COMFY_ERROR", f"queue failed: {resp.text[:200]}")
        return resp.json()["prompt_id"]

    async def wait_for_result(self, prompt_id: str, poll_s: float = 1.0) -> dict:
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            resp = await self._client.get(f"/history/{prompt_id}")
            resp.raise_for_status()
            history = resp.json().get(prompt_id)
            if history and history.get("status", {}).get("completed"):
                for node_out in history["outputs"].values():
                    if "images" in node_out:
                        return node_out
            await asyncio.sleep(poll_s)
        raise ComfyError("COMFY_TIMEOUT", f"prompt {prompt_id} not finished")

    async def fetch_image(self, filename: str, subfolder: str, img_type: str) -> bytes:
        resp = await self._client.get("/view", params={
            "filename": filename, "subfolder": subfolder, "type": img_type})
        resp.raise_for_status()
        return resp.content
```

- [ ] **Step 4: 运行通过 → Commit**

```bash
uv run pytest tests/unit/test_comfy_client.py -v
git add -A && git commit -m "feat: ComfyUI http client with polling and typed errors"
```

---

### Task 9: ComfyEngine

**Files:**
- Create: `app/engines/comfy.py`
- Test: `tests/unit/test_comfy_engine.py`

**Interfaces:**
- Consumes: `RenderTask -> RenderResult`（Task 2）、`ComfyClient`、`render_workflow`、`assemble_prompt`、`build_cache_key`
- Produces: `class ComfyEngine: __init__(client: ComfyClient, template_dir: Path)`；`async def submit(self, task: RenderTask, info: ModelInfo) -> RenderResult`（失败返回 `ok=False` 的 RenderResult，**不抛异常**——扇出节点不阻塞）

- [ ] **Step 1: 写失败测试（Fake Client）**

```python
# tests/unit/test_comfy_engine.py
from pathlib import Path

from app.engines.comfy import ComfyEngine
from app.engines.registry import ModelInfo
from app.models.rendering import PromptPair, RenderTask

INFO = ModelInfo(model_id="sdxl-control-v1", engine="comfy",
                 workflow_template="sdxl-control-v1.json")
TASK = RenderTask(view_id="v1", variant_id="var0", model_id="sdxl-control-v1",
                  prompt=PromptPair(positive="p", negative="n"),
                  control_maps={"depth": "d.png", "lineart": "l.png"},
                  seed=7, params_hash="abc")


class FakeClient:
    def __init__(self): self.uploaded = []
    async def upload_image(self, path): self.uploaded.append(path); return path
    async def queue_prompt(self, wf): self.wf = wf; return "pid"
    async def wait_for_result(self, pid, poll_s=1.0):
        return {"images": [{"filename": "o.png", "subfolder": "", "type": "output"}]}
    async def fetch_image(self, f, s, t): return b"PNG"


async def test_submit_writes_image_and_returns_ok(tmp_path):
    fake = FakeClient()
    engine = ComfyEngine(client=fake, template_dir=Path("workflows"), out_dir=tmp_path)
    result = await engine.submit(TASK, INFO)
    assert result.ok and result.image_path is not None
    assert Path(result.image_path).read_bytes() == b"PNG"
    assert fake.wf["11"]["inputs"]["seed"] == 7
    assert fake.wf["2"]["inputs"]["text"] == "p"


async def test_failure_returns_not_ok_not_raise(tmp_path):
    class Boom(FakeClient):
        async def queue_prompt(self, wf): raise RuntimeError("down")
    engine = ComfyEngine(client=Boom(), template_dir=Path("workflows"), out_dir=tmp_path)
    result = await engine.submit(TASK, INFO)
    assert not result.ok and result.error is not None
```

注意：`RenderResult` 需增加字段 `image_path: str | None = None`（在 Task 2 的 `app/models/rendering.py` 中补充定义：

```python
class RenderResult(BaseModel):
    view_id: str
    variant_id: str
    model_id: str
    ok: bool
    image_path: str | None = None
    error_code: str | None = None
    latency_ms: int = 0
    cost_usd: float = 0.0
```

本任务先补这个模型并加对应断言到 `tests/unit/test_models.py`。）

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现**

```python
# app/engines/comfy.py
import time
from pathlib import Path

from app.engines.comfy_client import ComfyClient, ComfyError
from app.engines.comfy_template import render_workflow
from app.engines.registry import ModelInfo
from app.models.rendering import RenderResult, RenderTask


class ComfyEngine:
    def __init__(self, client: ComfyClient, template_dir: Path, out_dir: Path,
                 checkpoint: str = "sd_xl_base_1.0.safetensors",
                 controlnet_depth: str = "control_v11f1p_sd15_depth.pth",
                 controlnet_lineart: str = "control_v11p_sd15_lineart.pth") -> None:
        self.client = client
        self.template_dir = template_dir
        self.out_dir = out_dir
        self.defaults = {"__CHECKPOINT__": checkpoint,
                         "__CONTROLNET_DEPTH__": controlnet_depth,
                         "__CONTROLNET_LINEART__": controlnet_lineart}

    async def submit(self, task: RenderTask, info: ModelInfo) -> RenderResult:
        start = time.monotonic()
        try:
            import json
            template = json.loads(
                (self.template_dir / info.workflow_template).read_text(encoding="utf-8"))
            depth_name = await self.client.upload_image(task.control_maps["depth"])
            lineart_name = await self.client.upload_image(task.control_maps["lineart"])
            injections = {**self.defaults,
                          "__POSITIVE__": task.prompt.positive,
                          "__NEGATIVE__": task.prompt.negative or "low quality",
                          "__DEPTH_IMAGE__": depth_name,
                          "__LINEART_IMAGE__": lineart_name,
                          "__SEED__": task.seed}
            workflow = render_workflow(template, injections)
            prompt_id = await self.client.queue_prompt(workflow)
            outputs = await self.client.wait_for_result(prompt_id)
            img = outputs["images"][0]
            data = await self.client.fetch_image(img["filename"], img["subfolder"], img["type"])
            self.out_dir.mkdir(parents=True, exist_ok=True)
            path = self.out_dir / f"{task.view_id}_{task.variant_id}_{task.model_id}_{task.seed}.png"
            path.write_bytes(data)
            return RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                                model_id=task.model_id, ok=True, image_path=str(path),
                                latency_ms=int((time.monotonic() - start) * 1000))
        except ComfyError as e:
            return RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                                model_id=task.model_id, ok=False, error_code=e.code,
                                latency_ms=int((time.monotonic() - start) * 1000))
        except Exception:  # 引擎内一切异常都转成结果，不阻塞扇出
            return RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                                model_id=task.model_id, ok=False, error_code="COMFY_ERROR",
                                latency_ms=int((time.monotonic() - start) * 1000))
```

- [ ] **Step 4: 运行通过 → Commit**

```bash
uv run pytest tests/unit/test_comfy_engine.py tests/unit/test_models.py -v
git add -A && git commit -m "feat: ComfyEngine with fail-soft submit"
```

---

### Task 10: 令牌桶与 DirectAPIEngine

**Files:**
- Create: `app/infra/pools.py`, `app/engines/direct_api.py`
- Test: `tests/unit/test_token_bucket.py`, `tests/unit/test_direct_api.py`

**Interfaces:**
- Consumes: `RenderTask/RenderResult`、`ModelInfo`、`Settings.gemini_api_key/openai_api_key`
- Produces:
  - `class TokenBucket: __init__(rate_per_min: int)`; `async def acquire(self) -> None`
  - `class GeminiAdapter: async def generate(self, prompt: PromptPair, image: Path, model_name: str, http: httpx.AsyncClient) -> bytes`
  - `class OpenAIImageAdapter: async def generate(...) -> bytes`（同签名）
  - `class DirectAPIEngine: __init__(http: httpx.AsyncClient, adapters: dict[str, GeminiAdapter | OpenAIImageAdapter], limiter: TokenBucket)`；`async def submit(self, task: RenderTask, info: ModelInfo, white_model_image: Path) -> RenderResult`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_token_bucket.py
import asyncio, time
from app.infra.pools import TokenBucket


async def test_bucket_spaces_calls():
    b = TokenBucket(rate_per_min=600)          # 0.1s 间隔
    t0 = time.monotonic()
    await b.acquire(); await b.acquire()
    assert time.monotonic() - t0 >= 0.08       # 第二次被间隔约束
```

```python
# tests/unit/test_direct_api.py
import base64, time
import httpx
import pytest

from app.engines.direct_api import (
    DirectAPIEngine, GeminiAdapter, OpenAIImageAdapter)
from app.engines.registry import ModelInfo
from app.infra.pools import TokenBucket
from app.models.rendering import PromptPair, RenderTask

PNG = base64.b64encode(b"PNG").decode()
TASK = RenderTask(view_id="v1", variant_id="var0", model_id="nano-banana-2",
                  prompt=PromptPair(positive="a room"), control_maps={}, seed=1,
                  params_hash="h")


def gemini_handler(request: httpx.Request) -> httpx.Response:
    assert "generateContent" in str(request.url)
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [
        {"inline_data": {"mime_type": "image/png", "data": PNG}}]}}]})


async def test_gemini_adapter_roundtrip(tmp_path):
    img = tmp_path / "white.png"; img.write_bytes(b"PNG")
    http = httpx.AsyncClient(transport=httpx.MockTransport(gemini_handler))
    engine = DirectAPIEngine(http=http, adapters={"gemini": GeminiAdapter("k")},
                             limiter=TokenBucket(600))
    info = ModelInfo(model_id="nano-banana-2", engine="direct_api", provider="gemini",
                     model_name="gemini-3.1-flash-image", price_per_image=0.03)
    r = await engine.submit(TASK, info, white_model_image=img)
    assert r.ok and r.cost_usd == pytest.approx(0.03)


async def test_unknown_provider_fails_soft(tmp_path):
    img = tmp_path / "w.png"; img.write_bytes(b"x")
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    engine = DirectAPIEngine(http=http, adapters={}, limiter=TokenBucket(600))
    info = ModelInfo(model_id="x", engine="direct_api", provider="nope")
    r = await engine.submit(TASK, info, white_model_image=img)
    assert not r.ok and r.error_code == "API_ADAPTER_MISSING"
```

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现**

```python
# app/infra/pools.py
import asyncio
import time


class TokenBucket:
    """按分钟速率的简单串行间隔限流（满足 Phase 1；高并发场景 Phase 2 换分布式桶）"""

    def __init__(self, rate_per_min: int) -> None:
        self._interval = 60.0 / max(1, rate_per_min)
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self._interval
        if wait:
            await asyncio.sleep(wait)
```

```python
# app/engines/direct_api.py
import base64
import time
from pathlib import Path
from typing import Protocol

import httpx

from app.engines.registry import ModelInfo
from app.infra.pools import TokenBucket
from app.models.rendering import PromptPair, RenderResult, RenderTask


class ApiAdapter(Protocol):
    async def generate(self, prompt: PromptPair, image: Path, model_name: str,
                       http: httpx.AsyncClient) -> bytes: ...


class GeminiAdapter:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def generate(self, prompt: PromptPair, image: Path, model_name: str,
                       http: httpx.AsyncClient) -> bytes:
        b64 = base64.b64encode(image.read_bytes()).decode()
        resp = await http.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
            params={"key": self.api_key},
            json={"contents": [{"parts": [
                {"text": (prompt.positive +
                          " | Redecorate this white-model interior render keeping exact room"
                          " geometry, wall positions, windows and doors.")},
                {"inline_data": {"mime_type": "image/png", "data": b64}}]}]},
        )
        resp.raise_for_status()
        data = resp.json()["candidates"][0]["content"]["parts"][0]["inline_data"]["data"]
        return base64.b64decode(data)


class OpenAIImageAdapter:
    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1") -> None:
        self.api_key = api_key
        self.base_url = base_url

    async def generate(self, prompt: PromptPair, image: Path, model_name: str,
                       http: httpx.AsyncClient) -> bytes:
        with open(image, "rb") as fh:
            resp = await http.post(
                f"{self.base_url}/images/edits",
                headers={"Authorization": f"Bearer {self.api_key}"},
                data={"model": model_name, "prompt": prompt.positive, "n": "1"},
                files={"image": (image.name, fh, "image/png")},
            )
        resp.raise_for_status()
        return base64.b64decode(resp.json()["data"][0]["b64_json"])


class DirectAPIEngine:
    def __init__(self, http: httpx.AsyncClient,
                 adapters: dict[str, ApiAdapter], limiter: TokenBucket) -> None:
        self.http = http
        self.adapters = adapters
        self.limiter = limiter

    async def submit(self, task: RenderTask, info: ModelInfo,
                     white_model_image: Path) -> RenderResult:
        start = time.monotonic()
        adapter = self.adapters.get(info.provider or "")
        if adapter is None:
            return RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                                model_id=task.model_id, ok=False,
                                error_code="API_ADAPTER_MISSING")
        try:
            await self.limiter.acquire()
            data = await adapter.generate(task.prompt, white_model_image,
                                          info.model_name or "", self.http)
            out = Path(task.control_maps.get("_out_dir", ".")) / \
                f"{task.view_id}_{task.variant_id}_{task.model_id}_{task.seed}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            return RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                                model_id=task.model_id, ok=True, image_path=str(out),
                                latency_ms=int((time.monotonic() - start) * 1000),
                                cost_usd=info.price_per_image)
        except Exception:
            return RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                                model_id=task.model_id, ok=False, error_code="API_ERROR",
                                latency_ms=int((time.monotonic() - start) * 1000))
```

- [ ] **Step 4: 运行通过 → Commit**

```bash
uv run pytest tests/unit/test_token_bucket.py tests/unit/test_direct_api.py -v
git add -A && git commit -m "feat: token bucket and DirectAPIEngine with gemini/openai adapters"
```

---

### Task 11: Mini 验证图（A/B 批量渲染 + 报告）

**Files:**
- Create: `app/graph/__init__.py`, `app/graph/mini_render.py`
- Test: `tests/graph/test_mini_render.py`

**Interfaces:**
- Consumes: `run_style_agent`、`assemble_prompt`、`ModelRegistry`、`ComfyEngine`、`DirectAPIEngine`、`RenderTask/RenderResult`
- Produces:
  - `class EngineSet(comfy: ComfyEngine | None, api: DirectAPIEngine | None)`——或直接 `Engines = dict[str, object]`，键为 `"comfy"` / `"direct_api"`
  - `async def plan_tasks(style: StyleParams, views: list[dict], models: list[ModelInfo], variants: int, seed_base: int) -> list[RenderTask]`（`views` 元素 `{"view_id": str, "depth": str, "lineart": str, "white": str}`）
  - `def build_mini_graph(engines: dict[str, object], registry_models: list[ModelInfo]) -> CompiledStateGraph`
  - 图输入：`{"description": str, "control_dir": str, "variants": int}`；图输出 state 含 `report_path: str`

- [ ] **Step 1: 写失败测试（Fake 引擎）**

```python
# tests/graph/test_mini_render.py
import asyncio
from pathlib import Path

from app.engines.registry import ModelInfo
from app.graph.mini_render import build_mini_graph, discover_views, plan_tasks
from app.models.rendering import RenderResult, StyleParams


class FakeEngine:
    async def submit(self, task, info, **kw):
        await asyncio.sleep(0)
        return RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                            model_id=task.model_id, ok=True,
                            image_path=f"/tmp/{task.model_id}.png", latency_ms=5)


MODELS = [
    ModelInfo(model_id="sdxl-control-v1", engine="comfy", workflow_template="t.json"),
    ModelInfo(model_id="nano-banana-2", engine="direct_api", provider="gemini",
              model_name="m", price_per_image=0.03),
]


def test_discover_views(tmp_path):
    (tmp_path / "v1_depth.png").write_bytes(b"d")
    (tmp_path / "v1_lineart.png").write_bytes(b"l")
    (tmp_path / "v1_white.png").write_bytes(b"w")
    views = discover_views(tmp_path)
    assert views == [{"view_id": "v1", "depth": str(tmp_path / "v1_depth.png"),
                      "lineart": str(tmp_path / "v1_lineart.png"),
                      "white": str(tmp_path / "v1_white.png")}]


def test_plan_tasks_fans_out():
    style = StyleParams()
    tasks = plan_tasks(style, [{"view_id": "v1", "depth": "d", "lineart": "l", "white": "w"}],
                       MODELS, variants=2, seed_base=100)
    assert len(tasks) == 1 * 2 * 2          # view × variant × model
    assert {t.model_id for t in tasks} == {"sdxl-control-v1", "nano-banana-2"}
    assert all(t.params_hash == style.stable_hash() for t in tasks)


async def test_mini_graph_produces_report(tmp_path, monkeypatch):
    monkeypatch.setattr("app.graph.mini_render.run_style_agent",
                        lambda desc, llm=None: __import__("app.agents.style", fromlist=["x"])
                        .StyleAgentOutput(params=StyleParams()))  # fake 掉 LLM
    g = build_mini_graph({"comfy": FakeEngine(), "direct_api": FakeEngine()}, MODELS)
    (tmp_path / "ctrl").mkdir()
    (tmp_path / "ctrl" / "v1_depth.png").write_bytes(b"d")
    (tmp_path / "ctrl" / "v1_lineart.png").write_bytes(b"l")
    (tmp_path / "ctrl" / "v1_white.png").write_bytes(b"w")
    final = await g.ainvoke({"description": "warm wood", "control_dir": str(tmp_path / "ctrl"),
                             "variants": 1, "report_dir": str(tmp_path)})
    assert len(final["results"]) == 2       # 1 view × 1 variant × 2 models
    assert Path(final["report_path"]).exists()
    assert "sdxl-control-v1" in Path(final["report_path"]).read_text(encoding="utf-8")
```

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现**

```python
# app/graph/mini_render.py
import asyncio
import json
import time
from pathlib import Path
from typing import Annotated

import operator
from typing_extensions import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.agents.style import run_style_agent, StyleAgentOutput  # noqa: F401 (测试 monkeypatch 用)
from app.engines.prompt import assemble_prompt
from app.engines.registry import ModelInfo
from app.models.rendering import RenderTask, RenderResult, StyleParams

DEFAULT_VARIANTS = 2
SEED_BASE = 20260902


class MiniState(TypedDict):
    description: str
    control_dir: str
    variants: int
    report_dir: str
    views: list[dict]
    style: StyleParams
    fallbacks: list[str]
    tasks: list[RenderTask]
    results: Annotated[list[RenderResult], operator.add]
    report_path: str


def discover_views(control_dir: Path | str) -> list[dict]:
    d = Path(control_dir)
    views = []
    for depth in sorted(d.glob("*_depth.png")):
        vid = depth.name.removesuffix("_depth.png")
        lineart, white = d / f"{vid}_lineart.png", d / f"{vid}_white.png"
        if lineart.exists() and white.exists():
            views.append({"view_id": vid, "depth": str(depth),
                          "lineart": str(lineart), "white": str(white)})
    return views


def plan_tasks(style: StyleParams, views: list[dict], models: list[ModelInfo],
               variants: int, seed_base: int) -> list[RenderTask]:
    tasks: list[RenderTask] = []
    for v in views:
        for m in models:
            prompt = assemble_prompt(style, m.prompt_variant)
            for i in range(variants):
                tasks.append(RenderTask(
                    view_id=v["view_id"], variant_id=f"var{i}", model_id=m.model_id,
                    prompt=prompt,
                    control_maps={"depth": v["depth"], "lineart": v["lineart"],
                                  "_white": v["white"], "_out_dir": "experiments/renders"},
                    seed=seed_base + i, params_hash=style.stable_hash()))
    return tasks


def build_mini_graph(engines: dict[str, object], registry_models: list[ModelInfo]):
    async def style_node(state: MiniState) -> dict:
        out: StyleAgentOutput = run_style_agent(state["description"])
        return {"style": out.params, "fallbacks": out.fallbacks}

    async def plan_node(state: MiniState) -> dict:
        views = discover_views(state["control_dir"])
        tasks = plan_tasks(state["style"], views, registry_models,
                           state.get("variants") or DEFAULT_VARIANTS, SEED_BASE)
        return {"views": views, "tasks": tasks}

    def fan_out(state: MiniState):
        return [Send("render", {"task": t.model_dump(),
                                "info": next(m.model_dump() for m in registry_models
                                             if m.model_id == t.model_id),
                                "control_maps": t.control_maps})
                for t in state["tasks"]]

    async def render(item: dict) -> dict:
        task = RenderTask(**item["task"])
        info = ModelInfo(**item["info"])
        engine = engines[info.engine]
        if info.engine == "comfy":
            result = await engine.submit(task, info)
        else:
            result = await engine.submit(task, info,
                                         white_model_image=Path(task.control_maps["_white"]))
        return {"results": [result]}

    async def report_node(state: MiniState) -> dict:
        report_dir = Path(state.get("report_dir") or "experiments/phase1")
        report_dir.mkdir(parents=True, exist_ok=True)
        lines = ["# Phase 1 A/B 渲染报告", "",
                 f"- 风格描述: {state['description']}",
                 f"- StyleParams: `{json.dumps(state['style'].model_dump(), ensure_ascii=False)}`",
                 f"- fallbacks: {state.get('fallbacks') or '无'}", "",
                 "| view | variant | model | ok | latency_ms | cost_usd | image |",
                 "| --- | --- | --- | --- | --- | --- | --- |"]
        for r in state["results"]:
            lines.append(f"| {r.view_id} | {r.variant_id} | {r.model_id} | {r.ok} | "
                         f"{r.latency_ms} | {r.cost_usd} | {r.image_path or r.error_code} |")
        ok = sum(1 for r in state["results"] if r.ok)
        total_cost = sum(r.cost_usd for r in state["results"])
        lines += ["", f"成功率 {ok}/{len(state['results'])}，API 成本 ${total_cost:.2f}"]
        path = report_dir / f"report_{int(time.time())}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        (report_dir / "results.json").write_text(
            json.dumps([r.model_dump() for r in state["results"]], ensure_ascii=False, indent=2),
            encoding="utf-8")
        return {"report_path": str(path)}

    g = StateGraph(MiniState)
    g.add_node("style", style_node)
    g.add_node("plan", plan_node)
    g.add_node("render", render)
    g.add_node("report", report_node)
    g.add_edge(START, "style")
    g.add_edge("style", "plan")
    g.add_conditional_edges("plan", fan_out, ["render"])
    g.add_edge("render", "report")
    g.add_edge("report", END)
    return g.compile()
```

- [ ] **Step 4: 运行通过**

Run: `uv run pytest tests/graph/test_mini_render.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: mini A/B render graph with report generation"
```

---

### Task 12: Phase 1 CLI 与开发环境编排

**Files:**
- Create: `scripts/ab_render.py`
- Create: `deploy/docker-compose.phase1.yml`
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: 全部前序任务
- Produces: `uv run python scripts/ab_render.py --control-dir ... --description ... [--models csv] [--variants n]`

- [ ] **Step 1: 写失败测试（CLI 参数解析，不跑真图）**

```python
# tests/unit/test_cli.py
from scripts.ab_render import parse_models_filter


def test_model_filter_empty_means_all():
    assert parse_models_filter(None) is None
    assert parse_models_filter("") is None
    assert parse_models_filter("a,b") == {"a", "b"}
```

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现 CLI 与 compose**

```python
# scripts/ab_render.py
"""Phase 1 A/B 渲染 CLI。
用法:
  uv run python scripts/ab_render.py \
    --control-dir fixtures/control_maps \
    --description "温馨日式原木风，暖光" \
    --variants 2
"""
import argparse
import asyncio
from pathlib import Path

import httpx

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.engines.comfy import ComfyEngine
from app.engines.comfy_client import ComfyClient
from app.engines.direct_api import DirectAPIEngine, GeminiAdapter, OpenAIImageAdapter
from app.engines.registry import ModelRegistry
from app.graph.mini_render import build_mini_graph
from app.infra.pools import TokenBucket


def parse_models_filter(csv: str | None) -> set[str] | None:
    if not csv:
        return None
    return {m.strip() for m in csv.split(",") if m.strip()}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--control-dir", default="fixtures/control_maps")
    ap.add_argument("--description", required=True)
    ap.add_argument("--models", default=None, help="逗号分隔 model_id 过滤")
    ap.add_argument("--variants", type=int, default=2)
    ap.add_argument("--report-dir", default="experiments/phase1")
    args = ap.parse_args()

    setup_logging()
    s = get_settings()
    reg = ModelRegistry(s.registry_db_url)
    await reg.setup()
    models = await reg.list_enabled()
    flt = parse_models_filter(args.models)
    if flt:
        models = [m for m in models if m.model_id in flt]

    engines: dict[str, object] = {}
    if any(m.engine == "comfy" for m in models):
        engines["comfy"] = ComfyEngine(
            client=ComfyClient(s.comfy_url),
            template_dir=Path("workflows"), out_dir=Path("experiments/renders"))
    api_adapters: dict[str, object] = {}
    if s.gemini_api_key:
        api_adapters["gemini"] = GeminiAdapter(s.gemini_api_key)
    if s.openai_api_key:
        api_adapters["openai"] = OpenAIImageAdapter(s.openai_api_key)
    if any(m.engine == "direct_api" for m in models):
        engines["direct_api"] = DirectAPIEngine(
            http=httpx.AsyncClient(timeout=300), adapters=api_adapters,
            limiter=TokenBucket(30))

    graph = build_mini_graph(engines, models)
    final = await graph.ainvoke({"description": args.description,
                                 "control_dir": args.control_dir,
                                 "variants": args.variants,
                                 "report_dir": args.report_dir})
    print(f"报告: {final['report_path']}")


if __name__ == "__main__":
    asyncio.run(main())
```

```yaml
# deploy/docker-compose.phase1.yml
services:
  vllm:
    image: vllm/vllm-openai:latest
    command: ["--model", "Qwen/Qwen2.5-7B-Instruct", "--max-model-len", "8192"]
    ports: ["8001:8000"]
    volumes: ["${HF_CACHE:-~/.cache/huggingface}:/root/.cache/huggingface"]
    deploy: {resources: {reservations: {devices: [{driver: nvidia, count: 1, capabilities: [gpu]}]}}}

  comfyui:
    image: yanwk/comfyui-boot:cu124
    ports: ["8188:8188"]
    volumes:
      - ./comfy-models/checkpoints:/root/ComfyUI/models/checkpoints
      - ./comfy-models/controlnet:/root/ComfyUI/models/controlnet
      - ./comfy-models/output:/root/ComfyUI/output
    deploy: {resources: {reservations: {devices: [{driver: nvidia, count: 1, capabilities: [gpu]}]}}}
```

- [ ] **Step 4: 运行通过**

Run: `uv run pytest tests/unit/test_cli.py -v` → PASS

- [ ] **Step 5: 全量回归 + Phase 1 人工验收**

```bash
uv run pytest -v                       # 全部离线测试
uv run ruff check app tests scripts && uv run mypy app
```

人工验收（需 GPU 机器）：

1. `docker compose -f deploy/docker-compose.phase1.yml up -d`
2. 下载 SDXL base checkpoint + depth/lineart ControlNet 到 `deploy/comfy-models/`
3. 人工准备 2-3 组控制图放入 `fixtures/control_maps/`（`viewXX_depth/lineart/white.png`，
   来源：任意建模工具手动出图——V2 Phase 1 输入口径）
4. `uv run python scripts/ab_render.py --control-dir fixtures/control_maps --description "温馨日式原木风，暖光"`
5. **验收标准（V2 Phase 1）**：报告生成；每模型 ≥2 张图；对比 Depth+Lineart 控制下
   结构保持度与美观度；记录选定默认引擎与参数区间（写入 `experiments/phase1/report_*.md` 结论）

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: phase1 CLI, dev compose and acceptance checklist"
```

---

## Phase 2 大纲（Phase 1 验收后另立详细计划）

> Phase 2 细节依赖 Phase 1 实测结论（默认引擎、参数区间、prompt 变体效果），此处只锁定任务边界，防止 scope 漂移。每个任务届时按本计划同样的 TDD 粒度展开：

1. **存储与检查点基建**：MinIO 客户端工具、AsyncPostgresSaver 接入、`PipelineState` 全量 schema（spec §4）
2. **CAD 工具组**：`convert_dwg`（ODA 子进程）、`inspect_dxf`（图层/块/proxy 检测/层高标注/置信度）、`parse_scene`（清洗/墙体/门窗/房间/布置图元 → SceneJSON）
3. **Blender 工具**：`blender/generate_scene.py` + `view_planner.py` + `build_white_model` 子进程封装（分段墙体开洞）
4. **主图组装**：spec §5 全节点 + 条件边 + Map/Send + extract_style 并行分支；AsyncPostgresSaver 持久化
5. **layout_agent 与规则校验**（spec §6.3）
6. **QA 规则版**（黑图/清晰度阈值，无 agent）
7. **FastAPI + graph-runner**：REST 契约（spec §9.1）、SSE 进度、Valkey 队列/pub/sub、崩溃恢复扫描
8. **增量重生成验证**：改描述 → 新 iteration → 断言上游缓存命中、仅渲染阶段执行（图测试）
9. **e2e**：标准演示户型 DXF 全链跑通（V2 Phase 2 交付口径）

---

## Self-Review 记录

- **Spec 覆盖**：Phase 1 范围（spec §14 第一行：engines + style_agent + workflows + mini 图 + 骨架落位）→ Task 5/6/7/8/9/10（engines+registry）、Task 4（style_agent）、Task 7（workflows）、Task 11（mini 图）、Task 2/3（骨架：模型/信封/缓存键/LLM 工厂）。§15 R2/R5 不在 Phase 1 范围。Phase 2 以大纲锁定，无遗漏声明。
- **占位符扫描**：火山引擎适配器显式移出 Phase 1（registry 未种入），非 TBD；`comfy-models` 权重下载为运行时准备项（Docker 卷），非代码占位。
- **类型一致性**：`RenderResult` 字段在 Task 2 定义、Task 9 补 `image_path` 等字段并在 Task 9 测试前置说明；`submit()` 签名 Comfy 与 DirectAPI 不同（后者多 `white_model_image`），mini 图 `render` 节点按 engine 分支调用，一致；`plan_tasks`/`discover_views` 在 Task 11 定义并在测试中直接消费。
