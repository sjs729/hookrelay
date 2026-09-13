"""HookRelay 应用入口。

本模块负责组装 FastAPI 应用：配置中间件、异常处理器与路由。
Day 1 只包含健康检查，业务路由会在后续按天挂载：

- Day 2  认证与用户账号
- Day 3  接收地址管理与事件接收入口
- Day 4  事件查询与死信重放
- Day 5  指标端点、就绪检查与请求追踪
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from starlette.responses import Response

from app.api.auth import router as auth_router
from app.api.endpoints import router as endpoints_router
from app.api.events import router as events_router
from app.api.ingest import router as ingest_router
from app.api.stats import router as stats_router
from app.config import get_settings
from app.db import SessionDep, engine
from app.metrics import refresh_queue_gauges
from app.observability import RequestContextMiddleware, setup_logging

settings = get_settings()

setup_logging(settings.log_level, json_format=settings.log_json)
logger = logging.getLogger("hookrelay")

# OpenAPI 文档页的分组定义，让接口在 /docs 中按业务模块归类
TAGS_METADATA = [
    {
        "name": "系统",
        "description": "健康检查与运行状态。给部署平台和负载均衡调用的探针。",
    },
    {
        "name": "认证",
        "description": (
            "账号注册与身份验证。除注册接口外，本服务所有接口都需要在请求头中"
            "携带 `Authorization: Bearer <API Key>`。"
        ),
    },
    {
        "name": "接收地址",
        "description": (
            "管理接收地址（endpoint）。一个接收地址 = 一个入站 token + 一个转发目标。"
            "创建时会下发签名密钥，**明文只返回一次**。"
        ),
    },
    {
        "name": "事件接收",
        "description": (
            "公网入站入口。第三方服务把 Webhook 打到这里，需要携带 HMAC 签名与时间戳。"
            "该接口不使用 API Key 鉴权——身份已由 URL 中的 token 与请求签名共同证明。"
        ),
    },
    {
        "name": "事件查询",
        "description": (
            "查看事件及每一次投递的结果，并把进入死信的事件重新放回队列。"
            "死信不会自动恢复——这是有意为之：反复重试一个已经确认不可用的目标"
            "只会浪费资源，是否需要恢复取决于下游何时修好。"
        ),
    },
    {
        "name": "统计",
        "description": (
            "账号维度的投递情况汇总。与 `/metrics` 的区别：那个是给监控系统"
            "抓取的时间序列，这个是给你看的单次快照。"
        ),
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期钩子：启动时初始化资源，关闭时释放资源。

    这里刻意不做任何数据库连接操作：数据库不可用时服务仍应该能启动，
    这样部署平台上的进程不会因为数据库临时不可达而被反复重启。
    真正的就绪检查放在 /health/ready。

    投递 Worker 是独立进程（`uv run python -m app.worker`），不在这里启动：
    Web 服务与 Worker 的重启是两回事，绑定在一起会导致发布 Web 新版本时
    把正在进行的投递任务一起打断。
    """
    logger.info("HookRelay 启动 | environment=%s", settings.env)
    yield
    # 关闭连接池，让数据库端看到连接正常断开，
    # 而不是积压一堆处于半开状态的连接
    await engine.dispose()
    logger.info("HookRelay 已关闭")


app = FastAPI(
    title="HookRelay",
    description=(
        "Webhook 中继与可靠投递服务。\n\n"
        "接收第三方 Webhook 事件后立即返回，再由后台 Worker "
        "可靠、幂等、可观测地投递到目标地址。"
    ),
    version="0.1.0",
    lifespan=lifespan,
    openapi_tags=TAGS_METADATA,
)

# 开发环境放开跨域便于调试；生产环境默认不开放，需要时按白名单配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=[] if settings.is_production else ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 请求追踪放在最外层：连被 CORS 直接短路的预检请求也会留下访问日志。
# add_middleware 是后加的在外层，所以这一句必须放在 CORS 之后
app.add_middleware(RequestContextMiddleware)

# 挂载业务路由。后续每天的模块在这里逐个加入
app.include_router(auth_router)
app.include_router(endpoints_router)
app.include_router(ingest_router)
app.include_router(events_router)
app.include_router(stats_router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常处理：避免把内部堆栈直接暴露给调用方。

    HTTPException 有 FastAPI 内置的处理器，不会走到这里。
    """
    logger.exception("未处理异常 | path=%s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误"},
    )


@app.get("/", tags=["系统"], summary="服务信息")
async def root() -> dict[str, str]:
    """返回服务基本信息，方便快速确认服务是否部署成功。"""
    return {
        "service": "HookRelay",
        "version": app.version,
        "environment": settings.env,
        "docs": "/docs",
    }


@app.get("/health", tags=["系统"], summary="存活检查")
async def health() -> dict[str, str]:
    """返回服务存活状态。

    这是给部署平台和负载均衡用的探针，必须保持轻量，因此不依赖数据库。
    两者区别很关键：存活检查失败，平台会杀掉并重建进程；
    如果它依赖数据库，数据库抖一下就会导致全部实例被重启——
    本已脆弱的服务会被自己的探针踩得更碎。
    涉及外部依赖的检查放在 /health/ready。
    """
    return {
        "status": "ok",
        "service": "HookRelay",
        "version": app.version,
        "environment": settings.env,
    }


@app.get(
    "/health/ready",
    tags=["系统"],
    summary="就绪检查",
    responses={503: {"description": "依赖尚未就绪，暂时不要转发流量"}},
)
async def readiness(session: SessionDep) -> Response:
    """就绪检查：确认数据库连通。

    与 /health 的分工：这里失败表示"现在还不能服务请求"，
    负载均衡应该停止转发流量，但**不该重启进程**——
    数据库不通是外部原因，重启本服务解决不了。

    只跑 `SELECT 1`：验证的是连接真的能用（包括连接池里那些
    可能已经失效的连接），而不是只确认配置存在。
    """
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        logger.warning("就绪检查失败：数据库不可达")
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "database": "down"},
        )
    return JSONResponse(content={"status": "ready", "database": "up"})


@app.get("/metrics", tags=["系统"], summary="Prometheus 指标", include_in_schema=False)
async def metrics(session: SessionDep) -> Response:
    """暴露 Prometheus 格式的指标。

    不进 OpenAPI 文档：这是给监控系统抓取的机器端点，
    不是给调用方用的业务接口。

    每次抓取前刷新队列类指标，所以拿到的积压量是拉取那一刻的真实值，
    而不是某个中间状态累加出来的估计值。
    """
    await refresh_queue_gauges(session)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
