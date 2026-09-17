# tests/unit/test_archive_source.py
"""原图存档：run_e2e 把上传原图副本（_source.<ext>）落进运行目录，
供前端「用此原图重新生成」。存档是尽力而为——失败不打断主流程。"""
from app.agents.vision.render_e2e import archive_source


def test_archive_source_copies_with_underscore_prefix(tmp_path):
    src = tmp_path / "plan.dwg"
    src.write_bytes(b"DWGBYTES")
    out = tmp_path / "run"
    out.mkdir()
    archive_source(src, out)
    assert (out / "_source.dwg").read_bytes() == b"DWGBYTES"


def test_archive_source_missing_file_swallows_error(tmp_path):
    src = tmp_path / "plan.dwg"  # 不存在 → copy 抛 OSError → 吞掉不外抛
    out = tmp_path / "run"
    out.mkdir()
    archive_source(src, out)  # 不抛即通过
    assert not (out / "_source.dwg").exists()
