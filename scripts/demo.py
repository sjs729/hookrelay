"""HookRelay 完整使用演示。

这个脚本不是测试，是一份能跑起来的说明书：它按真实使用顺序走完一遍
完整链路，每一步都打印"在做什么"和"为什么会看到这个结果"。

    uv run python scripts/demo.py

前置条件（三个进程）：

    1. Web 服务    uv run uvicorn app.main:app --port 8000
    2. 投递 Worker  uv run python -m app.worker
    3. 演示接收端   uv run python scripts/demo_sink.py

想验证线上部署时，用 --base-url 指向公网地址；远端没有配套的
演示接收端，脚本会自动跳过依赖 sink 的那几步：

    uv run python scripts/demo.py --base-url https://hookrelay-xxxx.onrender.com
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

# 允许直接用 `python scripts/demo.py` 运行：
# 此时脚本所在目录是 scripts/，项目根不在 sys.path 上
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_SINK_URL = "http://127.0.0.1:9000"

# 轮询等待投递结果的上限。Worker 默认每秒轮询一次，
# 加上重试退避，几秒足够；给到 30 秒是为了容错而不是常态
WAIT_TIMEOUT_SECONDS = 30.0

STEP_INDEX = 0


def step(title: str, why: str) -> None:
    """打印一个步骤的分隔标题。"""
    global STEP_INDEX
    STEP_INDEX += 1
    print(f"\n{'=' * 72}")
    print(f"步骤 {STEP_INDEX}：{title}")
    print(f"{'=' * 72}")
    if why:
        print(f"说明：{why}\n")


def ok(message: str) -> None:
    print(f"  [通过] {message}")


def info(message: str) -> None:
    print(f"  {message}")


def fail(message: str) -> None:
    print(f"  [失败] {message}")


def build_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    """计算请求签名。

    签名对象是 ``时间戳 + "." + 原始请求体``，而不是只有请求体。
    把时间戳纳入签名范围，攻击者就无法把旧请求原样重放——
    改动时间戳会让签名对不上，不改则超出容忍窗口。
    """
    message = f"{timestamp}.".encode() + raw_body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def send_event(
    client: httpx.Client,
    ingest_url: str,
    secret: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> httpx.Response:
    """按上游服务的方式发送一个事件。"""
    raw_body = json.dumps(payload, ensure_ascii=False).encode()
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-HookRelay-Timestamp": timestamp,
        "X-HookRelay-Signature": build_signature(secret, timestamp, raw_body),
    }
    if idempotency_key:
        headers["X-HookRelay-Idempotency-Key"] = idempotency_key
    return client.post(ingest_url, content=raw_body, headers=headers)


def wait_for_status(
    client: httpx.Client,
    api_headers: dict[str, str],
    base_url: str,
    event_id: str,
    targets: set[str],
    *,
    timeout: float = WAIT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """轮询事件，直到状态落入目标集合或超时。

    投递是异步的：接收到返回 202 时事件只是落了库，
    真正的 HTTP 请求是 Worker 稍后发出的。所以要等。
    """
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"{base_url}/api/events/{event_id}", headers=api_headers)
        response.raise_for_status()
        last = response.json()
        if last["status"] in targets:
            return last
        time.sleep(0.5)
    return last


def wait_for_retry(
    client: httpx.Client,
    api_headers: dict[str, str],
    base_url: str,
    event_id: str,
    *,
    timeout: float = WAIT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """等到事件至少失败过一次、正在等待下一次投递时返回。

    这里不能像其他步骤那样直接等某个状态名，因为事件状态里没有 "retrying"：
    重试等待中的事件状态仍然是 pending——它确实还在等被取走投递。
    """
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"{base_url}/api/events/{event_id}", headers=api_headers)
        response.raise_for_status()
        last = response.json()
        if last["status"] in {"dead", "succeeded"}:
            return last
        # pending 且已有错误信息 = 投过一次、失败了、排在下一轮
        if last["status"] == "pending" and last.get("last_error"):
            return last
        time.sleep(0.3)
    return last


def set_sink_mode(client: httpx.Client, sink_url: str, mode: str, fail_status: int = 500) -> None:
    """切换演示接收端的行为。"""
    client.post(
        f"{sink_url}/control",
        params={"mode": mode, "fail_status": fail_status},
    ).raise_for_status()


def sink_reachable(client: httpx.Client, sink_url: str) -> bool:
    try:
        return client.get(f"{sink_url}/health", timeout=3.0).status_code == 200
    except httpx.HTTPError:
        return False


def show_attempts(detail: dict[str, Any]) -> None:
    """打印每一次投递尝试的关键信息。"""
    for attempt in detail.get("attempts", []):
        code = attempt.get("status_code")
        error = attempt.get("error")
        duration = attempt.get("duration_ms")
        outcome = f"HTTP {code}" if code else (error or "无结果")
        info(f"第 {attempt['attempt_number']} 次：{outcome}（耗时 {duration} ms）")


def main() -> int:
    parser = argparse.ArgumentParser(description="HookRelay 完整使用演示")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="HookRelay 服务地址")
    parser.add_argument(
        "--sink-url", default=DEFAULT_SINK_URL, help="演示接收端地址（用于探测与切换模式）"
    )
    parser.add_argument(
        "--target-url",
        default=None,
        help=(
            "接收地址的转发目标。默认取 --sink-url/sink。"
            "当 HookRelay 跑在 Docker 里而接收端在宿主机上时，"
            "要传 http://host.docker.internal:9000/sink——"
            "容器里的 127.0.0.1 指的是容器自己"
        ),
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    sink_url = args.sink_url.rstrip("/")
    target_url = args.target_url or f"{sink_url}/sink"
    failures = 0

    print("HookRelay 使用演示")
    print(f"服务地址：{base_url}")
    print(f"接收端：  {sink_url}")
    print(f"转发目标：{target_url}")

    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        # ---------- 前置检查 ----------
        step("确认服务在运行", "后面所有请求都依赖它")
        try:
            health = client.get(f"{base_url}/health").json()
        except httpx.HTTPError as exc:
            fail(f"连不上 {base_url}：{exc}")
            print("\n请先启动服务：uv run uvicorn app.main:app --port 8000")
            return 1
        ok(f"服务存活：{health['service']} {health['version']}（{health['environment']}）")

        ready = client.get(f"{base_url}/health/ready")
        if ready.status_code == 200:
            ok(f"数据库就绪：{ready.json()['database']}")
        else:
            fail(f"数据库未就绪：{ready.text}")
            return 1

        has_sink = sink_reachable(client, sink_url)
        if has_sink:
            ok(f"演示接收端在线：{sink_url}")
        else:
            info("演示接收端未启动，将跳过失败重试与死信相关步骤")
            info("需要时另开终端运行：uv run python scripts/demo_sink.py")

        # ---------- 注册 ----------
        step(
            "注册账号并获取 API Key",
            "API Key 是调用管理接口的凭据。明文只在注册响应里出现一次，"
            "服务端只保存它的哈希——所以必须当场保存。",
        )
        email = f"demo-{int(time.time())}@{uuid.uuid4().hex[:8]}.example.com"
        response = client.post(
            f"{base_url}/api/auth/register",
            json={"email": email, "password": "Demo-Password-123"},
        )
        if response.status_code != 201:
            fail(f"注册失败：{response.status_code} {response.text}")
            return 1
        account = response.json()
        api_key = account["api_key"]
        api_headers = {"Authorization": f"Bearer {api_key}"}
        ok(f"账号已创建：{account['email']}")
        info(f"API Key 前缀：{account['api_key_prefix']}（完整值已收到，此处不回显）")

        # ---------- 创建接收地址 ----------
        step(
            "创建接收地址",
            "一个接收地址 = 一个入站 token + 一个转发目标。"
            "上游把 Webhook 打到 ingest_url，HookRelay 负责转发到 target_url。",
        )
        response = client.post(
            f"{base_url}/api/endpoints",
            headers=api_headers,
            json={
                "name": "演示接收地址",
                "target_url": target_url,
                "max_attempts": 5,
                "timeout_seconds": 10,
            },
        )
        if response.status_code != 201:
            fail(f"创建失败：{response.status_code} {response.text}")
            return 1
        endpoint = response.json()
        ingest_url = endpoint["ingest_url"]
        secret = endpoint["secret"]
        ok(f"接收地址已创建：{endpoint['name']}")
        info(f"入站地址：{ingest_url}")
        info(f"签名密钥：明文只在这次返回，之后的查询里只有拖码 {endpoint['secret_masked']}")

        # ---------- 成功投递 ----------
        step(
            "发送一个事件，观察它被成功投递",
            "上游用签名密钥计算 HMAC-SHA256 签名。HookRelay 先验证签名与时间戳，"
            "再把事件落库并立刻返回 202——此时投递还没发生。",
        )
        if has_sink:
            set_sink_mode(client, sink_url, "ok")

        response = send_event(
            client,
            ingest_url,
            secret,
            {"order_id": 1001, "action": "created", "amount": 99.5},
        )
        if response.status_code != 202:
            fail(f"事件未被接受：{response.status_code} {response.text}")
            failures += 1
        else:
            accepted = response.json()
            event_id = accepted["event_id"]
            ok("已接受，返回 202（不是 200）")
            info(
                "202 的含义是「已接收、尚未完成」。事件此刻只是安全落库了，"
                "投递是后台 Worker 稍后做的事。"
            )
            detail = wait_for_status(client, api_headers, base_url, event_id, {"succeeded", "dead"})
            if detail["status"] == "succeeded":
                ok(f"事件已投递成功（共尝试 {detail['attempt_count']} 次）")
                show_attempts(detail)
            else:
                fail(f"事件未成功，最终状态：{detail['status']}")
                failures += 1

        # ---------- 签名校验 ----------
        step(
            "验证签名确实在起作用",
            "把签名改成错的，请求应当被拒绝。如果这里通过了，"
            "说明签名校验形同虚设，任何人都能伪造事件。",
        )
        raw = b'{"forged": true}'
        response = client.post(
            ingest_url,
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-HookRelay-Timestamp": str(int(time.time())),
                "X-HookRelay-Signature": "sha256=" + "0" * 64,
            },
        )
        if response.status_code == 401:
            ok(f"伪造签名被拒绝：401 {response.json().get('detail')}")
        else:
            fail(f"伪造签名竟然通过了：{response.status_code}")
            failures += 1

        # ---------- 过期时间戳 ----------
        step(
            "验证时间戳在防重放",
            "用正确的签名，但时间戳设在 1 小时前，应当被拒绝。"
            "这就是签名对象里必须包含时间戳的原因。",
        )
        stale_ts = str(int(time.time()) - 3600)
        raw = b'{"replayed": true}'
        response = client.post(
            ingest_url,
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-HookRelay-Timestamp": stale_ts,
                "X-HookRelay-Signature": build_signature(secret, stale_ts, raw),
            },
        )
        if response.status_code == 401:
            ok(f"过期时间戳被拒绝：401 {response.json().get('detail')}")
        else:
            fail(f"过期时间戳竟然通过了：{response.status_code}")
            failures += 1

        # ---------- 幂等 ----------
        step(
            "验证幂等键能挡住重复事件",
            "上游重试是常态（网络抖动、超时后重发）。带上同一个幂等键重发，"
            "HookRelay 认出这是同一个事件，不会重复投递给下游。",
        )
        idem_key = f"order-2001-{uuid.uuid4().hex[:8]}"
        first = send_event(client, ingest_url, secret, {"order_id": 2001}, idempotency_key=idem_key)
        second = send_event(
            client, ingest_url, secret, {"order_id": 2001}, idempotency_key=idem_key
        )
        if first.status_code == 202 and second.status_code == 202:
            duplicate = second.json()["duplicate"]
            if duplicate is True:
                ok("第二次发送被识别为重复（duplicate=true），未重复入队")
            else:
                fail(f"第二次发送未被识别为重复：duplicate={duplicate}")
                failures += 1
        else:
            fail(f"请求异常：{first.status_code} / {second.status_code}")
            failures += 1

        # ---------- 失败重试 ----------
        # 只有在确认进了死信之后才能重放，所以这里显式记录事件 id。
        # 不初始化的话，上面若未产生死信，下面的重放步骤会直接抛
        # UnboundLocalError —— 演示脚本不应该以读栈错误结束
        dead_event_id: str | None = None
        if has_sink:
            step(
                "让目标返回 503，观察自动重试",
                "5xx 属于「对方暂时不可用」，值得重试。HookRelay 会按指数退避"
                "重新排队，而不是立刻放弃。",
            )
            set_sink_mode(client, sink_url, "fail", 503)
            response = send_event(client, ingest_url, secret, {"order_id": 3001})
            event_id = response.json()["event_id"]
            detail = wait_for_retry(client, api_headers, base_url, event_id)
            if detail["status"] in {"pending", "delivering"}:
                ok(f"投递失败已进入重试等待，下次投递时间：{detail['next_attempt_at']}")
                info(f"最后一次错误：{detail['last_error']}")
                info("注意事件状态仍是 pending：重试不是另一个状态，就是“还在排队等投递”。")
                info("指数退避的间隔会逐次变长，避免把已经吃力的下游压垮。")
            else:
                fail(f"预期重试，实际状态：{detail['status']}")
                failures += 1

            step(
                "让目标返回 400，观察直接进死信",
                "4xx 属于「请求本身有问题」，重试再多次结果都一样。"
                "这类错误不重试，直接进死信等人工处理——这是两者的关键区别。",
            )
            set_sink_mode(client, sink_url, "fail", 400)
            response = send_event(client, ingest_url, secret, {"order_id": 4001})
            event_id = response.json()["event_id"]
            detail = wait_for_status(
                client, api_headers, base_url, event_id, {"dead", "succeeded"}, timeout=10.0
            )
            if detail["status"] == "dead":
                dead_event_id = event_id
                ok(f"已进入死信，尝试 {detail['attempt_count']} 次后放弃")
                info(f"错误：{detail['last_error']}")
            else:
                fail(f"预期进死信，实际状态：{detail['status']}")
                failures += 1

            step(
                "修复目标后重放死信",
                "死信不会自动恢复——反复重试一个明确不可用的目标只会浪费资源。"
                "下游修好后手动重放，事件会重新回到队列。",
            )
            set_sink_mode(client, sink_url, "ok")

            if dead_event_id is None:
                # 上一步没产生死信（比如那次投递意外成功了），
                # 就没有可重放的对象。报告并跳过后面的步骤
                fail("上一步没有产生死信事件，跳过重放演示")
                failures += 1
            else:
                response = client.post(
                    f"{base_url}/api/events/{dead_event_id}/replay", headers=api_headers
                )
                if response.status_code != 200:
                    fail(f"重放失败：{response.status_code} {response.text}")
                    failures += 1
                else:
                    ok(f"已重新入队：{response.json()['message']}")
                    detail = wait_for_status(
                        client, api_headers, base_url, dead_event_id, {"succeeded", "dead"}
                    )
                    if detail["status"] == "succeeded":
                        ok(f"重放后投递成功（本次尝试 {detail['attempt_count']} 次）")
                        info("注意尝试次数已清重新计数——重放是新一轮投递，不是原来那轮的延续。")
                    else:
                        fail(f"重放后仍未成功：{detail['status']}")
                        failures += 1

        # ---------- 统计 ----------
        step(
            "查看账号统计",
            "这是给你看的单次快照。给监控系统抓取的时间序列在 /metrics，两者用途不同。",
        )
        stats = client.get(f"{base_url}/api/stats", headers=api_headers).json()
        ok(
            f"接收地址 {stats['endpoints']} 个，累计事件 {stats['total_events']} 条，"
            f"成功 {stats['succeeded']}，死信 {stats['dead']}"
        )
        info(f"成功率：{stats['success_rate'] * 100:.1f}%")
        info(f"累计投递尝试：{stats['total_attempts']} 次（含重试）")
        info(f"平均单次投递耗时：{stats['avg_delivery_latency_ms']} ms")

        # ---------- 收尾 ----------
        step(
            "清理与下一步",
            "",
        )
        info(f"本次演示创建了账号 {email}")
        info("留意：注册接口返回的 API Key 和接收地址的签名密钥都只明文返回一次，")
        info("生产环境里应当立即写入密钥管理系统。")
        info(f"交互式 API 文档：{base_url}/docs")
        info(f"Prometheus 指标：{base_url}/metrics")

    print(f"\n{'=' * 72}")
    if failures:
        print(f"演示结束：{failures} 项未达预期")
    else:
        print("演示结束：全部步骤符合预期")
    print(f"{'=' * 72}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
