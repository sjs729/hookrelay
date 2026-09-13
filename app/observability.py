"""结构化日志与请求追踪。

这里解决两个运维上的实际问题：

1. **一次请求的日志要能串起来**。接收、入队、投递、重试发生在不同协程、
   不同进程里，靠 request_id 把它们关联成一条链路。

2. **日志要能被程序读**。自由文本日志上线后只能靠正则去捞，
   结构化日志可以直接按字段过滤和聚合。

request_id 用 `contextvars` 保存，而不是塞进全局变量或函数参数：
`contextvars` 是按协程隔离的，多个并发请求各自持有自己的值，不会串号。
"""

import json
import logging
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from string import ascii_letters, digits
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# 入站请求头：调用方可以自带，方便和自己的日志对齐
REQUEST_ID_HEADER_IN = b"x-request-id"
# 出站响应头：把本次分配的 request_id 回给调用方，出问题时报这个值就能定位
REQUEST_ID_HEADER_OUT = "X-Request-Id"

# request_id 长度上限。头部是外部输入，不限制长度等于允许别人
# 往日志里塞任意大小的字符串
MAX_REQUEST_ID_LENGTH = 64
# 只接受这些字符，防止构造出跨行、含控制字符的值污染日志
REQUEST_ID_SAFE_CHARS = frozenset(ascii_letters + digits + "-_.")

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

access_logger = logging.getLogger("hookrelay.access")
logger = logging.getLogger("hookrelay")


def get_request_id() -> str:
    """读取当前上下文的 request_id，没有则为 "-"。"""
    return request_id_var.get()


def ctx(**fields: Any) -> dict[str, Any]:
    """把业务字段打包成 logging 的 extra 参数。

    用法：`logger.info("事件已入队", extra=ctx(event_id=event_id))`

    把这些字段放独立键下，是为了不和 LogRecord 自带的属性
    （message、levelname、pathname 等）撞名。
    """
    return {"ctx": fields}


class JsonLogFormatter(logging.Formatter):
    """把日志渲染成单行 JSON。

    单行是刻意的：多行 JSON 在日志采集系统里会被拆成多条记录，
    堆栈信息也就散了。
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }

        context = getattr(record, "ctx", None)
        if isinstance(context, dict):
            payload.update(context)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


class TextLogFormatter(logging.Formatter):
    """本地开发用的可读格式：固定宽度对齐，一眼能扫完。

    把 extra 字段追加在末尾，避免为了看清一个字段而切换成 JSON 模式。
    """

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{datetime.fromtimestamp(record.created, tz=UTC).astimezone():%H:%M:%S} "
            f"{record.levelname:<8} {record.name} | {record.getMessage()}"
        )
        context = getattr(record, "ctx", None)
        if isinstance(context, dict) and context:
            rendered = " ".join(f"{key}={value}" for key, value in context.items())
            base = f"{base} | {rendered}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def setup_logging(level: str = "INFO", *, json_format: bool = False) -> None:
    """配置根日志。

    json_format 为真时输出结构化日志，适合部署平台采集；
    否则输出便于人读的文本，适合本地开发。
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter() if json_format else TextLogFormatter())

    root = logging.getLogger()
    # 清掉已有 handler，避免重复初始化时同一条日志被打印多次
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # 访问日志由 RequestContextMiddleware 统一记录（信息更全：含耗时与 request_id），
    # 这里关掉 uvicorn 自带的，否则一次请求会出现两行
    logging.getLogger("uvicorn.access").disabled = True


def _incoming_request_id(scope: Scope) -> str | None:
    """从请求头里提取调用方自带的 request_id。

    头部是外部输入，必须校验：长度受限、字符受白名单约束。
    不合法就当作没提供，由服务端重新生成，而不是拒绝请求——
    追踪标识不该成为调用方的负担。
    """
    for name, value in scope.get("headers", []):
        if name != REQUEST_ID_HEADER_IN:
            continue
        candidate = value.decode("latin-1").strip()
        if not candidate or len(candidate) > MAX_REQUEST_ID_LENGTH:
            return None
        if set(candidate) <= REQUEST_ID_SAFE_CHARS:
            return candidate
        return None
    return None


class RequestContextMiddleware:
    """纯 ASGI 中间件：分配 request_id、回写响应头、记录访问日志。

    这里不用 FastAPI 常见的 `@app.middleware("http")`（内部是
    BaseHTTPMiddleware）。后者会把响应包一层，在流式响应场景下
    可能造成缓冲和连接提前关闭；纯 ASGI 中间件只是转发消息，不改变语义。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # 只处理 HTTP 请求；lifespan 等其他类型的消息原样放行
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        # 默认按 500 记录：如果请求处理过程中抛异常，
        # 下面记录日志时取到的就是 500，而不是悄悄丢掉这条访问日志
        captured_status = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal captured_status
            if message["type"] == "http.response.start":
                captured_status = message["status"]
                # MutableHeaders 直接改的是 message 里的 headers 列表
                MutableHeaders(scope=message)[REQUEST_ID_HEADER_OUT] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            # 放在 finally 里：无论请求正常结束还是抛异常，都要留下访问日志。
            # 异常本身由 app 的兜底处理器记，这里只负责"谁访问了什么、结果如何"
            access_logger.info(
                "%s %s",
                scope.get("method", "-"),
                scope.get("path", "-"),
                extra=ctx(
                    method=scope.get("method", "-"),
                    path=scope.get("path", "-"),
                    status_code=captured_status,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    client=_client_host(scope),
                ),
            )
            # 复位上下文变量。虽然每个请求都会 set 一次，
            # 但复用连接处理下一个请求的是同一个任务上下文，不 reset 会残留旧值
            request_id_var.reset(token)


def _client_host(scope: Scope) -> str:
    """取客户端地址。

    优先用 X-Forwarded-For 的第一段，因为在 Render、Koyeb 这类平台上，
    连接来源是平台的代理，socket 地址是代理的地址而非真实调用方。
    注意这个头可以被伪造，只适合用于日志排查，不能用于鉴权。
    """
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-for":
            forwarded = value.decode("latin-1").split(",")[0].strip()
            if forwarded:
                return forwarded
            break
    client = scope.get("client")
    return client[0] if client else "-"
