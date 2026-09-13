"""请求级依赖：从请求中解析出当前用户。

FastAPI 的依赖注入让鉴权只需写一次：任何需要登录的接口，
只要在参数里声明 `user: CurrentUser`，框架就会自动完成
"读请求头 → 查库比对 → 校验状态" 这一整套流程。

这是依赖注入相对"每个函数开头复制一段校验代码"的实际价值：
鉴权逻辑有且只有一处实现，改规则时不可能漏掉某个接口。
"""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from app.db import SessionDep
from app.models import User
from app.security import hash_api_key

# auto_error=False：缺少 Authorization 头时不要抛 FastAPI 默认的 403，
# 而是交给我们自己处理，返回语义更准确的 401（未认证 vs 无权限是两回事）
_bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    """构造统一的 401 响应。

    WWW-Authenticate 头是 HTTP 规范要求的，客户端据此得知该用哪种认证方式。
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> User:
    """从 `Authorization: Bearer <api_key>` 解析出当前用户。

    实现要点：数据库里只存了 Key 的哈希，所以这里把收到的 Key 也哈希一遍再比对。
    哈希结果长度固定，可以直接命中 users.api_key_hash 上的唯一索引，
    查一次就能定位到用户，不需要遍历。
    """
    if credentials is None:
        raise _unauthorized("缺少 Authorization 请求头")

    api_key_hash = hash_api_key(credentials.credentials)

    user = (
        await session.execute(select(User).where(User.api_key_hash == api_key_hash))
    ).scalar_one_or_none()

    # 刻意不区分"Key 不存在"与"Key 对应账号被停用"，
    # 统一返回相同文案，避免给攻击者提供可用来探测的信息
    if user is None:
        raise _unauthorized("API Key 无效")

    if not user.is_active:
        raise _unauthorized("API Key 无效")

    return user


# 路由里写 `user: CurrentUser` 即可
CurrentUser = Annotated[User, Depends(get_current_user)]
