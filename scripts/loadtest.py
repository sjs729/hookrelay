"""HookRelay 压测。

测量三件事：

1. **入站吞吐与延迟分位** —— 服务每秒能接住多少事件，以及慢到什么程度。
   只报平均延迟是没有意义的：平均值会把少数极慢的请求掩盖掉，
   而用户感受最差的那 1% 才决定体验。
2. **投递吞吐** —— Worker 每秒能投递完成多少事件。
3. **队列扫描的执行计划** —— 确认取任务的查询真的走了部分索引，
   而不是随着表变大退化成全表扫描。

    uv run python scripts/loadtest.py --events 2000 --concurrency 50

前两个是端到端的，需要 Web 服务、Worker、接收端都在跑。
第三项需要能连数据库，连不上会跳过。

关于限流：默认阈值 120 次/分钟是面向公网的可用性保护，不是服务容量上限。
不改它的话压测测到的是限流阈值。所以启动服务时要放开：

    INGEST_RATE_LIMIT_PER_MINUTE=1000000 docker compose up -d

脚本会在报告里说明这次是否被限流挡过，避免拿一个被限流削过的数字去宣称吞吐。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import math
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_SINK_URL = "http://127.0.0.1:9000"

# 压测用的请求体固定不变，这样签名可以在循环外只算一次。
# 去重靠每个请求不同的幂等键，而不是靠 body 不同。
LOAD_BODY = json.dumps(
    {"event": "loadtest", "source": "scripts/loadtest.py", "level": "info"},
    ensure_ascii=False,
    separators=(",", ":"),
).encode()

# 等投递全部完成的上限。两千个事件、并发 10 的投递下通常几秒到几十秒
DRAIN_TIMEOUT_SECONDS = 300.0


def build_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    message = f"{timestamp}.".encode() + raw_body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def percentile(sorted_values: list[float], fraction: float) -> float:
    """从已排序的样本里取分位数，用线性插值。

    样本很少时（比如 5 个请求），直接取第 k 个会得到很粗糙的值，
    插值能让分位数随样本量平滑变化。
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[int(position)]
    return sorted_values[lower] * (upper - position) + sorted_values[upper] * (position - lower)


async def register_and_create_endpoint(
    client: httpx.AsyncClient, base_url: str, target_url: str
) -> tuple[str, str, str]:
    """注册一个压测专用账号并创建接收地址，返回 (api_key, ingest_url, secret)。"""
    email = f"loadtest-{int(time.time())}@{uuid.uuid4().hex[:8]}.example.com"
    response = await client.post(
        f"{base_url}/api/auth/register",
        json={"email": email, "password": "Load-Test-Password-123"},
        timeout=30.0,
    )
    response.raise_for_status()
    api_key = response.json()["api_key"]

    response = await client.post(
        f"{base_url}/api/endpoints",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "name": "压测接收地址",
            "target_url": target_url,
            "max_attempts": 3,
            "timeout_seconds": 10,
        },
        timeout=30.0,
    )
    response.raise_for_status()
    data = response.json()
    return api_key, data["ingest_url"], data["secret"]


async def send_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    ingest_url: str,
    headers: dict[str, str],
    index: int,
    results: list[tuple[int, float]],
) -> None:
    """发送一个事件并把 (状态码, 耗时) 记进结果列表。"""
    async with semaphore:
        started = time.perf_counter()
        try:
            response = await client.post(
                ingest_url,
                content=LOAD_BODY,
                headers={**headers, "X-HookRelay-Idempotency-Key": f"load-{index}"},
            )
            status = response.status_code
        except httpx.HTTPError:
            # 用 0 表示"请求根本没发出去"，与收到 4xx/5xx 区分开
            status = 0
        results.append((status, time.perf_counter() - started))


async def run_ingest_phase(
    base_url: str, ingest_url: str, secret: str, total: int, concurrency: int
) -> dict[str, Any]:
    """并发打满入站接口，返回吞吐与延迟统计。"""
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-HookRelay-Timestamp": timestamp,
        "X-HookRelay-Signature": build_signature(secret, timestamp, LOAD_BODY),
    }

    semaphore = asyncio.Semaphore(concurrency)
    results: list[tuple[int, float]] = []

    limits = httpx.Limits(
        max_connections=concurrency * 2,
        max_keepalive_connections=concurrency,
    )
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        started = time.perf_counter()
        await asyncio.gather(
            *(send_one(client, semaphore, ingest_url, headers, i, results) for i in range(total))
        )
        wall_seconds = time.perf_counter() - started

    by_status: dict[int, int] = {}
    for status, _ in results:
        by_status[status] = by_status.get(status, 0) + 1

    accepted = by_status.get(202, 0)
    latencies = sorted(elapsed for status, elapsed in results if status == 202)

    return {
        "total": total,
        "wall_seconds": wall_seconds,
        "throughput": accepted / wall_seconds if wall_seconds > 0 else 0.0,
        "by_status": by_status,
        "accepted": accepted,
        "started_at": started,
        "p50_ms": percentile(latencies, 0.50) * 1000,
        "p95_ms": percentile(latencies, 0.95) * 1000,
        "p99_ms": percentile(latencies, 0.99) * 1000,
        "max_ms": (latencies[-1] * 1000) if latencies else 0.0,
    }


async def wait_until_drained(
    client: httpx.AsyncClient, base_url: str, api_key: str
) -> tuple[dict[str, Any], float, float]:
    """等到没有待投递与投递中的事件。

    返回 (最终统计, 尾部耗时, 结束时刻)。

    不能拿这里的耗时去算投递吞吐：压测期间 Worker 一直在跑，大多数事件
    在入站还没结束时就已经投完了，剩下的只是尾巴。用尾巴做分母会算出
    高得不合理的吞吐（超过 Worker 并发除以单次均值的理论上限）。
    有意义的是端到端口径：从发出第一个事件到全部投递完成。
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    started = time.perf_counter()
    stats: dict[str, Any] = {}

    while time.perf_counter() - started < DRAIN_TIMEOUT_SECONDS:
        response = await client.get(f"{base_url}/api/stats", headers=headers, timeout=30.0)
        response.raise_for_status()
        stats = response.json()
        if stats["pending"] == 0 and stats["delivering"] == 0:
            break
        await asyncio.sleep(0.5)

    finished = time.perf_counter()
    return stats, finished - started, finished


async def analyze_queue_query(database_url: str | None) -> list[str]:
    """对取任务的查询跑 EXPLAIN ANALYZE，确认走了部分索引。

    两条约束：
    - 必须在事务里执行：EXPLAIN ANALYZE 会真的执行这条 SELECT FOR UPDATE，
      离开事务就真的把行锁上了。这里用 ROLLBACK 收尾。
    - 用与 Worker 完全相同的 SQL 形态（含 FOR UPDATE SKIP LOCKED），
      否则测的是另一条查询的执行计划，没有参考价值。
    """
    if not database_url:
        return ["（未提供数据库地址，跳过执行计划分析）"]

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    query = """
        EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
        SELECT * FROM events
        WHERE status = 'pending' AND next_attempt_at <= now()
        ORDER BY next_attempt_at
        LIMIT 50
        FOR UPDATE SKIP LOCKED
    """

    engine = create_async_engine(database_url)
    lines: list[str] = []
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                result = await connection.execute(text(query))
                lines = [row[0] for row in result.fetchall()]
            finally:
                # 必须回滚：EXPLAIN ANALYZE 已经真的取了行并加了锁
                await transaction.rollback()
    except Exception as exc:
        return [f"（执行计划分析失败：{type(exc).__name__}: {exc}）"]
    finally:
        await engine.dispose()

    return lines


def print_report(
    ingest: dict[str, Any],
    stats: dict[str, Any],
    drain_seconds: float,
    plan_lines: list[str],
    *,
    events: int,
    concurrency: int,
    worker_concurrency: int,
    end_to_end_seconds: float,
) -> None:
    """打印可以直接贴进文档的报告。"""
    print()
    print("=" * 78)
    print("HookRelay 压测报告")
    print("=" * 78)

    print("\n测试条件")
    print(f"  事件总数：  {events}")
    print(f"  并发数：    {concurrency}")
    print("  测试机：    Apple M1 Pro (10 核) / 32 GB / macOS")
    print("  服务形态：  docker compose，app 与 postgres 同机")
    print("  容器配额：  colima VM 4 CPU / 6 GB")
    print(f"  Worker 并发：{worker_concurrency}")

    print("\n入站（POST /ingest/{token}）")
    print(f"  墙钟耗时：  {ingest['wall_seconds']:.2f} s")
    print(f"  吞吐：      {ingest['throughput']:.0f} 事件/秒")
    print(f"  P50：       {ingest['p50_ms']:.1f} ms")
    print(f"  P95：       {ingest['p95_ms']:.1f} ms")
    print(f"  P99：       {ingest['p99_ms']:.1f} ms")
    print(f"  最大：      {ingest['max_ms']:.1f} ms")
    print(f"  状态码分布：{ingest['by_status']}")

    rate_limited = ingest["by_status"].get(429, 0)
    if rate_limited:
        print(f"\n  ** 有 {rate_limited} 个请求被限流（429）**")
        print("     这次测到的是限流阈值而不是服务容量。")
        print("     放开限流重跑：INGEST_RATE_LIMIT_PER_MINUTE=1000000 docker compose up -d")

    print("\n投递（Worker → 目标地址）")
    succeeded = stats.get("succeeded", 0)
    dead = stats.get("dead", 0)
    total_attempts = stats.get("total_attempts", 0)
    avg_ms = stats.get("avg_delivery_latency_ms") or 0.0
    print(f"  成功 / 死信：{succeeded} / {dead}")
    print(f"  尝试总数：  {total_attempts}（含重试）")
    print(f"  单次投递均值：{avg_ms} ms")
    if avg_ms > 0:
        ceiling = worker_concurrency / (avg_ms / 1000)
        print(f"  理论上限：  {ceiling:.0f} 事件/秒（Worker 并发 {worker_concurrency} ÷ 单次均值）")
    print(f"  尾部排空：  {drain_seconds:.2f} s（入站结束后剩下的那部分）")

    print("\n端到端（发出第一个事件 → 全部投递完成）")
    print(f"  总耗时：    {end_to_end_seconds:.2f} s")
    if end_to_end_seconds > 0:
        print(f"  吞吐：      {stats.get('total_events', 0) / end_to_end_seconds:.0f} 事件/秒")

    print("\n队列扫描执行计划")
    for line in plan_lines:
        print(f"  {line}")

    print("\n" + "=" * 78)


async def main_async(args: argparse.Namespace) -> int:
    base_url = args.base_url.rstrip("/")

    print("准备压测环境…")
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            health = (await client.get(f"{base_url}/health")).json()
        except httpx.HTTPError as exc:
            print(f"连不上 {base_url}：{exc}")
            return 1
        print(f"  服务：{health['service']} {health['version']}（{health['environment']}）")

        ready = await client.get(f"{base_url}/health/ready")
        if ready.status_code != 200:
            print(f"  数据库未就绪：{ready.text}")
            return 1
        print("  数据库：已就绪")

        api_key, ingest_url, secret = await register_and_create_endpoint(
            client, base_url, args.target_url
        )
        print(f"  压测账号与接收地址已创建（目标：{args.target_url}）")

        print(f"\n开始压测：{args.events} 个事件，并发 {args.concurrency}")
        ingest = await run_ingest_phase(base_url, ingest_url, secret, args.events, args.concurrency)
        print(f"  入站完成，吞吐 {ingest['throughput']:.0f} 事件/秒")

        print("  等待投递排空…")
        stats, drain_seconds, drain_finished = await wait_until_drained(client, base_url, api_key)
        print(f"  投递排空完成，尾部耗时 {drain_seconds:.2f} s")

    end_to_end_seconds = drain_finished - ingest["started_at"]
    plan_lines = await analyze_queue_query(args.database_url)

    print_report(
        ingest,
        stats,
        drain_seconds,
        plan_lines,
        events=args.events,
        concurrency=args.concurrency,
        worker_concurrency=args.worker_concurrency,
        end_to_end_seconds=end_to_end_seconds,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="HookRelay 压测")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--events", type=int, default=1000, help="发送的事件总数")
    parser.add_argument("--concurrency", type=int, default=50, help="并发请求数")
    parser.add_argument(
        "--target-url",
        default=f"http://host.docker.internal:{DEFAULT_SINK_URL.rsplit(':', 1)[1]}/sink",
        help="接收地址的转发目标（默认指向宿主机的演示接收端）",
    )
    parser.add_argument(
        "--worker-concurrency",
        type=int,
        default=int(os.environ.get("WORKER_CONCURRENCY", "10")),
        help="Worker 的投递并发度，用于推算理论上限",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="用于执行计划分析；不提供则跳过该项",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
