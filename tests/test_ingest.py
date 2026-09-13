"""入站接收入口测试。

`POST /ingest/{token}` 是整个服务唯一的匿名写入点，也是最主要的攻击面。
这里把六道检查逐条验证，包括各种"差一点就对"的输入。
"""

import time
from urllib.parse import urlparse

import pytest

from app.api import ingest as ingest_module
from tests.conftest import json_body, sign_headers


def ingest_path(endpoint: dict[str, str]) -> str:
    """从入站地址里取出路径部分。

    ASGITransport 直接调用 ASGI 应用，不经过网络，
    所以只需要路径，不关心 ingest_url 里的主机名。
    """
    return urlparse(endpoint["ingest_url"]).path


def header_value(headers: dict[str, str], name: str) -> str | None:
    """按不区分大小写的方式取请求头。

    ASGI 规范要求头名一律小写，所以入库的都是小写形式。
    HTTP 头本身大小写不敏感，测试不应该依赖具体写法。
    """
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


@pytest.fixture(autouse=True)
def _reset_limiter() -> None:
    """清空限流计数。

    限流器是模块级单例，计数按 endpoint 累积。不清理的话，
    测试之间会互相影响——后面的用例可能因为前面的请求被限流而失败，
    而且失败信息会指向一个完全无关的地方。
    """
    ingest_module._ingest_limiter.reset()
    yield
    ingest_module._ingest_limiter.reset()


class TestHappyPath:
    async def test_accepts_correctly_signed_event(self, client, endpoint) -> None:
        body = json_body({"event_id": "evt-1", "amount": 100})
        response = await client.post(
            ingest_path(endpoint),
            content=body,
            headers=sign_headers(endpoint["secret"], body),
        )
        assert response.status_code == 202
        data = response.json()
        assert data["duplicate"] is False
        assert data["status"] == "pending"

    async def test_returns_location_header(self, client, endpoint) -> None:
        """202 的响应要告诉调用方去哪里查这条事件。

        异步接口只返回"收到了"是不够的，调用方需要能追踪后续结果，
        否则它只能靠轮询列表接口。
        """
        body = json_body({"event_id": "evt-location"})
        response = await client.post(
            ingest_path(endpoint),
            content=body,
            headers=sign_headers(endpoint["secret"], body),
        )
        assert response.headers["Location"] == f"/api/events/{response.json()['event_id']}"

    async def test_accepts_non_json_body(self, client, endpoint) -> None:
        """非 JSON 的请求体也要能收下。

        第三方发来的不一定都是 JSON，把内容丢掉等于让用户排查不了问题。
        """
        body = b"plain text payload"
        response = await client.post(
            ingest_path(endpoint),
            content=body,
            headers={
                **sign_headers(endpoint["secret"], body),
                "Content-Type": "text/plain",
            },
        )
        assert response.status_code == 202


class TestTokenChecks:
    async def test_unknown_token_returns_404(self, client) -> None:
        body = json_body({"event_id": "evt"})
        response = await client.post(
            "/ingest/this-token-does-not-exist",
            content=body,
            headers=sign_headers("any-secret", body),
        )
        assert response.status_code == 404

    async def test_disabled_endpoint_returns_403(self, client, endpoint, auth_headers) -> None:
        """停用的接收地址拒收新事件。

        这是"紧急刹车"：下游出问题时，用户可以立刻停止接收，
        而不是等队列里堆积成千上万条注定失败的事件。
        """
        patch = await client.patch(
            f"/api/endpoints/{endpoint['id']}",
            headers=auth_headers,
            json={"is_active": False},
        )
        assert patch.status_code == 200

        body = json_body({"event_id": "evt-disabled"})
        response = await client.post(
            ingest_path(endpoint),
            content=body,
            headers=sign_headers(endpoint["secret"], body),
        )
        assert response.status_code == 403


class TestSignatureChecks:
    async def test_wrong_signature_returns_401(self, client, endpoint) -> None:
        body = json_body({"event_id": "evt"})
        response = await client.post(
            ingest_path(endpoint),
            content=body,
            headers=sign_headers("wrong-secret", body),
        )
        assert response.status_code == 401

    async def test_tampered_body_returns_401(self, client, endpoint) -> None:
        """签名对得上但内容被改过，必须拒绝。

        这正是签名的意义所在：中间人即使篡改了请求体，
        因为拿不到密钥，也算不出与新内容匹配的签名。
        """
        original = json_body({"event_id": "evt", "amount": 1})
        tampered = json_body({"event_id": "evt", "amount": 999999})
        response = await client.post(
            ingest_path(endpoint),
            content=tampered,
            headers=sign_headers(endpoint["secret"], original),
        )
        assert response.status_code == 401

    async def test_missing_signature_header_returns_401(self, client, endpoint) -> None:
        body = json_body({"event_id": "evt"})
        headers = sign_headers(endpoint["secret"], body)
        del headers["X-HookRelay-Signature"]
        response = await client.post(ingest_path(endpoint), content=body, headers=headers)
        assert response.status_code == 401

    async def test_missing_timestamp_header_returns_401(self, client, endpoint) -> None:
        body = json_body({"event_id": "evt"})
        headers = sign_headers(endpoint["secret"], body)
        del headers["X-HookRelay-Timestamp"]
        response = await client.post(ingest_path(endpoint), content=body, headers=headers)
        assert response.status_code == 401

    async def test_stale_timestamp_returns_401(self, client, endpoint) -> None:
        """过期时间戳被拒绝，防止攻击者录下合法请求后反复重放。"""
        stale = str(int(time.time()) - 3600)
        body = json_body({"event_id": "evt"})
        response = await client.post(
            ingest_path(endpoint),
            content=body,
            headers=sign_headers(endpoint["secret"], body, timestamp=stale),
        )
        assert response.status_code == 401

    async def test_future_timestamp_returns_401(self, client, endpoint) -> None:
        """未来时间戳同样拒绝。

        如果只拦"太旧"，攻击者可以把时间戳设到很远的未来，
        让这条请求在很长时间内都能通过校验。
        """
        future = str(int(time.time()) + 3600)
        body = json_body({"event_id": "evt"})
        response = await client.post(
            ingest_path(endpoint),
            content=body,
            headers=sign_headers(endpoint["secret"], body, timestamp=future),
        )
        assert response.status_code == 401


class TestSizeLimit:
    async def test_oversized_body_returns_413(self, client, endpoint) -> None:
        """超过体积上限返回 413。"""
        body = b"x" * (1024 * 1024 + 1)
        response = await client.post(
            ingest_path(endpoint),
            content=body,
            headers={
                **sign_headers(endpoint["secret"], body),
                "Content-Type": "text/plain",
            },
        )
        assert response.status_code == 413

    async def test_body_at_limit_is_accepted(self, client, endpoint) -> None:
        """正好等于上限的请求体应当放行。

        边界用例值得单独测：把上限判定写成 `<` 而不是 `<=`，
        会让"刚好卡在上限"的合法请求被拒，而这种问题在日常使用中很难发现。
        """
        body = b"x" * (1024 * 1024)
        response = await client.post(
            ingest_path(endpoint),
            content=body,
            headers={
                **sign_headers(endpoint["secret"], body),
                "Content-Type": "text/plain",
            },
        )
        assert response.status_code == 202


class TestIdempotency:
    async def test_same_event_id_is_deduplicated(self, client, endpoint) -> None:
        """同一个 event_id 连发两次，只产生一条事件。"""
        body = json_body({"event_id": "evt-same", "amount": 10})
        headers = sign_headers(endpoint["secret"], body)

        first = await client.post(ingest_path(endpoint), content=body, headers=headers)
        second = await client.post(ingest_path(endpoint), content=body, headers=headers)

        assert first.status_code == second.status_code == 202
        assert first.json()["duplicate"] is False
        assert second.json()["duplicate"] is True
        # 两次返回同一个事件 ID，调用方据此知道"你重试的那条我已经收过了"
        assert first.json()["event_id"] == second.json()["event_id"]

    async def test_duplicate_returns_202_not_409(self, client, endpoint) -> None:
        """重复事件返回 202 而不是 409。

        对调用方来说"事件已被接收"这个事实没有变，重复是它自己重试造成的。
        返回 409 会诱导调用方去改代码处理"冲突"，而正确做法是什么都不用做。
        """
        body = json_body({"event_id": "evt-repeat"})
        headers = sign_headers(endpoint["secret"], body)
        await client.post(ingest_path(endpoint), content=body, headers=headers)
        response = await client.post(ingest_path(endpoint), content=body, headers=headers)
        assert response.status_code == 202

    async def test_same_body_without_id_is_deduplicated(self, client, endpoint) -> None:
        """没有 event_id 时，完全相同的请求体视为同一条事件。

        这是兜底策略：调用方没给任何幂等标识，那么两次内容完全一致的请求，
        通常就是同一次投递的重试。
        """
        body = json_body({"amount": 42, "note": "no id here"})
        headers = sign_headers(endpoint["secret"], body)
        first = await client.post(ingest_path(endpoint), content=body, headers=headers)
        second = await client.post(ingest_path(endpoint), content=body, headers=headers)
        assert second.json()["duplicate"] is True
        assert first.json()["event_id"] == second.json()["event_id"]

    async def test_different_bodies_are_separate_events(self, client, endpoint) -> None:
        """内容不同就是两条独立事件，不能被误判为重复。"""
        first_body = json_body({"amount": 1})
        second_body = json_body({"amount": 2})
        first = await client.post(
            ingest_path(endpoint),
            content=first_body,
            headers=sign_headers(endpoint["secret"], first_body),
        )
        second = await client.post(
            ingest_path(endpoint),
            content=second_body,
            headers=sign_headers(endpoint["secret"], second_body),
        )
        assert first.json()["event_id"] != second.json()["event_id"]

    async def test_same_event_id_on_different_endpoints_is_independent(
        self, client, endpoint, auth_headers
    ) -> None:
        """幂等作用域是"每个接收地址内"，不是全局。

        不同来源用各自的 event_id 命名空间，两个不同的接收地址
        凑巧用了相同 ID 时不应该互相影响。
        """
        other = (
            await client.post(
                "/api/endpoints",
                headers=auth_headers,
                json={"name": "第二个地址", "target_url": "http://127.0.0.1:9000/sink"},
            )
        ).json()

        body = json_body({"event_id": "shared-id"})
        first = await client.post(
            ingest_path(endpoint),
            content=body,
            headers=sign_headers(endpoint["secret"], body),
        )
        second = await client.post(
            ingest_path(other),
            content=body,
            headers=sign_headers(other["secret"], body),
        )

        assert first.json()["duplicate"] is False
        assert second.json()["duplicate"] is False
        assert first.json()["event_id"] != second.json()["event_id"]

    async def test_idempotency_header_takes_priority(self, client, endpoint) -> None:
        """显式的 Idempotency-Key 请求头优先级高于请求体里的字段。"""
        body = json_body({"event_id": "inner-id"})
        headers = {**sign_headers(endpoint["secret"], body), "X-HookRelay-Idempotency-Key": "outer"}

        first = await client.post(ingest_path(endpoint), content=body, headers=headers)
        # 换掉请求体里的 event_id，但幂等键不变 → 仍视为同一条
        changed = json_body({"event_id": "another-id"})
        second = await client.post(
            ingest_path(endpoint),
            content=changed,
            headers={
                **sign_headers(endpoint["secret"], changed),
                "X-HookRelay-Idempotency-Key": "outer",
            },
        )

        assert first.json()["event_id"] == second.json()["event_id"]


class TestRateLimit:
    async def test_exceeding_limit_returns_429(self, client, endpoint, monkeypatch) -> None:
        """超过限流阈值返回 429，并给出 Retry-After。"""
        monkeypatch.setattr(ingest_module._ingest_limiter, "_limit", 2)
        body = json_body({"event_id": "evt-limit"})
        headers = sign_headers(endpoint["secret"], body)

        for _ in range(2):
            response = await client.post(ingest_path(endpoint), content=body, headers=headers)
            assert response.status_code == 202

        blocked = await client.post(ingest_path(endpoint), content=body, headers=headers)
        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers

    async def test_rate_limit_is_per_endpoint(
        self, client, endpoint, auth_headers, monkeypatch
    ) -> None:
        """限流按接收地址独立计数。

        按 IP 限流会误伤共享出口的调用方；按 endpoint 计数才准确反映
        "某个接收地址被打了多少流量"。
        """
        monkeypatch.setattr(ingest_module._ingest_limiter, "_limit", 1)

        other = (
            await client.post(
                "/api/endpoints",
                headers=auth_headers,
                json={"name": "另一个地址", "target_url": "http://127.0.0.1:9000/sink"},
            )
        ).json()

        first_body = json_body({"event_id": "a"})
        assert (
            await client.post(
                ingest_path(endpoint),
                content=first_body,
                headers=sign_headers(endpoint["secret"], first_body),
            )
        ).status_code == 202
        # 第一个地址已用满额度
        assert (
            await client.post(
                ingest_path(endpoint),
                content=first_body,
                headers=sign_headers(endpoint["secret"], first_body),
            )
        ).status_code == 429

        # 第二个地址不受影响
        second_body = json_body({"event_id": "b"})
        assert (
            await client.post(
                ingest_path(other),
                content=second_body,
                headers=sign_headers(other["secret"], second_body),
            )
        ).status_code == 202


class TestStoredData:
    async def test_headers_are_stored_without_credentials(
        self, client, endpoint, auth_headers
    ) -> None:
        """入库的请求头不能包含调用方凭据。

        这些头会被原样转发给下游，也会出现在查询接口里。
        带上 Authorization 就等于把调用方的凭据散播到了本服务的数据库和日志中。
        """
        body = json_body({"event_id": "evt-headers"})
        headers = {
            **sign_headers(endpoint["secret"], body),
            "Authorization": "Bearer caller-owned-token",
            "Cookie": "session=caller-session",
            "X-Custom-Trace": "trace-123",
        }
        accepted = await client.post(ingest_path(endpoint), content=body, headers=headers)
        event_id = accepted.json()["event_id"]

        detail = await client.get(f"/api/events/{event_id}", headers=auth_headers)
        stored = detail.json()["headers"]

        assert header_value(stored, "Authorization") is None
        assert header_value(stored, "Cookie") is None
        # 自有的签名头也不保留：那是给本服务校验用的，出站会用 endpoint 密钥重签
        assert header_value(stored, "X-HookRelay-Signature") is None
        # 业务相关的自定义头要保留，排查问题时用得上
        assert header_value(stored, "X-Custom-Trace") == "trace-123"
