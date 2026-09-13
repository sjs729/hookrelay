"""并发验证：多个 Worker 抢同一个队列时，一条事件只会被投递一次。

这是 `FOR UPDATE SKIP LOCKED` 正确性的直接证据。如果锁写错了
（比如把 SELECT 和 UPDATE 拆成两个事务、或者用普通 FOR UPDATE），
就会出现两个 Worker 同时拿到同一条事件、对下游连发两次请求。

关键在于**不采信 HookRelay 自己的说法**，而是看接收端实际收到了什么：
接收端收到的请求条数，必须等于事件条数。多一条就是重复投递。

前置条件：

1. Web 服务在跑
2. 演示接收端在跑
3. 至少已有一个 Worker 在跑（本脚本会再启动 2 个，形成三方竞争）

用法：

    uv run python scripts/verify_concurrency.py
"""

import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

API = "http://127.0.0.1:8000"
SINK = "http://127.0.0.1:9000"
EVENT_COUNT = 40
EXTRA_WORKERS = 2

RUN_ID = uuid.uuid4().hex[:8]
PASSWORD = "Verify-Concurrency-Password"

results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"{'✅' if ok else '❌'} {label}" + (f"  →  {detail}" if detail else ""))


def start_worker() -> subprocess.Popen[str]:
    """再启动一个 Worker 进程，参与抢任务。"""
    env = dict(os.environ)
    env["PATH"] = f"/Users/mac/Library/Python/3.9/bin:{env.get('PATH', '')}"
    # 用绝对路径找到 uv：一是避免依赖当前 shell 的 PATH 顺序，
    # 二是在显式传递 PATH 给子进程时，写相对名字容易被同目录下的假可执行文件劫持
    uv_path = shutil.which("uv", path=env["PATH"])
    if uv_path is None:
        raise RuntimeError("找不到 uv，请确认已安装并加入 PATH")
    # 命令与参数完全写死在本文件里，没有任何外部输入参与拼接，不会构成注入风险
    return subprocess.Popen(  # noqa: S603
        [uv_path, "run", "python", "-m", "app.worker"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        text=True,
    )


def main() -> int:
    client = httpx.Client(timeout=30.0)

    print("\n=== 0. 前置检查 ===")
    for name, url in [("Web 服务", f"{API}/health"), ("演示接收端", f"{SINK}/health")]:
        try:
            code = client.get(url).status_code
        except httpx.HTTPError as exc:
            check(f"{name}可访问", False, str(exc))
            return 1
        check(f"{name}可访问", code == 200, f"HTTP {code}")

    registered = client.post(
        f"{API}/api/auth/register",
        json={"email": f"verify-concurrency-{RUN_ID}@example.com", "password": PASSWORD},
    )
    if registered.status_code != 201:
        check("注册账号", False, f"HTTP {registered.status_code}: {registered.text}")
        return 1
    headers = {"Authorization": f"Bearer {registered.json()['api_key']}"}

    created = client.post(
        f"{API}/api/endpoints",
        headers=headers,
        json={"name": "并发验收目标", "target_url": f"{SINK}/sink", "max_attempts": 3},
    )
    created.raise_for_status()
    endpoint = created.json()
    check("创建接收地址", True, f"目标={SINK}/sink")

    client.post(f"{SINK}/reset")
    client.post(
        f"{SINK}/control",
        params={"mode": "ok", "secret": endpoint["secret"]},
    )

    # ---------- 灌入事件 ----------
    print(f"\n=== 1. 向队列灌入 {EVENT_COUNT} 条事件 ===")
    event_ids: list[str] = []
    for index in range(EVENT_COUNT):
        payload = {"event_id": f"concurrency-{RUN_ID}-{index}", "seq": index}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        timestamp = str(int(time.time()))
        mac = hmac.new(
            endpoint["secret"].encode(),
            timestamp.encode() + b"." + body,
            hashlib.sha256,
        )
        response = client.post(
            endpoint["ingest_url"],
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-HookRelay-Timestamp": timestamp,
                "X-HookRelay-Signature": f"sha256={mac.hexdigest()}",
            },
        )
        if response.status_code != 202:
            check(f"第 {index} 条事件入队", False, f"HTTP {response.status_code}")
            return 1
        event_ids.append(response.json()["event_id"])

    check(f"{EVENT_COUNT} 条事件全部入队", len(event_ids) == EVENT_COUNT, f"共 {len(event_ids)} 条")

    # ---------- 多 Worker 竞争 ----------
    print(f"\n=== 2. 再启动 {EXTRA_WORKERS} 个 Worker，形成多方竞争 ===")
    workers: list[subprocess.Popen[str]] = []
    try:
        for _ in range(EXTRA_WORKERS):
            workers.append(start_worker())
        check(f"已启动 {EXTRA_WORKERS} 个额外 Worker", True, "加上原有 Worker 共三方竞争")

        # 等所有事件离开队列
        deadline = time.time() + 120
        pending = EVENT_COUNT
        while time.time() < deadline:
            summary = client.get(
                f"{API}/api/events",
                headers=headers,
                params={"limit": 200},
            ).json()
            pending = sum(
                1 for item in summary["items"] if item["status"] in {"pending", "delivering"}
            )
            if pending == 0:
                break
            time.sleep(0.5)

        check("全部事件处理完毕", pending == 0, f"仍有 {pending} 条在队列中")

        final = client.get(f"{API}/api/events", headers=headers, params={"limit": 200}).json()
        items: list[dict[str, Any]] = final["items"]
        succeeded = [item for item in items if item["status"] == "succeeded"]
        check("全部投递成功", len(succeeded) == EVENT_COUNT, f"成功 {len(succeeded)}/{EVENT_COUNT}")

        # ---------- 核心断言：没有一条被投递两次 ----------
        print("\n=== 3. 核心断言：并发下没有重复投递 ===")

        detailed = [
            client.get(f"{API}/api/events/{item['id']}", headers=headers).json() for item in items
        ]

        max_attempts = max(item["attempt_count"] for item in detailed)
        check(
            "每条事件只被投递一次（attempt_count 全为 1）",
            max_attempts == 1,
            f"最大 attempt_count={max_attempts}",
        )

        record_counts = {len(item["attempts"]) for item in detailed}
        check(
            "每条事件只有一条投递记录",
            record_counts == {1},
            f"投递记录条数集合={record_counts}",
        )

        received = client.get(f"{SINK}/received", params={"limit": 200}).json()
        check(
            "接收端收到的请求数与事件数一致",
            received["total"] == EVENT_COUNT,
            f"接收端收到 {received['total']} 条，事件 {EVENT_COUNT} 条",
        )

        delivery_ids = [item["delivery_id"] for item in received["items"]]
        check(
            "投递编号互不重复",
            len(set(delivery_ids)) == len(delivery_ids),
            f"{len(set(delivery_ids))} 个唯一编号 / {len(delivery_ids)} 次投递",
        )

        all_valid = all(item["signature_valid"] for item in received["items"])
        check("所有投递的签名都验得通", all_valid, f"{len(received['items'])} 条逐一验签")
    finally:
        for worker in workers:
            worker.terminate()
        for worker in workers:
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()

    print("\n=== 汇总 ===")
    passed = sum(results)
    print(f"通过 {passed}/{len(results)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
