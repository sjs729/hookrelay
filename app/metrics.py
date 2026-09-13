"""Prometheus 指标定义。

指标设计上有一条容易踩的线：**标签基数**。

每个不同的标签组合在 Prometheus 里都是一条独立的时间序列。把
endpoint_id、event_id 这类近乎唯一的维度做成标签，几万个接收地址
就会产生几万条序列，直接把监控系统撑爆。所以这里的标签只用
取值有限的枚举（结果、拒绝原因），具体是哪个接收地址出了问题，
靠日志和数据库去查，不靠指标。

指标本身分三类，分别回答三个不同的问题：

- Counter：累计发生了多少次（只增不减）
- Histogram：耗时落在哪些区间（延迟分布，不只看平均值）
- Gauge：此刻的瞬时值（队列积压有多深）
"""

import logging

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Event, EventStatus

logger = logging.getLogger("hookrelay.metrics")

# 投递类指标注册到一个独立 registry。
#
# 原因是 Web 与 Worker 是两个进程，各自的内存 registry 互不可见。
# 如果把投递指标放进默认 registry，Worker 的指标端口里会同时出现
# 一堆恒为 0 的队列指标——看的人分不清那是"真的没积压"，
# 还是"这个进程压根不统计这项"。分开之后，每个端口暴露的都是
# 该进程真正负责的指标。
WORKER_REGISTRY = CollectorRegistry()

# ---------- 入站接收（Web 进程） ----------

INGEST_TOTAL = Counter(
    "hookrelay_ingest_total",
    "入站接收的 Webhook 事件数，按处理结果区分",
    ["result"],
)

INGEST_REJECTED_TOTAL = Counter(
    "hookrelay_ingest_rejected_total",
    "入站请求被拒绝的次数，按拒绝原因区分",
    ["reason"],
)


# ---------- 投递（Worker 进程） ----------

DELIVERY_TOTAL = Counter(
    "hookrelay_delivery_total",
    "投递尝试次数，按结果区分（succeeded 成功 / retrying 待重试 / dead 已放弃）",
    ["result"],
    registry=WORKER_REGISTRY,
)

DELIVERY_DURATION = Histogram(
    "hookrelay_delivery_duration_seconds",
    "单次向下游投递的耗时",
    # 分桶覆盖毫秒级到十秒级：绝大多数投递在 100ms 内完成，
    # 但超时上限是 10 秒，桶要能区分"正常慢"和"接近超时"
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=WORKER_REGISTRY,
)

STALE_RECLAIMED_TOTAL = Counter(
    "hookrelay_stale_reclaimed_total",
    "被判定为僵尸任务并回收的次数。持续增长说明 Worker 在异常退出",
    registry=WORKER_REGISTRY,
)


# ---------- 队列状态（Web 进程，拉取时实时查库） ----------

QUEUE_DEPTH = Gauge(
    "hookrelay_queue_depth",
    "待投递的事件数（status=pending）",
)

IN_FLIGHT = Gauge(
    "hookrelay_in_flight",
    "正在投递中的事件数（status=delivering）",
)

DEAD_LETTER_DEPTH = Gauge(
    "hookrelay_dead_letter_depth",
    "死信数量（status=dead）。增长说明有下游持续不可用",
)


async def refresh_queue_gauges(session: AsyncSession) -> None:
    """从数据库刷新队列类指标。

    这里刻意不在每次入队/投递时就地增减计数：Gauge 表达的是"此刻的真实状态"，
    而计数增减一旦漏掉某条分支（比如 Worker 被强杀），就会永久偏移，
    之后再也没人知道真实积压量。直接查一次数据库，得到的值永远是对的，
    代价只是一次带索引的 count——而且只在 /metrics 被拉取时才执行。
    """
    try:
        # 三个状态各自计数。用一条 SQL 的 FILTER 聚合拿到全部结果，
        # 避免为了刷新指标连查三次库
        row = await session.execute(
            select(
                func.count().filter(Event.status == EventStatus.PENDING),
                func.count().filter(Event.status == EventStatus.DELIVERING),
                func.count().filter(Event.status == EventStatus.DEAD),
            ).select_from(Event)
        )
        pending, delivering, dead = row.one()
    except Exception:
        # 指标刷新失败不应该让 /metrics 整体不可用：
        # 保留上一次的数值，比返回 500 更有利于排障
        logger.exception("刷新队列指标失败")
        return

    QUEUE_DEPTH.set(pending)
    IN_FLIGHT.set(delivering)
    DEAD_LETTER_DEPTH.set(dead)
