#!/bin/bash
set -e

cd "$(dirname "$0")/.."

echo "============================================"
echo "  Short URL: PostgreSQL vs MongoDB Benchmark"
echo "============================================"
echo ""

# Install load test deps
pip install -q aiohttp 2>/dev/null || pip3 install -q aiohttp 2>/dev/null

echo "=== Phase 1: PostgreSQL ==="
docker compose down -v 2>/dev/null || true
docker compose up -d --build --wait
sleep 3
python3 loadtest/bench.py --url http://localhost --tag postgres --creates 500 --redirects 2000 --stats-requests 200
docker compose down -v

echo ""
echo "=== Phase 2: MongoDB ==="
docker compose -f docker-compose.yml -f docker-compose.mongo.yml up -d --build --wait
sleep 3
python3 loadtest/bench.py --url http://localhost --tag mongodb --creates 500 --redirects 2000 --stats-requests 200
docker compose -f docker-compose.yml -f docker-compose.mongo.yml down -v

echo ""
echo "=== Phase 3: Comparison ==="
python3 loadtest/bench.py --compare results/postgres.json results/mongodb.json
