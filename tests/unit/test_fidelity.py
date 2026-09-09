# tests/unit/test_fidelity.py
"""VLM Fidelity Check 单测：报告解析 / 缓存 / 错误路径（LLM 全 mock，不打真端点）。"""
from pathlib import Path

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.tools import fidelity
from app.tools.fidelity import check_fidelity

PNG_1PX = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
           b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\x00\x01"
           b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")

GOOD_JSON = ('{"score": 82, "summary": "骨架一致，缺沙发", "issues": '
             '[{"kind": "missing_furniture", "description": "缺沙发", '
             '"location": "客房南墙", "severity": "medium"}]}')


class FakeLLM(BaseChatModel):
    response: str = GOOD_JSON
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        object.__setattr__(self, "calls", self.calls + 1)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.response))])


@pytest.fixture
def two_pngs(tmp_path: Path):
    cad = tmp_path / "cad.png"
    render = tmp_path / "white.png"
    cad.write_bytes(PNG_1PX)
    render.write_bytes(PNG_1PX)
    return cad, render


def test_missing_input(tmp_path: Path, two_pngs, monkeypatch):
    cad, _render = two_pngs
    monkeypatch.setattr(fidelity, "make_chat_model", lambda *a, **k: FakeLLM())
    r = asyncio_run(check_fidelity(cad, tmp_path / "nope.png"))
    assert not r.ok and r.error is not None and r.error.code == "INPUT_INVALID"


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


def test_parses_report_and_writes_json(tmp_path: Path, two_pngs, monkeypatch):
    cad, render = two_pngs
    fake = FakeLLM()
    monkeypatch.setattr(fidelity, "make_chat_model", lambda *a, **k: fake)
    out = tmp_path / "report.json"
    r = asyncio_run(check_fidelity(cad, render, out))
    assert r.ok and r.data is not None
    assert r.data.score == 82
    assert r.data.issues[0].kind == "missing_furniture"
    assert r.data.model
    assert out.exists()
    assert fake.calls == 1


def test_cache_hit_second_call(tmp_path: Path, two_pngs, monkeypatch):
    cad, render = two_pngs
    fake = FakeLLM()
    monkeypatch.setattr(fidelity, "make_chat_model", lambda *a, **k: fake)
    out = tmp_path / "report.json"
    r1 = asyncio_run(check_fidelity(cad, render, out))
    r2 = asyncio_run(check_fidelity(cad, render, out))
    assert r1.ok and r2.ok
    assert r2.cache_hit and not r1.cache_hit
    assert fake.calls == 1                           # 第二次未调 LLM


def test_bad_json_reports_parse_failure(tmp_path: Path, two_pngs, monkeypatch):
    cad, render = two_pngs
    monkeypatch.setattr(fidelity, "make_chat_model",
                        lambda *a, **k: FakeLLM(response="不是 JSON"))
    r = asyncio_run(check_fidelity(cad, render))
    assert not r.ok and r.error is not None and r.error.code == "FIDELITY_PARSE_FAILED"


def test_markdown_fenced_json_accepted(tmp_path: Path, two_pngs, monkeypatch):
    cad, render = two_pngs
    monkeypatch.setattr(fidelity, "make_chat_model",
                        lambda *a, **k: FakeLLM(response=f"```json\n{GOOD_JSON}\n```"))
    r = asyncio_run(check_fidelity(cad, render))
    assert r.ok and r.data is not None and r.data.score == 82
