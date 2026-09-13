"""Day 5 验收脚本：可观测性端到端验证。

需要同时运行三个进程：

    uv run uvicorn app.main:app --port 8000     # Web 服务（/metrics 在这里）
    uv run python scripts/demo_sink.py --port 9000
    uv run python -m app.worker                 # 投递 Worker（指标在 :9101）

与前面几天的验证脚本不同，这里**故意不 mock 任何东西**：
可观测性的价值恰恰在于"真实运行时能看到什么"，用假的指标文本自证毫无意义。
所以脚本会真的推事件、真的等投递、真的去抓 /metrics。

用法：

    uv run python scripts/verify_day5.py
"""

import io
import json
import logging
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.observability import JsonLogFormatter, ctx, request_id_var
from app.security import compute_signature

WEB = "http://127.0.0.1:8000"
SINK = "http://127.0.0.1:9000"
WORKER_METRICS = "http://127.0.0.1:9101"

PASSWORD = "Verify-Day5-Password"
RUN_ID = uuid.uuid4().hex[:8]

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  [通过] {label}" + (f" | {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [失败] {label}" + (f" | {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


def parse_metric(text: str, name: str, **labels: str) -> float:
    """从 Prometheus 文本格式里取出某个指标的当前值。

    只匹配形如 `name{k="v"} 12.0` 的行。这里不引入 prometheus_client 的
    解析器：验收脚本应当独立于被测系统，直接用最笨的方式读文本更可信，
    也能顺便确认输出的确实是标准 Prometheus 格式。
    """
    wanted = ",".join(f'{key}="{value}"' for key, value in sorted(labels.items()))
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        if not line.startswith(name):
            continue
        body, _, value = line.partition(" ")
        if "{" in body:
            label_part = body[body.index("{") + 1 : body.rindex("}")]
            actual = ",".join(sorted(label_part.split(","))) if label_part else ""
        else:
            actual = ""
        if actual == wanted:
            return float(value)
    return 0.0


def sign(secret: str, body: bytes, timestamp: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-HookRelay-Timestamp": timestamp,
        "X-HookRelay-Signature": compute_signature(secret, timestamp, body),
    }


def post_event(client: httpx.Client, endpoint: dict, payload: dict) -> httpx.Response:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return client.post(
        f"{WEB}/ingest/{endpoint['token']}",
        content=body,
        headers=sign(endpoint["secret"], body, str(int(time.time()))),
    )


def wait_for_status(
    client: httpx.Client, headers: dict, event_id: str, target: str, timeout: int = 40
) -> str:
    """轮询事件状态直到到达目标值或超时。"""
    deadline = time.time() + timeout
    status = "unknown"
    while time.time() < deadline:
        response = client.get(f"{WEB}/api/events/{event_id}", headers=headers)
        if response.status_code == 200:
            status = response.json()["status"]
            if status == target:
                return status
        time.sleep(0.4)
    return status


def main() -> int:
    print(f"HookRelay Day 5 验收 | 运行标识 {RUN_ID}")

    with httpx.Client(timeout=20.0) as client:
        # ---------- 前置：三个进程都要在 ----------
        section("前置检查：服务可用性")
        for label, url in [
            ("Web 服务", f"{WEB}/health"),
            ("演示接收端", f"{SINK}/health"),
            ("Worker 指标端口", f"{WORKER_METRICS}/metrics"),
        ]:
            try:
                status_code = client.get(url).status_code
            except httpx.HTTPError as exc:
                print(f"  [失败] {label} 无法连接：{exc}")
                return 1
            check(f"{label} 可访问", status_code == 200, f"HTTP {status_code}")

        # ---------- 1. 就绪检查 ----------
        section("1. 就绪检查 /health/ready")
        ready = client.get(f"{WEB}/health/ready")
        check("就绪检查返回 200", ready.status_code == 200)
        ready_body = ready.json()
        check("数据库状态为 up", ready_body.get("database") == "up", str(ready_body))

        # ---------- 2. 准备账号与接收地址 ----------
        section("2. 准备测试账号与接收地址")
        email = f"verify-day5-{RUN_ID}@example.com"
        registered = client.post(
            f"{WEB}/api/auth/register", json={"email": email, "password": PASSWORD}
        )
        check("注册成功", registered.status_code == 201, f"HTTP {registered.status_code}")
        headers = {"Authorization": f"Bearer {registered.json()['api_key']}"}

        created = client.post(
            f"{WEB}/api/endpoints",
            headers=headers,
            json={"name": f"Day5 验收 {RUN_ID}", "target_url": f"{SINK}/sink", "max_attempts": 3},
        )
        check("创建接收地址", created.status_code == 201, f"HTTP {created.status_code}")
        endpoint = created.json()

        client.post(f"{SINK}/reset")
        client.post(f"{SINK}/control", params={"mode": "ok", "secret": endpoint["secret"]})

        # ---------- 3. 请求 ID 透传 ----------
        section("3. request_id 中间件")
        response = client.get(f"{WEB}/health")
        request_id = response.headers.get("X-Request-Id")
        check("响应头带 X-Request-Id", bool(request_id), request_id or "缺失")
        check("request_id 长度合理", bool(request_id and len(request_id) >= 8), request_id or "")

        echoed = client.get(f"{WEB}/health", headers={"X-Request-Id": "verify-fixed-id-123"})
        check(
            "调用方传入的 request_id 被沿用",
            echoed.headers.get("X-Request-Id") == "verify-fixed-id-123",
            echoed.headers.get("X-Request-Id", "缺失"),
        )

        # ---------- 4. 接收指标 ----------
        section("4. 接收侧指标")
        before = parse_metric(
            client.get(f"{WEB}/metrics").text, "hookrelay_ingest_total", result="accepted"
        )

        event_ids: list[str] = []
        for index in range(3):
            pushed = post_event(client, endpoint, {"event_id": f"{RUN_ID}-ok-{index}", "n": index})
            check(f"事件 {index} 入站 202", pushed.status_code == 202, f"HTTP {pushed.status_code}")
            event_ids.append(pushed.json()["event_id"])

        # 重复推送一次，验证幂等分支也被计入指标
        duplicate = post_event(client, endpoint, {"event_id": f"{RUN_ID}-ok-0", "n": 0})
        check("重复事件仍返回 202", duplicate.status_code == 202)
        check("重复事件标记 duplicate", duplicate.json()["duplicate"] is True)

        after = parse_metric(
            client.get(f"{WEB}/metrics").text, "hookrelay_ingest_total", result="accepted"
        )
        check("accepted 计数增加 3（重复不计）", after - before == 3, f"{before} → {after}")

        duplicate_metric = parse_metric(
            client.get(f"{WEB}/metrics").text, "hookrelay_ingest_total", result="duplicate"
        )
        check("duplicate 计数已记录", duplicate_metric >= 1, f"当前 {duplicate_metric}")

        # ---------- 5. 队列指标 ----------
        section("5. 队列深度指标")
        metrics_text = client.get(f"{WEB}/metrics").text
        queue_depth = parse_metric(metrics_text, "hookrelay_queue_depth")
        check("队列深度指标存在且为数值", queue_depth == queue_depth, f"queue_depth={queue_depth}")

        # ---------- 6. 投递指标 ----------
        section("6. 投递侧指标")
        worker_before = parse_metric(
            client.get(f"{WORKER_METRICS}/metrics").text,
            "hookrelay_delivery_total",
            result="succeeded",
        )

        for index, event_id in enumerate(event_ids):
            status = wait_for_status(client, headers, event_id, "succeeded")
            check(f"事件 {index} 投递成功", status == "succeeded", f"实际 {status}")

        worker_after = parse_metric(
            client.get(f"{WORKER_METRICS}/metrics").text,
            "hookrelay_delivery_total",
            result="succeeded",
        )
        check(
            "succeeded 计数增加 3",
            worker_after - worker_before == 3,
            f"{worker_before} → {worker_after}",
        )

        duration_text = client.get(f"{WORKER_METRICS}/metrics").text
        check(
            "投递耗时直方图有数据",
            parse_metric(duration_text, "hookrelay_delivery_duration_seconds_count") >= 3,
            f"样本数 {parse_metric(duration_text, 'hookrelay_delivery_duration_seconds_count')}",
        )

        # ---------- 7. 失败与重试指标 ----------
        section("7. 失败与重试指标")
        retrying_before = parse_metric(
            client.get(f"{WORKER_METRICS}/metrics").text,
            "hookrelay_delivery_total",
            result="retrying",
        )
        client.post(f"{SINK}/control", params={"mode": "fail", "fail_status": 503})

        failing = post_event(client, endpoint, {"event_id": f"{RUN_ID}-fail", "n": -1})
        check("失败事件入站 202", failing.status_code == 202)
        failing_id = failing.json()["event_id"]

        # 等 Worker 尝试一次并判定为可重试
        status = wait_for_status(client, headers, failing_id, "pending", timeout=5)
        time.sleep(2.5)
        retrying_after = parse_metric(
            client.get(f"{WORKER_METRICS}/metrics").text,
            "hookrelay_delivery_total",
            result="retrying",
        )
        check(
            "retrying 计数增加",
            retrying_after > retrying_before,
            f"{retrying_before} → {retrying_after}",
        )

        detail = client.get(f"{WEB}/api/events/{failing_id}", headers=headers).json()
        check("失败原因已记录", bool(detail.get("last_error")), str(detail.get("last_error")))
        check(
            "已产生投递记录",
            len(detail.get("attempts", [])) >= 1,
            f"{len(detail.get('attempts', []))} 条",
        )

        # 恢复接收端，避免影响后续
        client.post(f"{SINK}/control", params={"mode": "ok"})

        # ---------- 8. 统计接口 ----------
        section("8. 统计接口 /api/stats")
        stats = client.get(f"{WEB}/api/stats", headers=headers)
        check("统计接口返回 200", stats.status_code == 200)
        data = stats.json()
        check("接收地址数正确", data["endpoints"] == 1, str(data["endpoints"]))
        check("事件总数正确", data["total_events"] == 4, str(data["total_events"]))
        check("成功数正确", data["succeeded"] == 3, str(data["succeeded"]))
        check("成功率基于已终结事件", data["success_rate"] == 1.0, str(data["success_rate"]))
        check(
            "平均投递耗时已统计",
            data["avg_delivery_latency_ms"] is not None,
            f"{data['avg_delivery_latency_ms']} ms",
        )

        # ---------- 9. 死信指标 ----------
        section("9. 死信指标")
        client.post(f"{SINK}/control", params={"mode": "fail", "fail_status": 400})
        dead_event = post_event(client, endpoint, {"event_id": f"{RUN_ID}-dead", "n": -2})
        dead_id = dead_event.json()["event_id"]
        status = wait_for_status(client, headers, dead_id, "dead", timeout=10)
        check("4xx 直接进入死信", status == "dead", f"实际 {status}")

        time.sleep(1.0)
        dead_depth = parse_metric(client.get(f"{WEB}/metrics").text, "hookrelay_dead_letter_depth")
        check("死信深度指标 > 0", dead_depth > 0, f"dead_letter_depth={dead_depth}")

        client.post(f"{SINK}/control", params={"mode": "ok"})

        # ---------- 10. 结构化日志 ----------
        section("10. 结构化日志")
        # 走一遍真实的 logging 管线（logger → handler → formatter），
        # 而不是手工拼一个 LogRecord：只有整条链路通着，
        # 才能证明 ctx() 与 extra 的配合方式确实有效
        stream = io.StringIO()
        probe = logging.getLogger("hookrelay.verify")
        probe.handlers = [logging.StreamHandler(stream)]
        probe.setLevel(logging.INFO)
        probe.propagate = False
        probe.handlers[0].setFormatter(JsonLogFormatter())

        token = request_id_var.set("trace-abc-123")
        try:
            probe.info("事件已接收", extra=ctx(endpoint_id="ep-1", event_id="evt-1"))
        finally:
            request_id_var.reset(token)

        rendered = stream.getvalue().strip()
        check("输出是单行", "\n" not in rendered, f"{len(rendered)} 字符")

        try:
            parsed = json.loads(rendered)
        except json.JSONDecodeError as exc:
            check("输出可被解析为 JSON", False, str(exc))
            parsed = {}
        else:
            check("输出可被解析为 JSON", True)

        check("日志含 request_id 字段", "request_id" in parsed, str(sorted(parsed)))
        check(
            "日志含 logger 字段",
            parsed.get("logger") == "hookrelay.verify",
            str(parsed.get("logger")),
        )
        check("日志含时间戳", "ts" in parsed, str(parsed.get("ts")))
        check("extra 字段已展开", parsed.get("event_id") == "evt-1", str(parsed.get("event_id")))
        check("中文未被转义", "事件已接收" in rendered)
        check(
            "request_id 被写入日志",
            parsed.get("request_id") == "trace-abc-123",
            str(parsed.get("request_id")),
        )

        # ---------- 清场 ----------
        section("清场")
        print("  验收数据带 verify-day5- 前缀，可用下面的语句一次性删除：")
        print("    DELETE FROM users WHERE email LIKE 'verify-day5-%';")
        print("  外键级联会一并清掉 endpoints / events / delivery_attempts。")

    print(f"\n{'=' * 64}")
    print(f"结果：{passed} 通过，{failed} 失败")
    print(f"{'=' * 64}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
