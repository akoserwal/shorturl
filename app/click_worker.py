import asyncio
import logging
import os

from app.repository import get_repository
from app import cache

FLUSH_INTERVAL = int(os.getenv("CLICK_FLUSH_INTERVAL", "10"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("click_worker")

repo = get_repository()


async def flush_clicks_to_db():
    flushed = await cache.flush_clicks()
    if not flushed:
        return

    for short_code, count in flushed.items():
        await repo.increment_clicks(short_code, count)

    log.info("Flushed %d URL click counts to DB", len(flushed))


async def main():
    await repo.init()
    log.info("Click worker started (flush every %ds, backend=%s)",
             FLUSH_INTERVAL, os.getenv("DB_BACKEND", "postgres"))
    while True:
        try:
            await flush_clicks_to_db()
        except Exception:
            log.exception("Flush failed, will retry next interval")
        await asyncio.sleep(FLUSH_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
