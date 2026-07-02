from typing import Optional
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.repository import URLRepository, URLRecord
from app.database import DATABASE_URL, Base, URL


class PostgresRepository(URLRepository):
    def __init__(self):
        self.engine = create_async_engine(DATABASE_URL, pool_size=20, max_overflow=10)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, checkfirst=True)

    async def create_url(self, record: URLRecord) -> URLRecord:
        async with self.session_factory() as session:
            url = URL(
                id=record.id,
                short_code=record.short_code,
                long_url=record.long_url,
                expires_at=record.expires_at,
            )
            session.add(url)
            await session.commit()
            await session.refresh(url)
            return URLRecord(
                id=url.id,
                short_code=url.short_code,
                long_url=url.long_url,
                click_count=url.click_count,
                created_at=url.created_at,
                expires_at=url.expires_at,
            )

    async def get_by_short_code(self, short_code: str) -> Optional[URLRecord]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(URL).where(URL.short_code == short_code)
            )
            url = result.scalar_one_or_none()
            if not url:
                return None
            return URLRecord(
                id=url.id,
                short_code=url.short_code,
                long_url=url.long_url,
                click_count=url.click_count,
                created_at=url.created_at,
                expires_at=url.expires_at,
            )

    async def increment_clicks(self, short_code: str, count: int) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                update(URL)
                .where(URL.short_code == short_code)
                .values(click_count=URL.click_count + count)
            )
