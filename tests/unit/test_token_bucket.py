# tests/unit/test_token_bucket.py
import asyncio, time
from app.infra.pools import TokenBucket


async def test_bucket_spaces_calls():
    b = TokenBucket(rate_per_min=600)          # 0.1s 间隔
    t0 = time.monotonic()
    await b.acquire(); await b.acquire()
    assert time.monotonic() - t0 >= 0.08       # 第二次被间隔约束
