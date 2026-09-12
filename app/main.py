"""HookRelay 应用入口。

本模块负责组装 FastAPI 应用：配置中间件、异常处理器与路由。
Day 1 只包含健康检查，业务路由会在后续按天挂载：

- Day 2  认证与接收地址管理
- Day 3  事件接收入口
- Day 4  事件查询与死信重放
- Day 5  指标端点与就绪检查
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings

settings = get_settings()

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("hookrelay")

# OpenAPI 文档页的分组定义，让接口在 /docs 中按业务模块归类
TAGS_METADATA = [
    {
        "name": "系统",
        "description": "健康检查与运行状态。给部署平台和负载均衡调用的探针。",
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期钩子：启动时初始化资源，关闭时释放资源。

    后续会在这里挂载数据库连接池（Day 2）与投递 Worker 的启停（Day 4）。
    """
    logger.info("HookRelay 启动 | environment=%s", settings.env)
    yield
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
    涉及外部依赖的检查放在 /health/ready（Day 5 实现）。
    """
    return {
        "status": "ok",
        "service": "HookRelay",
        "version": app.version,
        "environment": settings.env,
    }
