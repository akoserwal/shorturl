import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class URLRecord:
    id: int
    short_code: str
    long_url: str
    click_count: int = 0
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class URLRepository(ABC):
    @abstractmethod
    async def init(self) -> None: ...

    @abstractmethod
    async def create_url(self, record: URLRecord) -> URLRecord: ...

    @abstractmethod
    async def get_by_short_code(self, short_code: str) -> Optional[URLRecord]: ...

    @abstractmethod
    async def increment_clicks(self, short_code: str, count: int) -> None: ...


def get_repository() -> URLRepository:
    backend = os.getenv("DB_BACKEND", "postgres")
    if backend == "mongodb":
        from app.mongo_repository import MongoRepository
        return MongoRepository()
    from app.pg_repository import PostgresRepository
    return PostgresRepository()
