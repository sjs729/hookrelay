"""投递 Worker：把 pending 事件可靠地送到目标地址。

这是整个项目最核心的模块，独立于 Web 服务运行：

    uv run python -m app.worker

它要解决三个问题，每个对应一个具体的技术手段：

1. 【多个 Worker 不能重复消费同一条任务】
   PostgreSQL 的 `FOR UPDATE SKIP LOCKED`。取任务的事务里就把状态改成
   delivering 并提交，其他 Worker 的并发查询会直接跳过这些行，而不是
   排队等待锁——这一点很关键：如果用普通 `FOR UPDATE`，第二个 Worker
   会阻塞到第一个提交，然后发现任务已经不可用，白白浪费一次等待。

2. 【失败要退避重试，不能立刻重试把下游打爆】
   retry.py 算出的等待时间写回 next_attempt_at，下一轮扫描时只有
   到期的任务才会被取走。

3. 【Worker 自己崩了，任务不能永远卡住】
   给任务打租约（locked_at + locked_by）。停在 delivering 且租约过期的
   任务会被回收成 pending，交给其他 Worker 继续处理。
"""

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select, update

from app.config import get_settings
from app.db import SessionFactory
from app.models import DeliveryAttempt, Endpoint, Event, EventStatus
from app.security import decrypt_secret
from app.services.delivery import DeliveryResult, FailureKind, deliver_once
from app.services.retry import compute_delay

logger = logging.getLogger("hookrelay.worker")
settings = get_settings()


class DeliveryWorker:
    """投递 Worker。

    生命周期：run() 进入循环，每轮取一批到期任务并发投递，
    没有任务时休眠一个轮询间隔。收到停止信号后退出循环，
    当前正在投递的任务会自然结束（不会被打断）。
    """

    def __init__(self, worker_id: str | None = None) -> None:
        # Worker 身份用于租约标记，排查"这条任务是被谁取走的"时必需。
        # 用 主机名-进程号-随机后缀，保证同机多进程也不会重名。
        self.worker_id = worker_id or (
            f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        )
        self._stop = asyncio.Event()
        # 信号量限制同时进行的投递数量。
        # 不用"把 batch_size 调小"来限流，是因为批量取任务和并发投递
        # 是两件事：一次取 50 条能减少数据库往返，同时只投 10 条能避免
        # 瞬间对下游发起 50 个连接。
        self._semaphore = asyncio.Semaphore(settings.worker_concurrency)
        self._last_reclaim_at: datetime | None = None

    def request_stop(self) -> None:
        """请求停止。由信号处理器调用，只设标志，不做任何清理动作。"""
        self._stop.set()

    # ---------- 取任务 ----------

    async def claim_batch(self, batch_size: int) -> list[Event]:
        """取一批到期的 pending 任务，并在同一事务里标记为 delivering。

        核心 SQL 等价于：

            SELECT * FROM events
            WHERE status = 'pending' AND next_attempt_at <= now()
            ORDER BY next_attempt_at
            LIMIT :batch
            FOR UPDATE SKIP LOCKED

        三个部分各自的作用：
        - `next_attempt_at <= now()`：只取已经到期的，没到重试时间的跳过
        - `ORDER BY next_attempt_at`：先到期的先投，避免老任务被饿死
        - `FOR UPDATE SKIP LOCKED`：锁住这些行，同时让并发的事务跳过它们。
          "跳过"而不是"等待"是关键——等待会让多个 Worker 串行化，
          失去并发的意义。

        SELECT 和 UPDATE 必须在同一个事务里，否则中间会出现一个窗口：
        任务被选中但还没标记，另一个 Worker 的查询会再次选中同一条。
        """
        async with SessionFactory() as session:
            stmt = (
                select(Event)
                .where(
                    Event.status == EventStatus.PENDING.value,
                    Event.next_attempt_at <= func.now(),
                )
                .order_by(Event.next_attempt_at)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            events = list((await session.execute(stmt)).scalars().all())
            if not events:
                return []

            now = datetime.now(UTC)
            for event in events:
                event.status = EventStatus.DELIVERING.value
                event.locked_at = now
                event.locked_by = self.worker_id

            # 提交后这些任务才算真正被"认领"，其他 Worker 看不到它们
            await session.commit()
            return events

    # ---------- 投递 ----------

    async def _deliver_one(
        self,
        client: httpx.AsyncClient,
        event: Event,
        endpoint: Endpoint | None,
        attempt_number: int,
    ) -> None:
        """投递一条事件并写回结果。"""
        if endpoint is None:
            # endpoint 被删了，事件会被外键级联删除。
            # 这里不做任何写回，避免对着一条已经不存在的事件更新。
            logger.warning("事件 %s 对应的接收地址已不存在，跳过", event.id)
            return

        try:
            secret = decrypt_secret(endpoint.secret_encrypted)
        except ValueError:
            logger.exception("事件 %s 的签名密钥无法解密，判定为不可重试失败", event.id)
            await self._apply_result(
                event,
                endpoint,
                DeliveryResult(
                    ok=False,
                    # 归类为不可重试失败：密钥解不开是配置问题，
                    # 重试一百次还是解不开，只会让事件一直占着队列
                    kind=FailureKind.CLIENT_ERROR,
                    status_code=None,
                    duration_ms=0,
                    response_body=None,
                    error="服务端密钥无法解密，无法为请求签名",
                ),
                attempt_number,
            )
            return

        # 数据库存的是解析后的 JSONB，这里重新序列化后发送。
        # ensure_ascii=False 保留中文原文，separators 去掉多余空格让体积更小。
        payload_bytes = json.dumps(
            event.payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

        result = await deliver_once(
            client,
            target_url=endpoint.target_url,
            secret=secret,
            event_id=str(event.id),
            attempt=attempt_number,
            payload_bytes=payload_bytes,
            inbound_headers=event.headers or {},
            timeout_seconds=endpoint.timeout_seconds,
        )

        await self._apply_result(event, endpoint, result, attempt_number)

    async def _apply_result(
        self,
        event: Event,
        endpoint: Endpoint,
        result: DeliveryResult,
        attempt_number: int,
    ) -> None:
        """把投递结果写回数据库：记录尝试明细，并决定事件的下一个状态。

        状态迁移规则：

            成功                        → succeeded（终态）
            失败但不可重试                → dead（终态，可人工重放）
            失败但已达最大尝试次数         → dead（终态）
            失败且可重试、次数还有剩       → pending，等 next_attempt_at 到期

        用一个独立会话写入，而不是复用取任务时的会话：
        投递是并发的，多个协程共用同一个 AsyncSession 会互相干扰
        （SQLAlchemy 的 session 不是并发安全的）。
        """
        now = datetime.now(UTC)

        async with SessionFactory() as session:
            fresh = await session.get(Event, event.id)
            if fresh is None:
                # 事件在投递期间被删除（用户删了接收地址），无需写回
                return

            session.add(
                DeliveryAttempt(
                    event_id=fresh.id,
                    # 写入时带上轮次，重放后新轮的尝试不会与历史记录冲突
                    generation=fresh.attempt_generation,
                    attempt_number=attempt_number,
                    status_code=result.status_code,
                    response_body=result.response_body,
                    error=result.error,
                    duration_ms=result.duration_ms,
                )
            )

            fresh.attempt_count = attempt_number
            # 无论结果如何都要解除租约，否则任务会被误判成僵尸
            fresh.locked_at = None
            fresh.locked_by = None
            fresh.last_error = result.error

            if result.ok:
                fresh.status = EventStatus.SUCCEEDED.value
                fresh.completed_at = now
                fresh.last_error = None
            elif not result.retryable or attempt_number >= endpoint.max_attempts:
                fresh.status = EventStatus.DEAD.value
                fresh.completed_at = now
            else:
                delay = compute_delay(
                    attempt_number,
                    base_delay=settings.retry_base_delay_seconds,
                    max_delay=settings.retry_max_delay_seconds,
                    jitter_ratio=settings.retry_jitter_ratio,
                )
                fresh.status = EventStatus.PENDING.value
                fresh.next_attempt_at = now + timedelta(seconds=delay)

            await session.commit()

        if result.ok:
            logger.info(
                "投递成功 | event=%s attempt=%s status=%s 耗时=%sms",
                event.id,
                attempt_number,
                result.status_code,
                result.duration_ms,
            )
        else:
            logger.warning(
                "投递失败 | event=%s attempt=%s/%s kind=%s 错误=%s",
                event.id,
                attempt_number,
                endpoint.max_attempts,
                result.kind,
                result.error,
            )

    # ---------- 僵尸任务回收 ----------

    async def reclaim_stale(self) -> int:
        """回收租约过期的僵尸任务。

        产生的场景：Worker 在投递过程中被强杀（OOM、部署时被替换、
        机器断电）。它取走并标记为 delivering 的任务，状态永远不会变，
        也没有别人会来碰——如果不回收，这些事件就永久丢失了。

        回收本身用一条 UPDATE 完成，不需要先查询再逐条更新：
        条件是"状态是 delivering 且 locked_at 早于截止时间"，
        数据库的原子性保证不会有多个 Worker 同时回收同一批。

        注意不清零 attempt_count：已经真实发生过的尝试要如实保留，
        否则一个反复让 Worker 崩溃的事件会被无限重试下去。
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=settings.worker_lock_timeout_seconds)

        async with SessionFactory() as session:
            result = await session.execute(
                update(Event)
                .where(
                    Event.status == EventStatus.DELIVERING.value,
                    Event.locked_at < cutoff,
                )
                .values(
                    status=EventStatus.PENDING.value,
                    next_attempt_at=datetime.now(UTC),
                    locked_at=None,
                    locked_by=None,
                )
            )
            await session.commit()
            count = result.rowcount

        if count:
            logger.warning("回收了 %s 条租约过期的僵尸任务", count)
        return count

    # ---------- 主循环 ----------

    async def _sleep(self, seconds: float) -> None:
        """可被停止信号打断的休眠。

        直接用 asyncio.sleep 的话，收到停止信号最多要等满一个轮询间隔
        才退出——部署时这会表现为"关闭很慢"。改成等待停止事件，
        信号一来立刻醒来。
        """
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _run_once(self, client: httpx.AsyncClient) -> int:
        """执行一轮：取任务、并发投递、返回处理条数。"""
        events = await self.claim_batch(settings.worker_batch_size)
        if not events:
            return 0

        # 一次性把这批事件涉及的 endpoint 全查出来。
        # 如果在循环里逐个查就是典型的 N+1 查询：50 条任务打 50 次数据库。
        endpoint_ids = {event.endpoint_id for event in events}
        async with SessionFactory() as session:
            endpoints = {
                endpoint.id: endpoint
                for endpoint in (
                    await session.execute(select(Endpoint).where(Endpoint.id.in_(endpoint_ids)))
                )
                .scalars()
                .all()
            }

        # 并发投递。信号量在 _deliver_one 内部生效，
        # 所以这里虽然一次性启动了 50 个协程，实际同时在跑的只有 worker_concurrency 个。
        await asyncio.gather(
            *(
                self._deliver_one(
                    client,
                    event,
                    endpoints.get(event.endpoint_id),
                    event.attempt_count + 1,
                )
                for event in events
            )
        )
        return len(events)

    async def run(self) -> None:
        """Worker 主循环。"""
        logger.info(
            "Worker 启动 | id=%s 并发=%s 批量=%s 轮询=%ss",
            self.worker_id,
            settings.worker_concurrency,
            settings.worker_batch_size,
            settings.worker_poll_interval_seconds,
        )

        # 连接池复用：httpx 的 AsyncClient 会保持 TCP 连接，
        # 避免每次投递都重新握手（尤其是 HTTPS 还要额外做 TLS 协商）
        limits = httpx.Limits(
            max_connections=settings.worker_concurrency * 2,
            max_keepalive_connections=settings.worker_concurrency,
        )

        async with httpx.AsyncClient(limits=limits) as client:
            while not self._stop.is_set():
                try:
                    processed = await self._run_once(client)
                except Exception:
                    # 单轮出错不能让 Worker 退出：数据库瞬时不可用、
                    # 某条数据异常，都不应该导致整个投递服务停摆
                    logger.exception("Worker 本轮执行异常，将继续运行")
                    processed = 0

                if self._stop.is_set():
                    break

                # 定期回收僵尸任务，不必每轮都做
                now = datetime.now(UTC)
                if (
                    self._last_reclaim_at is None
                    or (now - self._last_reclaim_at).total_seconds() >= 60
                ):
                    try:
                        await self.reclaim_stale()
                    except Exception:
                        logger.exception("回收僵尸任务失败")
                    self._last_reclaim_at = now

                if processed == 0:
                    # 没有任务就休眠，避免空转把 CPU 打满
                    await self._sleep(settings.worker_poll_interval_seconds)

        logger.info("Worker 已停止 | id=%s", self.worker_id)


async def main() -> None:
    """Worker 进程入口。"""
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    worker = DeliveryWorker()

    # 注册信号处理，让容器编排系统（Docker、Render、K8s）的停止指令
    # 能够优雅地结束进程，而不是等超时后被强杀
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.request_stop)

    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
