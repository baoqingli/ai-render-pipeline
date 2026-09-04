# tests/unit/test_convert_dwg.py
import subprocess
from pathlib import Path

from app.tools.cad import convert as conv


def _make_dwg(p: Path) -> Path:
    p.write_bytes(b"AC1032fake-dwg-bytes")
    return p


async def test_convert_success_via_fake_oda(tmp_path, monkeypatch):
    dwg = _make_dwg(tmp_path / "a.dwg")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = list(cmd)
        Path(cmd[2], "a.dxf").write_bytes(b"fake-dxf")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(conv.subprocess, "run", fake_run)
    result = await conv.convert_dwg(dwg, tmp_path / "out")
    assert result.ok and result.data is not None and result.data.name == "a.dxf"
    assert "DXF" in seen["cmd"]                     # 参数含 DXF 输出类型


async def test_convert_cache_hit_second_call(tmp_path, monkeypatch):
    dwg = _make_dwg(tmp_path / "a.dwg")
    runs = []

    def fake_run(cmd, **kw):
        runs.append(1)
        Path(cmd[2], "a.dxf").write_bytes(b"fake-dxf")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(conv.subprocess, "run", fake_run)
    first = await conv.convert_dwg(dwg, tmp_path / "out")
    second = await conv.convert_dwg(dwg, tmp_path / "out")
    assert first.ok and second.ok and second.cache_hit and len(runs) == 1


async def test_oda_timeout_is_retryable(tmp_path, monkeypatch):
    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd="x", timeout=300)

    monkeypatch.setattr(conv.subprocess, "run", hang)
    result = await conv.convert_dwg(_make_dwg(tmp_path / "a.dwg"), tmp_path / "out")
    assert not result.ok and result.error is not None
    assert result.error.code == "ODA_TIMEOUT" and result.error.retryable is True


async def test_oda_missing_is_not_retryable(tmp_path, monkeypatch):
    def boom(cmd, **kw):
        raise FileNotFoundError("no exe")

    monkeypatch.setattr(conv.subprocess, "run", boom)
    result = await conv.convert_dwg(_make_dwg(tmp_path / "a.dwg"), tmp_path / "out")
    assert not result.ok and result.error is not None
    assert result.error.code == "ODA_MISSING" and not result.error.retryable


async def test_empty_input_file_invalid(tmp_path):
    p = tmp_path / "a.dwg"
    p.write_bytes(b"")
    result = await conv.convert_dwg(p, tmp_path / "out")
    assert not result.ok and result.error.code == "INPUT_INVALID"
