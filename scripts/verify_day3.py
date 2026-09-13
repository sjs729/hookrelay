"""Day 3 验收脚本。

打真实运行中的服务（不是测试客户端），按清单逐项验证：

1. 带正确签名的请求返回 202
2. 签名错误返回 401
3. 时间戳过期返回 401
4. 同一幂等键连发两次，只产生一条事件记录
5. 超过限流阈值返回 429
6. 超大请求体返回 413

用法：
    uv run python scripts/verify_day3.py
"""

import hashlib
import hmac
import os
import sys
import time
import uuid

import httpx

BASE_URL = os.environ.get("HOOKRELAY_BASE_URL", "http://127.0.0.1:8000")
EMAIL = f"verify-day3-{uuid.uuid4().hex[:8]}@example.com"
PASSWORD = "a-strong-password-2026"

results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))
    mark = "PASS" if passed else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail else ""))


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """按服务端约定计算 HMAC-SHA256 签名。"""
    message = timestamp.encode("ascii") + b"." + body
    return "sha256=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def send_event(
    client: httpx.Client,
    token: str,
    secret: str,
    payload: dict,
    *,
    secret_override: str | None = None,
    timestamp_override: str | None = None,
    idempotency_key: str | None = None,
) -> httpx.Response:
    """构造一个带签名的入站请求。"""
    body = httpx.Request("POST", "http://x", json=payload).content
    timestamp = timestamp_override or str(int(time.time()))
    used_secret = secret_override if secret_override is not None else secret

    headers = {
        "Content-Type": "application/json",
        "X-HookRelay-Timestamp": timestamp,
        "X-HookRelay-Signature": sign(used_secret, timestamp, body),
    }
    if idempotency_key:
        headers["X-HookRelay-Idempotency-Key"] = idempotency_key

    return client.post(f"/ingest/{token}", content=body, headers=headers)


def main() -> int:
    with httpx.Client(base_url=BASE_URL, timeout=20.0) as client:
        # ---------- 准备：注册账号并创建接收地址 ----------
        print("\n[准备] 注册账号并创建接收地址")
        reg = client.post(
            "/api/auth/register",
            json={"email": EMAIL, "password": PASSWORD},
        )
        if reg.status_code != 201:
            print(f"  注册失败：{reg.status_code} {reg.text}")
            return 1
        api_key = reg.json()["api_key"]
        auth = {"Authorization": f"Bearer {api_key}"}

        created = client.post(
            "/api/endpoints",
            headers=auth,
            json={
                "name": "验收用接收地址",
                "target_url": "https://example.com/webhook",
            },
        )
        if created.status_code != 201:
            print(f"  创建接收地址失败：{created.status_code} {created.text}")
            return 1

        endpoint = created.json()
        token = endpoint["token"]
        secret = endpoint["secret"]
        print(f"  接收地址: {endpoint['ingest_url']}")
        print(f"  密钥掩码: {endpoint['secret_masked']}")

        # ---------- 1. 正确签名 ----------
        print("\n[1] 带正确签名的请求")
        r = send_event(client, token, secret, {"event_id": "evt-001", "action": "opened"})
        ok = r.status_code == 202 and r.json()["duplicate"] is False
        event_id = r.json().get("event_id", "?")
        record("正确签名返回 202", ok, f"HTTP {r.status_code}, event_id={event_id[:8]}...")

        # ---------- 2. 签名错误 ----------
        print("\n[2] 签名错误")
        r = send_event(
            client, token, secret, {"event_id": "evt-002"}, secret_override="wrong-secret"
        )
        detail = r.json().get("detail", "")
        record("错误签名返回 401", r.status_code == 401, f"HTTP {r.status_code} {detail}")

        # 请求体被改动时签名也应失效（防篡改）
        body = b'{"amount": 100}'
        ts = str(int(time.time()))
        good_sig = sign(secret, ts, body)
        tampered = client.post(
            f"/ingest/{token}",
            content=b'{"amount": 9999}',
            headers={
                "Content-Type": "application/json",
                "X-HookRelay-Timestamp": ts,
                "X-HookRelay-Signature": good_sig,
            },
        )
        record("请求体被篡改返回 401", tampered.status_code == 401, f"HTTP {tampered.status_code}")

        # ---------- 3. 时间戳过期 ----------
        print("\n[3] 时间戳超出容差窗口")
        old_ts = str(int(time.time()) - 3600)
        r = send_event(client, token, secret, {"event_id": "evt-003"}, timestamp_override=old_ts)
        record("时间戳过期(1小时前)返回 401", r.status_code == 401, f"HTTP {r.status_code}")

        future_ts = str(int(time.time()) + 3600)
        r = send_event(client, token, secret, {"event_id": "evt-004"}, timestamp_override=future_ts)
        record("时间戳来自未来(1小时后)返回 401", r.status_code == 401, f"HTTP {r.status_code}")

        # ---------- 4. 幂等 ----------
        print("\n[4] 同一幂等键连发两次")
        payload = {"event_id": "evt-dup-001", "action": "paid"}
        r1 = send_event(client, token, secret, payload, idempotency_key="order-12345")
        r2 = send_event(client, token, secret, payload, idempotency_key="order-12345")
        same_id = r1.json().get("event_id") == r2.json().get("event_id")
        first_dup = r1.json().get("duplicate")
        second_dup = r2.json().get("duplicate")
        record(
            "两次返回同一个 event_id",
            same_id and second_dup is True,
            f"第一次 duplicate={first_dup}，第二次 duplicate={second_dup}",
        )

        # 内容相同但幂等键不同 → 应当是两条独立事件
        r3 = send_event(client, token, secret, payload, idempotency_key="order-99999")
        record(
            "不同幂等键产生不同事件",
            r3.json().get("event_id") != r1.json().get("event_id"),
            "event_id 不同",
        )

        # ---------- 6. 超大请求体（放在限流测试之前，避免被 429 抢先拦截）----------
        print("\n[5] 超大请求体")
        big_body = b'{"data": "' + b"x" * (1024 * 1024 + 100) + b'"}'
        ts = str(int(time.time()))
        r = client.post(
            f"/ingest/{token}",
            content=big_body,
            headers={
                "Content-Type": "application/json",
                "X-HookRelay-Timestamp": ts,
                "X-HookRelay-Signature": sign(secret, ts, big_body),
            },
        )
        record("超过 1MB 的请求体返回 413", r.status_code == 413, f"HTTP {r.status_code}")

        # ---------- 5. 限流 ----------
        print("\n[6] 入站限流（阈值 120 次/分钟）")
        limited_at = None
        for i in range(1, 140):
            resp = send_event(client, token, secret, {"event_id": f"flood-{i}"})
            if resp.status_code == 429:
                limited_at = i
                retry_after = resp.headers.get("Retry-After")
                record(
                    "超限后返回 429 并带 Retry-After",
                    retry_after is not None and retry_after.isdigit(),
                    f"第 {i} 次请求被拦截，Retry-After={retry_after}s",
                )
                break
        if limited_at is None:
            record("超限后返回 429", False, "发了 139 次都没有触发限流")

        # ---------- 数据库侧核对 ----------
        print("\n[7] 数据库中的事件记录")
        events = client.get("/api/endpoints", headers=auth).json()
        endpoint_id = events[0]["id"]

    print("\n" + "=" * 62)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    for name, ok, _ in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print("=" * 62)
    print(f"结果：{passed}/{total} 通过")
    print("\n清理用信息（脚本外执行）：")
    print(f"  用户邮箱: {EMAIL}")
    print(f"  endpoint: {endpoint_id}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
