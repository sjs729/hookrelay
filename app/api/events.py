"""事件查询与死信重放。

事件不直接属于用户，而是通过接收地址（endpoint）间接归属。
所有查询一律 join endpoint 并带上 user_id 条件，
而不是"先查事件、再查 endpoint 判断归属"——后者容易在某个分支漏掉校验，
一旦漏掉就是用户 A 能读到用户 B 的事件内容。
"""

import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, SessionDep
from app.models import DeliveryAttempt, Endpoint, Event, EventStatus
from app.schemas import (
    DeliveryAttemptResponse,
    EventDetailResponse,
    EventListResponse,
    EventReplayResponse,
    EventResponse,
)

logger = logging.getLogger("hookrelay.events")

router = APIRouter(prefix="/api/events", tags=["事件查询"])


async def _get_owned_event(session: SessionDep, user_id: UUID, event_id: UUID) -> Event:
    """取出属于该用户的事件；不存在或不属于该用户一律返回 404。"""
    stmt = (
        select(Event)
        .join(Endpoint, Endpoint.id == Event.endpoint_id)
        .where(Event.id == event_id, Endpoint.user_id == user_id)
    )
    event = (await session.execute(stmt)).scalar_one_or_none()
    if event is None:
        # 刻意不区分"事件不存在"和"事件不属于你"。
        # 如果前者返回 404、后者返回 403，攻击者就能通过状态码差异
        # 枚举出哪些 UUID 是真实存在的。
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="事件不存在")
    return event


@router.get("", response_model=EventListResponse, summary="查询事件列表")
async def list_events(
    session: SessionDep,
    current_user: CurrentUser,
    status_filter: Annotated[
        EventStatus | None,
        Query(alias="status", description="按状态过滤：pending / delivering / succeeded / dead"),
    ] = None,
    endpoint_id: Annotated[UUID | None, Query(description="只看某个接收地址下的事件")] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="每页条数")] = 50,
    offset: Annotated[int, Query(ge=0, description="跳过的条数")] = 0,
) -> EventListResponse:
    """分页查询当前用户的事件。

    死信排查的典型用法是 `?status=dead`，看哪些事件已经放弃投递、
    需要人工介入。响应里的 total 让调用方不必额外发一次请求才知道总数。
    """
    conditions = [Endpoint.user_id == current_user.id]
    if status_filter is not None:
        conditions.append(Event.status == status_filter.value)
    if endpoint_id is not None:
        conditions.append(Event.endpoint_id == endpoint_id)

    base = select(Event).join(Endpoint, Endpoint.id == Event.endpoint_id).where(*conditions)

    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()

    rows = (
        (await session.execute(base.order_by(Event.created_at.desc()).limit(limit).offset(offset)))
        .scalars()
        .all()
    )

    return EventListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[EventResponse.model_validate(row) for row in rows],
    )


@router.get("/{event_id}", response_model=EventDetailResponse, summary="查看事件详情")
async def get_event(
    event_id: UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> EventDetailResponse:
    """查看事件原始内容，以及每一次投递尝试的结果。

    投递记录按 (轮次, 尝试序号) 排序，所以重放过的事件也能按照
    真实发生顺序展示，不会新旧交错。
    """
    event = await _get_owned_event(session, current_user.id, event_id)

    attempts = (
        (
            await session.execute(
                select(DeliveryAttempt)
                .where(DeliveryAttempt.event_id == event.id)
                .order_by(DeliveryAttempt.generation, DeliveryAttempt.attempt_number)
            )
        )
        .scalars()
        .all()
    )

    return EventDetailResponse(
        **EventResponse.model_validate(event).model_dump(),
        payload=event.payload,
        headers=event.headers or {},
        attempts=[DeliveryAttemptResponse.model_validate(attempt) for attempt in attempts],
    )


@router.post(
    "/{event_id}/replay",
    response_model=EventReplayResponse,
    status_code=status.HTTP_200_OK,
    summary="重放事件（死信复活）",
)
async def replay_event(
    event_id: UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> EventReplayResponse:
    """把一条已经终结的事件重新放回队列。

    适用场景：下游服务故障一段时间，事件耗尽重试进入死信；下游修好之后，
    运维手动把事件推回去，而不是要求上游业务系统重新发一遍——
    上游往往已经无法重现那个时刻的数据。

    这里有两个动作容易被忽略，但少了哪一个都会出错：

    1. `attempt_count` 清零
       不清零的话，事件刚被 Worker 取走就会因为
       `attempt_number >= max_attempts` 立刻回到死信，重放等于没做。

    2. `attempt_generation` 递增
       清零之后新轮的尝试会从 1 重新编号，如果不区分轮次，
       就会和历史上的第 1 次投递记录撞上 delivery_attempts 的唯一约束，
       导致投递结果写不进去。

    `locked_at` / `locked_by` 也要一并清空，否则这条事件会被
    僵尸任务回收逻辑误判成"正被某个 Worker 持有"。
    """
    event = await _get_owned_event(session, current_user.id, event_id)

    if event.status in {EventStatus.PENDING.value, EventStatus.DELIVERING.value}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"事件当前状态为 {event.status}，正在处理中，无需重放",
        )

    now = datetime.now(UTC)
    previous_status = event.status

    event.status = EventStatus.PENDING.value
    event.attempt_count = 0
    event.attempt_generation += 1
    event.next_attempt_at = now
    event.completed_at = None
    event.last_error = None
    event.locked_at = None
    event.locked_by = None

    await session.commit()

    # 重放是一次人为干预，值得留痕：出问题时能看出
    # "这条事件被谁在什么时候重新推回过队列"
    logger.info(
        "事件重放 | event=%s user=%s 原状态=%s 新轮次=%s",
        event.id,
        current_user.id,
        previous_status,
        event.attempt_generation,
    )

    return EventReplayResponse(
        event_id=event.id,
        status=event.status,
        scheduled_at=now,
        message="事件已重新入队，Worker 将在下一轮扫描时投递",
    )
