import os
from datetime import datetime, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient

from app.repository import URLRepository, URLRecord

MONGODB_URL = os.getenv("MONGODB_URL", "mongodb://localhost:27017")
MONGODB_DB = os.getenv("MONGODB_DB", "shorturl")


class MongoRepository(URLRepository):
    def __init__(self):
        self.client = AsyncIOMotorClient(MONGODB_URL)
        self.db = self.client[MONGODB_DB]
        self.urls = self.db["urls"]

    async def init(self):
        await self.urls.create_index("short_code", unique=True)

    async def create_url(self, record: URLRecord) -> URLRecord:
        now = datetime.now(timezone.utc)
        doc = {
            "_id": record.id,
            "short_code": record.short_code,
            "long_url": record.long_url,
            "click_count": 0,
            "created_at": record.created_at or now,
            "expires_at": record.expires_at,
        }
        await self.urls.insert_one(doc)
        return URLRecord(
            id=doc["_id"],
            short_code=doc["short_code"],
            long_url=doc["long_url"],
            click_count=0,
            created_at=doc["created_at"],
            expires_at=doc["expires_at"],
        )

    async def get_by_short_code(self, short_code: str) -> Optional[URLRecord]:
        doc = await self.urls.find_one({"short_code": short_code})
        if not doc:
            return None
        return URLRecord(
            id=doc["_id"],
            short_code=doc["short_code"],
            long_url=doc["long_url"],
            click_count=doc["click_count"],
            created_at=doc["created_at"],
            expires_at=doc.get("expires_at"),
        )

    async def increment_clicks(self, short_code: str, count: int) -> None:
        await self.urls.update_one(
            {"short_code": short_code},
            {"$inc": {"click_count": count}},
        )
