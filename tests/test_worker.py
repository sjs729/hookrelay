"""投递循环测试。

这里测的是 Worker 的调度行为，不是单次 HTTP 请求（那部分在 test_delivery.py）。
所以把真正的网络调用换掉，只观察"事件有没有按预期流转"。

用真实数据库：这些行为依赖事务、行锁和唯一约束，
用假数据库会把最值得验证的部分替换掉。
"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.db import SessionFactory
from app.models import DeliveryAttempt, Endpoint, Event, EventStatus
from app.services.delivery import DeliveryResult, FailureKind
from app.worker import DeliveryWorker
from tests.conftest import json_body, sign_headers


def succeeding_result(duration_ms: int = 8) -> DeliveryResult:
    return DeliveryResult(
        ok=True,
        kind=FailureKind.NONE,
        status_code=200,
        duration_ms=duration_ms,
        response_body='{"ok":true}',
        error=None,
    )


def failing_result(status_code: int = 503) -> DeliveryResult:
    return DeliveryResult(
        ok=False,
        kind=FailureKind.SERVER_ERROR,
        status_code=status_code,
        duration_ms=12,
        response_body="upstream unavailable",
        error=f"目标服务返回 {status_code}",
    )


async def push_events(client, endpoint, count: int, prefix: str = "evt") -> list[str]:
    event_ids: list[str] = []
    for index in range(count):
        body = json_body({"event_id": f"{prefix}-{index}"})
        response = await client.post(
            f"/ingest/{endpoint['token']}",
            content=body,
            headers=sign_headers(endpoint["secret"], body),
        )
        assert response.status_code == 202, response.text
        event_ids.append(response.json()["event_id"])
    return event_ids


@pytest.fixture
def worker() -> DeliveryWorker:
    return DeliveryWorker(worker_id="test-worker")


class TestRunOnce:
    async def test_delivers_claimed_events(self, client, endpoint, worker, monkeypatch) -> None:
        """一轮下来把队列里的事件全部投递完成。"""
        monkeypatch.setattr(
            "app.worker.deliver_once",
            lambda *args, **kwargs: asyncio.sleep(0, result=succeeding_result()),
        )
        await push_events(client, endpoint, 3)

        async with httpx.AsyncClient() as http:
            processed = await worker._run_once(http)

        assert processed == 3
        async with SessionFactory() as session:
            events = (
                (
                    await session.execute(
                        select(Event).where(Event.endpoint_id == UUID(endpoint["id"]))
                    )
                )
                .scalars()
                .all()
            )
        assert all(event.status == EventStatus.SUCCEEDED.value for event in events)
        assert all(event.attempt_count == 1 for event in events)

    async def test_returns_zero_on_empty_queue(self, worker) -> None:
        """队列为空时返回 0，调用方据此决定要不要休眠。"""
        async with httpx.AsyncClient() as http:
            assert await worker._run_once(http) == 0

    async def test_records_one_attempt_per_delivery(
        self, client, endpoint, worker, monkeypatch
    ) -> None:
        """一次成功投递只产生一条投递记录。"""
        monkeypatch.setattr(
            "app.worker.deliver_once",
            lambda *args, **kwargs: asyncio.sleep(0, result=succeeding_result()),
        )
        await push_events(client, endpoint, 2)

        async with httpx.AsyncClient() as http:
            await worker._run_once(http)

        async with SessionFactory() as session:
            attempts = (await session.execute(select(DeliveryAttempt))).scalars().all()
        assert len(attempts) == 2
        assert all(attempt.status_code == 200 for attempt in attempts)

    async def test_failure_schedules_retry(self, client, endpoint, worker, monkeypatch) -> None:
        """下游 5xx 后事件回到 pending，并带上一个未来的重试时间。

        关键点是 attempt_count 已经加过：下一次认领时它就是 2，
        重试预算靠这个计数逐次消耗掉。
        """
        monkeypatch.setattr(
            "app.worker.deliver_once",
            lambda *args, **kwargs: asyncio.sleep(0, result=failing_result()),
        )
        await push_events(client, endpoint, 1)

        async with httpx.AsyncClient() as http:
            await worker._run_once(http)

        async with SessionFactory() as session:
            event = (await session.execute(select(Event))).scalar_one()
        assert event.status == EventStatus.PENDING.value
        assert event.attempt_count == 1
        assert event.next_attempt_at > datetime.now(UTC)

    async def test_retryable_failure_event_is_not_claimed_immediately(
        self, client, endpoint, worker, monkeypatch
    ) -> None:
        """重试时间没到之前，下一轮不会再把它取出来。

        这是退避能生效的前提：如果 next_attempt_at 不参与筛选，
        所谓的"指数退避"就退化成了忙等重试。
        """
        monkeypatch.setattr(
            "app.worker.deliver_once",
            lambda *args, **kwargs: asyncio.sleep(0, result=failing_result()),
        )
        await push_events(client, endpoint, 1)

        async with httpx.AsyncClient() as http:
            await worker._run_once(http)
            second_round = await worker._run_once(http)

        assert second_round == 0

    async def test_dead_letter_after_exhausting_attempts(
        self, client, endpoint, worker, monkeypatch
    ) -> None:
        """用满重试预算后进入死信，不再被认领。"""
        monkeypatch.setattr(
            "app.worker.deliver_once",
            lambda *args, **kwargs: asyncio.sleep(0, result=failing_result()),
        )
        await push_events(client, endpoint, 1)

        async with SessionFactory() as session:
            ep = await session.get(Endpoint, UUID(endpoint["id"]))
            assert ep is not None
            ep.max_attempts = 1
            await session.commit()

        async with httpx.AsyncClient() as http:
            await worker._run_once(http)
            second_round = await worker._run_once(http)

        async with SessionFactory() as session:
            event = (await session.execute(select(Event))).scalar_one()
        assert event.status == EventStatus.DEAD.value
        assert second_round == 0

    async def test_broken_secret_is_not_retried(self, client, endpoint, worker) -> None:
        """密钥无法解密时直接进死信。

        这属于配置损坏，重试解不开的密文没有意义。
        这里不 mock deliver_once：投递前的解密步骤就会失败，
        根本走不到发请求那一步。
        """
        await push_events(client, endpoint, 1)

        async with SessionFactory() as session:
            ep = await session.get(Endpoint, UUID(endpoint["id"]))
            assert ep is not None
            ep.secret_encrypted = "broken-not-a-fernet-token"
            await session.commit()

        async with httpx.AsyncClient() as http:
            await worker._run_once(http)

        async with SessionFactory() as session:
            event = (await session.execute(select(Event))).scalar_one()
        assert event.status == EventStatus.DEAD.value
        assert event.last_error is not None

    async def test_deleted_endpoint_is_skipped(self, client, endpoint, worker, monkeypatch) -> None:
        """接收地址不存在时跳过投递，不写回任何结果。

        正常路径下外键会把事件一起删掉，这里是防御性处理：
        即使出现孤儿事件，也不该因为对着不存在的行写回而崩掉整个批次。
        """
        monkeypatch.setattr(
            "app.worker.deliver_once",
            lambda *args, **kwargs: asyncio.sleep(0, result=succeeding_result()),
        )
        event_ids = await push_events(client, endpoint, 1)

        async with SessionFactory() as session:
            event = await session.get(Event, UUID(event_ids[0]))
            assert event is not None

        # 直接调用内部方法并把 endpoint 传成 None，模拟"地址已不存在"
        async with httpx.AsyncClient() as http:
            await worker._deliver_one_inner(http, event, None, 1)

        async with SessionFactory() as session:
            fresh = await session.get(Event, UUID(event_ids[0]))
            assert fresh is not None
            # 状态没有被改动，事件仍是原始的 pending
            assert fresh.status == EventStatus.PENDING.value


class TestWorkerLoop:
    async def test_run_stops_on_request(self, client, endpoint, worker, monkeypatch) -> None:
        """收到停止信号后主循环退出。

        run() 是常驻循环，必须能被优雅关闭，否则部署时每次发布
        都要等超时被强杀，正在投递的事件会被留在 delivering 状态。
        """
        monkeypatch.setattr(
            "app.worker.deliver_once",
            lambda *args, **kwargs: asyncio.sleep(0, result=succeeding_result()),
        )
        monkeypatch.setattr("app.worker.settings.worker_poll_interval_seconds", 0.01)

        await push_events(client, endpoint, 1)

        task = asyncio.create_task(worker.run())
        # 给循环足够的时间把事件投递完
        for _ in range(200):
            await asyncio.sleep(0.01)
            async with SessionFactory() as session:
                event = (await session.execute(select(Event))).scalar_one_or_none()
            if event is not None and event.status == EventStatus.SUCCEEDED.value:
                break

        worker.request_stop()
        await asyncio.wait_for(task, timeout=5)

        async with SessionFactory() as session:
            event = (await session.execute(select(Event))).scalar_one()
        assert event.status == EventStatus.SUCCEEDED.value

    async def test_loop_survives_delivery_exception(
        self, client, endpoint, worker, monkeypatch
    ) -> None:
        """单条投递抛异常不会让 Worker 退出。

        数据库瞬时不可用、下游返回畸形响应这类问题都应该是"这一轮没成"，
        而不是"整个投递服务停摆"。否则一次抖动就需要人去重启进程。
        """

        async def exploding(*args, **kwargs):
            raise RuntimeError("模拟的意外错误")

        monkeypatch.setattr("app.worker.deliver_once", exploding)
        monkeypatch.setattr("app.worker.settings.worker_poll_interval_seconds", 0.01)

        await push_events(client, endpoint, 1)

        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.3)
        still_running = not task.done()
        worker.request_stop()
        await asyncio.wait_for(task, timeout=5)

        assert still_running, "Worker 因单条投递异常退出了"

    async def test_sleep_is_interrupted_by_stop(self, worker) -> None:
        """休眠期间收到停止信号要立刻醒来。

        否则关闭一次 Worker 要等完整的轮询间隔，
        在容器编排里会表现为"关闭缓慢"甚至被强杀。
        """
        worker.request_stop()
        loop = asyncio.get_running_loop()
        started = loop.time()
        # 请求一个很长的休眠，但因为它已经被要求停止，应当立即返回
        await worker._sleep(30)
        assert loop.time() - started < 1
