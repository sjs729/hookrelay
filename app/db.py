"""数据库连接层：异步引擎、会话工厂与请求级依赖。

三个关键取舍，都是面试可展开的点：

1. 为什么用异步驱动（asyncpg）而不是同步驱动？
   Webhook 服务的负载特征不是 CPU 密集，而是"大量等待"——等下游 HTTP 响应、
   等数据库返回结果。同步模式下，一个请求在等待时会占住整个线程；异步模式下，
   等待期间事件循环可以转去处理其他请求。同样的机器配置，可支撑的并发差数倍。

2. 连接池为什么必要？
   建立一条数据库连接要做 TCP 握手和认证，属于毫秒级的昂贵操作。
   如果每个请求都新建连接，这部分开销会直接吃掉吞吐量。连接池让连接被反复复用。

3. pool_pre_ping 为什么开着？
   数据库重启，或者中间的网络设备静默掐断闲置连接后，池子里会残留"已经死掉的连接"。
   取用前先做一次极轻量的探活，代价很小，换来的是不把报错抛到用户面前。
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

settings = get_settings()

# 连接池参数。数值按"单实例部署 + 免费平台规格"的规模设定：
# 常驻 10 条 + 峰值最多再借 20 条，对 32GB 内存的开发机和生产免费层都绰绰有余。
POOL_SIZE = 10
MAX_OVERFLOW = 20
POOL_TIMEOUT_SECONDS = 30
POOL_RECYCLE_SECONDS = 1800


def create_engine(url: str) -> AsyncEngine:
    """按给定数据库地址创建异步引擎。

    抽成函数是为了让测试能对独立的测试库建自己的引擎，而不复用全局引擎。
    全局引擎在模块导入时就绑定到配置里的开发库，测试若直接复用它，
    就会把数据写进开发库。
    """
    return create_async_engine(
        url,
        echo=False,  # 设为 True 会把每条 SQL 打到日志，调试时才开
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT_SECONDS,
        pool_recycle=POOL_RECYCLE_SECONDS,
        pool_pre_ping=True,
    )


# 全局引擎：应用运行时使用的连接池
engine: AsyncEngine = create_engine(settings.database_url)

SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    # commit 之后对象属性仍然可读，否则 FastAPI 序列化响应时会触发额外查询
    expire_on_commit=False,
    # 关闭自动 flush：避免查询操作意外触发未预期的 INSERT/UPDATE
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：为每个请求提供独立会话，请求结束后归还连接。

    事务边界交给业务代码控制（谁改数据谁 commit）。这里只保证三件事：

    - 每个请求拿到独立会话，请求之间不会串数据
    - 出异常时自动回滚，避免把失败的半截事务留在连接上污染后续请求
    - 无论成功还是失败都关闭会话，把连接还给连接池
    """
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# 路由里写 `session: SessionDep` 即可，比反复写 Annotated[AsyncSession, Depends(...)] 清爽
SessionDep = Annotated[AsyncSession, Depends(get_session)]
