"""测试公共装置。

有三处需要特别说明：

1. **环境变量必须在导入 app 之前设置**。`app.db` 在模块导入时就根据配置
   创建好了数据库引擎，之后才改环境变量已经晚了，测试会打到开发库上。
   所以下面的赋值出现在文件最顶部，早于任何 app 相关导入。

2. **测试库名必须显式以 `_test` 结尾**，否则直接报错退出。清表是不可逆操作，
   一个写错的环境变量就足以删掉开发数据，值得加这道闸。

3. **表结构由模型直接创建**，不走 alembic 迁移。测试要回答的是"代码行为
   是否符合预期"，迁移是否可用属于另一个问题，混在一起会让失败原因变模糊。
"""

import os

TEST_DATABASE_URL = os.environ.get(
    "HOOKRELAY_TEST_DATABASE_URL",
    "postgresql+asyncpg://localhost:5432/hookrelay_test",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
# Fernet 密钥从 SECRET_KEY 派生，测试里给个固定值即可，不涉及真实密钥管理
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-for-production")


def _assert_is_test_database(url: str) -> None:
    """拒绝在非测试库上运行。"""
    database = url.rsplit("/", 1)[-1].split("?")[0]
    if not database.endswith("_test"):
        raise RuntimeError(
            f"测试库名必须以 _test 结尾，当前是 {database!r}。"
            "请检查 TEST_DATABASE_URL / HOOKRELAY_TEST_DATABASE_URL 配置。"
        )


_assert_is_test_database(TEST_DATABASE_URL)

import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402

import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402

TEST_PASSWORD = "Test-Password-123"
DEFAULT_TARGET_URL = "http://127.0.0.1:9000/sink"


# ---------- 数据库 ----------


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _database_schema() -> AsyncIterator[None]:
    """建出全部表，并在整个测试会话结束后释放连接池。"""
    async with engine.begin() as conn:
        # 先删后建：反复跑测试时不会因为残留的旧表结构而出错
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables() -> AsyncIterator[None]:
    """每个测试结束后清空数据。

    放在测试之后清理（而不是之前），是为了失败时能直接看到留下的数据，
    方便连上数据库排查。CASCADE 保证按外键顺序一并清干净，
    不需要自己排删除顺序。
    """
    yield
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE delivery_attempts, events, endpoints, users RESTART IDENTITY CASCADE")
        )


# ---------- HTTP 客户端 ----------


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """直接调用 ASGI 应用的客户端，不需要真的监听端口。

    比起一个真实服务再发请求快得多，失败时堆栈也直接指向视图函数，
    不用在进程之间对日志。
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


# ---------- 账号与资源 ----------


@pytest_asyncio.fixture
async def account(client: AsyncClient) -> dict[str, str]:
    """注册一个测试账号，返回邮箱、密码与 API Key。"""
    email = f"user-{uuid.uuid4().hex[:10]}@example.com"
    response = await client.post(
        "/api/auth/register",
        json={"email": email, "password": TEST_PASSWORD},
    )
    assert response.status_code == 201, response.text
    return {
        "email": email,
        "password": TEST_PASSWORD,
        "api_key": response.json()["api_key"],
    }


@pytest_asyncio.fixture
async def auth_headers(account: dict[str, str]) -> dict[str, str]:
    """当前账号的鉴权请求头。"""
    return {"Authorization": f"Bearer {account['api_key']}"}


@pytest_asyncio.fixture
async def endpoint(client: AsyncClient, auth_headers: dict[str, str]) -> dict[str, str]:
    """创建一个接收地址，返回含明文 secret 与入站 URL 的完整信息。"""
    response = await client.post(
        "/api/endpoints",
        headers=auth_headers,
        json={"name": "测试接收地址", "target_url": DEFAULT_TARGET_URL},
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------- 辅助函数 ----------


def sign_headers(
    secret: str,
    body: bytes,
    *,
    timestamp: str | None = None,
    signature: str | None = None,
) -> dict[str, str]:
    """按协议生成入站请求头。

    签名对象是 `时间戳 + '.' + 原始请求体`，与 app.security.compute_signature
    保持一致。这里独立实现一遍而不是调用项目代码：测试如果复用被测代码的
    签名函数，签名算法本身写错了也测不出来。
    """
    ts = timestamp if timestamp is not None else str(int(time.time()))
    if signature is None:
        digest = hmac.new(
            secret.encode("utf-8"),
            ts.encode("utf-8") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        signature = f"sha256={digest}"
    return {
        "Content-Type": "application/json",
        "X-HookRelay-Timestamp": ts,
        "X-HookRelay-Signature": signature,
    }


def json_body(payload: dict[str, object]) -> bytes:
    """把字典序列化成请求体字节。

    这里固定 separators 而不是用默认值：签名校验基于**原始字节**，
    测试必须能精确控制发出去的每一个字符，否则验证不到真实行为。
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
