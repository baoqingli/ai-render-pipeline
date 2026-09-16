# app/api/jobs.py
"""异步作业注册表：提交-轮询模式的核心（docs/http-api-design-2026-09.md §三）。

进程内存实现——产物本身已持久化磁盘（output/<日期>/<运行>/），注册表丢失
可按目录重建；全局 Semaphore 限流保护 OpenRouter（各环节另有 429/5xx 自动
重试兜底）；作业级超时防挂死占坑。
"""
import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_CONCURRENCY = 2
JOB_TIMEOUT_S = 900


@dataclass
class Job:
    id: str
    type: str                              # render | edit
    status: str = "pending"                # pending|running|succeeded|failed
    stage: str = "queued"                  # vision|validation|generate|edit|done
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"job_id": self.id, "type": self.type, "status": self.status,
                "stage": self.stage, "error": self.error,
                "created_at": self.created_at, "result": self.result}


class JobRegistry:
    """作业注册表 + 后台执行。非线程安全（asyncio 单线程模型内使用）。"""

    def __init__(self, max_concurrency: int = MAX_CONCURRENCY,
                 timeout_s: int = JOB_TIMEOUT_S):
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._sem = asyncio.Semaphore(max_concurrency)
        self.timeout_s = timeout_s

    def create(self, jtype: str) -> Job:
        jid = (f"{jtype[0]}-{time.strftime('%Y%m%d-%H%M%S')}"
               f"-{secrets.token_hex(2)}")
        job = Job(id=jid, type=jtype)
        self._jobs[jid] = job
        return job

    def get(self, jid: str) -> Job | None:
        return self._jobs.get(jid)

    def list(self, jtype: str | None = None,
             status: str | None = None) -> list[Job]:
        jobs = sorted(self._jobs.values(),
                      key=lambda j: j.created_at, reverse=True)
        if jtype:
            jobs = [j for j in jobs if j.type == jtype]
        if status:
            jobs = [j for j in jobs if j.status == status]
        return jobs

    def spawn(self, job: Job,
              factory: Callable[[Callable[[str], None]], Any]) -> None:
        """后台执行作业。factory(progress_cb) 返回协程；progress_cb 更新
        job.stage（回调异常绝不打断业务）。"""

        def cb(stage: str) -> None:
            job.stage = stage

        async def _run() -> None:
            async with self._sem:
                job.status = "running"
                job.stage = "running"
                try:
                    job.result = await asyncio.wait_for(
                        factory(cb), timeout=self.timeout_s)
                    job.status = "succeeded"
                    job.stage = "done"
                except asyncio.TimeoutError:
                    job.status = "failed"
                    job.stage = "timeout"
                    job.error = f"作业超时（>{self.timeout_s}s）"
                except Exception as exc:  # noqa: BLE001 作业失败转状态不崩服务
                    job.status = "failed"
                    job.stage = "failed"
                    job.error = str(exc)[:500]

        self._tasks[job.id] = asyncio.create_task(_run())
