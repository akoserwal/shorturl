# Database Selection: PostgreSQL vs MongoDB

**Date:** 2026-07-01
**Context:** Distributed Short URL Service — 300M DAU, 150M writes/day, 6B reads/day

---

## 1. System Access Patterns

Before comparing databases, we need to understand what we're optimizing for.

| Operation | Pattern | Peak Rate | Complexity |
|---|---|---|---|
| Create URL | Insert single record (id, short_code, long_url, timestamps) | 5,200/sec | Trivial |
| Redirect | Point lookup by `short_code` → return `long_url` | 208,000/sec | Trivial |
| Click stats | Read counter per URL | Low (analytics) | Trivial |
| Analytics | Top URLs, time-range aggregations | Rare (internal) | Moderate |

**Key observation:** Both hot paths are single-key operations. No joins, no multi-document
transactions, no complex queries in the critical path. This means the database choice is
driven by **operational concerns at scale**, not query expressiveness.

---

## 2. Head-to-Head Comparison

### 2.1 Read Performance

| Metric | PostgreSQL | MongoDB |
|---|---|---|
| Single-key lookup | ~0.5ms (B-tree index) | ~0.3ms (document fetch) |
| Index type | B-tree, Hash, GIN, GiST | B-tree, Hashed, Text |
| Read scaling | App-managed replica routing | Built-in `readPreference` |
| Connection overhead | Per-connection process (use PgBouncer) | Lightweight (thread-per-connection) |

**Impact on our system:** Minimal. Redis handles 90%+ of reads. The database sees
only cache misses (~20K reads/sec at 90% hit rate). Both databases handle this easily
with 2-3 read replicas.

**Winner:** Tie (Redis dominates the read path).

### 2.2 Write Performance

| Metric | PostgreSQL | MongoDB |
|---|---|---|
| Insert throughput (single node) | 10-15K/sec | 15-25K/sec |
| Write concern | Synchronous commit (configurable) | Configurable (`w:1` to `w:majority`) |
| WAL / Journal | WAL (write-ahead log) | Journal (similar concept) |
| Batch insert | `COPY` or multi-row INSERT (very fast) | `insertMany` (fast) |
| Our peak write load | 5,200/sec | 5,200/sec |

**Impact on our system:** 5,200 writes/sec is well within PostgreSQL's single-node
capacity (10-15K/sec). MongoDB's higher ceiling doesn't provide a meaningful advantage
at this write volume.

**Winner:** MongoDB (slight edge on raw throughput, but irrelevant at our scale).

### 2.3 Horizontal Scaling (Sharding)

This is the most consequential difference.

| Metric | PostgreSQL | MongoDB |
|---|---|---|
| Native sharding | No (requires Citus extension or app-level routing) | **Yes** (built-in, hash or range) |
| Shard key | Manual partitioning by `hash(short_code) % N` | `sh.shardCollection("urls", {short_code: "hashed"})` |
| Rebalancing | Manual data migration | **Automatic** chunk balancing |
| Adding shards | Downtime or complex online migration | Online, automatic rebalancing |
| Cross-shard queries | App must scatter-gather | Mongos handles transparently |
| Setup complexity | Low (single binary) | Higher (mongos + config servers + shard replicas) |

**Impact on our system:**

```
Storage growth:  150M URLs/day × 500 bytes = 75 GB/day = 27 TB/year

With TTL expiry (30-day default):
  75 GB × 30 = 2.25 TB active data  →  Fits on single Postgres node

Without TTL expiry (store forever):
  Year 1: 27 TB   →  Need 3+ shards
  Year 3: 81 TB   →  Need 10+ shards
```

**Winner:**
- **If TTL expiry is used:** PostgreSQL (single node suffices, sharding unnecessary)
- **If all URLs stored forever:** MongoDB (auto-sharding is dramatically easier)

### 2.4 Replication & High Availability

| Metric | PostgreSQL | MongoDB |
|---|---|---|
| Replication | Streaming replication (async/sync) | Replica sets (async/sync) |
| Failover | Manual or Patroni/pg_auto_failover | **Automatic** (replica set election) |
| Read routing | App-level (SQLAlchemy `read_replica` engine) | Built-in `readPreference: secondaryPreferred` |
| Split-brain protection | Requires external tooling | Built-in majority writes |
| Replica lag | Typically < 1s | Typically < 1s |

**Impact on our system:** MongoDB's built-in automatic failover and read routing is
genuinely easier to operate. PostgreSQL requires Patroni or similar tooling to match.

**Winner:** MongoDB (less operational burden for HA).

### 2.5 Data Model

| Metric | PostgreSQL | MongoDB |
|---|---|---|
| Schema | Fixed columns, migrations required | Schema-less documents |
| Our data shape | `{id, short_code, long_url, click_count, created_at, expires_at}` | Same, as a document |
| Schema evolution | `ALTER TABLE` (can be slow on large tables) | Add fields freely |
| Data validation | Database-enforced constraints (`UNIQUE`, `NOT NULL`) | Application-enforced (or JSON Schema) |
| UNIQUE constraint | Native, database-guaranteed | Unique index, database-guaranteed |

**Impact on our system:** Our schema has 6 fixed fields and is unlikely to change
significantly. Schema flexibility is not a meaningful advantage here. However,
PostgreSQL's `ALTER TABLE` on a 27 TB table would be extremely painful — another
argument for MongoDB if storing everything forever.

**Winner:** Tie (our schema is simple and stable).

### 2.6 Analytics & Reporting

| Metric | PostgreSQL | MongoDB |
|---|---|---|
| Top 10 URLs by clicks | `SELECT ... ORDER BY click_count DESC LIMIT 10` | `db.urls.find().sort({click_count: -1}).limit(10)` |
| Clicks per day this week | `GROUP BY date_trunc('day', created_at)` | Aggregation pipeline (`$group`, `$dateToString`) |
| Complex analytics | SQL — CTEs, window functions, subqueries | Aggregation pipeline — powerful but verbose |
| Ecosystem | Grafana, Metabase, dbt, any SQL tool | MongoDB Charts, limited BI tool support |
| Ad-hoc queries | Any SQL client | `mongosh` or Compass |

**Impact on our system:** Analytics is an internal, low-frequency need. But when the
product team asks "show me URL creation trends by hour for the past month," SQL is
a 1-line answer. The aggregation pipeline equivalent is 10+ lines.

**Winner:** PostgreSQL (SQL is more expressive and has better tooling).

### 2.7 Operational Complexity

| Metric | PostgreSQL | MongoDB |
|---|---|---|
| Minimum production setup | 1 primary + 2 replicas = **3 nodes** | 1 mongos + 3 config + 3 shard nodes = **7 nodes** (sharded) |
| Without sharding | 3 nodes | 3 nodes (replica set) |
| Backup | `pg_dump`, `pg_basebackup`, WAL archiving | `mongodump`, oplog-based PITR |
| Monitoring | `pg_stat_statements`, pgBadger | `mongostat`, `mongotop`, Atlas metrics |
| Connection pooling | PgBouncer required at scale | Built-in (driver-level) |
| Upgrades | Well-documented, `pg_upgrade` | Rolling upgrades (replica sets) |

**Impact on our system:** Without sharding, both are similar. With sharding, MongoDB
adds significant operational overhead (config servers, mongos routing layer). But
that overhead buys you automatic rebalancing, which is worth it if you need it.

**Winner:** PostgreSQL (simpler), unless sharding is required.

### 2.8 Cost

| Metric | PostgreSQL | MongoDB |
|---|---|---|
| License | Open source (PostgreSQL License) | Open source (SSPL) or Atlas (paid) |
| Managed service | RDS/Aurora ($) | Atlas ($$$) |
| Node count (sharded) | 3 per shard (Citus) | 7+ minimum (mongos + config + shards) |
| AWS RDS pricing (db.r6g.xlarge) | ~$0.48/hr | ~$0.52/hr (DocumentDB) |
| Storage pricing | Standard EBS | Standard EBS |

**Impact on our system:** At 300M DAU scale, infrastructure cost matters.
PostgreSQL is cheaper per-node and requires fewer nodes for equivalent throughput.

**Winner:** PostgreSQL.

---

## 3. Decision Matrix

Scored 1-5 (5 = best) weighted by importance to this specific system.

| Criterion | Weight | PostgreSQL | MongoDB | Reasoning |
|---|---|---|---|---|
| Read performance (cache miss) | 10% | 4 | 4 | Redis handles 90%+, DB barely matters |
| Write performance | 15% | 4 | 5 | Both handle 5.2K/sec; Mongo has headroom |
| Horizontal sharding | 25% | 2 | 5 | Mongo's biggest advantage |
| High availability | 15% | 3 | 5 | Mongo's auto-failover is easier |
| Analytics | 10% | 5 | 3 | SQL wins for reporting |
| Operational simplicity | 15% | 5 | 3 | Fewer moving parts |
| Cost | 10% | 5 | 3 | Fewer nodes, cheaper managed services |
| **Weighted Score** | | **3.55** | **4.10** | |

---

## 4. Recommendation

### Default choice: PostgreSQL

For most teams building a short URL service, PostgreSQL is the right choice:

- The hot path is Redis, not the database — DB choice has minimal latency impact
- 5,200 writes/sec fits comfortably on a single Postgres primary
- TTL expiry keeps storage manageable (2.25 TB active vs. 27 TB/year)
- SQL analytics, simpler ops, lower cost, larger talent pool
- Sharding can be deferred to year 2+ with Citus or app-level routing

### Choose MongoDB instead if:

1. **All URLs must be stored forever** (no TTL) — 27 TB/year makes auto-sharding essential
2. **Growth is uncertain and could be 10x** — MongoDB's elastic sharding absorbs spikes
   without manual intervention
3. **Your team already operates MongoDB in production** — operational familiarity outweighs
   theoretical advantages
4. **Multi-region deployment is planned** — MongoDB's zone sharding places data near users
   more naturally than Postgres

### The Hybrid Option

Use both:
- **PostgreSQL** for the URL table (strong consistency, SQL analytics, simple ops)
- **MongoDB** for click analytics (append-heavy, time-series, naturally shardable)

This avoids the "analytics writes competing with URL reads" problem entirely, but adds
operational complexity of managing two database systems.

---

## 5. Scenario Comparison

| Scenario | Best Choice | Why |
|---|---|---|
| Startup, < 10M URLs, small team | PostgreSQL | Simplicity, cost, SQL |
| Scale-up, 100M-1B URLs, TTL expiry | PostgreSQL + Citus | Manageable storage, SQL analytics |
| Scale-up, 1B+ URLs, no TTL | MongoDB | Auto-sharding is essential |
| Multi-region, global service | MongoDB | Zone sharding, built-in geo-routing |
| Analytics-heavy (BI dashboards) | PostgreSQL | SQL ecosystem, Grafana/Metabase |
| Write-heavy (> 50K writes/sec) | MongoDB | Higher single-node write ceiling |

---

## 6. Migration Path

If starting with PostgreSQL and needing to migrate later:

```
Phase 1 (Day 1 - Month 6):    PostgreSQL single primary + 2 replicas
Phase 2 (Month 6 - Year 1):   Add Citus for hash-based sharding
Phase 3 (Year 1+):            Evaluate: Citus scaling OR migrate to MongoDB

Migration cost: ~2-4 weeks engineering time
Risk: Data migration downtime (mitigated with dual-write pattern)
```

If starting with MongoDB:

```
Phase 1 (Day 1):               MongoDB 3-node replica set
Phase 2 (When needed):         Enable sharding (online, no downtime)
Phase 3 (When needed):         Add shards (online, automatic rebalancing)

No migration needed — MongoDB scales in place.
```

---

## 7. Final Verdict

**For this system (300M DAU short URL service): PostgreSQL.**

The decisive factors:
1. Redis dominates the data path — the DB is a persistence layer, not a performance layer
2. 5,200 writes/sec is well within Postgres capacity
3. TTL expiry keeps storage bounded — the sharding advantage doesn't materialize
4. SQL analytics, lower cost, simpler operations, larger talent pool

MongoDB becomes the right choice if the requirements shift to: no TTL, multi-region,
or > 50K writes/sec. At that point, auto-sharding becomes worth the operational overhead.
