"""接收地址管理接口。

一个安全细节贯穿本模块：**所有查询都以当前用户为边界**。

每个操作都会在 WHERE 条件里带上 user_id，而不是"先按 ID 查出来、再判断属主"。
两种写法结果看似一样，差别在于：

- 先查再判断：代码里必须记得写这个判断，漏掉任何一处就是一个越权漏洞
- 直接带上 user_id 过滤：别人的记录根本查不出来，不存在"忘记判断"的可能

另外，请求别人的 ID 时返回 404 而不是 403。
403 等于告诉对方"这个 ID 确实存在，只是不属于你"，
这就成了一个可以用来枚举他人资源的信号。
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select

from app.api.deps import CurrentUser
from app.db import SessionDep
from app.models import Endpoint
from app.schemas import (
    EndpointCreatedResponse,
    EndpointCreateRequest,
    EndpointResponse,
    EndpointSecretResponse,
    EndpointUpdateRequest,
)
from app.security import (
    decrypt_secret,
    encrypt_secret,
    generate_signing_secret,
    generate_url_token,
    mask_secret,
)

router = APIRouter(prefix="/api/endpoints", tags=["接收地址"])


async def _get_owned_endpoint(
    session: SessionDep,
    user_id: UUID,
    endpoint_id: UUID,
) -> Endpoint:
    """取出属于指定用户的接收地址，不存在则 404。

    把 user_id 直接写进 WHERE 条件，而不是查出来后再比较：
    越权检查变成"查询条件的一部分"，不存在漏写的可能。
    """
    endpoint = (
        await session.execute(
            select(Endpoint).where(
                Endpoint.id == endpoint_id,
                Endpoint.user_id == user_id,
            )
        )
    ).scalar_one_or_none()

    if endpoint is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="接收地址不存在",
        )
    return endpoint


def _mask_of(endpoint: Endpoint) -> str:
    """解密后取掩码。

    单独包一层是因为解密可能失败——最常见的原因是 SECRET_KEY 被换过，
    旧密文用新密钥解不开。这种时候只让这一个字段显示异常，
    不应该把整个列表接口搞崩。
    """
    try:
        return mask_secret(decrypt_secret(endpoint.secret_encrypted))
    except ValueError:
        return "********"


def _to_response(endpoint: Endpoint) -> EndpointResponse:
    """把 ORM 对象转成响应模型，签名密钥只输出掩码。"""
    return EndpointResponse(
        id=endpoint.id,
        name=endpoint.name,
        token=endpoint.token,
        target_url=endpoint.target_url,
        secret_masked=_mask_of(endpoint),
        max_attempts=endpoint.max_attempts,
        timeout_seconds=endpoint.timeout_seconds,
        is_active=endpoint.is_active,
        created_at=endpoint.created_at,
        updated_at=endpoint.updated_at,
    )


def _build_ingest_url(request: Request, token: str) -> str:
    """拼出完整的入站接收地址。

    用发起请求的域名来拼，而不是写死在配置里：
    本地开发是 127.0.0.1、部署后是平台分配的域名，用户访问哪个域名，
    拼出来就是哪个，不需要维护一份 BASE_URL 配置（也就不存在配错的问题）。
    """
    return f"{str(request.base_url).rstrip('/')}/ingest/{token}"


@router.post(
    "",
    response_model=EndpointCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建接收地址",
    description=(
        "创建后会返回签名密钥明文与完整接收地址，**两者都只返回这一次**。"
        "密钥用于给发往本服务的事件计算 HMAC 签名，丢失后只能重置。"
    ),
)
async def create_endpoint(
    payload: EndpointCreateRequest,
    request: Request,
    session: SessionDep,
    current_user: CurrentUser,
) -> EndpointCreatedResponse:
    """创建一个接收地址，自动生成入站 token 与 HMAC 签名密钥。"""
    # 密钥以明文生成、加密入库，明文只在本次响应里出现一次
    secret = generate_signing_secret()

    endpoint = Endpoint(
        user_id=current_user.id,
        name=payload.name,
        token=generate_url_token(),
        target_url=str(payload.target_url),
        secret_encrypted=encrypt_secret(secret),
        max_attempts=payload.max_attempts,
        timeout_seconds=payload.timeout_seconds,
    )
    session.add(endpoint)
    await session.commit()
    # 取回数据库生成的 created_at / updated_at
    await session.refresh(endpoint)

    return EndpointCreatedResponse(
        **_to_response(endpoint).model_dump(),
        secret=secret,
        ingest_url=_build_ingest_url(request, endpoint.token),
    )


@router.get(
    "",
    response_model=list[EndpointResponse],
    summary="列出我的接收地址",
)
async def list_endpoints(
    session: SessionDep,
    current_user: CurrentUser,
) -> list[EndpointResponse]:
    """列出当前账号下所有接收地址。"""
    rows = (
        (
            await session.execute(
                select(Endpoint)
                .where(Endpoint.user_id == current_user.id)
                .order_by(Endpoint.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_to_response(endpoint) for endpoint in rows]


@router.get(
    "/{endpoint_id}",
    response_model=EndpointResponse,
    summary="查看接收地址详情",
)
async def get_endpoint(
    endpoint_id: UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> EndpointResponse:
    """按 ID 查看详情。只能查看自己的，他人的 ID 会返回 404。"""
    endpoint = await _get_owned_endpoint(session, current_user.id, endpoint_id)
    return _to_response(endpoint)


@router.patch(
    "/{endpoint_id}",
    response_model=EndpointResponse,
    summary="修改接收地址",
)
async def update_endpoint(
    endpoint_id: UUID,
    payload: EndpointUpdateRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> EndpointResponse:
    """局部更新。只更新明确传入的字段，未传的字段保持原值。"""
    endpoint = await _get_owned_endpoint(session, current_user.id, endpoint_id)

    # exclude_unset 只保留调用方显式传入的字段。
    # 不加它的话，Pydantic 会把未传的可选字段也填成 None，
    # 于是"没打算改这个字段"会被误解成"要把这个字段清空"。
    updates = payload.model_dump(exclude_unset=True)

    for field, value in updates.items():
        if field == "target_url" and value is not None:
            # AnyHttpUrl 需要转成 str 才能入库
            value = str(value)
        setattr(endpoint, field, value)

    await session.commit()
    await session.refresh(endpoint)
    return _to_response(endpoint)


@router.delete(
    "/{endpoint_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除接收地址",
    description="会同时删除该地址下的所有事件与投递记录（数据库外键级联）。",
)
async def delete_endpoint(
    endpoint_id: UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Response:
    """删除接收地址及其关联数据。"""
    endpoint = await _get_owned_endpoint(session, current_user.id, endpoint_id)
    await session.delete(endpoint)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{endpoint_id}/secret",
    response_model=EndpointSecretResponse,
    summary="重置签名密钥",
    description=(
        "生成新的签名密钥，旧密钥立即失效。"
        "用于密钥疑似泄露时轮换——不轮换就只能一直用同一个密钥，"
        "这是长期运行的系统必须提供的操作。"
    ),
)
async def rotate_secret(
    endpoint_id: UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> EndpointSecretResponse:
    """轮换签名密钥，返回新密钥明文（仅此一次）。"""
    endpoint = await _get_owned_endpoint(session, current_user.id, endpoint_id)

    secret = generate_signing_secret()
    endpoint.secret_encrypted = encrypt_secret(secret)

    await session.commit()

    return EndpointSecretResponse(
        id=endpoint.id,
        secret=secret,
        secret_masked=mask_secret(secret),
    )
