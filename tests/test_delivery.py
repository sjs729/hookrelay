"""投递引擎测试。

分两层：

1. **单次投递**（deliver_once）用 httpx 的 MockTransport 拦截请求，
   不依赖任何外部服务，运行快且能精确构造超时、连接失败这些真实网络里
   很难稳定复现的场景。

2. **状态迁移**（_apply_result / claim_batch / reclaim_stale）用真实数据库，
   因为它们验证的正是数据库层面的行为——约束、事务、并发行锁。
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.db import SessionFactory
from app.models import DeliveryAttempt, Endpoint, Event, EventStatus
from app.services.delivery import DeliveryResult, FailureKind, deliver_once
from app.worker import DeliveryWorker
from tests.conftest import json_body, sign_headers

TARGET_URL = "http://downstream.example.com/hook"


def make_result(
    *,
    ok: bool,
    kind: FailureKind = FailureKind.NONE,
    status_code: int | None = 200,
    duration_ms: int = 12,
    error: str | None = None,
) -> DeliveryResult:
    """构造一个投递结果，测试里只关心结果和状态迁移的对应关系。"""
    return DeliveryResult(
        ok=ok,
        kind=kind,
        status_code=status_code,
        duration_ms=duration_ms,
        response_body='{"ok":true}',
        error=error,
    )


async def deliver_with(handler, **overrides) -> tuple[DeliveryResult, httpx.Request]:
    """用给定的响应行为跑一次投递，返回结果与捕获到的请求。"""
    captured: list[httpx.Request] = []

    async def wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return await handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wrapped)) as client:
        result = await deliver_once(
            client,
            target_url=overrides.get("target_url", TARGET_URL),
            secret=overrides.get("secret", "test-secret"),
            event_id=overrides.get("event_id", "evt-1"),
            attempt=overrides.get("attempt", 1),
            payload_bytes=overrides.get("payload_bytes", b'{"a":1}'),
            inbound_headers=overrides.get("inbound_headers", {}),
            timeout_seconds=overrides.get("timeout_seconds", 5),
        )
    return result, captured[0]


class TestDeliverOnce:
    async def test_success(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        result, _ = await deliver_with(handler)
        assert result.ok
        assert result.status_code == 200
        assert result.kind == FailureKind.NONE
        assert result.retryable is False

    async def test_response_body_is_truncated(self) -> None:
        """响应体只保留开头一段。

        一个返回整页 HTML 错误页的下游足够把数据库撑大，
        而排查问题只需要开头那几十个字符。
        """

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"x" * 5000)

        result, _ = await deliver_with(handler)
        assert result.response_body is not None
        assert len(result.response_body) <= 1024 + len("…（已截断）")

    async def test_outbound_headers_include_signature(self) -> None:
        """出站请求带签名、时间戳、投递编号与尝试次数。

        下游靠这四个头做两件事：验证请求确实来自 HookRelay，
        以及识别重复投递（至少一次语义下重投是可能发生的）。
        """

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        _, request = await deliver_with(handler, event_id="evt-abc", attempt=3)
        assert request.headers["X-HookRelay-Signature"].startswith("sha256=")
        assert request.headers["X-HookRelay-Timestamp"].isdigit()
        assert request.headers["X-HookRelay-Delivery-Id"] == "evt-abc"
        assert request.headers["X-HookRelay-Attempt"] == "3"

    async def test_inbound_headers_are_forwarded(self) -> None:
        """入站时保留下来的业务请求头要转发给下游。"""

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        _, request = await deliver_with(handler, inbound_headers={"x-custom-trace": "t-1"})
        assert request.headers["x-custom-trace"] == "t-1"

    async def test_timeout_is_retryable(self) -> None:
        """超时属于暂时性故障，要重试。"""

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        result, _ = await deliver_with(handler)
        assert not result.ok
        assert result.kind == FailureKind.TIMEOUT
        assert result.retryable is True

    async def test_connection_error_is_retryable(self) -> None:
        """连不上同样重试：下游可能在重启，过一会儿就好了。"""

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        result, _ = await deliver_with(handler)
        assert not result.ok
        assert result.kind == FailureKind.CONNECTION
        assert result.retryable is True

    @pytest.mark.parametrize("status_code", [500, 502, 503, 504])
    async def test_server_errors_are_retryable(self, status_code: int) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code)

        result, _ = await deliver_with(handler)
        assert result.kind == FailureKind.SERVER_ERROR
        assert result.retryable is True

    @pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
    async def test_client_errors_are_not_retryable(self, status_code: int) -> None:
        """普通 4xx 不重试。

        请求本身有问题（参数错了、路径不存在），重试一百次结果都一样，
        只会让事件一直占着队列、白白消耗投递资源。
        """

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code)

        result, _ = await deliver_with(handler)
        assert result.kind == FailureKind.CLIENT_ERROR
        assert result.retryable is False

    @pytest.mark.parametrize("status_code", [408, 429])
    async def test_rate_limit_and_request_timeout_are_retryable(self, status_code: int) -> None:
        """408 和 429 是特殊的 4xx：下游明确表示"稍后再来"，必须重试。"""

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code)

        result, _ = await deliver_with(handler)
        assert result.retryable is True

    async def test_redirect_is_not_followed(self) -> None:
        """不自动跟随重定向。

        自动跟随意味着把签名和请求体转发到一个我们未经验证的新地址——
        那等于把下游密钥暴露给重定向目标。重定向应当被当作一次失败，
        由人来判断新地址是否可信。
        """

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "http://evil.example.com/steal"})

        result, _ = await deliver_with(handler)
        assert not result.ok
        # 3xx 归为客户端侧错误，不重试
        assert result.retryable is False


class TestStateTransitions:
    """验证投递结果如何改变事件状态。"""

    async def _load(self, event_id: str) -> tuple[Event, Endpoint]:
        """取出事件与接收地址的 ORM 对象。"""
        async with SessionFactory() as session:
            event = await session.get(Event, UUID(event_id))
            assert event is not None
            endpoint = await session.get(Endpoint, event.endpoint_id)
            assert endpoint is not None
            return event, endpoint

    async def _create_event(self, client, endpoint) -> str:
        body = json_body({"event_id": "evt-state"})
        response = await client.post(
            f"/ingest/{endpoint['token']}",
            content=body,
            headers=sign_headers(endpoint["secret"], body),
        )
        assert response.status_code == 202, response.text
        return response.json()["event_id"]

    async def _reload(self, event_id: str) -> Event:
        async with SessionFactory() as session:
            event = await session.get(Event, UUID(event_id))
            assert event is not None
            return event

    async def test_success_marks_event_succeeded(self, client, endpoint) -> None:
        event_id = await self._create_event(client, endpoint)
        event, ep = await self._load(event_id)

        worker = DeliveryWorker(worker_id="test-worker")
        await worker._apply_result(event, ep, make_result(ok=True), 1)

        fresh = await self._reload(event_id)
        assert fresh.status == EventStatus.SUCCEEDED.value
        assert fresh.attempt_count == 1
        assert fresh.completed_at is not None
        # 成功后要清掉上次的错误信息，否则查询接口会显示一条已经解决的错误
        assert fresh.last_error is None

    async def test_failure_schedules_retry(self, client, endpoint) -> None:
        event_id = await self._create_event(client, endpoint)
        event, ep = await self._load(event_id)

        worker = DeliveryWorker(worker_id="test-worker")
        await worker._apply_result(
            event,
            ep,
            make_result(ok=False, kind=FailureKind.SERVER_ERROR, status_code=500, error="下游 500"),
            1,
        )

        fresh = await self._reload(event_id)
        assert fresh.status == EventStatus.PENDING.value
        assert fresh.attempt_count == 1
        assert fresh.last_error == "下游 500"
        # 重试时间被推后，Worker 下一轮不会立刻再打一次
        assert fresh.next_attempt_at > datetime.now(UTC)

    async def test_client_error_goes_straight_to_dead(self, client, endpoint) -> None:
        """不可重试的失败直接进死信，不等重试次数耗尽。

        重试次数是留给"可能恢复"的故障的。4xx 不会自己变好，
        让它白等完剩余的重试预算只是浪费时间和队列空间。
        """
        event_id = await self._create_event(client, endpoint)
        event, ep = await self._load(event_id)

        worker = DeliveryWorker(worker_id="test-worker")
        await worker._apply_result(
            event,
            ep,
            make_result(ok=False, kind=FailureKind.CLIENT_ERROR, status_code=400, error="下游 400"),
            1,
        )

        fresh = await self._reload(event_id)
        assert fresh.status == EventStatus.DEAD.value
        assert fresh.completed_at is not None

    async def test_exhausting_attempts_goes_to_dead(self, client, endpoint) -> None:
        """用满重试次数后进入死信。"""
        event_id = await self._create_event(client, endpoint)
        event, ep = await self._load(event_id)

        worker = DeliveryWorker(worker_id="test-worker")
        await worker._apply_result(
            event,
            ep,
            make_result(ok=False, kind=FailureKind.SERVER_ERROR, status_code=503, error="下游 503"),
            ep.max_attempts,  # 已经用到最后一次
        )

        fresh = await self._reload(event_id)
        assert fresh.status == EventStatus.DEAD.value

    async def test_attempt_record_is_written(self, client, endpoint) -> None:
        """每次尝试都要单独留痕。

        只记录"最终失败了"是不够的——排查时需要知道是哪几次、
        什么时候、收到了什么响应。
        """
        event_id = await self._create_event(client, endpoint)
        event, ep = await self._load(event_id)

        worker = DeliveryWorker(worker_id="test-worker")
        await worker._apply_result(
            event, ep, make_result(ok=False, kind=FailureKind.SERVER_ERROR, status_code=503), 1
        )
        await worker._apply_result(event, ep, make_result(ok=True), 2)

        async with SessionFactory() as session:
            attempts = (
                (
                    await session.execute(
                        select(DeliveryAttempt)
                        .where(DeliveryAttempt.event_id == UUID(event_id))
                        .order_by(DeliveryAttempt.attempt_number)
                    )
                )
                .scalars()
                .all()
            )

        assert [a.attempt_number for a in attempts] == [1, 2]
        assert attempts[1].status_code == 200

    async def test_lease_is_released_on_failure(self, client, endpoint) -> None:
        """失败后必须解除租约。

        否则这条事件会在 locked_at 过期后被误判为僵尸任务而被重复回收。
        """
        event_id = await self._create_event(client, endpoint)
        event, ep = await self._load(event_id)

        worker = DeliveryWorker(worker_id="test-worker")
        await worker._apply_result(
            event, ep, make_result(ok=False, kind=FailureKind.SERVER_ERROR, status_code=500), 1
        )

        fresh = await self._reload(event_id)
        assert fresh.locked_at is None
        assert fresh.locked_by is None


class TestClaiming:
    async def _create_events(self, client, endpoint, count: int) -> None:
        for index in range(count):
            body = json_body({"event_id": f"evt-claim-{index}"})
            response = await client.post(
                f"/ingest/{endpoint['token']}",
                content=body,
                headers=sign_headers(endpoint["secret"], body),
            )
            assert response.status_code == 202, response.text

    async def test_claim_marks_events_as_delivering(self, client, endpoint) -> None:
        await self._create_events(client, endpoint, 3)

        worker = DeliveryWorker(worker_id="worker-a")
        claimed = await worker.claim_batch(batch_size=10)
        assert len(claimed) == 3

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
        # 认领即标记，同一事务内完成，避免"选中但未标记"的窗口
        assert all(e.status == EventStatus.DELIVERING.value for e in events)
        assert all(e.locked_by == "worker-a" for e in events)

    async def test_second_claim_gets_nothing(self, client, endpoint) -> None:
        """已被认领的任务不会被另一个 Worker 再取走。

        这是 SKIP LOCKED 的核心行为：并发的第二个事务直接跳过被锁定的行，
        而不是排队等待锁。
        """
        await self._create_events(client, endpoint, 2)

        first = DeliveryWorker(worker_id="worker-a")
        second = DeliveryWorker(worker_id="worker-b")

        claimed_first = await first.claim_batch(batch_size=10)
        claimed_second = await second.claim_batch(batch_size=10)

        assert len(claimed_first) == 2
        assert claimed_second == []

    async def test_future_events_are_not_claimed(self, client, endpoint) -> None:
        """还没到重试时间的事件不会被取走。"""
        await self._create_events(client, endpoint, 1)

        async with SessionFactory() as session:
            event = (
                await session.execute(
                    select(Event).where(Event.endpoint_id == UUID(endpoint["id"]))
                )
            ).scalar_one()
            event.next_attempt_at = datetime.now(UTC) + timedelta(minutes=10)
            await session.commit()

        worker = DeliveryWorker(worker_id="worker-a")
        assert await worker.claim_batch(batch_size=10) == []

    async def test_claim_returns_empty_when_queue_is_empty(self) -> None:
        worker = DeliveryWorker(worker_id="worker-a")
        assert await worker.claim_batch(batch_size=10) == []


class TestStaleReclaim:
    async def _make_stale(self, event_id: str) -> None:
        """把事件伪造成"被 Worker 取走后卡住"的状态。"""
        async with SessionFactory() as session:
            event = await session.get(Event, UUID(event_id))
            assert event is not None
            event.status = EventStatus.DELIVERING.value
            event.locked_at = datetime.now(UTC) - timedelta(hours=1)
            event.locked_by = "crashed-worker"
            await session.commit()

    async def test_stale_delivering_event_is_reclaimed(self, client, endpoint) -> None:
        """租约过期的任务被回收成 pending，重新可投递。

        不回收的话，Worker 崩溃时取走的那些事件状态永远停在 delivering，
        没有任何人会再碰它们——等于静默丢失。
        """
        body = json_body({"event_id": "evt-stale"})
        accepted = await client.post(
            f"/ingest/{endpoint['token']}",
            content=body,
            headers=sign_headers(endpoint["secret"], body),
        )
        event_id = accepted.json()["event_id"]
        await self._make_stale(event_id)

        worker = DeliveryWorker(worker_id="recovery-worker")
        reclaimed = await worker.reclaim_stale()
        assert reclaimed == 1

        async with SessionFactory() as session:
            event = await session.get(Event, UUID(event_id))
            assert event is not None
            assert event.status == EventStatus.PENDING.value
            assert event.locked_at is None
            assert event.locked_by is None

    async def test_reclaim_does_not_reset_attempt_count(self, client, endpoint) -> None:
        """回收不清零尝试次数。

        否则一个反复让 Worker 崩溃的事件会被无限重试下去：
        每次崩溃回收都把计数抹掉，重试预算永远用不完。
        """
        body = json_body({"event_id": "evt-stale-count"})
        accepted = await client.post(
            f"/ingest/{endpoint['token']}",
            content=body,
            headers=sign_headers(endpoint["secret"], body),
        )
        event_id = accepted.json()["event_id"]

        async with SessionFactory() as session:
            event = await session.get(Event, UUID(event_id))
            assert event is not None
            event.attempt_count = 3
            await session.commit()

        await self._make_stale(event_id)

        worker = DeliveryWorker(worker_id="recovery-worker")
        await worker.reclaim_stale()

        async with SessionFactory() as session:
            event = await session.get(Event, UUID(event_id))
            assert event is not None
            assert event.attempt_count == 3

    async def test_fresh_lease_is_not_reclaimed(self, client, endpoint) -> None:
        """还在租约期内的任务不能被抢走。

        否则正常投递中的事件会被另一个 Worker 重复投递，
        稳定的重复投递比偶尔失败严重得多。
        """
        body = json_body({"event_id": "evt-fresh"})
        accepted = await client.post(
            f"/ingest/{endpoint['token']}",
            content=body,
            headers=sign_headers(endpoint["secret"], body),
        )
        event_id = accepted.json()["event_id"]

        async with SessionFactory() as session:
            event = await session.get(Event, UUID(event_id))
            assert event is not None
            event.status = EventStatus.DELIVERING.value
            event.locked_at = datetime.now(UTC)
            event.locked_by = "working-worker"
            await session.commit()

        worker = DeliveryWorker(worker_id="recovery-worker")
        assert await worker.reclaim_stale() == 0
