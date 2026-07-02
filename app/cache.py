import os
from typing import Optional
import redis.asyncio as redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL = int(os.getenv("CACHE_TTL", "86400"))

pool = redis.ConnectionPool.from_url(REDIS_URL, max_connections=50)


def get_client() -> redis.Redis:
    return redis.Redis(connection_pool=pool)


async def cache_url(short_code: str, long_url: str) -> None:
    client = get_client()
    await client.set(f"url:{short_code}", long_url, ex=CACHE_TTL)


async def get_cached_url(short_code: str) -> Optional[str]:
    client = get_client()
    val = await client.get(f"url:{short_code}")
    return val.decode() if val else None


async def increment_clicks(short_code: str) -> None:
    client = get_client()
    await client.incr(f"clicks:{short_code}")


async def flush_clicks() -> dict[str, int]:
    client = get_client()
    cursor = "0"
    flushed = {}
    while cursor:
        cursor, keys = await client.scan(cursor=cursor, match="clicks:*", count=100)
        for key in keys:
            count = await client.getdel(key)
            if count:
                short_code = key.decode().removeprefix("clicks:")
                flushed[short_code] = int(count)
    return flushed
