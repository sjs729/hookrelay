"""Alembic 迁移环境配置（异步版）。

相对 `alembic init` 生成的默认模板，改了三点：

1. 数据库地址从应用配置读取，不再写死在 alembic.ini 里。
   否则很容易出现"应用连的是 A 库、迁移改的是 B 库"这种排查起来很痛苦的问题。

2. 用异步引擎执行迁移。应用本身跑在异步栈上（asyncpg），如果迁移改走同步驱动，
   就得额外装 psycopg2——多一个依赖，也多一个配置出错的地方。

3. target_metadata 指向应用的模型 metadata。这是 autogenerate 的依据：
   Alembic 对比"模型描述的结构"和"数据库实际的结构"，把差异生成成迁移脚本。

注意 compare_type 与 compare_server_default 都打开了：默认情况下 Alembic 不比较
字段类型和默认值的差异，改字段类型时会静默生成空迁移，排查起来很费时间。
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.models import Base

config = context.config
settings = get_settings()

# 把应用配置里的数据库地址回填进 Alembic 配置，
# 这样 alembic.ini 不需要保存任何连接信息（也就不会有人往里面塞密码）
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL 文本，不连接数据库。

    用于把迁移脚本交给 DBA 审核，或在不能直连生产库的环境里生成 DDL。
    """
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在给定连接上执行迁移（同步上下文，由 run_sync 驱动）。"""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """用异步引擎连接数据库并执行迁移。

    poolclass=NullPool：迁移是一次性动作，跑完就退出，
    没必要维护连接池。
    """
    connectable = create_async_engine(
        settings.database_url,
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
