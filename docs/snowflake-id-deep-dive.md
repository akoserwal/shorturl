# Snowflake ID: Deep Dive

**Date:** 2026-07-01
**Context:** Distributed Short URL Service — how globally unique IDs are generated
across multiple servers without any central coordination

---

## 1. The Problem

In a distributed URL shortener with 50+ API servers, every server must generate unique
IDs independently. The constraints:

| Constraint | Why |
|---|---|
| **Globally unique** | Two servers must never produce the same ID |
| **No coordination** | No central counter, no database round-trip, no distributed lock |
| **Monotonically increasing** | Roughly time-ordered for database index efficiency |
| **Compact** | Must encode to a short Base62 string (7-11 chars) |
| **High throughput** | 4M+ IDs per second per server |

Traditional approaches and why they fail:

| Approach | Failure mode |
|---|---|
| Auto-increment (DB sequence) | Single point of failure, bottleneck under high write load |
| UUID v4 (random) | 128 bits → 22+ char Base62 string, too long for a short URL |
| UUID v7 (time-ordered) | Still 128 bits, same length problem |
| Central counter service | Network round-trip on every create, SPOF |
| Redis INCR | Works but adds Redis as a write-path dependency |

Snowflake solves all of these: **64 bits, no coordination, time-ordered, 4M IDs/sec/server.**

---

## 2. The 64-Bit Structure

A Snowflake ID is a single 64-bit integer composed of three fields:

```
 63                          22  21        12  11          0
┌──────────────────────────────┬────────────┬──────────────┐
│       Timestamp (41 bits)    │ Worker ID  │  Sequence    │
│     milliseconds since       │ (10 bits)  │  (12 bits)   │
│     custom epoch             │            │              │
└──────────────────────────────┴────────────┴──────────────┘
 ◄──── time-ordering ────────► ◄── who ───► ◄── counter ──►
```

### Field Breakdown

| Field | Bits | Range | Purpose |
|---|---|---|---|
| **Timestamp** | 41 | 0 to 2,199,023,255,551 ms (~69.7 years) | When the ID was generated |
| **Worker ID** | 10 | 0 to 1,023 | Which server generated it |
| **Sequence** | 12 | 0 to 4,095 | Counter within the same millisecond |

### Bit Layout (MSB to LSB)

```
Bit 63 ──────────────────────────────────────────────────── Bit 0

[T T T T T T T T T T T T T T T T T T T T T T T T T T T T T T T T T T T T T T T T T][W W W W W W W W W W][S S S S S S S S S S S S]
 ◄────────────────── 41 bits ──────────────────────────────►◄───── 10 bits ──────────►◄────── 12 bits ─────►
                  Timestamp                                      Worker ID                  Sequence
```

### How the ID Is Assembled (from `snowflake.py`)

```python
id = ((timestamp - CUSTOM_EPOCH) << 22) | (worker_id << 12) | sequence
```

Concrete example:

```
timestamp - epoch = 63,072,000,000 ms  (2 years after epoch)
worker_id = 5
sequence = 42

Binary:
  timestamp: 0000000000111010110111100001010000000000000  (41 bits)
  worker_id:                                      0000000101  (10 bits)
  sequence:                                                 000000101010  (12 bits)

Shift and OR:
  (timestamp << 22) = 0000000000111010110111100001010000000000000_0000000000_000000000000
  (worker_id << 12) = 0000000000000000000000000000000000000000000_0000000101_000000000000
  sequence           = 0000000000000000000000000000000000000000000_0000000000_000000101010

  Final 64-bit ID    = 0000000000111010110111100001010000000000000_0000000101_000000101010
                     = 264,781,201,408,010,282 (decimal)
                     = "1rGH5cz" (Base62)
```

---

## 3. How Collisions Are Avoided

Snowflake guarantees uniqueness through **three independent mechanisms**, each protecting
against a different collision scenario.

### 3.1 Different Servers → Different Worker ID Bits

The 10-bit worker ID field ensures that two different servers **can never produce the
same ID**, even if they generate an ID at the exact same millisecond with the same
sequence number.

```
Server A (worker_id=1), ts=1000, seq=0:
  ..._ 0000000001 _000000000000  →  ID = X

Server B (worker_id=2), ts=1000, seq=0:
  ..._ 0000000010 _000000000000  →  ID = Y

X ≠ Y  (bits 12-21 differ)
```

**This is the core distributed guarantee.** As long as no two live servers share
a worker ID, their ID spaces are completely disjoint — they literally cannot
collide, regardless of timing.

### 3.2 Same Server, Same Millisecond → Sequence Counter

When the same server generates multiple IDs within the same millisecond, the
12-bit sequence counter increments:

```python
if ts == self.last_timestamp:
    self.sequence = (self.sequence + 1) & MAX_SEQUENCE  # wraps at 4095
    if self.sequence == 0:
        # All 4096 slots used — spin until next millisecond
        while ts <= self.last_timestamp:
            ts = self._current_ms()
```

This allows **4,096 unique IDs per millisecond per server**. If all 4,096 slots
are exhausted (extremely rare — would require 4M IDs/sec), the generator
**blocks until the next millisecond** rather than producing a duplicate.

```
Server A, ts=1000, seq=0:  ..._ 0000000001 _000000000000  →  unique
Server A, ts=1000, seq=1:  ..._ 0000000001 _000000000001  →  unique
Server A, ts=1000, seq=2:  ..._ 0000000001 _000000000010  →  unique
...
Server A, ts=1000, seq=4095: ..._ 0000000001 _111111111111  →  unique
Server A, ts=1000, seq=4096: BLOCKED — waits for ts=1001
```

### 3.3 Same Server, Different Millisecond → Timestamp Bits Differ

When a new millisecond starts, the sequence resets to 0 but the timestamp
field changes, producing a different ID:

```
Server A, ts=1000, seq=0:  0000...001111101000_0000000001_000000000000
Server A, ts=1001, seq=0:  0000...001111101001_0000000001_000000000000
                                           ^^^
                                    timestamp differs → different ID
```

### 3.4 Thread Safety → Mutex Lock

Multiple concurrent requests on the same server could race to read/write
`last_timestamp` and `sequence`. A threading lock prevents this:

```python
def generate(self) -> int:
    with self._lock:        # ← only one thread at a time
        ts = self._current_ms()
        # ... sequence logic ...
        return id
```

Without the lock, two threads could both read `sequence=41`, both increment
to 42, and produce the same ID.

### 3.5 Database UNIQUE Constraint → Last Resort

Even with all the above guarantees, the `short_code` column has a UNIQUE
constraint in the database:

```sql
CREATE TABLE urls (
    short_code VARCHAR(12) NOT NULL UNIQUE,
    ...
);
```

If a collision somehow occurred (e.g., two servers misconfigured with the
same worker ID), the INSERT would raise an `IntegrityError` rather than
silently creating a duplicate.

### Collision Protection Summary

```
Layer 1: Worker ID bits          → different servers can NEVER collide
Layer 2: Sequence counter        → same server, same ms, up to 4,096 IDs
Layer 3: Spin-wait on overflow   → blocks rather than wrapping
Layer 4: Threading lock          → concurrent threads can't race
Layer 5: DB UNIQUE constraint    → catches misconfiguration
```

```
Can two servers produce the same ID?
│
├── Do they have different worker IDs?
│   └── YES → IMPOSSIBLE. Bits 12-21 always differ.
│
└── Do they have the SAME worker ID? (misconfiguration)
    └── YES → POSSIBLE. But DB UNIQUE constraint catches it.
             Fix: use lease-based worker ID assignment.
```

---

## 4. The Custom Epoch

Standard Unix timestamps count milliseconds since 1970-01-01. With 41 bits,
that would overflow around year 2039. By using a **custom epoch** closer to
today, we extend the usable range:

```python
CUSTOM_EPOCH = 1719792000000  # 2024-07-01T00:00:00Z in milliseconds
```

```
Standard epoch (1970): 41 bits covers 1970 → 2039 (69 years)
Custom epoch (2024):   41 bits covers 2024 → 2093 (69 years)
```

The custom epoch is an arbitrary constant — it only needs to be:
1. In the past (so `current_time - epoch` is always positive)
2. The same across all servers
3. Never changed after deployment

---

## 5. Capacity and Limits

### 5.1 IDs Per Second

```
Per server:   4,096 IDs/ms × 1,000 ms = 4,096,000 IDs/sec
With 50 servers: 50 × 4,096,000 = 204,800,000 IDs/sec
With 1,024 servers: 1,024 × 4,096,000 = 4,194,304,000 IDs/sec
```

Our system needs 5,200 writes/sec peak — **0.13% of a single server's capacity.**

### 5.2 Total ID Space

```
64-bit ID space: 2^64 = 18.4 quintillion unique IDs
Our usage: 150M/day × 365 × 100 years = 5.4 trillion IDs
Headroom: 3,400,000x more IDs than we'll ever need
```

### 5.3 Base62 Encoding Length

```
64-bit Snowflake ID → Base62 string

Minimum: 62^6  = 56.8 billion    →  7 chars covers most early IDs
Maximum: 62^10 = 839.3 trillion  → 11 chars covers the full 64-bit range
Typical: 62^7  = 3.52 trillion   →  7-8 chars for production IDs
```

### 5.4 Worker ID Space

```
10 bits = 1,024 unique worker IDs (0 to 1,023)
Our need: 50 servers → 4.9% utilization
```

---

## 6. Distributed Worker ID Assignment

The one operational risk: **duplicate worker IDs**. If two servers are assigned
the same worker ID, their Snowflake outputs WILL collide.

### 6.1 Current PoC: Hardcoded Environment Variables

```yaml
# docker-compose.yml
api1:
  environment:
    WORKER_ID: "1"    # manual assignment
api2:
  environment:
    WORKER_ID: "2"    # manual assignment
```

**Works for:** 2-5 servers with static infrastructure.
**Breaks when:** Auto-scaling (K8s HPA, ECS), servers restart with reassigned IPs.

### 6.2 Production: Lease-Based Assignment via Redis

Each server tries to claim a worker ID on startup by setting a key with a TTL:

```python
async def acquire_worker_id(redis_client) -> int:
    for candidate_id in range(1024):
        key = f"worker:{candidate_id}"
        acquired = await redis_client.set(key, hostname, nx=True, ex=30)
        if acquired:
            return candidate_id
    raise RuntimeError("No available worker IDs")
```

**Lease renewal:** Every 15 seconds, the server extends its key's TTL:

```python
async def renew_lease(redis_client, worker_id):
    while True:
        await redis_client.expire(f"worker:{worker_id}", 30)
        await asyncio.sleep(15)
```

**On shutdown:** Release the key immediately:

```python
async def release_worker_id(redis_client, worker_id):
    await redis_client.delete(f"worker:{worker_id}")
```

**On crash:** The TTL expires after 30 seconds, freeing the ID for another server.

```
Server A starts → claims worker:7 (TTL 30s)
Server A renews every 15s → worker:7 stays alive
Server A crashes → worker:7 expires after 30s
Server C starts → claims worker:7 (now available)
```

**Gap safety:** There is a 30-second window after crash where worker:7 is "reserved"
but unused. This is fine — it wastes 30 seconds of ID space, not correctness.

### 6.3 Alternative: ZooKeeper / etcd Sequential Nodes

For systems already running ZooKeeper or etcd:

```
Server starts → creates ephemeral sequential node:
  /workers/worker-0000000007

Node number (7) becomes the worker ID.
Ephemeral node auto-deletes when the session ends (crash/shutdown).
```

**Pros:** Stronger consistency guarantees than Redis SETNX.
**Cons:** Requires ZooKeeper/etcd infrastructure.

### 6.4 Alternative: Kubernetes StatefulSet Ordinal

In Kubernetes, StatefulSet pods get stable ordinal indices:

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: shorturl-api
spec:
  replicas: 50
  # Pod names: shorturl-api-0, shorturl-api-1, ..., shorturl-api-49
```

```python
import os
hostname = os.getenv("HOSTNAME", "shorturl-api-0")  # "shorturl-api-7"
worker_id = int(hostname.rsplit("-", 1)[1])           # 7
```

**Pros:** Zero external dependencies, inherently unique.
**Cons:** Only works with StatefulSets (not Deployments), limits scaling to 1,024 pods.

### Worker ID Assignment Comparison

| Method | Consistency | Infra needed | Auto-scale friendly | Recovery time |
|---|---|---|---|---|
| **Env var (hardcoded)** | Manual | None | No | N/A |
| **Redis lease** | Eventual | Redis | Yes | 30s (TTL) |
| **ZooKeeper/etcd** | Strong | ZK/etcd cluster | Yes | Session timeout |
| **K8s StatefulSet** | Strong | Kubernetes | Partial (StatefulSet only) | Pod restart |

---

## 7. Comparison With Other ID Schemes

### 7.1 Twitter Snowflake (Original)

Our implementation is based on Twitter's Snowflake (2010). Differences:

| Aspect | Twitter Snowflake | Our Implementation |
|---|---|---|
| Bits | 64 | 64 |
| Timestamp bits | 41 (ms since Twitter epoch) | 41 (ms since 2024-07-01) |
| Datacenter bits | 5 | 0 (not needed for single-region PoC) |
| Worker bits | 5 (per datacenter) | 10 (flat namespace) |
| Sequence bits | 12 | 12 |
| Max workers | 32 per datacenter × 32 DCs = 1,024 | 1,024 (flat) |
| Coordination | ZooKeeper | Redis lease or env var |

Twitter split the 10 worker bits into 5 datacenter + 5 worker. We use all 10 as
a flat worker namespace since the PoC is single-region. For multi-region, splitting
into datacenter + worker bits prevents cross-region coordination.

### 7.2 UUID v4 (Random)

```
UUID v4:  550e8400-e29b-41d4-a716-446655440000  (128 bits, 36 chars)
Base62:   2JxLkPQ4f7m9uR5WgT3vN8                (22 chars)

Snowflake: 264781201408010282                    (64 bits)
Base62:    1rGH5cz                               (7 chars)
```

| Aspect | UUID v4 | Snowflake |
|---|---|---|
| Uniqueness | Probabilistic (2^122 random bits) | Deterministic (guaranteed) |
| Length (Base62) | 22 chars | 7-11 chars |
| Time-ordered | No | Yes |
| Index performance | Poor (random inserts fragment B-tree) | Good (monotonic) |
| Coordination | None | Worker ID assignment |

### 7.3 UUID v7 (Time-Ordered)

```
UUID v7:  018f3e4a-1b2c-7d3e-8f4a-5b6c7d8e9f0a  (128 bits)
```

| Aspect | UUID v7 | Snowflake |
|---|---|---|
| Time-ordered | Yes | Yes |
| Length (Base62) | 22 chars | 7-11 chars |
| Uniqueness | Probabilistic (62 random bits) | Deterministic |
| Standard | IETF RFC 9562 | De facto (Twitter) |

UUID v7 is the closest competitor but is still too long for a short URL service.

### 7.4 ULID (Universally Unique Lexicographically Sortable Identifier)

```
ULID: 01ARZ3NDEKTSV4RRFFQ69G5FAV  (128 bits, 26 chars Crockford Base32)
```

| Aspect | ULID | Snowflake |
|---|---|---|
| Length | 26 chars (Base32) | 7-11 chars (Base62) |
| Time-ordered | Yes | Yes |
| Coordination | None | Worker ID |
| Collision risk | ~1 in 2^80 per ms | Zero (deterministic) |

### 7.5 Summary Table

| Scheme | Bits | Base62 Length | Time-ordered | Coordination | Collision Risk |
|---|---|---|---|---|---|
| **Snowflake** | 64 | **7-11 chars** | Yes | Worker ID | **Zero** |
| Auto-increment | 64 | 7-11 chars | Yes | Central DB | Zero (but SPOF) |
| UUID v4 | 128 | 22 chars | No | None | ~2^-122 |
| UUID v7 | 128 | 22 chars | Yes | None | ~2^-62 per ms |
| ULID | 128 | 17 chars (B62) | Yes | None | ~2^-80 per ms |
| NanoID | Variable | Configurable | No | None | Configurable |

**Snowflake is the only scheme that produces deterministically unique, short (7-11 char)
codes without a central coordination point.** This is why it's the standard choice for
URL shorteners, distributed databases (CockroachDB), social media (Twitter, Discord,
Instagram), and messaging systems.

---

## 8. Failure Modes and Mitigations

### 8.1 Clock Skew

If a server's clock jumps backward (NTP correction, VM migration), the
timestamp could produce an ID that conflicts with a previously generated one.

```
ts=1000, seq=0 → ID_A
clock jumps back to ts=999
ts=999, seq=0 → ID_B = ID_A?  (only if same worker, same sequence)
```

**Current mitigation:** The generator checks `ts < self.last_timestamp` implicitly
via the spin-wait loop — if time goes backward, it keeps spinning until the clock
catches up.

**Production mitigation:**
- Use NTP with `tinker panic 0` to prevent large jumps
- Detect backward jumps explicitly and refuse to generate IDs until clock recovers
- Monitor clock drift with Prometheus `node_timex_offset_seconds`

### 8.2 Worker ID Exhaustion

With 10 bits, maximum 1,024 concurrent workers. If auto-scaling tries to launch
server #1,025, it will fail to acquire a worker ID.

**Mitigation:**
- Monitor worker ID utilization
- Alert at 80% (820 workers)
- Increase to 12 bits (4,096 workers) by stealing 2 bits from sequence
  (reduces per-ms capacity from 4,096 to 1,024 — still 1M IDs/sec/server)

### 8.3 Sequence Exhaustion

If a single server receives > 4,096 requests in one millisecond, it blocks
until the next millisecond. This adds up to 1ms latency.

```
At 4,096,000 IDs/sec → this never happens in practice
At our peak of 5,200 IDs/sec → would need 5,200 in 1ms → impossible
```

**Mitigation:** Only relevant for extremely high-throughput single-server scenarios.
Solution: reduce worker bits, increase sequence bits.

### 8.4 Summary

| Failure Mode | Probability | Impact | Mitigation |
|---|---|---|---|
| Duplicate worker ID | Medium (misconfiguration) | ID collision | Lease-based assignment |
| Clock skew backward | Low (NTP) | Temporary block | Spin-wait + monitoring |
| Worker ID exhaustion | Very low | Can't start new servers | Monitor + expand bit allocation |
| Sequence exhaustion | Near zero | 1ms latency spike | Already 800x over-provisioned |

---

## 9. Code Reference

The complete implementation is in `app/snowflake.py` (45 lines):

```python
CUSTOM_EPOCH = 1719792000000       # 2024-07-01 in ms
TIMESTAMP_BITS = 41                # ~69 years
WORKER_BITS = 10                   # 1,024 servers
SEQUENCE_BITS = 12                 # 4,096 per ms

class SnowflakeGenerator:
    def __init__(self, worker_id: int):     # validate 0-1023
    def generate(self) -> int:              # returns 64-bit unique ID
        # 1. Get current timestamp in ms
        # 2. If same ms: increment sequence (spin-wait if overflow)
        # 3. If new ms: reset sequence to 0
        # 4. Bit-shift and OR: timestamp | worker_id | sequence
```

The ID flows through the system as:

```
SnowflakeGenerator.generate()
        │
        ▼
   64-bit integer (e.g., 264781201408010282)
        │
        ▼
   base62.encode()
        │
        ▼
   Short code string (e.g., "1rGH5cz")
        │
        ▼
   Stored in DB as both `id` (BIGINT PK) and `short_code` (VARCHAR UNIQUE)
        │
        ▼
   Returned to user as: http://short.url/1rGH5cz
```
