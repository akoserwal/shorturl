# Distributed Short URL Service

A distributed URL shortening service designed for scale (300M DAU target). Generates globally unique short codes across multiple servers without central coordination using Snowflake IDs + Base62 encoding.

## Architecture

```
Client → Nginx (LB) → FastAPI (x N) → Redis (cache) → PostgreSQL / MongoDB
                                    ↘ Click Worker (background flush)
```

- **Snowflake ID** — 64-bit distributed ID (41-bit timestamp + 10-bit worker + 12-bit sequence), no DB round-trip
- **Base62 encoding** — compact 7-11 character short codes
- **Redis caching** — 90%+ redirect cache hit rate, buffered click counting via INCR + background flush
- **Dual database support** — PostgreSQL (SQLAlchemy) or MongoDB (Motor) via repository pattern, switchable by env var
- **Horizontally scalable** — stateless API servers, up to 1,024 workers

## Quick Start

### Prerequisites

- Docker and Docker Compose (or Podman)
- Python 3.12+ (for load tests only)

### Run with PostgreSQL

```bash
docker compose up -d --build
```

### Run with MongoDB

```bash
docker compose -f docker-compose.mongo.yml up -d --build
```

### Verify

```bash
# Health check
curl http://localhost/health

# Create a short URL
curl -s -X POST http://localhost/api/shorten \
  -H "Content-Type: application/json" \
  -d '{"url": "https://github.com"}' | jq

# Redirect (follow the short URL)
curl -L http://localhost/{short_code}

# View stats
curl -s http://localhost/api/stats/{short_code} | jq
```

### Teardown

```bash
# PostgreSQL stack
docker compose down -v

# MongoDB stack
docker compose -f docker-compose.mongo.yml down -v
```

## API

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/shorten` | Create a short URL |
| `GET` | `/{short_code}` | 301 redirect to original URL |
| `GET` | `/api/stats/{short_code}` | Click count and metadata |
| `GET` | `/health` | Health check |

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
  "short_url": "http://localhost/1rGH5cz",
  "short_code": "1rGH5cz",
  "long_url": "https://example.com/very/long/path",
  "created_at": "2026-07-01T12:00:00Z",
  "expires_at": "2026-07-31T12:00:00Z"
}
```

## Project Structure

```
shorturl/
├── app/
│   ├── main.py              # FastAPI application, endpoints
│   ├── snowflake.py          # Distributed ID generator
│   ├── base62.py             # Base62 encode/decode
│   ├── cache.py              # Redis caching + click buffering
│   ├── click_worker.py       # Background flush: Redis → DB
│   ├── repository.py         # URLRepository ABC + factory
│   ├── pg_repository.py      # PostgreSQL implementation (SQLAlchemy)
│   ├── mongo_repository.py   # MongoDB implementation (Motor)
│   └── database.py           # SQLAlchemy engine, ORM model
├── docs/
│   ├── design.md             # System design document
│   ├── mongodb-design.md     # MongoDB-specific design
│   ├── database-tradeoffs.md # PostgreSQL vs MongoDB analysis
│   ├── benchmark-results.md  # Load test comparison results
│   └── snowflake-id-deep-dive.md  # Snowflake ID algorithm
├── diagrams/
│   ├── architecture.excalidraw          # Production architecture
│   ├── snowflake-id.excalidraw          # Snowflake bit layout + flow
│   └── snowflake-collision-avoidance.excalidraw  # 5-layer collision prevention
├── loadtest/
│   ├── bench.py              # Async load test (aiohttp)
│   └── run_bench.sh          # Full PG vs Mongo benchmark
├── results/
│   ├── postgres.json         # PostgreSQL benchmark data
│   └── mongodb.json          # MongoDB benchmark data
├── docker-compose.yml        # PostgreSQL stack
├── docker-compose.mongo.yml  # MongoDB stack
├── Dockerfile                # Python 3.12-slim
├── nginx.conf                # Load balancer config
└── requirements.txt          # Python dependencies
```

## Database Backends

The repository pattern lets you switch databases with one environment variable:

```bash
DB_BACKEND=postgres   # default — SQLAlchemy + asyncpg
DB_BACKEND=mongodb    # Motor async driver, no ORM
```

### Benchmark Summary (C=50)

| Operation | PostgreSQL p95 | MongoDB p95 | Winner |
|---|---|---|---|
| **Create** | 164 ms | 29 ms | MongoDB (5.6x) |
| **Redirect** | 18 ms | 15 ms | Tied (Redis-dominated) |
| **Stats** | 85 ms | 14 ms | MongoDB (5.9x) |

MongoDB wins writes and reads due to no ORM overhead. Redirects are tied because Redis handles 90%+ of reads. See [docs/benchmark-results.md](docs/benchmark-results.md) for full analysis.

## Snowflake ID Generation

Each API server generates unique IDs without coordination:

```
64-bit ID = [Timestamp 41 bits][Worker ID 10 bits][Sequence 12 bits]
```

- **4,096,000 IDs/sec** per server
- **1,024 servers** supported concurrently
- **~69 years** before timestamp overflow (custom epoch: 2024-07-01)
- **5 collision avoidance layers**: worker ID separation, sequence counter, spin-wait on overflow, thread mutex, DB UNIQUE constraint

See [docs/snowflake-id-deep-dive.md](docs/snowflake-id-deep-dive.md) for the full algorithm breakdown.

## Load Testing

### Run the full comparison

```bash
# Install test dependencies
pip install aiohttp

# Run PG vs MongoDB benchmark end-to-end
./loadtest/run_bench.sh
```

### Run against a single backend

```bash
python3 loadtest/bench.py --url http://localhost --tag my-test

# Custom parameters
python3 loadtest/bench.py \
  --url http://localhost \
  --tag stress \
  --concurrency 10,50,100,200 \
  --creates 2000 \
  --redirects 10000 \
  --stats-requests 500
```

### Compare results

```bash
python3 loadtest/bench.py --compare results/postgres.json results/mongodb.json
```

## Scaling for 300M DAU

| Metric | Daily | Peak (3x burst) |
|---|---|---|
| Writes (create) | 150M | 5,200/sec |
| Reads (redirect) | 6B | 208,000/sec |

| Component | PoC | Production |
|---|---|---|
| API Servers | 2 | ~50 |
| Redis | 1 standalone | 6-node cluster |
| Database Primary | 1 | 1 (PG) or 3 shards (Mongo) |
| Database Replicas | 0 | 3-5 |
| Click Workers | 1 | 3-5 |
| Load Balancers | 1 Nginx | Multiple behind ALB |

See [docs/design.md](docs/design.md) for the full scaling analysis.

## Documentation

| Document | Description |
|---|---|
| [System Design](docs/design.md) | Architecture, data model, API, scaling analysis |
| [MongoDB Design](docs/mongodb-design.md) | MongoDB-specific implementation and sharding |
| [Database Tradeoffs](docs/database-tradeoffs.md) | PostgreSQL vs MongoDB across 8 dimensions |
| [Benchmark Results](docs/benchmark-results.md) | Load test data with root cause analysis |
| [Snowflake ID](docs/snowflake-id-deep-dive.md) | ID generation algorithm and collision avoidance |

## Configuration

| Variable | Default | Description |
|---|---|---|
| `DB_BACKEND` | `postgres` | Database backend (`postgres` or `mongodb`) |
| `DATABASE_URL` | — | PostgreSQL connection string |
| `MONGODB_URL` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGODB_DB` | `shorturl` | MongoDB database name |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `WORKER_ID` | required | Snowflake worker ID (0-1023) |
| `BASE_URL` | `http://localhost` | Base URL for generated short links |
| `CLICK_FLUSH_INTERVAL` | `10` | Seconds between click flush cycles |

## Tech Stack

- **Python 3.12** / FastAPI / Uvicorn
- **PostgreSQL 16** + SQLAlchemy (async) + asyncpg
- **MongoDB 7** + Motor (async)
- **Redis 7** + hiredis
- **Nginx** (load balancer)
- **Docker Compose** (orchestration)
