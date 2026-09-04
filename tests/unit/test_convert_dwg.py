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
    expected = f"a.{conv._sha(dwg)}.dxf"            # 产物名含内容 hash
    assert result.ok and result.data is not None and result.data.name == expected
    assert result.data.exists()
    assert not (tmp_path / "out" / "a.dxf").exists()  # ODA 原始名已改挂 hash
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
    assert first.data == second.data                # 同内容 → 同 hash 产物名


async def test_convert_stale_cache_same_stem_changed_content(tmp_path, monkeypatch):
    # 回归（review Important 3）：同 stem 换内容不得命中旧产物——产物名含内容 hash，
    # 新内容 → 新名字 → 必须重转
    dwg = _make_dwg(tmp_path / "a.dwg")
    runs = []

    def fake_run(cmd, **kw):
        runs.append(1)
        Path(cmd[2], "a.dxf").write_bytes(b"fake-dxf")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(conv.subprocess, "run", fake_run)
    first = await conv.convert_dwg(dwg, tmp_path / "out")
    dwg.write_bytes(b"AC1032fake-dwg-bytes-v2")     # 同名不同内容
    second = await conv.convert_dwg(dwg, tmp_path / "out")
    assert first.ok and second.ok
    assert first.data is not None and second.data is not None
    assert not second.cache_hit                     # 不得命中旧产物
    assert len(runs) == 2                           # 重新转换
    assert first.data != second.data                # 新 hash → 新产物名
    assert second.data.exists() and first.data.exists()  # 旧产物留档不清理


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
