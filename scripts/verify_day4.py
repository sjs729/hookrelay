"""Day 4 验收脚本：投递引擎端到端验证。

前置条件（三个进程都要在跑）：

1. Web 服务   uv run uvicorn app.main:app --reload --port 8000
2. 演示接收端 uv run python scripts/demo_sink.py
3. Worker     uv run python -m app.worker

脚本本身只做"发请求 → 等结果 → 断言"，不启动也不结束任何进程。
这样任何一步失败都能单独复现，而不是被进程管理逻辑搅在一起。

用法：

    uv run python scripts/verify_day4.py
"""

import hashlib
import hmac
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

API = "http://127.0.0.1:8000"
SINK = "http://127.0.0.1:9000"

# 每次运行用不同的邮箱后缀，脚本可以直接重复执行，不会因为上一次残留的
# 同名账号而失败。这些临时账号在验收结束后统一清理。
RUN_ID = uuid.uuid4().hex[:8]
EMAIL = f"verify-day4-{RUN_ID}@example.com"
PASSWORD = "Verify-Day4-Password"

results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    """打印一条断言结果并记录，最后统一汇总。"""
    results.append(ok)
    mark = "✅" if ok else "❌"
    print(f"{mark} {label}" + (f"  →  {detail}" if detail else ""))
    return ok


def sign(secret: str, body: bytes) -> tuple[str, str]:
    """按接收端约定的规则计算签名。

    签名对象是 `时间戳 + b"." + 原始请求体`，而不是只有请求体。
    把时间戳纳入签名，攻击者改时间戳就会导致验签失败，
    时间戳窗口检查才真正有效。
    """
    timestamp = str(int(time.time()))
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return timestamp, f"sha256={mac.hexdigest()}"


def post_event(
    client: httpx.Client,
    ingest_url: str,
    secret: str,
    payload: dict[str, Any],
) -> httpx.Response:
    """向入站地址投递一条带签名的事件。"""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    timestamp, signature = sign(secret, body)
    return client.post(
        ingest_url,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-HookRelay-Timestamp": timestamp,
            "X-HookRelay-Signature": signature,
        },
    )


def wait_for_status(
    client: httpx.Client,
    headers: dict[str, str],
    event_id: str,
    targets: set[str],
    timeout: float = 90.0,
) -> dict[str, Any]:
    """轮询事件，直到它进入目标状态之一或超时。

    Worker 是异步的，脚本必须等待而不是立刻断言——
    这一点在验收脚本里也要如实体现，否则测的是"发出去的那一刻"，
    而不是"最终有没有投递成功"。
    """
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        response = client.get(f"{API}/api/events/{event_id}", headers=headers)
        response.raise_for_status()
        last = response.json()
        if last["status"] in targets:
            return last
        time.sleep(0.5)
    return last


def set_sink_mode(
    client: httpx.Client,
    mode: str,
    fail_status: int = 500,
    secret: str | None = None,
) -> None:
    """切换演示接收端的行为，可选地同时下发签名密钥。"""
    params: dict[str, Any] = {"mode": mode, "fail_status": fail_status}
    if secret is not None:
        params["secret"] = secret
    response = client.post(f"{SINK}/control", params=params)
    response.raise_for_status()


def main() -> int:
    client = httpx.Client(timeout=20.0)

    # ---------- 前置检查：三个进程是否都在 ----------
    print("\n=== 0. 前置检查 ===")
    for name, url in [("Web 服务", f"{API}/health"), ("演示接收端", f"{SINK}/health")]:
        try:
            code = client.get(url).status_code
        except httpx.HTTPError as exc:
            check(f"{name}可访问", False, str(exc))
            print("\n请先启动缺失的进程，再重新运行本脚本。")
            return 1
        check(f"{name}可访问", code == 200, f"HTTP {code}")

    # Worker 不做独立探测：它是否在跑，会在下面第一个真实投递里直接体现。
    # 专门为它加一个探针接口，反而会引入与业务无关的状态。

    # ---------- 准备账号与接收地址 ----------
    print("\n=== 1. 准备账号与接收地址 ===")

    registered = client.post(
        f"{API}/api/auth/register",
        json={"email": EMAIL, "password": PASSWORD},
    )
    if registered.status_code != 201:
        check("注册验收账号", False, f"HTTP {registered.status_code}: {registered.text}")
        return 1
    api_key = registered.json()["api_key"]
    check("注册验收账号", True, EMAIL)

    headers = {"Authorization": f"Bearer {api_key}"}

    created = client.post(
        f"{API}/api/endpoints",
        headers=headers,
        json={"name": "Day4 验收目标", "target_url": f"{SINK}/sink", "max_attempts": 5},
    )
    created.raise_for_status()
    endpoint = created.json()
    secret = endpoint["secret"]
    ingest_url = endpoint["ingest_url"]
    check("创建接收地址", True, f"目标={SINK}/sink")

    client.post(f"{SINK}/reset")
    # 把签名密钥交给接收端，让它能独立验签——这是"投递出去的请求是否真的
    # 能被第三方验证"的唯一证据，不能只由发送方自己声明。
    set_sink_mode(client, "ok", secret=secret)

    # ---------- 场景一：正常投递 ----------
    print("\n=== 2. 场景一：正常投递（发得出去 + 送得到 + 签名验得通）===")

    response = post_event(
        client,
        ingest_url,
        secret,
        {"event_id": "day4-happy-path", "action": "order.created", "order_no": "A-2026-001"},
    )
    check("入站接收返回 202", response.status_code == 202, f"HTTP {response.status_code}")
    event_id = response.json()["event_id"]

    event = wait_for_status(client, headers, event_id, {"succeeded", "dead"}, timeout=60)
    check(
        "事件最终投递成功",
        event.get("status") == "succeeded",
        f"status={event.get('status')} attempts={event.get('attempt_count')}",
    )
    check(
        "首次尝试即成功",
        event.get("attempt_count") == 1,
        f"attempt_count={event.get('attempt_count')}",
    )

    attempts = event.get("attempts", [])
    if attempts:
        first = attempts[0]
        check(
            "记录到投递明细",
            first["status_code"] is not None,
            f"status_code={first['status_code']} 耗时={first['duration_ms']}ms",
        )
        check("下游返回 200", first["status_code"] == 200, f"status_code={first['status_code']}")

    received = client.get(f"{SINK}/received").json()
    check("接收端确实收到事件", received["total"] >= 1, f"共收到 {received['total']} 条")
    if received["items"]:
        item = received["items"][-1]
        check(
            "接收端独立验签通过",
            item["signature_valid"] is True,
            f"signature_valid={item['signature_valid']}",
        )
        check("投递请求带投递编号", bool(item["delivery_id"]), f"delivery_id={item['delivery_id']}")

    # ---------- 场景二：下游 5xx，观察退避重试 ----------
    print("\n=== 3. 场景二：下游 500，观察退避重试 ===")

    set_sink_mode(client, "fail", fail_status=500)
    response = post_event(
        client,
        ingest_url,
        secret,
        {"event_id": "day4-retry-path", "action": "order.created", "order_no": "A-2026-002"},
    )
    retry_event_id = response.json()["event_id"]

    # 等它至少失败两次，说明重试真的发生了
    deadline = time.time() + 60
    retry_event: dict[str, Any] = {}
    while time.time() < deadline:
        retry_event = client.get(f"{API}/api/events/{retry_event_id}", headers=headers).json()
        if retry_event["attempt_count"] >= 2:
            break
        time.sleep(0.5)

    check(
        "500 触发重试",
        retry_event.get("attempt_count", 0) >= 2,
        f"已尝试 {retry_event.get('attempt_count')} 次，状态={retry_event.get('status')}",
    )
    check(
        "失败后回到等待队列（未立即放弃）",
        retry_event.get("status") == "pending",
        f"status={retry_event.get('status')} 下次投递={retry_event.get('next_attempt_at')}",
    )

    # 从明细里读出两次尝试的时间差，确认退避确实生效
    attempts = retry_event.get("attempts", [])
    check(
        "每次尝试都单独留痕",
        len(attempts) == retry_event.get("attempt_count"),
        f"明细 {len(attempts)} 条，attempt_count={retry_event.get('attempt_count')}",
    )
    if attempts:
        check(
            "记录下游返回的状态码",
            attempts[-1]["status_code"] == 500,
            f"status_code={attempts[-1]['status_code']}",
        )
        check("保留失败原因", bool(attempts[-1]["error"]), f"error={attempts[-1]['error']}")

    # ---------- 场景三：超过最大尝试次数进入死信 ----------
    print("\n=== 4. 场景三：耗尽重试预算，进入死信 ===")

    fail_fast = client.post(
        f"{API}/api/endpoints",
        headers=headers,
        json={
            "name": "Day4 死信验收",
            "target_url": f"{SINK}/sink",
            "max_attempts": 1,
        },
    )
    fail_fast.raise_for_status()
    ff = fail_fast.json()

    response = post_event(
        client,
        ff["ingest_url"],
        ff["secret"],
        {"event_id": "day4-dead-path", "action": "order.created"},
    )
    dead_event_id = response.json()["event_id"]

    dead_event = wait_for_status(client, headers, dead_event_id, {"dead", "succeeded"}, timeout=60)
    check(
        "超过上限后进入死信",
        dead_event.get("status") == "dead",
        f"status={dead_event.get('status')} attempts={dead_event.get('attempt_count')}",
    )
    check(
        "死信保留失败原因",
        bool(dead_event.get("last_error")),
        f"last_error={dead_event.get('last_error')}",
    )

    dead_list = client.get(
        f"{API}/api/events", headers=headers, params={"status": "dead"}
    ).json()
    check(
        "死信列表可查询",
        any(item["id"] == dead_event_id for item in dead_list["items"]),
        f"共 {dead_list['total']} 条死信",
    )

    # ---------- 场景四：修复下游后重放 ----------
    print("\n=== 5. 场景四：下游修好后重放死信 ===")

    set_sink_mode(client, "ok")
    replay = client.post(f"{API}/api/events/{dead_event_id}/replay", headers=headers)
    check("重放接口返回 200", replay.status_code == 200, f"HTTP {replay.status_code}")

    replayed = wait_for_status(client, headers, dead_event_id, {"succeeded", "dead"}, timeout=60)
    check(
        "重放后投递成功",
        replayed.get("status") == "succeeded",
        f"status={replayed.get('status')}",
    )
    check(
        "重放后尝试次数已重置",
        replayed.get("attempt_count") == 1,
        f"attempt_count={replayed.get('attempt_count')}",
    )

    all_attempts = replayed.get("attempts", [])
    check(
        "历史投递记录未被丢弃",
        len(all_attempts) >= 2,
        f"共 {len(all_attempts)} 条记录（含重放前失败的那次）",
    )

    # ---------- 场景五：越权访问防护 ----------
    print("\n=== 6. 场景五：越权与非法输入防护 ===")

    other = client.post(
        f"{API}/api/auth/register",
        json={"email": f"verify-day4-other-{RUN_ID}@example.com", "password": PASSWORD},
    )
    if other.status_code == 201:
        other_headers = {"Authorization": f"Bearer {other.json()['api_key']}"}
        peek = client.get(f"{API}/api/events/{dead_event_id}", headers=other_headers)
        check(
            "他人事件不可见（返回 404 而非 403）",
            peek.status_code == 404,
            f"HTTP {peek.status_code}",
        )

        replay_other = client.post(
            f"{API}/api/events/{dead_event_id}/replay", headers=other_headers
        )
        check(
            "他人事件不可重放",
            replay_other.status_code == 404,
            f"HTTP {replay_other.status_code}",
        )
    else:
        check("创建越权测试账号", False, f"HTTP {other.status_code}")

    again = client.post(f"{API}/api/events/{dead_event_id}/replay", headers=headers)
    check(
        "已成功的事件允许再次重放（运维可能需要重新推送）",        again.status_code == 200,
        f"HTTP {again.status_code}",
    )

    # ---------- 汇总 ----------
    print("\n=== 汇总 ===")
    passed = sum(results)
    total = len(results)
    print(f"通过 {passed}/{total}")
    if passed != total:
        print("存在失败项，请根据上面的 ❌ 逐条排查。")
        return 1

    print("\n提示：验收数据仍在库里（临时账号 verify-day4-*@example.com），")
    print("确认无误后可以执行清理。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
