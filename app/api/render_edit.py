# app/api/render_edit.py
"""生成 + 局部编辑 HTTP API（docs/http-api-design-2026-09.md）。

不依赖 PG/Valkey（作业注册表在进程内存，产物持久化磁盘）；可作为独立
轻量服务启动（scripts/run_render_api.py），也可挂进主 app（app.py）。
"""
from pathlib import Path

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles

from app.agents.vision.render_e2e import run_e2e
from app.api.jobs import JobRegistry
from app.engines.local_edit_agent import (DEFAULT_EDIT_MODEL,
                                          DEFAULT_VLM_MODEL, LocalEditAgent)

_INPUT_EXTS = {".dwg", ".dxf", ".png", ".jpg", ".jpeg"}
_IMG_EXTS = {".png", ".jpg", ".jpeg"}


def create_render_edit_router(output_root: Path) -> APIRouter:
    root = output_root.resolve()
    (root / "uploads").mkdir(parents=True, exist_ok=True)
    router = APIRouter(prefix="/api/v1")
    registry = JobRegistry()

    def url_of(p: str | Path | None) -> str | None:
        """磁盘绝对路径 → /files/... URL；越界（不在 output 根内）返回 None。"""
        if not p:
            return None
        p = Path(p)
        try:
            rel = p.resolve().relative_to(root)
        except ValueError:
            return None
        return f"/files/{rel.as_posix()}"

    def _safe_inside(rel_path: str) -> Path:
        p = (root / rel_path).resolve()
        if not p.is_relative_to(root):
            raise HTTPException(403, "路径越界")
        if not p.exists():
            raise HTTPException(404, f"文件不存在: {rel_path}")
        return p

    def _api_key() -> str:
        from app.core.config import get_settings

        key = get_settings().llm_api_key or ""
        if not key:
            raise HTTPException(500, "服务端未配置 ARP_LLM_API_KEY")
        return key

    # ── 生成 ─────────────────────────────────────────────────────────────
    @router.post("/renders", status_code=202)
    async def submit_render(file: UploadFile = File(...),
                            desc: str | None = Form(None),
                            edit: list[str] | None = Form(None),
                            force: bool = Form(True),
                            model: str | None = Form(None),
                            gpt_model: str | None = Form(None),
                            n: int = Form(1),
                            max_iters: int = Form(3)):
        ext = Path(file.filename or "").suffix.lower()
        if ext not in _INPUT_EXTS:
            raise HTTPException(400, f"不支持的格式 {ext}（支持 "
                                     f"{sorted(_INPUT_EXTS)}）")
        job = registry.create("render")
        updir = root / "uploads" / job.id
        updir.mkdir(parents=True, exist_ok=True)
        src = updir / f"input{ext}"
        src.write_bytes(await file.read())  # noqa: ASYNC230

        async def factory(cb):
            r = await run_e2e(src, root, model=model, max_iters=max_iters,
                              force=force, gpt_model=gpt_model, n=n,
                              desc=desc, edit=edit, progress_cb=cb)
            if not r.ok or r.data is None:
                code = r.error.code if r.error else "FAILED"
                raise RuntimeError(f"[{code}] "
                                   f"{r.error.message if r.error else '生成失败'}")
            d = r.data
            return {
                "final": url_of(d.get("final")),
                "renders": [url_of(x) for x in d.get("renders", [])],
                "edited": url_of(d.get("edited")),
                "validation_passed": bool(
                    d.get("validation", {}).get("passed")),
                "desc_prompt": d.get("desc_prompt"),
                "out_dir": url_of(d.get("out_dir")),
            }

        registry.spawn(job, factory)
        return {"job_id": job.id, "status": job.status}

    # ── 局部编辑 ─────────────────────────────────────────────────────────
    @router.post("/edits", status_code=202)
    async def submit_edit(instruction: str = Form(...),
                          image: UploadFile | None = File(None),
                          image_path: str | None = Form(None),
                          mask: UploadFile | None = File(None),
                          mask_path: str | None = Form(None),
                          no_compile: bool = Form(False),
                          max_retries: int = Form(2),
                          edit_model: str | None = Form(None),
                          vlm_model: str | None = Form(None)):
        if bool(image is not None) == bool(image_path):
            raise HTTPException(400, "image（上传文件）与 image_path（服务端"
                                     "相对路径）必须二选一")
        job = registry.create("edit")
        updir = root / "uploads" / job.id
        updir.mkdir(parents=True, exist_ok=True)

        src: Path
        if image is not None:
            ext = Path(image.filename or "").suffix.lower()
            if ext not in _IMG_EXTS:
                raise HTTPException(400, f"不支持的图片格式 {ext}")
            src = updir / f"source{ext}"
            src.write_bytes(await image.read())  # noqa: ASYNC230
        else:
            src = _safe_inside(image_path)

        mask_src: Path | None = None
        if mask is not None:
            mask_src = updir / "mask.png"
            mask_src.write_bytes(await mask.read())  # noqa: ASYNC230
        elif mask_path:
            mask_src = _safe_inside(mask_path)

        async def factory(cb):
            agent = LocalEditAgent(
                api_key=_api_key(), out_dir=src.parent,
                edit_model=edit_model or DEFAULT_EDIT_MODEL,
                vlm_model=vlm_model or DEFAULT_VLM_MODEL,
                max_retries=max_retries)
            rep = await agent.run(src, instruction, mask_path=mask_src,
                                  compile_instruction=not no_compile)
            return {
                "edited": url_of(rep.get("edited")),
                "mask": url_of(rep.get("mask")),
                "verify": rep.get("verify"),
                "attempts": rep.get("attempts"),
                "instruction_compiled": rep.get("instruction_compiled"),
            }

        registry.spawn(job, factory)
        return {"job_id": job.id, "status": job.status}

    # ── 作业查询 ─────────────────────────────────────────────────────────
    @router.get("/jobs/{job_id}")
    async def get_job(job_id: str):
        job = registry.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        return job.to_dict()

    @router.get("/jobs")
    async def list_jobs(jtype: str | None = None,
                        status: str | None = None):
        return [j.to_dict() for j in registry.list(jtype=jtype, status=status)]

    @router.get("/healthz")
    async def healthz():
        return {"ok": True, "jobs": len(registry.list())}

    return router


def create_render_edit_app(output_root: Path | None = None) -> FastAPI:
    """独立轻量服务（不依赖 PG/Valkey）：/api/v1/* + /files 静态产物。"""
    from app.core.config import get_settings

    root = Path(output_root or get_settings().output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="ai-render-pipeline/render-edit")
    app.include_router(create_render_edit_router(root))
    app.mount("/files", StaticFiles(directory=root), name="files")
    return app
