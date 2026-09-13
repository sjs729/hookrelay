"""容器环境端到端验证。

在 `docker compose up` 起来的真实容器上，走一遍完整业务链路：

    注册账号 → 创建接收地址 → 投递一条签名事件 → 接收端确认收到

与 verify_day5.py 的区别：那个脚本验证的是「可观测性」，直接对着
宿主机上跑的开发服务；这个脚本验证的是「镜像里的东西能工作」，
包括容器里的迁移、容器内 Web 与 Worker 的协作、以及容器访问宿主机
接收端时的地址解析。

用法（需先启动接收端与 compose）：

    uv run python scripts/demo_sink.py &
    docker compose up -d
    uv run python scripts/verify_docker.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.security import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    compute_signature,
)

# 容器里的 Web 映射到宿主机 8000
API_BASE = os.environ.get("HOOKRELAY_API_BASE", "http://127.0.0.1:8000")
# 接收端跑在宿主机上，从脚本这边看是 127.0.0.1
SINK_BASE = os.environ.get("HOOKRELAY_SINK_BASE", "http://127.0.0.1:9000")
# 但这个地址是写给「容器里的 Worker」用的。
# host.docker.internal 是容器访问宿主机的约定名，colima 下 host.lima.internal 同样可用。
# 如果这里填 127.0.0.1，Worker 会连到容器自己，表现为投递永远失败。
SINK_FROM_CONTAINER = os.environ.get(
    "HOOKRELAY_SINK_FROM_CONTAINER",
    "http://host.docker.internal:9000/sink",
)

PASSWORD = "Docker-Verify-123"
passed = 0
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    """打印一条检查结果。"""
    global passed, failed
    if ok:
        passed += 1
        print(f"  [通过] {name}" + (f" | {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [失败] {name}" + (f" | {detail}" if detail else ""))


def section(title: str) -> None:
    """打印分节标题。"""
    print()
    print("=" * 64)
    print(title)
    print("=" * 64)


def counter_value(text: str, name: str, label: str = "") -> float:
    """从 Prometheus 文本里取出某个计数器的值。

    计数器是累积的，当前值多少并不说明什么，
    有意义的是「本次操作让它增加了多少」。所以要先取基线再算增量。
    """
    for line in text.splitlines():
        if line.startswith("#") or not line.startswith(name):
            continue
        if label and label not in line:
            continue
        try:
            return float(line.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
            continue
    return 0.0


def main() -> int:
    """执行全部检查并返回失败数。"""
    email = f"verify-docker-{secrets.token_hex(4)}@example.com"

    with httpx.Client(timeout=30.0) as client:
        # ---------- 前置：三个端点都要活着 ----------
        section("0. 前置检查")
        try:
            health = client.get(f"{API_BASE}/health")
            check("Web 服务存活", health.status_code == 200, health.text[:60])
        except httpx.HTTPError as exc:
            check("Web 服务存活", False, str(exc))
            print("\n  未检测到服务，请先执行 docker compose up -d")
            return 1

        ready = client.get(f"{API_BASE}/health/ready")
        check(
            "就绪检查通过（容器内数据库连通）",
            ready.status_code == 200 and ready.json().get("database") == "up",
            ready.text[:60],
        )

        try:
            sink_health = client.get(f"{SINK_BASE}/health")
            check("接收端存活", sink_health.status_code == 200, sink_health.text[:60])
        except httpx.HTTPError as exc:
            check("接收端存活", False, str(exc))
            print("\n  未检测到接收端，请先执行 uv run python scripts/demo_sink.py")
            return 1

        client.post(f"{SINK_BASE}/reset")

        # 指标计数器是累积的，先取基线，结束时用增量断言。
        # 直接断言「值为 1」在跑过第二遍之后就会失效，
        # 那样的测试通过与否取决于历史，而不取决于本次行为。
        worker_metrics_url = "http://127.0.0.1:9101/metrics"
        try:
            baseline_delivery = counter_value(
                client.get(worker_metrics_url).text,
                "hookrelay_delivery_total",
                'result="succeeded"',
            )
        except httpx.HTTPError:
            baseline_delivery = 0.0
        print(f"  投递成功计数基线：{baseline_delivery:g}")

        # ---------- 1. 注册 ----------
        section("1. 注册账号")
        resp = client.post(
            f"{API_BASE}/api/auth/register",
            json={"email": email, "password": PASSWORD},
        )
        check("注册返回 201", resp.status_code == 201, str(resp.status_code))
        if resp.status_code != 201:
            print(f"  响应：{resp.text[:200]}")
            return 1
        api_key = resp.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}
        check("拿到 API Key", bool(api_key), f"{api_key[:12]}…")

        # ---------- 2. 创建接收地址 ----------
        section("2. 创建接收地址")
        resp = client.post(
            f"{API_BASE}/api/endpoints",
            headers=headers,
            json={
                "name": "docker-verify",
                # 注意这里用的是容器视角的宿主机地址
                "target_url": SINK_FROM_CONTAINER,
                "max_attempts": 3,
                "timeout_seconds": 5,
            },
        )
        check("创建返回 201", resp.status_code == 201, str(resp.status_code))
        if resp.status_code != 201:
            print(f"  响应：{resp.text[:200]}")
            return 1
        endpoint = resp.json()
        check("返回签名密钥", bool(endpoint.get("secret")))
        check("返回接收地址", "ingest_url" in endpoint, endpoint.get("ingest_url", ""))
        # 接收地址的路由是 /ingest/{token}，不带 /api 前缀。
        # 直接用服务端返回的地址而不是自己拼路径，免得路由改了脚本还照旧。
        ingest_token = endpoint["ingest_url"].rsplit("/", 1)[-1]
        ingest_path = f"{API_BASE}/ingest/{ingest_token}"
        print(f"  目标地址：{SINK_FROM_CONTAINER}")
        print(f"  接收地址：{ingest_path}")

        # ---------- 3. 投递一条签名事件 ----------
        section("3. 发送签名事件")
        payload = {"order_id": "A-1001", "amount": 199, "from": "docker-verify"}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = compute_signature(endpoint["secret"], timestamp, body)

        resp = client.post(
            ingest_path,
            content=body,
            headers={
                "Content-Type": "application/json",
                TIMESTAMP_HEADER: timestamp,
                SIGNATURE_HEADER: signature,
            },
        )
        check("接收返回 202", resp.status_code == 202, str(resp.status_code))
        if resp.status_code != 202:
            print(f"  响应：{resp.text[:200]}")
            return 1
        event_id = resp.json().get("event_id")
        check("返回事件 ID", bool(event_id), str(event_id))

        # ---------- 4. 等 Worker 投递 ----------
        section("4. 等待容器内 Worker 投递")
        status = ""
        for _ in range(30):
            time.sleep(1)
            detail = client.get(f"{API_BASE}/api/events/{event_id}", headers=headers)
            if detail.status_code != 200:
                continue
            status = detail.json()["status"]
            if status in {"succeeded", "dead"}:
                break
        check("事件投递成功", status == "succeeded", f"实际 {status}")
        if status != "succeeded":
            print(f"  事件详情：{detail.text[:300]}")

        # ---------- 5. 接收端确认 ----------
        section("5. 接收端确认收到")
        received = client.get(f"{SINK_BASE}/received", params={"limit": 10})
        check("接收端查询返回 200", received.status_code == 200)
        items = received.json().get("items", [])
        check("接收端收到 1 条", len(items) == 1, f"实际 {len(items)} 条")
        if items:
            got = items[0]
            # 注意比对的是解析后的字典而不是原始字符串：
            # 事件载荷存的是 JSONB，PostgreSQL 会重排键的顺序，
            # 字面量比较必然失败（这也正是幂等校验用 body 哈希而不用字典比较的原因）。
            try:
                got_payload = json.loads(got.get("body", ""))
            except json.JSONDecodeError:
                got_payload = None
            check(
                "投递内容与原始一致",
                got_payload == payload,
                json.dumps(got_payload, ensure_ascii=False)[:80] if got_payload else "无法解析",
            )
            # 至少一次语义下，下游需要靠这个标识自行去重
            check(
                "带上投递标识供下游去重",
                bool(got.get("delivery_id")),
                str(got.get("delivery_id"))[:36],
            )
            check("投递轮次为第 1 次", got.get("attempt") == "1", str(got.get("attempt")))

        # ---------- 6. 统计与指标 ----------
        section("6. 统计与指标")
        stats = client.get(f"{API_BASE}/api/stats", headers=headers)
        check("统计接口返回 200", stats.status_code == 200)
        if stats.status_code == 200:
            data = stats.json()
            check("成功数为 1", data.get("succeeded") == 1, str(data.get("succeeded")))
            check("事件总数为 1", data.get("total_events") == 1, str(data.get("total_events")))

        # Web 的 /metrics 在宿主机 8000 上可见
        metrics = client.get(f"{API_BASE}/metrics")
        check("Web 指标可读", metrics.status_code == 200)
        check(
            "接收计数已记录",
            "hookrelay_ingest_total{" in metrics.text,
            "指标名存在",
        )

        # Worker 的指标在另一个端口，compose 把它映射到宿主机 9101
        worker_metrics = client.get(worker_metrics_url)
        check("Worker 指标端口可读", worker_metrics.status_code == 200)
        now_delivery = counter_value(
            worker_metrics.text,
            "hookrelay_delivery_total",
            'result="succeeded"',
        )
        check(
            "投递成功计数增加了 1",
            now_delivery - baseline_delivery == 1,
            f"{baseline_delivery:g} → {now_delivery:g}",
        )

        # ---------- 清场 ----------
        section("清场")
        print("  验收数据带 verify-docker- 前缀，可用下面的语句一次性删除：")
        print("    docker compose exec db psql -U hookrelay -d hookrelay_dev \\")
        print("      -c \"DELETE FROM users WHERE email LIKE 'verify-docker-%';\"")
        print("  外键级联会一并清掉 endpoints / events / delivery_attempts。")

    section("结果")
    print(f"{passed} 通过，{failed} 失败")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
