# MongoDB Design Document — Distributed Short URL Service

**Date:** 2026-07-01
**Status:** Implemented (PoC)
**Related:** [design.md](design.md) (system overview), [database-tradeoffs.md](database-tradeoffs.md) (PG vs Mongo comparison), [benchmark-results.md](benchmark-results.md) (load test results)

---

## 1. Why MongoDB

The primary design document (`design.md`) describes the system with PostgreSQL as the storage layer. This document covers the MongoDB alternative, which was added to:

1. **Eliminate ORM overhead** — MongoDB's driver returns plain dicts, avoiding SQLAlchemy's object mapping, session tracking, and identity map costs
2. **Simplify the write path** — a single `insert_one()` replaces the ORM lifecycle of `Session.add()` → `commit()` → `refresh()`
3. **Enable native horizontal sharding** — MongoDB's built-in shard key routing avoids the application-layer sharding logic needed with PostgreSQL
4. **Support schema flexibility** — fields like `metadata`, `tags`, or `utm_params` can be added per-document without ALTER TABLE on a multi-TB collection

Benchmark results confirm the motivation: MongoDB achieves **74% lower p95 latency on writes** and **83% lower p95 on stats queries** compared to the PostgreSQL+SQLAlchemy stack under identical conditions.

---

## 2. Architecture

The MongoDB stack mirrors the PostgreSQL architecture — only the database layer changes. The API servers, Redis cache, Nginx load balancer, and click-flush worker are identical.

```
┌──────────┐     ┌──────────────┐     ┌─────────────────┐     ┌───────────┐
│  Client   │────▶│ Load Balancer │────▶│  API Servers     │────▶│   Redis   │
│           │     │   (Nginx)    │     │  (FastAPI x N)   │     │  (Cache)  │
└──────────┘     └──────────────┘     └────────┬─────────┘     └───────────┘
                                               │
                                    ┌──────────┴──────────┐
                                    │   MongoDB Cluster    │
                                    │                      │
                                    │  ┌────────────────┐  │
                                    │  │ mongos (router) │  │
                                    │  └───────┬────────┘  │
                                    │  ┌───────┴────────┐  │
                                    │  │  Config Server  │  │
                                    │  │   (3-node RS)   │  │
                                    │  └───────┬────────┘  │
                                    │          │           │
                                    │  ┌───────┼────────┐  │
                                    │  │  Shard 0  │ S1  │  │
                                    │  │  (3-node  │(3n) │  │
                                    │  │   RS)     │     │  │
                                    │  └──────────┴─────┘  │
                                    └──────────────────────┘
```

### What Changes

| Component | PostgreSQL Stack | MongoDB Stack |
|---|---|---|
| Database | PostgreSQL 16 | MongoDB 7 |
| Driver | SQLAlchemy + asyncpg | Motor (async) |
| ORM | SQLAlchemy ORM | None (raw documents) |
| Repository | `PostgresRepository` | `MongoRepository` |
| Env var | `DB_BACKEND=postgres` | `DB_BACKEND=mongodb` |
| Compose file | `docker-compose.yml` | `docker-compose.mongo.yml` |

### What Stays the Same

- FastAPI application (`app/main.py`)
- Snowflake ID generation (`app/snowflake.py`)
- Base62 encoding (`app/base62.py`)
- Redis caching (`app/cache.py`)
- Click-flush worker (`app/click_worker.py`)
- Nginx load balancing (`nginx.conf`)
- API contract (all endpoints identical)
- Repository interface (`app/repository.py`)

---

## 3. Data Model

### Collection: `urls`

```javascript
{
  _id: NumberLong("264781201408010282"),    // Snowflake ID (BIGINT equivalent)
  short_code: "1rGH5cz",                   // Base62-encoded, UNIQUE index
  long_url: "https://example.com/very/long/path",
  click_count: NumberLong(0),
  created_at: ISODate("2026-07-01T12:00:00Z"),
  expires_at: ISODate("2026-07-31T12:00:00Z")  // nullable
}
```

### Indexes

```javascript
// Created on startup by MongoRepository.init()
db.urls.createIndex({ "short_code": 1 }, { unique: true })

// _id index is automatic (Snowflake ID, not ObjectId)
```

### Why Snowflake ID as `_id` (not ObjectId)

| Aspect | ObjectId | Snowflake as `_id` |
|---|---|---|
| Size | 12 bytes | 8 bytes (NumberLong) |
| Time-ordered | Yes (4-byte timestamp) | Yes (41-bit ms timestamp) |
| Base62 length | 16 chars | 7-11 chars |
| Cross-DB consistency | MongoDB-specific | Same ID in PG and Mongo |
| Shard key compatibility | Built-in | Works with hashed shard key |

Using Snowflake as `_id` means the same ID generation logic works across both database backends — switching between PostgreSQL and MongoDB doesn't change the IDs or short codes.

### Document vs Relational Mapping

```
PostgreSQL (relational)              MongoDB (document)
─────────────────────────           ─────────────────────────
urls table                           urls collection
├── id BIGINT PK                     ├── _id: NumberLong (PK)
├── short_code VARCHAR UNIQUE        ├── short_code: String (UNIQUE idx)
├── long_url TEXT                    ├── long_url: String
├── click_count BIGINT DEFAULT 0     ├── click_count: NumberLong
├── created_at TIMESTAMPTZ           ├── created_at: ISODate
└── expires_at TIMESTAMPTZ           └── expires_at: ISODate | null
```

The 1:1 mapping is deliberate — the short URL data model is simple enough that MongoDB's document flexibility doesn't add schema complexity, but it does remove the ORM layer entirely.

---

## 4. Repository Implementation

The system uses a repository abstraction (`URLRepository` ABC) that decouples the API layer from the database choice. The MongoDB implementation is in `app/mongo_repository.py`.

### Class Diagram

```
URLRepository (ABC)                    ← app/repository.py
├── init()
├── create_url(record) → URLRecord
├── get_by_short_code(code) → URLRecord?
└── increment_clicks(code, count)
         │
    ┌────┴─────────────┐
    │                  │
PostgresRepository     MongoRepository        ← app/pg_repository.py
(SQLAlchemy ORM)       (Motor, raw docs)         app/mongo_repository.py
```

### Backend Selection

```python
# app/repository.py
def get_repository() -> URLRepository:
    backend = os.getenv("DB_BACKEND", "postgres")  # default: postgres
    if backend == "mongodb":
        from app.mongo_repository import MongoRepository
        return MongoRepository()
    from app.pg_repository import PostgresRepository
    return PostgresRepository()
```

The factory uses lazy imports so neither driver needs to be installed unless selected.

### Key Implementation Details

**Connection setup:**
```python
class MongoRepository(URLRepository):
    def __init__(self):
        self.client = AsyncIOMotorClient(MONGODB_URL)
        self.db = self.client[MONGODB_DB]
        self.urls = self.db["urls"]
```

Motor's `AsyncIOMotorClient` manages a connection pool internally. Default pool size is 100 connections — sufficient for the PoC, tunable via `maxPoolSize` URI parameter for production.

**Index creation (idempotent):**
```python
async def init(self):
    await self.urls.create_index("short_code", unique=True)
```

`create_index()` is a no-op if the index already exists. This avoids the race condition that PostgreSQL hit with concurrent `CREATE TABLE` calls from multiple API servers.

**Write path (no ORM):**
```python
async def create_url(self, record: URLRecord) -> URLRecord:
    doc = {
        "_id": record.id,           # Snowflake ID, not ObjectId
        "short_code": record.short_code,
        "long_url": record.long_url,
        "click_count": 0,
        "created_at": record.created_at or datetime.now(timezone.utc),
        "expires_at": record.expires_at,
    }
    await self.urls.insert_one(doc)  # single async call, no commit/refresh
    return URLRecord(...)
```

vs PostgreSQL (3 async calls):
```python
session.add(url_obj)          # 1. stage in unit-of-work
await session.commit()        # 2. INSERT + WAL flush
await session.refresh(url_obj) # 3. SELECT to get server defaults
```

**Atomic click increment:**
```python
async def increment_clicks(self, short_code: str, count: int) -> None:
    await self.urls.update_one(
        {"short_code": short_code},
        {"$inc": {"click_count": count}},  # atomic, no read-modify-write
    )
```

MongoDB's `$inc` operator is atomic at the document level — no row-level lock contention, no explicit transaction needed.

---

## 5. Request Flows

### Create Flow (MongoDB)

```
Client → Nginx → API Server
  1. Validate URL (Pydantic)
  2. Generate Snowflake ID (in-memory, no DB call)
  3. Base62 encode → short_code
  4. MongoRepository.create_url()
     └── insert_one({_id: snowflake, short_code, long_url, ...})
         └── BSON serialize → wire protocol → journal write
  5. cache.cache_url(short_code, long_url) → Redis SET
  6. Return JSON response
```

**Latency breakdown (p50 at C=10):**
```
Pydantic validation:   ~0.1 ms
Snowflake + Base62:    ~0.01 ms
insert_one():          ~1.5 ms   ← vs ~4.0 ms (SQLAlchemy add+commit+refresh)
Redis SET:             ~0.5 ms
JSON serialization:    ~0.1 ms
────────────────────────────────
Total:                 ~2.5 ms   ← vs ~5.2 ms (PostgreSQL)
```

### Redirect Flow (identical for both backends)

```
Client → Nginx → API Server
  1. Redis GET url:{short_code}     → ~0.5 ms
  2. HIT  → Redis INCR clicks:{code} → ~0.3 ms, return 301
  3. MISS → MongoRepository.get_by_short_code()
     └── find_one({"short_code": code})
         └── BSON deserialize → URLRecord
     → cache result in Redis, return 301
```

Redirects are **Redis-dominated** — the database choice is irrelevant for 90%+ of reads. Benchmark confirms: redirect latency is within 8% between backends.

### Click Flush Flow

```
Click Worker (every 10 seconds):
  1. cache.flush_clicks() → SCAN clicks:* → GETDEL each
  2. For each {short_code: count}:
     └── MongoRepository.increment_clicks(short_code, count)
         └── update_one({"short_code": ...}, {"$inc": {"click_count": count}})
```

MongoDB's `$inc` is ideal here — atomic increment without read-modify-write cycles.

---

## 6. Docker Compose Setup

The MongoDB stack runs as a standalone compose file (`docker-compose.mongo.yml`), not an override. This avoids dependency issues with the PostgreSQL service declarations.

### Services

```yaml
services:
  mongodb:        # MongoDB 7, healthcheck via mongosh ping
  redis:          # Redis 7-alpine, port 6379
  api1:           # FastAPI, WORKER_ID=1, DB_BACKEND=mongodb
  api2:           # FastAPI, WORKER_ID=2, DB_BACKEND=mongodb
  click-worker:   # Background flush worker, DB_BACKEND=mongodb
  nginx:          # Load balancer, port 80
```

### Running

```bash
# Start MongoDB stack
docker compose -f docker-compose.mongo.yml up -d --build

# Verify health
curl http://localhost/health

# Create a short URL
curl -X POST http://localhost/api/shorten \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/hello"}'

# Redirect
curl -L http://localhost/{short_code}

# Stats
curl http://localhost/api/stats/{short_code}

# Teardown
docker compose -f docker-compose.mongo.yml down -v
```

### Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `DB_BACKEND` | `postgres` | Set to `mongodb` to use Mongo |
| `MONGODB_URL` | `mongodb://localhost:27017` | Connection string |
| `MONGODB_DB` | `shorturl` | Database name |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection |
| `WORKER_ID` | required | Snowflake worker ID (0-1023) |
| `BASE_URL` | `http://localhost` | Base URL for short links |
| `CLICK_FLUSH_INTERVAL` | `10` | Seconds between flush cycles |

---

## 7. Scaling MongoDB for 300M DAU

### 7.1 Sharding Strategy

MongoDB's native sharding distributes data across shards via a shard key. For the short URL service:

```
Shard Key: { short_code: "hashed" }
```

**Why hashed `short_code`:**
- Uniform distribution across shards (Snowflake IDs are time-ordered, which would cause hot-shard problems with range-based sharding)
- All queries include `short_code` — every lookup hits exactly one shard (targeted queries, not scatter-gather)
- The UNIQUE index on `short_code` is the shard key index — no additional index overhead

```
                    ┌─────────────────────┐
                    │    mongos (router)    │
                    │  Routes by hash(code) │
                    └──────────┬──────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
     ┌────────▼────────┐ ┌────▼────────┐ ┌────▼────────────┐
     │   Shard 0       │ │  Shard 1    │ │   Shard 2       │
     │ hash range 0-5k │ │ range 5k-10k│ │ range 10k-16k   │
     │ Primary + 2 Sec │ │ P + 2 Sec   │ │ P + 2 Sec       │
     │  50M docs       │ │  50M docs   │ │  50M docs       │
     └─────────────────┘ └─────────────┘ └─────────────────┘
```

### 7.2 Replica Set Configuration

Each shard runs as a 3-member replica set:

```
Shard 0 Replica Set:
  ├── Primary   (writes + reads)
  ├── Secondary (reads with readPreference=secondaryPreferred)
  └── Secondary (reads + backup)
```

**Read preference for the short URL service:**
- **Writes (create):** Primary only — strong consistency for URL creation
- **Reads (redirect cache miss):** `secondaryPreferred` — eventual consistency is acceptable (URL data is immutable after creation)
- **Stats queries:** `secondaryPreferred` — click counts are already eventually consistent due to Redis buffering

### 7.3 Capacity Sizing

| Component | PoC | 300M DAU |
|---|---|---|
| **mongos routers** | N/A (standalone) | 2-3 (behind LB) |
| **Config servers** | N/A | 3-node RS |
| **Shards** | 1 standalone | **3 shards × 3 nodes = 9 nodes** |
| **Total Mongo nodes** | 1 | **12-15** |

**Write throughput:**
```
Peak: 5,200 inserts/sec
Per shard: 5,200 / 3 = 1,733 inserts/sec
MongoDB single-node: ~30,000 inserts/sec (simple docs)
Utilization: 5.8% per shard → comfortable headroom
```

**Read throughput (cache misses):**
```
95% cache hit → 208K × 5% = 10,400 reads/sec
Per shard: 10,400 / 3 = 3,467 reads/sec
With 2 secondaries per shard: 3,467 / 3 = ~1,156 reads/sec/node
MongoDB single-node: ~50,000 reads/sec
Utilization: 2.3% → very comfortable
```

**Storage:**
```
Document size: ~300 bytes (BSON, smaller than SQL row overhead)
150M docs/day × 300B = 45 GB/day
45 GB × 365 = 16.4 TB/year
Per shard (3 shards): 5.5 TB/year
MongoDB recommended max per shard: ~2 TB working set
→ Add 4th shard at ~4 months, or enable TTL index for auto-expiry
```

### 7.4 TTL Index for Auto-Expiry

MongoDB supports TTL indexes that automatically delete expired documents:

```javascript
db.urls.createIndex(
  { "expires_at": 1 },
  { expireAfterSeconds: 0 }    // delete when expires_at is in the past
)
```

This eliminates the need for a separate cleanup cron job — MongoDB's background thread handles it automatically with minimal performance impact.

### 7.5 MongoDB vs PostgreSQL at Scale

| Scaling Dimension | PostgreSQL | MongoDB |
|---|---|---|
| **Sharding** | Application-layer (hash routing, multiple connection strings) | Built-in (`sh.shardCollection()`) |
| **Adding shards** | Manual: create DB, migrate data, update routing | `sh.addShard()` — automatic chunk balancing |
| **Rebalancing** | Manual export/import | Automatic chunk migration |
| **Read replicas** | Streaming replication, manual routing | `readPreference` in connection string |
| **Schema migration** | `ALTER TABLE` (locks on large tables) | No-op (schemaless), or `$set` default values |
| **Operational overhead** | Lower (fewer nodes, mature tooling) | Higher (mongos, config servers, shard management) |

---

## 8. Production Considerations

### 8.1 Connection Pooling

```python
# Production connection string with tuning
MONGODB_URL = (
    "mongodb://mongos1:27017,mongos2:27017/shorturl"
    "?replicaSet=rs0"
    "&maxPoolSize=200"
    "&minPoolSize=10"
    "&maxIdleTimeMS=30000"
    "&retryWrites=true"
    "&w=majority"
    "&readPreference=secondaryPreferred"
)
```

| Parameter | Value | Why |
|---|---|---|
| `maxPoolSize` | 200 | 50 API servers × 4 connections each |
| `retryWrites` | true | Automatic retry on transient network errors |
| `w=majority` | — | Write acknowledged by majority of replica set |
| `readPreference` | secondaryPreferred | Route reads to secondaries, fall back to primary |

### 8.2 Write Concern Tradeoffs

| Write Concern | Durability | Latency | Use Case |
|---|---|---|---|
| `w=1` (default) | Primary acknowledged | ~1-2 ms | PoC, dev |
| `w=majority` | Majority acknowledged | ~3-5 ms | Production writes |
| `w=majority, j=true` | Majority + journal flush | ~5-10 ms | Financial/critical data |

For the short URL service, `w=majority` is the right choice — URL creation should survive a primary failover, but we don't need journal-level durability since lost URLs can be recreated.

### 8.3 Monitoring

Key MongoDB metrics to track:

| Metric | Threshold | Action |
|---|---|---|
| `opcounters.insert` | > 20K/sec/shard | Add shard |
| `opcounters.query` | > 40K/sec/node | Add secondary |
| `connections.current` | > 80% of maxPoolSize | Increase pool or add mongos |
| `globalLock.activeClients.total` | > 100 | Investigate slow queries |
| `repl.lag` | > 10 seconds | Investigate secondary health |
| `mem.resident` | > 80% of RAM | Scale up or add shard |
| `wiredTiger.cache.bytes` | > 80% of configured | Increase cache size |

### 8.4 Backup Strategy

```
mongodump --oplog --gzip --archive=/backup/shorturl-$(date +%Y%m%d).gz
```

| Strategy | RPO | Impact |
|---|---|---|
| **Continuous oplog backup** | ~seconds | Zero (reads from secondary) |
| **Daily mongodump** | 24 hours | Low (runs against secondary) |
| **Cloud snapshots (Atlas)** | Point-in-time | Zero (managed) |

---

## 9. Migration Path: PostgreSQL → MongoDB

If starting with PostgreSQL and migrating to MongoDB later:

### Phase 1: Dual-Write (1-2 weeks)

```
API Server
  ├── Write to PostgreSQL (primary)
  └── Write to MongoDB (shadow, async)
      └── Compare: verify documents match rows
```

### Phase 2: Read from MongoDB (1 week)

```
API Server
  ├── Write to both (PostgreSQL primary)
  ├── Read from MongoDB (primary reads)
  └── Fallback to PostgreSQL on error
```

### Phase 3: MongoDB Primary (1 week)

```
API Server
  ├── Write to MongoDB (primary)
  ├── Write to PostgreSQL (shadow, for rollback)
  └── Read from MongoDB only
```

### Phase 4: Decommission PostgreSQL

```
API Server
  ├── Write to MongoDB only
  └── Read from MongoDB only
  
PostgreSQL: kept for 30 days as cold backup, then decommissioned
```

The repository abstraction makes this migration straightforward — each phase is a config change (`DB_BACKEND` env var), not a code change.

---

## 10. Summary

| Aspect | Decision | Rationale |
|---|---|---|
| **Driver** | Motor (async) | Native async, no ORM overhead |
| **`_id` field** | Snowflake ID (not ObjectId) | Cross-backend consistency, shorter Base62 |
| **Shard key** | `hashed(short_code)` | Uniform distribution, targeted queries |
| **Write concern** | `w=majority` | Survives primary failover |
| **Read preference** | `secondaryPreferred` | Offload reads, acceptable for immutable URL data |
| **TTL expiry** | TTL index on `expires_at` | Automatic cleanup, no cron job |
| **Connection pool** | 200 per mongos | Sized for 50 API servers |
| **Compose file** | Standalone (not override) | Avoids cross-dependency with PG services |

The MongoDB backend is a drop-in replacement for PostgreSQL via the repository pattern. It trades PostgreSQL's SQL analytics and ecosystem maturity for MongoDB's lower write latency, native sharding, and schema flexibility — the right choice when write throughput or horizontal scaling is the primary constraint.
