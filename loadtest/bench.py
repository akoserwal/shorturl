#!/usr/bin/env python3
"""
Comparative load test for Short URL service.

Usage:
  python loadtest/bench.py --url http://localhost --tag postgres
  python loadtest/bench.py --url http://localhost --tag mongodb
  python loadtest/bench.py --compare results/postgres.json results/mongodb.json
"""

import argparse
import asyncio
import json
import random
import statistics
import string
import time
from pathlib import Path

import aiohttp


async def run_phase(session, sem, tasks, phase_name):
    latencies = []
    errors = 0

    async def execute(task_fn):
        nonlocal errors
        async with sem:
            start = time.monotonic()
            try:
                await task_fn(session)
                latencies.append(time.monotonic() - start)
            except Exception:
                errors += 1

    await asyncio.gather(*[execute(t) for t in tasks])

    if not latencies:
        return {"phase": phase_name, "count": 0, "errors": errors}

    latencies.sort()
    return {
        "phase": phase_name,
        "count": len(latencies),
        "errors": errors,
        "p50_ms": round(latencies[len(latencies) // 2] * 1000, 2),
        "p95_ms": round(latencies[int(len(latencies) * 0.95)] * 1000, 2),
        "p99_ms": round(latencies[int(len(latencies) * 0.99)] * 1000, 2),
        "avg_ms": round(statistics.mean(latencies) * 1000, 2),
        "throughput_rps": round(len(latencies) / sum(latencies), 1) if sum(latencies) > 0 else 0,
    }


async def bench(base_url: str, concurrency: int, num_creates: int, num_redirects: int, num_stats: int):
    sem = asyncio.Semaphore(concurrency)
    short_codes = []
    connector = aiohttp.TCPConnector(limit=concurrency, force_close=False)

    async with aiohttp.ClientSession(connector=connector) as session:
        # Warmup
        for _ in range(20):
            rand_url = f"https://example.com/{''.join(random.choices(string.ascii_lowercase, k=10))}"
            async with session.post(f"{base_url}/api/shorten", json={"url": rand_url}) as resp:
                if resp.status == 201:
                    data = await resp.json()
                    short_codes.append(data["short_code"])

        # Phase 1: Create
        create_codes = []

        def make_create(idx):
            async def fn(s):
                url = f"https://example.com/bench/{idx}/{''.join(random.choices(string.ascii_lowercase, k=8))}"
                async with s.post(f"{base_url}/api/shorten", json={"url": url}) as resp:
                    if resp.status == 201:
                        data = await resp.json()
                        create_codes.append(data["short_code"])
            return fn

        create_result = await run_phase(
            session, sem, [make_create(i) for i in range(num_creates)], "create"
        )
        short_codes.extend(create_codes)

        if not short_codes:
            return {"concurrency": concurrency, "phases": [create_result], "error": "no URLs created"}

        # Phase 2: Redirect
        def make_redirect(_):
            code = random.choice(short_codes)
            async def fn(s):
                async with s.get(f"{base_url}/{code}", allow_redirects=False) as resp:
                    pass
            return fn

        redirect_result = await run_phase(
            session, sem, [make_redirect(i) for i in range(num_redirects)], "redirect"
        )

        # Phase 3: Stats
        def make_stats(_):
            code = random.choice(short_codes)
            async def fn(s):
                async with s.get(f"{base_url}/api/stats/{code}") as resp:
                    await resp.json()
            return fn

        stats_result = await run_phase(
            session, sem, [make_stats(i) for i in range(num_stats)], "stats"
        )

    return {
        "concurrency": concurrency,
        "phases": [create_result, redirect_result, stats_result],
    }


async def run_bench(base_url: str, tag: str, concurrency_levels: list[int],
                    num_creates: int, num_redirects: int, num_stats: int):
    print(f"\n{'='*60}")
    print(f"  Load Test: {tag.upper()}")
    print(f"  Target: {base_url}")
    print(f"  Creates: {num_creates} | Redirects: {num_redirects} | Stats: {num_stats}")
    print(f"{'='*60}\n")

    all_results = []
    for c in concurrency_levels:
        print(f"--- Concurrency: {c} ---")
        result = await bench(base_url, c, num_creates, num_redirects, num_stats)
        all_results.append(result)
        for phase in result["phases"]:
            if phase.get("count", 0) > 0:
                print(f"  {phase['phase']:10s}  p50={phase['p50_ms']:7.1f}ms  "
                      f"p95={phase['p95_ms']:7.1f}ms  p99={phase['p99_ms']:7.1f}ms  "
                      f"rps={phase['throughput_rps']:8.1f}  errors={phase['errors']}")
        print()

    output = {"tag": tag, "url": base_url, "results": all_results}
    out_path = Path(f"results/{tag}.json")
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))
    print(f"Results saved to {out_path}")
    return output


def compare(file_a: str, file_b: str):
    a = json.loads(Path(file_a).read_text())
    b = json.loads(Path(file_b).read_text())

    tag_a = a["tag"]
    tag_b = b["tag"]

    print(f"\n{'='*80}")
    print(f"  COMPARISON: {tag_a.upper()} vs {tag_b.upper()}")
    print(f"{'='*80}\n")

    header = (f"{'Concurrency':>12} {'Phase':>10} │ "
              f"{'p50 ' + tag_a:>12} {'p50 ' + tag_b:>12} │ "
              f"{'p95 ' + tag_a:>12} {'p95 ' + tag_b:>12} │ "
              f"{'rps ' + tag_a:>12} {'rps ' + tag_b:>12} │ Winner")
    print(header)
    print("─" * len(header))

    for ra, rb in zip(a["results"], b["results"]):
        conc = ra["concurrency"]
        for pa, pb in zip(ra["phases"], rb["phases"]):
            if pa.get("count", 0) == 0:
                continue
            p50_a = pa["p50_ms"]
            p50_b = pb["p50_ms"]
            p95_a = pa["p95_ms"]
            p95_b = pb["p95_ms"]
            rps_a = pa["throughput_rps"]
            rps_b = pb["throughput_rps"]

            winner = tag_a if p95_a < p95_b else tag_b
            if abs(p95_a - p95_b) / max(p95_a, p95_b, 0.001) < 0.05:
                winner = "tie"

            print(f"{conc:>12} {pa['phase']:>10} │ "
                  f"{p50_a:>10.1f}ms {p50_b:>10.1f}ms │ "
                  f"{p95_a:>10.1f}ms {p95_b:>10.1f}ms │ "
                  f"{rps_a:>10.1f} {rps_b:>10.1f} │ {winner}")
        print()

    # Summary
    print(f"\n{'='*80}")
    print("  SUMMARY")
    print(f"{'='*80}")
    for phase_name in ["create", "redirect", "stats"]:
        a_p95s = []
        b_p95s = []
        for ra, rb in zip(a["results"], b["results"]):
            for pa, pb in zip(ra["phases"], rb["phases"]):
                if pa["phase"] == phase_name and pa.get("count", 0) > 0:
                    a_p95s.append(pa["p95_ms"])
                    b_p95s.append(pb["p95_ms"])
        if a_p95s:
            avg_a = statistics.mean(a_p95s)
            avg_b = statistics.mean(b_p95s)
            winner = tag_a if avg_a < avg_b else tag_b
            diff = abs(avg_a - avg_b) / max(avg_a, avg_b) * 100
            print(f"  {phase_name:10s}  avg p95: {tag_a}={avg_a:.1f}ms  {tag_b}={avg_b:.1f}ms  "
                  f"→ {winner} wins by {diff:.0f}%")
    print()


def main():
    parser = argparse.ArgumentParser(description="Short URL load test")
    parser.add_argument("--url", default="http://localhost", help="Base URL")
    parser.add_argument("--tag", default="test", help="Label for this run")
    parser.add_argument("--concurrency", default="10,50,100,200", help="Comma-separated concurrency levels")
    parser.add_argument("--creates", type=int, default=1000)
    parser.add_argument("--redirects", type=int, default=5000)
    parser.add_argument("--stats-requests", type=int, default=500)
    parser.add_argument("--compare", nargs=2, metavar=("FILE_A", "FILE_B"), help="Compare two result files")
    args = parser.parse_args()

    if args.compare:
        compare(args.compare[0], args.compare[1])
        return

    levels = [int(x) for x in args.concurrency.split(",")]
    asyncio.run(run_bench(args.url, args.tag, levels, args.creates, args.redirects, args.stats_requests))


if __name__ == "__main__":
    main()
