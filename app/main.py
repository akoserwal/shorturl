import os
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, HttpUrl

from app.repository import get_repository, URLRecord
from app.snowflake import SnowflakeGenerator
from app.base62 import encode
from app import cache

WORKER_ID = int(os.getenv("WORKER_ID", "1"))
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

id_gen = SnowflakeGenerator(WORKER_ID)
repo = get_repository()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await repo.init()
    yield


app = FastAPI(title="Distributed Short URL Service", lifespan=lifespan)


class ShortenRequest(BaseModel):
    url: HttpUrl
    expires_in_days: int | None = None


class ShortenResponse(BaseModel):
    short_url: str
    short_code: str
    long_url: str
    created_at: datetime
    expires_at: datetime | None = None


class StatsResponse(BaseModel):
    short_code: str
    long_url: str
    click_count: int
    created_at: datetime


@app.get("/health")
async def health():
    return {"status": "ok", "worker_id": WORKER_ID}


@app.post("/api/shorten", response_model=ShortenResponse, status_code=201)
async def shorten_url(req: ShortenRequest):
    long_url = str(req.url)

    snowflake_id = id_gen.generate()
    short_code = encode(snowflake_id)

    expires_at = None
    if req.expires_in_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=req.expires_in_days)

    record = URLRecord(
        id=snowflake_id,
        short_code=short_code,
        long_url=long_url,
        created_at=datetime.now(timezone.utc),
        expires_at=expires_at,
    )
    record = await repo.create_url(record)

    await cache.cache_url(short_code, long_url)

    return ShortenResponse(
        short_url=f"{BASE_URL}/{short_code}",
        short_code=short_code,
        long_url=long_url,
        created_at=record.created_at,
        expires_at=expires_at,
    )


@app.get("/{short_code}")
async def redirect_url(short_code: str):
    cached = await cache.get_cached_url(short_code)
    if cached:
        await cache.increment_clicks(short_code)
        return RedirectResponse(url=cached, status_code=301)

    url_entry = await repo.get_by_short_code(short_code)

    if not url_entry:
        raise HTTPException(status_code=404, detail="Short URL not found")

    if url_entry.expires_at and url_entry.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Short URL has expired")

    await cache.cache_url(short_code, url_entry.long_url)
    await cache.increment_clicks(short_code)

    return RedirectResponse(url=url_entry.long_url, status_code=301)


@app.get("/api/stats/{short_code}", response_model=StatsResponse)
async def get_stats(short_code: str):
    url_entry = await repo.get_by_short_code(short_code)
    if not url_entry:
        raise HTTPException(status_code=404, detail="Short URL not found")

    redis_client = cache.get_client()
    buffered = await redis_client.get(f"clicks:{short_code}")
    total_clicks = url_entry.click_count + (int(buffered) if buffered else 0)

    return StatsResponse(
        short_code=url_entry.short_code,
        long_url=url_entry.long_url,
        click_count=total_clicks,
        created_at=url_entry.created_at,
    )
