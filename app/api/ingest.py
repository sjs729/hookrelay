"""事件接收入站接口。

这是整个服务暴露在公网上的入口，也是主要攻击面。请求要依次通过
六道检查才会被接受，任何一道不通过都会被拒绝：

    1. token 有效   → 404   接收地址不存在（或已删除）
    2. 地址启用中    → 403   地址被停用了
    3. 请求频率      → 429   调用方请求过快，附带 Retry-After 告知等多久
    4. 请求体大小    → 413   超过上限，边读边判，不做全量缓冲
    5. 时间戳时效    → 401   请求太旧或来自未来，拒绝重放
    6. HMAC 签名     → 401   签名不匹配，无法证明请求来自合法调用方

顺序不是随意排的：代价低的检查放在前面，避免为明显非法的请求
做昂贵的计算（HMAC 运算、JSON 解析、数据库写入）。
"""

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import SessionDep
from app.metrics import INGEST_REJECTED_TOTAL, INGEST_TOTAL
from app.models import Endpoint, Event, EventStatus
from app.observability import ctx as log_ctx
from app.schemas import EventAcceptedResponse
from app.security import (
    IDEMPOTENCY_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    decrypt_secret,
    is_timestamp_fresh,
    verify_signature,
)
from app.services.ratelimit import SlidingWindowLimiter

logger = logging.getLogger("hookrelay.ingest")
settings = get_settings()

router = APIRouter(tags=["事件接收"])

# 限流器按 endpoint 维度计数，进程内只需一个实例。
# 用 endpoint.id 而不是来源 IP 作为计数维度：IP 可能是共享出口（公司网关、
# 云函数的出口 IP），按 IP 限流会误伤无关的调用方；而 endpoint 是我们自己
# 签发的凭据，按它计数才能准确反映"某个接收地址被打了多少流量"。
_ingest_limiter = SlidingWindowLimiter(
    limit=settings.ingest_rate_limit_per_minute,
    window_seconds=60.0,
)

SignatureHeader = Annotated[str | None, Header(alias=SIGNATURE_HEADER)]
TimestampHeader = Annotated[str | None, Header(alias=TIMESTAMP_HEADER)]
IdempotencyKeyHeader = Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)]


def _reject(
    reason: str,
    status_code: int,
    detail: str,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    """记录拒绝原因并构造异常。

    把埋点和构造异常绑在一个函数里，是为了避免以后新增检查项时漏掉埋点：
    拒绝分支只能通过这个函数返回，指标自然不会缺项。
    """
    INGEST_REJECTED_TOTAL.labels(reason=reason).inc()
    return HTTPException(status_code=status_code, detail=detail, headers=headers)


async def _read_body_within_limit(request: Request, max_bytes: int) -> bytes:
    """流式读取请求体，超过上限立刻中断。

    为什么不写成 `body = await request.body()` 再判断长度：
    那样等于"先让攻击者把数据全部传完、内存先占上，然后才告诉他太大了"。
    边收边累计可以在超限的那一刻就断开，内存占用被压在 max_bytes 附近。

    另外不能依赖 Content-Length 头做判断：该头可以被伪造，
    分块传输（chunked）的请求则根本没有这个头。
    """
    chunks: list[bytes] = []
    total = 0

    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"请求体超过上限 {max_bytes} 字节",
            )
        chunks.append(chunk)

    return b"".join(chunks)


def _parse_payload(content_type: str, body: bytes) -> dict[str, Any]:
    """把请求体解析成可以直接存入 JSONB 的字典。

    events.payload 是非空 JSONB 列，所以任何情况下都必须给出一个字典。

    签名校验基于**原始字节**，与这里的解析无关。这一点很重要：
    绝不能用"重新序列化后的 JSON"去做签名校验——键顺序、空格、转义方式
    在序列化后都可能变化，算出来的签名会和发送方对不上。
    原始字节在读进来时就固定下来了，解析只是为了让数据便于查询。
    """
    if not body:
        return {}

    if "json" in content_type.lower():
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        if parsed is not None:
            # 顶层是数组或标量：包一层，保持 JSONB 列的类型要求
            return {"_value": parsed}

    # 非 JSON 类型，或声明了 JSON 但解析失败：原文保留，不让事件内容丢失
    return {"_raw": body.decode("utf-8", errors="replace")}


def _capture_headers(request: Request) -> dict[str, str]:
    """记录入站请求头，供排查问题与后续转发使用。

    过滤掉两类头：
    - Authorization / Cookie：调用方的凭据，转发给下游等于泄露
    - 我们自己的签名与时间戳头：那是给本服务校验用的，转发没有意义，
      出站请求会用 endpoint 密钥重新签名

    其余原样保留，方便排查"上游到底发了什么"。
    """
    excluded = {
        "authorization",
        "cookie",
        SIGNATURE_HEADER.lower(),
        TIMESTAMP_HEADER.lower(),
    }
    return {
        name: value for name, value in request.headers.items() if name.lower() not in excluded
    }


def _resolve_idempotency_key(
    provided: str | None,
    payload: dict[str, Any],
    body: bytes,
) -> str:
    """确定本次事件的幂等键，按可信度从高到低取值。

    1. 请求头 Idempotency-Key：调用方显式声明的去重标识，语义最准确
    2. 请求体里的 event_id / id：GitHub、Stripe 这类服务都会在 payload 里带唯一 ID
    3. 请求体哈希：兜底

    第 3 种要特别理解它的语义——它去重的是"内容完全一致的事件"。
    调用方既然没给任何幂等标识，那么两次内容完全相同的事件，
    通常就是同一次投递的重试。这个取舍在消息系统里是常见默认：
    宁可少投，也不要制造出业务上无法分辨的重复。
    """
    if provided and provided.strip():
        # 截断到列宽，避免超长 header 直接导致插入失败
        return provided.strip()[:255]

    for field in ("event_id", "id"):
        value = payload.get(field)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()[:255]

    return f"body:{hashlib.sha256(body).hexdigest()}"


@router.post(
    "/ingest/{token}",
    response_model=EventAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="接收事件",
    description=(
        "把第三方服务的 webhook 目标指向这里。\n\n"
        "请求必须携带 `X-HookRelay-Timestamp`（Unix 秒）与 "
        "`X-HookRelay-Signature`（`sha256=<hex>`）。\n\n"
        "签名算法：以 `{时间戳}.{原始请求体}` 为消息，"
        "用创建接收地址时下发的密钥做 HMAC-SHA256。\n\n"
        "返回 202 表示事件已安全落库，投递会在后台异步进行。"
    ),
    responses={
        404: {"description": "接收地址不存在"},
        403: {"description": "接收地址已停用"},
        429: {"description": "请求过于频繁"},
        413: {"description": "请求体超过上限"},
        401: {"description": "缺少签名、签名不匹配或时间戳过期"},
    },
)
async def ingest_event(
    token: str,
    request: Request,
    response: Response,
    session: SessionDep,
    signature: SignatureHeader = None,
    timestamp: TimestampHeader = None,
    idempotency_header: IdempotencyKeyHeader = None,
) -> EventAcceptedResponse:
    """接收第三方 webhook 事件并入队。"""

    # ---------- 第 1 道：token 是否有效 ----------
    endpoint = (
        await session.execute(select(Endpoint).where(Endpoint.token == token))
    ).scalar_one_or_none()
    if endpoint is None:
        raise _reject("unknown_token", status.HTTP_404_NOT_FOUND, "接收地址不存在")

    # ---------- 第 2 道：地址是否启用 ----------
    if not endpoint.is_active:
        raise _reject("endpoint_disabled", status.HTTP_403_FORBIDDEN, "接收地址已停用")

    # ---------- 第 3 道：频率限制 ----------
    retry_after = _ingest_limiter.check(str(endpoint.id))
    if retry_after is not None:
        # 向上取整：返回 0 会让调用方立刻重试，等于把限流变成重试风暴
        raise _reject(
            "rate_limited",
            status.HTTP_429_TOO_MANY_REQUESTS,
            "请求过于频繁，请稍后重试",
            headers={"Retry-After": str(max(1, int(retry_after + 0.999)))},
        )

    # ---------- 第 4 道：读取请求体（带大小上限）----------
    try:
        body = await _read_body_within_limit(request, settings.max_ingest_body_bytes)
    except HTTPException:
        # 体积超限是在流式读取过程中发现的，那一步拿不到 endpoint，
        # 所以在这里补记指标，保持拒绝原因统计的完整性
        INGEST_REJECTED_TOTAL.labels(reason="body_too_large").inc()
        raise

    # ---------- 第 5 道：时间戳时效 ----------
    if not timestamp:
        raise _reject(
            "missing_timestamp",
            status.HTTP_401_UNAUTHORIZED,
            f"缺少 {TIMESTAMP_HEADER} 请求头",
        )
    if not is_timestamp_fresh(timestamp, settings.signature_tolerance_seconds):
        raise _reject(
            "stale_timestamp",
            status.HTTP_401_UNAUTHORIZED,
            "请求时间戳超出允许范围，疑似重放",
        )

    # ---------- 第 6 道：HMAC 签名 ----------
    if not signature:
        raise _reject(
            "missing_signature",
            status.HTTP_401_UNAUTHORIZED,
            f"缺少 {SIGNATURE_HEADER} 请求头",
        )

    try:
        secret = decrypt_secret(endpoint.secret_encrypted)
    except ValueError:
        # 密钥解不开属于服务端配置问题，不是调用方的错，
        # 因此返回 500 而不是 401，避免调用方误以为是自己签名错了
        logger.exception("接收地址 %s 的签名密钥无法解密", endpoint.id)
        raise _reject(
            "secret_unavailable",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "服务端密钥配置异常",
        ) from None

    if not verify_signature(secret, timestamp, body, signature):
        logger.warning("签名校验失败", extra=log_ctx(endpoint_id=str(endpoint.id)))
        raise _reject("signature_mismatch", status.HTTP_401_UNAUTHORIZED, "签名校验失败")

    # ---------- 通过全部检查，写入事件 ----------
    payload = _parse_payload(request.headers.get("content-type", ""), body)
    idempotency_key = _resolve_idempotency_key(idempotency_header, payload, body)

    # 先把后续要用的字段取成普通变量。
    # session.rollback() 会把 ORM 对象标记为过期，之后访问它的任何属性
    # 都会隐式触发一次重新加载。同步环境下这只是一次额外查询，
    # 但在异步上下文里属于"意料之外的 IO"，会直接抛 MissingGreenlet。
    # 幂等冲突分支必然经历 rollback，所以这些值必须提前拿出来。
    endpoint_id = endpoint.id

    event = Event(
        endpoint_id=endpoint_id,
        payload=payload,
        headers=_capture_headers(request),
        idempotency_key=idempotency_key,
        status=EventStatus.PENDING.value,
        attempt_count=0,
        # 置为"现在"，让 Worker 下一轮扫描就能取到
        next_attempt_at=datetime.now(UTC),
    )
    session.add(event)

    try:
        await session.commit()
    except IntegrityError:
        # 唯一约束 (endpoint_id, idempotency_key) 拦下了重复事件。
        # 靠数据库约束而不是"先查再插"：并发下两个相同事件可能同时通过
        # 存在性检查，然后双双插入。数据库的唯一索引是原子的，绕不过去。
        await session.rollback()

        existing = (
            await session.execute(
                select(Event).where(
                    Event.endpoint_id == endpoint_id,
                    Event.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one()

        logger.info(
            "重复事件被去重",
            extra=log_ctx(endpoint_id=str(endpoint_id), event_id=str(existing.id)),
        )
        INGEST_TOTAL.labels(result="duplicate").inc()

        # 返回 202 而不是 409：对调用方来说，"事件已收到"这个事实没有变，
        # 重复投递是它自己重试导致的，不算错误。用 409 会诱导调用方改代码。
        response.headers["Location"] = f"/api/events/{existing.id}"
        return EventAcceptedResponse(
            event_id=existing.id,
            endpoint_id=endpoint_id,
            status=existing.status,
            duplicate=True,
        )

    await session.refresh(event)
    response.headers["Location"] = f"/api/events/{event.id}"

    INGEST_TOTAL.labels(result="accepted").inc()
    logger.info(
        "事件已接收",
        extra=log_ctx(endpoint_id=str(endpoint_id), event_id=str(event.id)),
    )

    return EventAcceptedResponse(
        event_id=event.id,
        endpoint_id=endpoint_id,
        status=event.status,
        duplicate=False,
    )
