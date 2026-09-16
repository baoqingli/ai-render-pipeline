# tests/unit/test_render_edit_dirs.py
"""列目录端点（front-end 设计 spec §4）：运行子目录列表 / 子目录图片列表 /
越界与不存在。全离线：TestClient + tmp_path 伪造 output 结构。"""
from fastapi.testclient import TestClient

from app.api.render_edit import create_render_edit_app


def _seed(tmp_path):
    d = tmp_path / "2026-09-16" / "114228"
    d.mkdir(parents=True)
    (d / "final.png").write_bytes(b"png")
    (d / "layout.png").write_bytes(b"png")
    (d / "elements.json").write_text("{}", encoding="utf-8")
    (d / "renders").mkdir()
    (d / "renders" / "gpt_render_01.png").write_bytes(b"png")
    (tmp_path / "2026-09-15" / "090000").mkdir(parents=True)


def test_dirs_lists_run_dirs_sorted_and_filters_non_dates(tmp_path):
    (tmp_path / "2026-09-15" / "090000").mkdir(parents=True)
    (tmp_path / "2026-09-16" / "114228").mkdir(parents=True)
    (tmp_path / "uploads").mkdir()  # 非日期目录必须被过滤
    with TestClient(create_render_edit_app(output_root=tmp_path)) as c:
        assert c.get("/api/v1/dirs").json() == [
            "2026-09-15/090000", "2026-09-16/114228"]


def test_dir_images_top_level_only_sorted(tmp_path):
    _seed(tmp_path)
    with TestClient(create_render_edit_app(output_root=tmp_path)) as c:
        r = c.get("/api/v1/dirs/2026-09-16/114228")
        assert r.status_code == 200
        # 只含顶层图片文件、按名称排序；json 与子目录 renders/ 不混入
        assert r.json() == [
            {"name": "final.png",
             "url": "/files/2026-09-16/114228/final.png"},
            {"name": "layout.png",
             "url": "/files/2026-09-16/114228/layout.png"},
        ]


def test_dir_images_empty_dir(tmp_path):
    (tmp_path / "2026-09-16" / "000000").mkdir(parents=True)
    with TestClient(create_render_edit_app(output_root=tmp_path)) as c:
        assert c.get("/api/v1/dirs/2026-09-16/000000").json() == []


def test_dir_images_missing_404(tmp_path):
    with TestClient(create_render_edit_app(output_root=tmp_path)) as c:
        assert c.get("/api/v1/dirs/2026-09-16/999999").status_code == 404


def test_dir_images_path_is_file_404(tmp_path):
    _seed(tmp_path)
    with TestClient(create_render_edit_app(output_root=tmp_path)) as c:
        assert c.get("/api/v1/dirs/2026-09-16/114228/final.png") \
            .status_code == 404


def test_dir_images_escape_403(tmp_path):
    # 字面 ../ 会被 httpx 规范化，须用编码形式才能到达端点
    with TestClient(create_render_edit_app(output_root=tmp_path)) as c:
        r = c.get("/api/v1/dirs/%2e%2e%2fetc")
        assert r.status_code == 403
