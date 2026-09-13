"""HTTP 接口层测试。

这里验证的是接口契约：状态码、响应结构、鉴权边界和数据可见范围。
尤其是越权部分——多租户服务里最危险的漏洞就是"能查到别人的数据"，
所以每个资源都配了一条越权用例。
"""

import uuid

import pytest

from app.db import SessionFactory, get_session
from app.main import app
from app.models import Endpoint, Event
from app.services.delivery import DeliveryResult, FailureKind
from app.worker import DeliveryWorker
from tests.conftest import DEFAULT_TARGET_URL, TEST_PASSWORD, json_body, sign_headers


async def register_second_account(client) -> dict[str, str]:
    """再注册一个账号，用于验证账号之间的数据隔离。"""
    email = f"other-{uuid.uuid4().hex[:10]}@example.com"
    response = await client.post(
        "/api/auth/register", json={"email": email, "password": TEST_PASSWORD}
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['api_key']}"}


async def push_event(client, endpoint, payload: dict[str, object]) -> str:
    """按协议推送一条事件，返回事件 ID。"""
    body = json_body(payload)
    response = await client.post(
        f"/ingest/{endpoint['token']}",
        content=body,
        headers=sign_headers(endpoint["secret"], body),
    )
    assert response.status_code == 202, response.text
    return response.json()["event_id"]


async def mark_succeeded(client, endpoint, event_id: str) -> None:
    """把事件直接置为投递成功。

    真实投递需要下游服务在场，接口测试不应该依赖它；
    这里只关心事件处于某个状态时接口如何响应。
    """
    async with SessionFactory() as session:
        event = await session.get(Event, uuid.UUID(event_id))
        assert event is not None
        ep = await session.get(Endpoint, event.endpoint_id)
        assert ep is not None

    worker = DeliveryWorker(worker_id="test-worker")
    await worker._apply_result(
        event,
        ep,
        DeliveryResult(
            ok=True,
            kind=FailureKind.NONE,
            status_code=200,
            duration_ms=10,
            response_body="{}",
            error=None,
        ),
        1,
    )


class TestAuthentication:
    async def test_missing_credentials_returns_401(self, client) -> None:
        response = await client.get("/api/endpoints")
        assert response.status_code == 401

    async def test_wrong_api_key_returns_401(self, client) -> None:
        response = await client.get(
            "/api/endpoints", headers={"Authorization": "Bearer hr_totally-wrong-key"}
        )
        assert response.status_code == 401

    async def test_malformed_authorization_header_returns_401(self, client, account) -> None:
        """缺少 Bearer 前缀的请求头视为未认证。"""
        response = await client.get(
            "/api/endpoints", headers={"Authorization": account["api_key"]}
        )
        assert response.status_code == 401

    async def test_current_user_returns_account(self, client, auth_headers, account) -> None:
        response = await client.get("/api/auth/me", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["email"] == account["email"]


class TestRegister:
    async def test_duplicate_email_is_rejected(self, client, account) -> None:
        """邮箱已被占用时不能再注册。

        否则同一个人注册两次会得到两个账号，
        而他自己分不清哪把 Key 属于哪一边。
        """
        response = await client.post(
            "/api/auth/register", json={"email": account["email"], "password": TEST_PASSWORD}
        )
        assert response.status_code == 409

    async def test_short_password_is_rejected(self, client) -> None:
        response = await client.post(
            "/api/auth/register", json={"email": "short@example.com", "password": "1234567"}
        )
        assert response.status_code == 422

    async def test_invalid_email_is_rejected(self, client) -> None:
        response = await client.post(
            "/api/auth/register", json={"email": "not-an-email", "password": TEST_PASSWORD}
        )
        assert response.status_code == 422


class TestEndpointsCrud:
    async def test_create_returns_secret_and_ingest_url(self, client, auth_headers) -> None:
        response = await client.post(
            "/api/endpoints",
            headers=auth_headers,
            json={"name": "订单回调", "target_url": DEFAULT_TARGET_URL},
        )
        assert response.status_code == 201
        data = response.json()
        # 明文密钥只在这里返回一次，之后任何接口都只给掩码
        assert data["secret"]
        assert data["token"] in data["ingest_url"]
        assert "*" in data["secret_masked"]

    async def test_list_is_scoped_to_owner(self, client, auth_headers, endpoint) -> None:
        response = await client.get("/api/endpoints", headers=auth_headers)
        assert response.status_code == 200
        names = [item["name"] for item in response.json()]
        assert endpoint["name"] in names

    async def test_get_detail(self, client, auth_headers, endpoint) -> None:
        response = await client.get(f"/api/endpoints/{endpoint['id']}", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["id"] == endpoint["id"]

    async def test_update_name_only(self, client, auth_headers, endpoint) -> None:
        """只改名字时其他字段保持不变。

        这里用 exclude_unset 过滤未传字段，避免"没传的字段被写成默认值"。
        """
        response = await client.patch(
            f"/api/endpoints/{endpoint['id']}",
            headers=auth_headers,
            json={"name": "改名后"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "改名后"
        assert data["target_url"] == endpoint["target_url"]
        assert data["max_attempts"] == endpoint["max_attempts"]

    async def test_delete_then_not_found(self, client, auth_headers, endpoint) -> None:
        response = await client.delete(f"/api/endpoints/{endpoint['id']}", headers=auth_headers)
        assert response.status_code == 204

        again = await client.get(f"/api/endpoints/{endpoint['id']}", headers=auth_headers)
        assert again.status_code == 404

    async def test_other_account_cannot_see_endpoint(self, client, endpoint) -> None:
        """越权访问返回 404，而不是 403。

        403 等于承认"这个资源存在，只是你没权限"，攻击者据此可以枚举出
        系统里有哪些 ID。404 让"不存在"和"不属于你"从外部看没有区别。
        """
        other_headers = await register_second_account(client)
        response = await client.get(f"/api/endpoints/{endpoint['id']}", headers=other_headers)
        assert response.status_code == 404

    async def test_other_account_cannot_delete_endpoint(self, client, endpoint) -> None:
        other_headers = await register_second_account(client)
        response = await client.delete(f"/api/endpoints/{endpoint['id']}", headers=other_headers)
        assert response.status_code == 404

    async def test_invalid_target_url_is_rejected(self, client, auth_headers) -> None:
        """目标地址必须是合法的 http(s) URL。

        这里挡掉错误配置，比等到投递时才失败要好：用户能立刻知道配错了。
        """
        response = await client.post(
            "/api/endpoints",
            headers=auth_headers,
            json={"name": "坏地址", "target_url": "not-a-url"},
        )
        assert response.status_code == 422


class TestSecretRotation:
    async def test_rotate_returns_new_secret(self, client, auth_headers, endpoint) -> None:
        response = await client.post(
            f"/api/endpoints/{endpoint['id']}/secret", headers=auth_headers
        )
        assert response.status_code == 200
        new_secret = response.json()["secret"]
        assert new_secret != endpoint["secret"]

    async def test_old_secret_stops_working(self, client, auth_headers, endpoint) -> None:
        """轮换后旧密钥立刻失效。

        如果旧密钥还能用，轮换就失去了意义——泄露的密钥依旧能伪造事件。
        """
        await client.post(f"/api/endpoints/{endpoint['id']}/secret", headers=auth_headers)

        body = json_body({"event_id": "evt-old-secret"})
        response = await client.post(
            f"/ingest/{endpoint['token']}",
            content=body,
            headers=sign_headers(endpoint["secret"], body),
        )
        assert response.status_code == 401

    async def test_new_secret_works(self, client, auth_headers, endpoint) -> None:
        rotated = await client.post(
            f"/api/endpoints/{endpoint['id']}/secret", headers=auth_headers
        )
        new_secret = rotated.json()["secret"]

        body = json_body({"event_id": "evt-new-secret"})
        response = await client.post(
            f"/ingest/{endpoint['token']}",
            content=body,
            headers=sign_headers(new_secret, body),
        )
        assert response.status_code == 202


class TestEventsApi:
    async def test_list_returns_total(self, client, auth_headers, endpoint) -> None:
        await push_event(client, endpoint, {"event_id": "e1"})
        await push_event(client, endpoint, {"event_id": "e2"})

        response = await client.get("/api/events", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    async def test_pagination(self, client, auth_headers, endpoint) -> None:
        for index in range(3):
            await push_event(client, endpoint, {"event_id": f"page-{index}"})

        page = await client.get("/api/events?limit=2&offset=0", headers=auth_headers)
        rest = await client.get("/api/events?limit=2&offset=2", headers=auth_headers)

        # total 是总数而不是当前页条数：调用方不必再发一次请求才知道有多少条
        assert page.json()["total"] == 3
        assert len(page.json()["items"]) == 2
        assert len(rest.json()["items"]) == 1

    async def test_filter_by_status(self, client, auth_headers, endpoint) -> None:
        event_id = await push_event(client, endpoint, {"event_id": "done"})
        await mark_succeeded(client, endpoint, event_id)

        succeeded = await client.get("/api/events?status=succeeded", headers=auth_headers)
        pending = await client.get("/api/events?status=pending", headers=auth_headers)

        assert succeeded.json()["total"] == 1
        assert pending.json()["total"] == 0

    async def test_invalid_status_filter_is_rejected(self, client, auth_headers) -> None:
        response = await client.get("/api/events?status=not-a-status", headers=auth_headers)
        assert response.status_code == 422

    async def test_filter_by_endpoint(self, client, auth_headers, endpoint) -> None:
        await push_event(client, endpoint, {"event_id": "of-endpoint"})
        response = await client.get(
            f"/api/events?endpoint_id={endpoint['id']}", headers=auth_headers
        )
        assert response.json()["total"] == 1

    async def test_detail_includes_payload_and_attempts(
        self, client, auth_headers, endpoint
    ) -> None:
        event_id = await push_event(client, endpoint, {"event_id": "detail", "amount": 7})
        await mark_succeeded(client, endpoint, event_id)

        response = await client.get(f"/api/events/{event_id}", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["payload"]["amount"] == 7
        assert data["status"] == "succeeded"
        assert len(data["attempts"]) == 1
        assert data["attempts"][0]["status_code"] == 200

    async def test_unknown_event_returns_404(self, client, auth_headers) -> None:
        response = await client.get(f"/api/events/{uuid.uuid4()}", headers=auth_headers)
        assert response.status_code == 404

    async def test_malformed_event_id_returns_422(self, client, auth_headers) -> None:
        """ID 不是合法 UUID 时返回 422，而不是 500。

        这是外部可控的路径参数，直接丢给数据库会触发类型错误。
        """
        response = await client.get("/api/events/not-a-uuid", headers=auth_headers)
        assert response.status_code == 422

    async def test_other_account_cannot_read_event(self, client, endpoint) -> None:
        event_id = await push_event(client, endpoint, {"event_id": "private"})
        other_headers = await register_second_account(client)

        response = await client.get(f"/api/events/{event_id}", headers=other_headers)
        assert response.status_code == 404

    async def test_other_account_list_is_empty(self, client, endpoint) -> None:
        await push_event(client, endpoint, {"event_id": "mine"})
        other_headers = await register_second_account(client)

        response = await client.get("/api/events", headers=other_headers)
        assert response.json()["total"] == 0


class TestReplay:
    async def test_replay_succeeded_event(self, client, auth_headers, endpoint) -> None:
        event_id = await push_event(client, endpoint, {"event_id": "replay-me"})
        await mark_succeeded(client, endpoint, event_id)

        response = await client.post(f"/api/events/{event_id}/replay", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["status"] == "pending"

    async def test_replay_resets_attempt_count(self, client, auth_headers, endpoint) -> None:
        """重放要把尝试次数清零。

        否则事件一出队就立刻超出重试上限，直接被判定为死信，
        用户会看到一个"重放成功但什么都没发生"的结果。
        """
        event_id = await push_event(client, endpoint, {"event_id": "reset-count"})
        await mark_succeeded(client, endpoint, event_id)

        await client.post(f"/api/events/{event_id}/replay", headers=auth_headers)

        async with SessionFactory() as session:
            event = await session.get(Event, uuid.UUID(event_id))
            assert event is not None
            assert event.attempt_count == 0

    async def test_replay_keeps_history(self, client, auth_headers, endpoint) -> None:
        """重放不清除历史投递记录。

        排查问题时需要看到"上一次为什么失败"，
        抹掉历史等于把唯一的证据丢了。
        """
        event_id = await push_event(client, endpoint, {"event_id": "keep-history"})
        await mark_succeeded(client, endpoint, event_id)
        await client.post(f"/api/events/{event_id}/replay", headers=auth_headers)

        detail = await client.get(f"/api/events/{event_id}", headers=auth_headers)
        assert len(detail.json()["attempts"]) == 1

    async def test_replay_bumps_generation(self, client, auth_headers, endpoint) -> None:
        """重放递增轮次编号。

        轮次是重放与唯一约束共存的解法：新一轮的尝试编号从 1 重新开始，
        但 (事件, 轮次, 编号) 三元组依然唯一，两条历史记录不会撞车。
        """
        event_id = await push_event(client, endpoint, {"event_id": "bump-gen"})
        await mark_succeeded(client, endpoint, event_id)
        await client.post(f"/api/events/{event_id}/replay", headers=auth_headers)

        async with SessionFactory() as session:
            event = await session.get(Event, uuid.UUID(event_id))
            assert event is not None
            assert event.attempt_generation == 1

    async def test_cannot_replay_pending_event(self, client, auth_headers, endpoint) -> None:
        """正在投递的事件不允许重放，返回 409。

        否则会出现同一条事件被两个流程处理，重复投递给下游。
        """
        event_id = await push_event(client, endpoint, {"event_id": "still-pending"})
        response = await client.post(f"/api/events/{event_id}/replay", headers=auth_headers)
        assert response.status_code == 409

    async def test_other_account_cannot_replay(self, client, endpoint) -> None:
        event_id = await push_event(client, endpoint, {"event_id": "not-yours"})
        await mark_succeeded(client, endpoint, event_id)
        other_headers = await register_second_account(client)

        response = await client.post(f"/api/events/{event_id}/replay", headers=other_headers)
        assert response.status_code == 404


class TestStats:
    async def test_counts_reflect_events(self, client, auth_headers, endpoint) -> None:
        succeeded_id = await push_event(client, endpoint, {"event_id": "ok"})
        await push_event(client, endpoint, {"event_id": "waiting"})
        await mark_succeeded(client, endpoint, succeeded_id)

        response = await client.get("/api/stats", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["endpoints"] == 1
        assert data["total_events"] == 2
        assert data["succeeded"] == 1
        assert data["pending"] == 1
        assert data["dead"] == 0
        assert data["total_attempts"] == 1

    async def test_success_rate_excludes_in_flight(self, client, auth_headers, endpoint) -> None:
        """成功率的分母只算已终结的事件。

        把还在排队的事件算进分母，会让刚接入的用户看到一个很低的成功率，
        而实际上什么都还没失败。
        """
        succeeded_id = await push_event(client, endpoint, {"event_id": "ok"})
        await push_event(client, endpoint, {"event_id": "waiting"})
        await mark_succeeded(client, endpoint, succeeded_id)

        response = await client.get("/api/stats", headers=auth_headers)
        assert response.json()["success_rate"] == 1.0

    async def test_empty_account_has_zero_rate(self, client, auth_headers) -> None:
        """没有任何事件时成功率为 0，而不是抛除零错误。"""
        response = await client.get("/api/stats", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["success_rate"] == 0.0

    async def test_stats_are_scoped_to_owner(self, client, endpoint) -> None:
        await push_event(client, endpoint, {"event_id": "mine"})
        other_headers = await register_second_account(client)

        response = await client.get("/api/stats", headers=other_headers)
        assert response.json()["total_events"] == 0


@pytest.mark.parametrize("path", ["/health", "/metrics"])
class TestOperationalEndpoints:
    async def test_no_auth_required(self, client, path: str) -> None:
        """健康检查和指标端点不能要求鉴权。

        监控系统在服务异常时无法先登录再探活；
        这两个端点也不返回任何业务数据，公开是安全的。
        """
        response = await client.get(path)
        assert response.status_code == 200

    async def test_metrics_exposes_ingest_counter(self, client, path: str) -> None:
        if path != "/metrics":
            return
        await client.get("/health")
        response = await client.get("/metrics")
        assert "hookrelay_ingest_total" in response.text


class TestReadiness:
    async def test_ready_when_database_reachable(self, client) -> None:
        response = await client.get("/health/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["database"] == "up"

    async def test_not_ready_when_database_unreachable(self, client) -> None:
        """数据库不通时返回 503，而不是 200。

        就绪检查必须真的去查一下数据库。如果它只回答"进程还活着"，
        负载均衡会把流量继续送进来，然后每个请求都失败。
        """

        class BrokenSession:
            async def execute(self, *args: object, **kwargs: object) -> None:
                raise RuntimeError("数据库不可达")

        async def broken_session():
            yield BrokenSession()

        app.dependency_overrides[get_session] = broken_session
        try:
            response = await client.get("/health/ready")
        finally:
            # 必须清理：依赖覆盖是全局的，残留会影响后续所有测试
            app.dependency_overrides.clear()

        assert response.status_code == 503
        assert response.json()["database"] == "down"
