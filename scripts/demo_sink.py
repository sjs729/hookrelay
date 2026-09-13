"""演示接收端：一个"可开关故障"的 Webhook 目标。

用途有两个：

1. 端到端验证 —— 看清 HookRelay 投递过来的请求到底长什么样，
   以及签名能不能验通（用同一个密钥独立算一遍）
2. 故障注入 —— 把接收端切成 500 或超时，观察 HookRelay 的重试行为，
   包括退避间隔是否真的在拉长、超过上限后是否进入死信

运行：

    uv run python scripts/demo_sink.py --secret <接收地址的签名密钥>

默认监听 http://127.0.0.1:9000，可用 `--port` 改。

调试期间随时可以切换行为：

    curl -X POST "http://127.0.0.1:9000/control?mode=fail"
    curl "http://127.0.0.1:9000/received"

这是一个演示/调试工具，不是产品的一部分：收到的数据只放在内存里，
进程重启就清空，也没有做任何鉴权。
"""

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

# 直接以脚本方式运行（python scripts/demo_sink.py）时，sys.path[0] 是 scripts/
# 而不是项目根目录，import app.* 会失败。把项目根显式加进来，两种运行方式都能用。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.security import SIGNATURE_HEADER, TIMESTAMP_HEADER, compute_signature

logger = logging.getLogger("hookrelay.sink")

# 接收端的行为模式。
# timeout 模式的休眠时间刻意大于默认投递超时（10 秒），
# 这样才能稳定触发客户端的读取超时。
MODE_OK = "ok"
MODE_FAIL = "fail"
MODE_SLOW = "slow"
MODE_TIMEOUT = "timeout"

SLOW_SECONDS = 2.0
TIMEOUT_SECONDS = 12.0

# 全局可变状态。演示工具不做并发保护：单进程、单用户、跑完就关。
STATE: dict[str, Any] = {
    "mode": MODE_OK,
    "secret": None,
    "fail_status": 500,
    "received": [],
}


def create_app() -> FastAPI:
    app = FastAPI(title="HookRelay 演示接收端", version="0.1.0")

    @app.post("/sink", summary="接收 HookRelay 投递的事件")
    async def sink(request: Request) -> Response:
        body = await request.body()
        timestamp = request.headers.get(TIMESTAMP_HEADER, "")
        signature = request.headers.get(SIGNATURE_HEADER, "")

        # 独立验签：用同一个密钥按同样的规则再算一遍。
        # 这不是为了"相信"这个请求，而是为了证明 HookRelay 确实
        # 用约定的密钥签了名——接收方照抄这段逻辑就能接入。
        signature_valid: bool | None = None
        if STATE["secret"]:
            expected = compute_signature(
                secret=STATE["secret"],
                timestamp=timestamp,
                body=body,
            )
            # 普通字符串比较在这里够用：这是本地演示，
            # 而且比较失败也不会泄露密钥信息（签名本身已经是公开值）
            signature_valid = expected == signature

        record = {
            "received_at": datetime.now(UTC).isoformat(),
            "signature_valid": signature_valid,
            "delivery_id": request.headers.get("X-HookRelay-Delivery-Id"),
            "attempt": request.headers.get("X-HookRelay-Attempt"),
            "timestamp": timestamp,
            "body": body.decode("utf-8", errors="replace"),
        }
        STATE["received"].append(record)

        mode = STATE["mode"]
        logger.info(
            "收到投递 | mode=%s attempt=%s 签名有效=%s 体积=%s字节",
            mode,
            record["attempt"],
            signature_valid,
            len(body),
        )

        if mode == MODE_TIMEOUT:
            # 故意不回响应，让 HookRelay 的读取超时生效
            await asyncio.sleep(TIMEOUT_SECONDS)
            return Response(status_code=200, content="too late")

        if mode == MODE_SLOW:
            await asyncio.sleep(SLOW_SECONDS)
            return Response(status_code=200, content="slow but ok")

        if mode == MODE_FAIL:
            status_code = STATE["fail_status"]
            return JSONResponse(
                status_code=status_code,
                content={"error": f"演示接收端被切换到失败模式（{status_code}）"},
            )

        return Response(status_code=200, content="ok")

    @app.post("/control", summary="切换接收端行为")
    async def control(
        mode: str = Query(description="ok / fail / slow / timeout"),
        fail_status: int = Query(default=500, ge=400, le=599, description="fail 模式返回的状态码"),
        secret: str | None = Query(
            default=None,
            description="运行时设置签名密钥，用于独立验签；验收脚本拿到密钥后会传进来",
        ),
    ) -> dict[str, Any]:
        """切换行为模式。

        切换成 fail 时还可以指定状态码，用来区分两类失败：
        - 503 / 502：典型的下游暂时不可用，应该重试
        - 400 / 404：请求本身有问题，重试多少次结果都一样
        """
        if mode not in {MODE_OK, MODE_FAIL, MODE_SLOW, MODE_TIMEOUT}:
            return JSONResponse(
                status_code=422,
                content={"detail": f"未知模式 {mode}"},
            )
        STATE["mode"] = mode
        STATE["fail_status"] = fail_status
        if secret is not None:
            STATE["secret"] = secret
        logger.info("接收端模式切换为 %s（fail_status=%s）", mode, fail_status)
        return {
            "mode": mode,
            "fail_status": fail_status,
            "verifying_signature": STATE["secret"] is not None,
        }

    @app.get("/received", summary="查看已收到的事件")
    async def received(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
        """返回最近收到的事件，用于确认投递是否真的到达、签名是否验通。"""
        items = STATE["received"][-limit:]
        return {
            "total": len(STATE["received"]),
            "mode": STATE["mode"],
            "items": items,
        }

    @app.post("/reset", summary="清空记录")
    async def reset() -> dict[str, int]:
        """清空已接收记录，方便开始一轮干净的验证。"""
        count = len(STATE["received"])
        STATE["received"].clear()
        return {"cleared": count}

    @app.get("/health", summary="存活检查")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": STATE["mode"]}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="HookRelay 演示接收端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument(
        "--secret",
        default=None,
        help="接收地址的签名密钥，用于独立验签；不传则跳过验签",
    )
    parser.add_argument(
        "--mode",
        default=MODE_OK,
        choices=[MODE_OK, MODE_FAIL, MODE_SLOW, MODE_TIMEOUT],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    STATE["mode"] = args.mode
    STATE["secret"] = args.secret
    if not args.secret:
        logger.warning("未提供 --secret，将跳过签名校验（只记录请求）")

    logger.info("演示接收端启动 | http://%s:%s/sink | 模式=%s", args.host, args.port, args.mode)
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
