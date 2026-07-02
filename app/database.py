import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import BigInteger, Text, DateTime, func

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://shorturl:shorturl@localhost:5432/shorturl",
)

engine = create_async_engine(DATABASE_URL, pool_size=20, max_overflow=10)
async_session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class URL(Base):
    __tablename__ = "urls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    short_code: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    long_url: Mapped[str] = mapped_column(Text, nullable=False)
    click_count: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at = mapped_column(DateTime(timezone=True), nullable=True)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, checkfirst=True)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
