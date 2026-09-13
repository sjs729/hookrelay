"""统计接口。

指标端点 `/metrics` 和这里的 `/api/stats` 看起来重叠，其实服务两类人：

- `/metrics` 给监控系统抓取，是时间序列，关心的是"趋势"和"告警"
- `/api/stats` 给用户看自己的账号，是单次快照，关心的是"我现在什么情况"

两者的查询口径也因此不同：前者是全局的，后者必须按 user_id 过滤。
"""

import logging

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import CurrentUser
from app.db import SessionDep
from app.models import DeliveryAttempt, Endpoint, Event, EventStatus
from app.schemas import StatsResponse

logger = logging.getLogger("hookrelay.stats")

router = APIRouter(prefix="/api/stats", tags=["统计"])


@router.get(
    "",
    response_model=StatsResponse,
    summary="投递统计",
    description=(
        "返回当前账号下的事件总数、各状态分布、成功率与平均投递耗时。\n\n"
        "所有数字都**只统计本人名下的接收地址**：先按 endpoint 归属过滤，"
        "再聚合事件。反过来先聚合再过滤是错的——那样会把别人的数据算进来。"
    ),
)
async def get_stats(session: SessionDep, current_user: CurrentUser) -> StatsResponse:
    """汇总当前用户的投递情况。"""
    # 一条 SQL 拿到全部状态计数。用 count().filter() 而不是发五次 count 查询：
    # 过滤条件相同，只有聚合条件不同，扫表一次就够了
    stats_row = (
        await session.execute(
            select(
                func.count(Event.id),
                func.count().filter(Event.status == EventStatus.PENDING.value),
                func.count().filter(Event.status == EventStatus.DELIVERING.value),
                func.count().filter(Event.status == EventStatus.SUCCEEDED.value),
                func.count().filter(Event.status == EventStatus.DEAD.value),
            )
            .select_from(Event)
            # 从事件往上回溯到 endpoint，才能判断归属。
            # 事件表本身没有 user_id，归属完全由 endpoint 决定
            .join(Endpoint, Endpoint.id == Event.endpoint_id)
            .where(Endpoint.user_id == current_user.id)
        )
    ).one()

    total_events, pending, delivering, succeeded, dead = stats_row

    endpoint_count = await session.scalar(
        select(func.count(Endpoint.id)).where(Endpoint.user_id == current_user.id)
    )

    attempts_row = (
        await session.execute(
            select(
                func.count(DeliveryAttempt.id),
                # 只对真正发出去的请求求平均耗时。
                # 密钥解不开那类失败根本没产生网络请求，耗时记 0，
                # 算进去会把平均值稀释成一个没有意义的偏小数字
                func.avg(DeliveryAttempt.duration_ms).filter(
                    DeliveryAttempt.status_code.is_not(None)
                ),
            )
            .select_from(DeliveryAttempt)
            .join(Event, Event.id == DeliveryAttempt.event_id)
            .join(Endpoint, Endpoint.id == Event.endpoint_id)
            .where(Endpoint.user_id == current_user.id)
        )
    ).one()

    total_attempts, avg_latency = attempts_row

    # 成功率的分母是已终结的事件，不包含还在排队或正在投递的。
    # 把进行中的算成失败会让成功率在流量高峰时莫名下跌，那不是真相
    finished = succeeded + dead
    success_rate = round(succeeded / finished, 4) if finished else 0.0

    return StatsResponse(
        endpoints=endpoint_count or 0,
        total_events=total_events or 0,
        pending=pending or 0,
        delivering=delivering or 0,
        succeeded=succeeded or 0,
        dead=dead or 0,
        success_rate=success_rate,
        total_attempts=total_attempts or 0,
        # avg() 在整数列上返回 Decimal，转成 float 才能被 JSON 序列化
        avg_delivery_latency_ms=round(float(avg_latency), 2) if avg_latency is not None else None,
    )
