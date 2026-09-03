# tests/unit/test_cli.py
import pytest

from app.engines.registry import ModelInfo
from scripts.ab_render import ensure_models, ensure_views, parse_models_filter


def test_model_filter_empty_means_all():
    assert parse_models_filter(None) is None
    assert parse_models_filter("") is None
    assert parse_models_filter("a,b") == {"a", "b"}


def test_model_filter_strips_whitespace_and_blanks():
    assert parse_models_filter(" a , b ,,") == {"a", "b"}


def test_ensure_views_rejects_dir_without_valid_views(tmp_path, capsys):
    """Task 11 评审加验：控制图目录产出 0 个视图时快速失败（清晰报错 + 非零退出，
    不进图、不 KeyError）。空目录与缺伴生图的半套视图都算无效。"""
    with pytest.raises(SystemExit) as ei:
        ensure_views(tmp_path)  # 空目录
    assert (ei.value.code or 0) != 0

    (tmp_path / "v1_depth.png").write_bytes(b"d")  # 只有 depth，缺 lineart/white
    with pytest.raises(SystemExit) as ei:
        ensure_views(tmp_path)
    assert (ei.value.code or 0) != 0

    err = capsys.readouterr().err
    assert "no valid views found" in err
    assert "viewXX_depth.png" in err and "viewXX_lineart.png" in err \
        and "viewXX_white.png" in err


def test_ensure_views_accepts_valid_triplet(tmp_path):
    for suffix in ("depth", "lineart", "white"):
        (tmp_path / f"v1_{suffix}.png").write_bytes(suffix.encode())
    views = ensure_views(tmp_path)
    assert [v["view_id"] for v in views] == ["v1"]


MODELS = [
    ModelInfo(model_id="sdxl-control-v1", engine="comfy"),
    ModelInfo(model_id="nano-banana-2", engine="direct_api"),
]


def test_ensure_models_rejects_zero_match(capsys):
    """终审 Important #3：--models 拼写错误（零匹配）时快速失败——
    stderr 打印请求 ID 与可用 enabled ID，非零码退出。"""
    with pytest.raises(SystemExit) as ei:
        ensure_models(MODELS, {"sdxl-control-typo"})
    assert (ei.value.code or 0) == 2
    err = capsys.readouterr().err
    assert "sdxl-control-typo" in err and "sdxl-control-v1" in err \
        and "nano-banana-2" in err


def test_ensure_models_rejects_empty_registry(capsys):
    with pytest.raises(SystemExit) as ei:
        ensure_models([], None)
    assert (ei.value.code or 0) == 2
    assert "available: []" in capsys.readouterr().err


def test_ensure_models_passes_matching_and_unfiltered():
    kept = ensure_models(MODELS, {"sdxl-control-v1"})
    assert [m.model_id for m in kept] == ["sdxl-control-v1"]
    assert [m.model_id for m in ensure_models(MODELS, None)] == \
        ["sdxl-control-v1", "nano-banana-2"]
