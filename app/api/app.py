# app/api/app.py
"""FastAPI 无状态业务接口（spec §9.1 契约）。"""
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import ApiDeps
from app.api.render_edit import create_render_edit_router
from app.infra.pg import PROJECTS, upsert_project

JOB_QUEUE = "arp:jobs"
EVENT_PREFIX = "arp:events"
ALLOWED_EXT = {".dwg", ".dxf"}


class RegenerateBody(BaseModel):
    description: str | None = None


def create_app(deps: ApiDeps) -> FastAPI:
    app = FastAPI(title="ai-render-pipeline")

    # 生成+局部编辑业务（docs/http-api-design-2026-09.md）：/api/v1/* + /files
    from app.core.config import get_settings

    output_root = Path(get_settings().output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    app.include_router(create_render_edit_router(output_root))
    app.mount("/files", StaticFiles(directory=output_root), name="files")

    @app.post("/api/projects", status_code=202)
    async def create_project(cad_file: UploadFile,
                             description: str | None = Form(None)):
        ext = Path(cad_file.filename or "").suffix.lower()
        if ext not in ALLOWED_EXT:
            raise HTTPException(400, f"不支持的图纸格式 {ext}（仅 .dwg/.dxf）")
        pid = uuid.uuid4().hex[:12]
        updir = Path(deps.data_dir) / "uploads"
        updir.mkdir(parents=True, exist_ok=True)
        (updir / f"{pid}{ext}").write_bytes(await cad_file.read())
        await upsert_project(deps.engine, pid, 1, "queued")
        await deps.vclient.rpush(JOB_QUEUE, json.dumps(
            {"project_id": pid, "iteration": 1,
             "cad_file_key": str(updir / f"{pid}{ext}"),
             "text_description": description}))
        return {"project_id": pid, "iteration": 1}

    @app.get("/api/projects/{pid}")
    async def project_status(pid: str):
        async with deps.engine.connect() as conn:
            rows = (await conn.execute(
                select(PROJECTS.c.iteration, PROJECTS.c.status)
                .where(PROJECTS.c.id == pid))).all()
        if not rows:
            raise HTTPException(404, "project not found")
        latest_iter, latest_status = max(rows, key=lambda r: r[0])
        summary = (Path(deps.data_dir) / "projects" / pid
                   / f"iteration-{latest_iter}" / "summary.json")
        return {"status_by_iteration": {str(i): s for i, s in rows},
                "latest": {"iteration": latest_iter, "status": latest_status,
                           "summary_exists": summary.exists()}}

    @app.post("/api/projects/{pid}/regenerate", status_code=202)
    async def regenerate(pid: str, body: RegenerateBody):
        async with deps.engine.connect() as conn:
            rows = (await conn.execute(
                select(PROJECTS.c.iteration)
                .where(PROJECTS.c.id == pid))).all()
        if not rows:
            raise HTTPException(404, "project not found")
        it = max(r[0] for r in rows) + 1
        uploads = sorted((Path(deps.data_dir) / "uploads").glob(f"{pid}.*"))
        if not uploads:
            raise HTTPException(410, "cad file missing")
        await upsert_project(deps.engine, pid, it, "queued")
        await deps.vclient.rpush(JOB_QUEUE, json.dumps(
            {"project_id": pid, "iteration": it,
             "cad_file_key": str(uploads[0]),
             "text_description": body.description}))
        return {"project_id": pid, "iteration": it}

    @app.get("/api/projects/{pid}/events")
    async def events(pid: str):
        async def gen():
            ps = deps.vclient.pubsub()
            await ps.subscribe(f"{EVENT_PREFIX}:{pid}")
            try:
                while True:
                    msg = await ps.get_message(ignore_subscribe_messages=True,
                                               timeout=10)
                    if msg is not None and msg.data:
                        # 真 valkey 默认 decode_responses=False：msg.data 是
                        # bytes，直接进 f-string 会渲染成 b'...' repr（帧语法
                        # 合法、载荷损坏），统一解码后再下发
                        data = (msg.data.decode() if isinstance(msg.data, bytes)
                                else msg.data)
                        yield f"data: {data}\n\n"
                    else:
                        yield ": ping\n\n"
            finally:
                await ps.aclose() if hasattr(ps, "aclose") else None

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app
