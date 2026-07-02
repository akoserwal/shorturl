# Distributed Short URL System — Design Document

**Date:** 2026-07-01
**Status:** Draft

---

## 1. Problem Statement

Design a distributed URL shortening service (like bit.ly/tinyurl) that converts long URLs into short, unique codes and redirects users back to the original URL. The system must handle high read throughput, scale horizontally, and generate globally unique short codes without central coordination.

---

## 2. Functional Requirements

| Requirement | Details |
|---|---|
| **Create short URL** | `POST /api/shorten` — accepts a long URL, returns a short code |
| **Redirect** | `GET /{short_code}` — 301 redirect to original URL |
| **Analytics** | Track click count per URL |
| **Expiration** | Optional TTL per URL |
| **Idempotency** | Same long URL returns same short code (optional, configurable) |

## 3. Non-Functional Requirements

| Requirement | Target |
|---|---|
| **Availability** | 99.9% uptime |
| **Latency** | < 10ms redirect (cache hit), < 50ms (cache miss) |
| **Read:Write ratio** | ~100:1 |
| **Scale** | 100M+ URLs, 10K+ redirects/sec |
| **Consistency** | Eventual consistency acceptable for analytics; strong for URL creation |

---

## 4. Architecture Overview

```
┌──────────┐     ┌──────────────┐     ┌─────────────────┐     ┌───────────┐
│  Client   │────▶│ Load Balancer │────▶│  API Servers     │────▶│   Redis   │
│           │     │   (Nginx)    │     │  (FastAPI x N)   │     │  (Cache)  │
└──────────┘     └──────────────┘     └────────┬─────────┘     └───────────┘
                                               │
                                    ┌──────────┴──────────┐
                                    │                     │
                               ┌────▼─────┐         ┌────▼─────┐
                               │ Postgres │         │ Postgres │
                               │ Primary  │────────▶│ Replica  │
                               └──────────┘  async  └──────────┘
                                               repl
```

### Components

| Component | Role | Scaling Strategy |
|---|---|---|
| **Nginx** | Load balancer, rate limiting, SSL termination | Horizontal (DNS round-robin) |
| **FastAPI** | Business logic, URL shortening, redirect | Horizontal (stateless) |
| **PostgreSQL** | Persistent URL storage, ID generation | Primary-replica, partitioning by short_code hash |
| **Redis** | Cache hot URLs, reduce DB reads | Cluster mode, LRU eviction |

---

## 5. Distributed ID Generation

The core challenge: generating globally unique short codes across multiple API servers without a single point of failure.

### Approach: Snowflake-inspired ID + Base62

Each API server generates a 64-bit ID composed of:

```
┌───────────────────┬────────────┬──────────────┐
│  Timestamp (ms)   │ Worker ID  │  Sequence    │
│    41 bits        │  10 bits   │  12 bits     │
└───────────────────┴────────────┴──────────────┘
```

- **41 bits timestamp** — milliseconds since custom epoch (~69 years)
- **10 bits worker ID** — supports up to 1024 API server instances
- **12 bits sequence** — 4096 IDs per millisecond per worker

The 64-bit ID is then encoded to **Base62** (a-z, A-Z, 0-9), producing a 7-11 character short code.

**Why not UUID?** UUIDs are 128-bit, producing 22+ char Base62 strings — too long for a short URL service.

**Why not a central counter?** Single point of failure, bottleneck under high write load.

---

## 6. Data Model

### PostgreSQL — `urls` table

```sql
CREATE TABLE urls (
    id          BIGINT       PRIMARY KEY,
    short_code  VARCHAR(12)  NOT NULL UNIQUE,
    long_url    TEXT         NOT NULL,
    click_count BIGINT       DEFAULT 0,
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    expires_at  TIMESTAMPTZ
);

CREATE INDEX idx_urls_short_code ON urls (short_code);
CREATE INDEX idx_urls_long_url ON urls USING hash (long_url);
```

### Redis Cache

```
KEY:   url:{short_code}
VALUE: {long_url}
TTL:   86400 seconds (24 hours)
```

---

## 7. API Design

### POST /api/shorten

**Request:**
```json
{
  "url": "https://example.com/very/long/path",
  "expires_in_days": 30
}
```

**Response (201):**
```json
{
  "short_url": "http://localhost:8000/abc1234",
  "short_code": "abc1234",
  "long_url": "https://example.com/very/long/path",
  "created_at": "2026-07-01T12:00:00Z",
  "expires_at": "2026-07-31T12:00:00Z"
}
```

### GET /{short_code}

**Response:** 301 Redirect with `Location` header → `long_url`

### GET /api/stats/{short_code}

**Response (200):**
```json
{
  "short_code": "abc1234",
  "long_url": "https://example.com/very/long/path",
  "click_count": 1542,
  "created_at": "2026-07-01T12:00:00Z"
}
```

---

## 8. Request Flows

### Create Flow
```
Client → Nginx → API Server
  1. Validate URL format
  2. Generate Snowflake ID (no DB round-trip)
  3. Encode ID → Base62 short_code
  4. INSERT into PostgreSQL
  5. SET in Redis cache
  6. Return short_url
```

### Redirect Flow
```
Client → Nginx → API Server
  1. GET from Redis (cache lookup)
  2. HIT  → increment click_count async, redirect 301
  3. MISS → SELECT from PostgreSQL
     a. Found    → cache in Redis, redirect 301
     b. Not found → 404
```

---

## 9. Scale Estimates — 300M DAU Target

### Traffic Model

```
300M DAU
├── 10% create URLs → 30M creators
│   └── 5 URLs/day each → 150M writes/day
└── 100% read URLs → 300M readers
    └── 20 reads/day each → 6B reads/day
```

| Metric | Daily | Per Second (avg) | Peak (3x burst) |
|---|---|---|---|
| **Writes** (create) | 150M | 1,736/sec | **5,200/sec** |
| **Reads** (redirect) | 6B | 69,444/sec | **208,000/sec** |
| **Read:Write ratio** | 40:1 | | |

### Component Sizing

#### API Servers (FastAPI + Uvicorn)

Single Uvicorn: ~3-5K async req/sec.

| Traffic | Peak req/sec | Per server | Servers needed |
|---|---|---|---|
| Writes | 5,200 | 3,000 | 2 |
| Reads | 208,000 | 5,000 | 42 |
| **Total** | **213,200** | | **~50 servers** |

Snowflake supports 1,024 workers, so 50 is well within range.

#### Redis Cluster

Single Redis: ~100K ops/sec. Each redirect = GET + INCR = 2 ops.

| Traffic | Ops/sec (peak) | Per node | Nodes needed |
|---|---|---|---|
| Reads | 208K × 2 = 416K | 100K | 5 primary |
| Writes (cache set) | 5,200 | included | — |
| **Total** | **~420K ops/sec** | | **6 nodes (3P + 3R)** |

#### PostgreSQL

| Path | Depends on cache hit rate | 80% hit | 90% hit | 95% hit |
|---|---|---|---|---|
| Write (INSERT) | Always hits primary | 5,200/sec | 5,200/sec | 5,200/sec |
| Read (SELECT) | Cache misses only | 41,600/sec | 20,800/sec | 10,400/sec |
| **Primary nodes** | | 1 | 1 | 1 |
| **Read replicas** | | **5** | **3** | **1-2** |

#### Storage Growth

```
150M URLs/day × 500 bytes = 75 GB/day
75 GB × 30 days = 2.25 TB/month
75 GB × 365 days = 27 TB/year
```

Single Postgres maxes out at ~5-10 TB usable. **Need sharding after ~3 months**
or aggressive TTL expiry to cap table size.

#### Network Bandwidth

```
Redirect: ~500 bytes/response
208K req/sec × 500B = 104 MB/sec = ~830 Mbps
```

Near 1 Gbps NIC saturation. Need multiple LB instances or 10G networking.

### Capacity Summary

| Component | Current PoC | Needed for 300M DAU |
|---|---|---|
| API Servers | 2 | **50** |
| Redis | 1 standalone | **6-node cluster** |
| Postgres Primary | 1 | 1 (sufficient) |
| Postgres Replicas | 0 | **3-5** |
| Postgres Shards | 1 | **3+ after 3 months** |
| Load Balancers | 1 Nginx | **Multiple behind ALB** |
| Click Workers | 1 | **3-5** |

### Base62 Capacity Check

```
150M URLs/day × 365 × 10 years = 547.5 billion URLs
62^7 = 3.5 trillion codes  ✓  (6.4x headroom)
```

---

## 10. Scaling Analysis — What Breaks at Millions of Users

### Bottleneck #1: Synchronous click-count UPDATE on every redirect (CRITICAL)

Every redirect does `UPDATE urls SET click_count = click_count + 1` synchronously.
At 10K redirects/sec this creates 10K write TPS on the primary — contention on hot rows,
increased WAL volume, and the redirect latency now includes a DB round-trip.

**Fix:** Buffer clicks in Redis via `INCR clicks:{short_code}`, flush to Postgres in
batches (every 10s or every 1000 clicks) via a background worker. Redirect path becomes
read-only — Redis GET + Redis INCR, no Postgres at all on cache hit.

### Bottleneck #2: Single PostgreSQL — no replicas, no sharding

One Postgres handles all reads AND writes. Ceiling: ~5-10K simple-query TPS per node.

**Fix (Phase 1):** Add streaming replicas. Route redirect reads (SELECT) to replicas,
writes (INSERT) to primary. This alone gives 3-5x read throughput.

**Fix (Phase 2):** Hash-based sharding on `short_code[0:2]` across N Postgres clusters
when data exceeds single-node storage (~500M+ URLs).

### Bottleneck #3: Single Redis — no HA, no clustering

One Redis = ~100K ops/sec ceiling, no failover. If Redis dies → thundering herd on Postgres.

**Fix:** Redis Sentinel (3 nodes) for HA, or Redis Cluster (6+ nodes) for both HA and
horizontal scaling. Add circuit breaker so Redis failure gracefully degrades to DB reads
instead of cascading failure.

### Bottleneck #4: Worker ID assignment is manual

Hardcoded `WORKER_ID` env var doesn't work with auto-scaling (K8s HPA, ECS).

**Fix:** Lease-based worker ID from Redis or etcd:
```
SETNX worker:{id} {hostname} EX 30  # try IDs 0-1023 until one succeeds
```
Renew lease every 15s. On shutdown, release the key.

### Bottleneck #5: Single Nginx — SPOF

**Fix:** Multiple Nginx behind cloud LB (ALB/NLB) or DNS round-robin with health checks.

### Bottleneck #6: No rate limiting

**Fix:** Nginx `limit_req_zone` for basic throttling, or a token-bucket in Redis for
distributed rate limiting across API servers.

---

## 11. Production Architecture (300M DAU)

```
                    ┌─────────────────────────────────────┐
                    │          CDN (CloudFront/Fastly)     │
                    │  • Cache 301 redirects at edge       │
                    │  • 50-80% of reads never hit origin  │
                    └─────────────────┬───────────────────┘
                                      │
                    ┌─────────────────▼───────────────────┐
                    │    Cloud Load Balancer (ALB x 2+)    │
                    │    • 10 Gbps, multi-AZ               │
                    └─────────────────┬───────────────────┘
                                      │
    ┌──────────┬──────────┬──────────┬▼─────────┬──────────┐
    │ API #1   │ API #2   │ API #3   │  ...     │ API #50  │
    │ wkr=1    │ wkr=2    │ wkr=3    │          │ wkr=50   │
    │ FastAPI  │ FastAPI  │ FastAPI  │          │ FastAPI  │
    └────┬─────┴────┬─────┴────┬─────┴──────────┴────┬─────┘
         │          │          │                      │
    ┌────▼──────────▼──────────▼──────────────────────▼────┐
    │        Redis Cluster (6 nodes: 3 primary + 3 replica) │
    │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐  │
    │  │  Shard 0-5461│ │Shard 5462-10k│ │Shard 10k-16k │  │
    │  │   P + R      │ │   P + R      │ │   P + R      │  │
    │  └──────────────┘ └──────────────┘ └──────────────┘  │
    │  420K ops/sec capacity  |  Auto-failover              │
    └────┬─────────────────────────────────────────────────┘
         │
    ┌────▼─────────────────────────────────────────────────┐
    │  PostgreSQL — Sharded by hash(short_code) % 3        │
    │                                                      │
    │  Shard 0             Shard 1             Shard 2     │
    │  ┌───────┐           ┌───────┐           ┌───────┐  │
    │  │Primary│──▶R1,R2   │Primary│──▶R1,R2   │Primary│  │
    │  │5.2K w │  21K r/s  │5.2K w │  21K r/s  │──▶R1  │  │
    │  └───────┘           └───────┘           └───────┘  │
    │  3 primaries + 5 replicas = ~8 nodes                 │
    └──────────────────────────────────────────────────────┘
         │
    ┌────▼─────────────────────────────────────────────────┐
    │  Background Workers (3-5 instances)                   │
    │  • Click flush: Redis → Postgres batch (every 10s)   │
    │  • URL expiry cleanup (hourly cron)                  │
    │  • Metrics → Prometheus + Grafana                    │
    │  • Abuse detection pipeline                          │
    └──────────────────────────────────────────────────────┘
```

### CDN Optimization (Critical for 6B reads/day)

A CDN caching 301 redirects at the edge is the single biggest cost/latency reducer:

```
Cache-Control: public, max-age=86400    (24h for permanent URLs)
Cache-Control: public, max-age=3600     (1h for expiring URLs)
```

At 60% CDN hit rate:
- 6B reads → only 2.4B reach origin → 28K req/sec instead of 69K
- API servers drop from 50 to ~20
- Postgres replicas drop from 5 to 2

---

## 12. PoC Scope

The proof-of-concept implements:
- Snowflake ID generation (distributed-ready, configurable worker ID)
- Base62 encoding
- FastAPI with create, redirect, and stats endpoints
- PostgreSQL for persistence with read-replica routing
- Redis for caching + buffered click counting
- Background click-flush worker
- Docker Compose for local orchestration (2 API servers, Nginx, Postgres, Redis)
- Health check endpoint
