# tests/unit/test_api.py
"""Task 6：FastAPI REST + SSE 投递（spec §9.1 契约）。

全离线：sqlite in-memory + StubValkey（dict 队列 + asyncio.Queue pub/sub 桥）。
REST 两例走 TestClient（brief 原文）；SSE 一例直接以 ASGI 协议驱动 app——
本仓 starlette 1.6 的 TestClient 会把响应体整体缓冲（portal.call 等 app
跑完才返回，httpx/httpx2 的 ASGITransport 同样缓冲），无限 SSE 生成器会
永久挂起，故手写 scope/receive/send 增量收 body 块，语义等价且同样覆盖
data 行、content-type 与断开取消。
"""
import asyncio
import contextlib
import json

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.app import create_app
from app.api.deps import ApiDeps
from app.infra.pg import _meta


class StubValkey:
    def __init__(self):
        self.queue = []
        self.subs = []

    async def rpush(self, key, msg):
        self.queue.append((key, json.loads(msg)))

    async def blpop(self, key):
        await asyncio.sleep(0)
        return (key, json.dumps(self.queue.pop(0)[1])) if self.queue else None

    async def publish(self, channel, message):
        for q in self.subs:
            q.put_nowait((channel, message))

    def pubsub(self):
        return StubPubSub(self)


class StubPubSub:
    def __init__(self, owner):
        self.owner = owner

    async def subscribe(self, channel):
        import asyncio as aio
        q = aio.Queue()
        self.owner.subs.append(q)
        self.q = q

    # 参数名对齐真 valkey pubsub.get_message(timeout=...)，app 以关键字调用
    async def get_message(self, ignore_subscribe_messages=True, timeout=None):  # noqa: ASYNC109
        try:
            ch, msg = await asyncio.wait_for(self.q.get(), timeout=timeout or 5)
            return type("M", (), {"channel": ch, "data": msg})()
        except TimeoutError:
            return None


async def _make_app(tmp_path):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(_meta.create_all)
    vk = StubValkey()
    return create_app(ApiDeps(engine=eng, vclient=vk, data_dir=tmp_path)), vk


def _client(tmp_path):
    return asyncio.run(_make_app(tmp_path))


def test_upload_creates_project_and_queues_job(tmp_path):
    app, vk = _client(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/projects", files={"cad_file": ("a.dxf", b"DXFBYTES")},
                   data={"description": "暖"})
        assert r.status_code == 202
        pid = r.json()["project_id"]
        assert r.json()["iteration"] == 1
        assert (tmp_path / "uploads" / f"{pid}.dxf").read_bytes() == b"DXFBYTES"
        key, job = vk.queue[0]
        assert key == "arp:jobs" and job["project_id"] == pid
        assert job["text_description"] == "暖"


def test_regenerate_bumps_iteration(tmp_path):
    app, vk = _client(tmp_path)
    with TestClient(app) as c:
        r1 = c.post("/api/projects", files={"cad_file": ("a.dxf", b"x")})
        pid = r1.json()["project_id"]
        r2 = c.post(f"/api/projects/{pid}/regenerate", json={"description": "换个风格"})
        assert r2.status_code == 202 and r2.json()["iteration"] == 2
        assert vk.queue[-1][1]["text_description"] == "换个风格"


def test_project_status_latest_summary_and_404(tmp_path):
    app, _ = _client(tmp_path)
    with TestClient(app) as c:
        assert c.get("/api/projects/none").status_code == 404
        assert c.post("/api/projects",
                      files={"cad_file": ("a.pdf", b"x")}).status_code == 400
        pid = c.post("/api/projects",
                     files={"cad_file": ("a.dxf", b"x")}).json()["project_id"]
        assert c.post(f"/api/projects/{pid}/regenerate", json={}).status_code == 202
        body = c.get(f"/api/projects/{pid}").json()
        assert body["status_by_iteration"] == {"1": "queued", "2": "queued"}
        assert body["latest"] == {"iteration": 2, "status": "queued",
                                  "summary_exists": False}
        summary = tmp_path / "projects" / pid / "iteration-2" / "summary.json"
        summary.parent.mkdir(parents=True)
        summary.write_text("{}")
        assert c.get(f"/api/projects/{pid}").json()["latest"]["summary_exists"] is True


def test_regenerate_unknown_404_and_missing_upload_410(tmp_path):
    app, _ = _client(tmp_path)
    with TestClient(app) as c:
        assert c.post("/api/projects/none/regenerate",
                      json={"description": "x"}).status_code == 404
        pid = c.post("/api/projects",
                     files={"cad_file": ("a.dxf", b"x")}).json()["project_id"]
        next((tmp_path / "uploads").glob(f"{pid}.*")).unlink()
        assert c.post(f"/api/projects/{pid}/regenerate", json={}).status_code == 410


async def _drive_sse(app, pid: str):
    """手写 ASGI 客户端收流：返回 (app 任务, body 块列表, headers, 块事件)。

    receive() 在 disconnect 事件上阻塞——starlette 的 StreamingResponse 并发
    跑 listen_for_disconnect，立刻回 http.disconnect 会当场掐断 body 迭代。
    """
    chunks: list[bytes] = []
    headers: list[tuple[bytes, bytes]] = []
    started = asyncio.Event()
    disconnected = asyncio.Event()
    chunk_added = asyncio.Event()

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            headers.extend(message.get("headers", []))
            started.set()
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
            chunk_added.set()

    path = f"/api/projects/{pid}/events"
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "GET", "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "headers": [],
             "server": ("test", 80), "client": ("testclient", 50000)}
    task = asyncio.create_task(app(scope, receive, send))
    await started.wait()
    return task, chunks, headers, chunk_added


async def test_events_sse_streams_published(tmp_path):
    app, vk = await _make_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r1 = await ac.post("/api/projects", files={"cad_file": ("a.dxf", b"x")})
        pid = r1.json()["project_id"]

    task, chunks, headers, chunk_added = await _drive_sse(app, pid)
    await asyncio.sleep(0.3)  # 让 subscribe 先注册到 StubValkey.subs
    await vk.publish(f"arp:events:{pid}", '{"stage":"finalized"}')

    async def _until_finalized():
        while not any(b"finalized" in c for c in chunks):
            await chunk_added.wait()
            chunk_added.clear()

    # 超时即失败，不静默跳过
    await asyncio.wait_for(_until_finalized(), timeout=10)
    task.cancel()  # 客户端断开即取消
    with contextlib.suppress(asyncio.CancelledError):
        await task

    body = b"".join(chunks)
    assert b'data: {"stage":"finalized"}\n\n' in body
    assert any(k.lower() == b"content-type" and v.startswith(b"text/event-stream")
               for k, v in headers)
