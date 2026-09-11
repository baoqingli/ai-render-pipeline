import json
from pathlib import Path

from app.tools import generated_render_validate as validator

PNG_1PX = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
           b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\x00\x01"
           b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def _report(score=86, bathroom="correct", issues=None):
    return json.dumps({
        "score": score,
        "summary": "布局基本一致",
        "orientation_alignment": "aligned",
        "zone_checks": [{
            "zone": "卫生间",
            "expected_location": "左侧",
            "verdict": bathroom,
            "observed": "马桶、台盆、淋浴",
            "expected_items": ["马桶", "台盆", "淋浴"],
            "found_items": ["马桶", "台盆", "淋浴"],
        }],
        "structure": {"verdict": "correct", "note": "一致"},
        "issues": issues or [],
    }, ensure_ascii=False)


def _images(tmp_path: Path, count=1):
    layout = tmp_path / "layout.png"
    layout.write_bytes(PNG_1PX)
    renders = tmp_path / "renders"
    renders.mkdir()
    for index in range(count):
        (renders / f"render_{index}.png").write_bytes(PNG_1PX)
    return layout, renders


def test_extract_json_accepts_fence_and_trailing_text():
    result = validator._extract_json(f"```json\n{_report()}\n```\n说明")
    assert result["score"] == 86


def test_bathroom_wrong_fails_even_above_threshold(tmp_path, monkeypatch):
    layout, renders = _images(tmp_path)
    monkeypatch.setattr(validator, "_call_openrouter",
                        lambda *args, **kwargs: _report(95, "wrong"))
    result = validator.validate_generated_render(
        layout, renders / "render_0.png", api_key="test-key")
    assert not result["passed"]
    assert result["gate"]["bathroom_failed"]


def test_critical_extra_bed_fails(tmp_path, monkeypatch):
    layout, renders = _images(tmp_path)
    issue = {"kind": "extra_bed", "severity": "high", "location": "卫生间",
             "description": "卫生间内出现床"}
    monkeypatch.setattr(validator, "_call_openrouter",
                        lambda *args, **kwargs: _report(90, issues=[issue]))
    result = validator.validate_generated_render(
        layout, renders / "render_0.png", api_key="test-key")
    assert not result["passed"]
    assert result["gate"]["critical_issue"]


def test_directory_ranks_results(tmp_path, monkeypatch):
    layout, renders = _images(tmp_path, count=2)
    responses = iter([_report(61), _report(88)])
    monkeypatch.setattr(validator, "_call_openrouter",
                        lambda *args, **kwargs: next(responses))
    result = validator.validate_render_directory(layout, renders, api_key="test-key")
    assert result["ranking"] == ["render_1.png", "render_0.png"]
    assert result["passed"] == 1
    assert result["best"] == "render_1.png"


def test_requires_runtime_api_key(tmp_path, monkeypatch):
    layout, renders = _images(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    try:
        validator.validate_generated_render(layout, renders / "render_0.png")
    except ValueError as exc:
        assert "OPENROUTER_API_KEY" in str(exc)
    else:
        raise AssertionError("missing API key should fail")
