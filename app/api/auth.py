"""认证接口：注册账号与查看当前账号。

当前只有一个"注册即签发 API Key"的流程，没有引入登录换取 token 的环节。
理由：这是面向开发者的服务型 API，调用方是程序，长期凭据（API Key）
比短期 token 更适合——不需要实现刷新逻辑，也不会因为 token 过期导致
用户的集成半夜挂掉。

代价要说明：API Key 一旦泄露，在主动撤销前一直有效。所以后续需要补上
"重置 API Key"接口（Day 3 做 endpoint 密钥管理时一并提供）。
"""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser
from app.db import SessionDep
from app.models import User
from app.schemas import RegisterRequest, RegisterResponse, UserResponse
from app.security import generate_api_key, hash_password

router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="注册账号并获取 API Key",
    description=(
        "注册成功后返回 API Key 明文，**仅此一次**。服务端只保存哈希，丢失后无法找回，只能重置。"
    ),
)
async def register(payload: RegisterRequest, session: SessionDep) -> RegisterResponse:
    """创建账号，签发一把新的 API Key。"""
    # 明文 key 只在这里存在，函数返回后就再也拿不到了
    api_key, api_key_hash, api_key_prefix = generate_api_key()

    user = User(
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        api_key_hash=api_key_hash,
        api_key_prefix=api_key_prefix,
    )
    session.add(user)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # 这里靠数据库唯一约束兜底，而不是"先查询邮箱是否存在、不存在再插入"。
        # 后者在并发下有两个请求同时通过检查、然后双双写入的竞态，
        # 唯一约束由存储引擎保证原子性，是唯一可靠的判据。
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该邮箱已被注册",
        ) from exc

    # 刷新一次，把数据库生成的 created_at 取回本地对象。
    # 不做这一步的话，后面读 user.created_at 会触发异步环境下的懒加载而报错。
    await session.refresh(user)

    return RegisterResponse(
        id=user.id,
        email=user.email,
        api_key=api_key,
        api_key_prefix=user.api_key_prefix,
        created_at=user.created_at,
    )


@router.get(
    "/me",
    response_model=UserResponse,
    summary="查看当前账号信息",
    description="用 API Key 访问，可用来确认鉴权是否正常工作。",
)
async def read_current_user(current_user: CurrentUser) -> User:
    """返回当前 API Key 对应的账号。"""
    return current_user
