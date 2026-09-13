"""请求与响应模型。

Pydantic 模型在这里承担两个职责：

1. 在业务逻辑之前校验外部输入。类型错误、邮箱格式非法、密码太短这类问题，
   应该在进入数据库操作之前就被拒绝，返回 422 并指明具体哪个字段有问题。
2. 生成 /docs 里展示的字段说明和示例，让接口文档自带使用说明。

一条硬性原则：**响应模型只暴露该暴露的字段**。
password_hash、api_key_hash、secret_encrypted 这类字段永远不出现在任何响应模型里。
宁可逐个字段显式声明，也不要图省事直接返回 ORM 对象——
那样一旦模型加了敏感字段，就会自动泄露出去。
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, EmailStr, Field


class RegisterRequest(BaseModel):
    """注册请求。"""

    email: EmailStr = Field(
        description="登录邮箱，同时也是账号唯一标识",
        examples=["dev@example.com"],
    )
    password: str = Field(
        min_length=8,
        max_length=128,
        description="密码，至少 8 位",
        examples=["a-strong-password"],
    )


class RegisterResponse(BaseModel):
    """注册响应。

    api_key 字段是这套系统里唯一的明文出现点：注册成功返回一次，
    之后服务端只保存哈希，再也没有办法取回。调用方必须自己存好。
    """

    id: UUID
    email: EmailStr
    api_key: str = Field(
        description="API Key 明文，仅此一次返回，请立即保存。服务端只存哈希，丢失无法找回。",
    )
    api_key_prefix: str = Field(
        description="用于在界面上辨识 Key 的前缀",
    )
    created_at: datetime


class UserResponse(BaseModel):
    """当前用户信息。刻意不包含任何哈希值。"""

    # from_attributes=True 让 Pydantic 能直接从 ORM 对象读取属性
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    api_key_prefix: str
    is_active: bool
    created_at: datetime


class EndpointCreateRequest(BaseModel):
    """创建接收地址。"""

    name: str = Field(
        min_length=1,
        max_length=100,
        description="名称，便于在列表里识别这个接收地址的用途",
        examples=["GitHub 推送通知"],
    )
    target_url: AnyHttpUrl = Field(
        description="事件要转发到的目标地址，必须是 http 或 https",
        examples=["https://example.com/webhook"],
    )
    max_attempts: int = Field(
        default=5,
        ge=1,
        le=20,
        description="最大投递尝试次数，含首次投递",
    )
    timeout_seconds: int = Field(
        default=10,
        ge=1,
        le=120,
        description="单次投递的超时时间（秒）",
    )


class EndpointUpdateRequest(BaseModel):
    """修改接收地址。

    所有字段都是可选的，只更新明确传了的字段。
    区分“没传这个字段”和“传了 null”需要额外处理，
    这里用 exclude_unset 在接口层过滤，未传的字段不会覆盖原值。
    """

    name: str | None = Field(default=None, min_length=1, max_length=100)
    target_url: AnyHttpUrl | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=20)
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    is_active: bool | None = Field(default=None, description="设置为 false 可暂停接收该地址的事件")


class EndpointResponse(BaseModel):
    """接收地址信息。

    签名密钥只返回拖码。完整密钥是一个可以伪造任意请求的凭据，
    不应当在每次查询时都拿出来传播一遍，降低泄露面。
    """

    id: UUID
    name: str
    token: str
    target_url: str
    secret_masked: str
    max_attempts: int
    timeout_seconds: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class EndpointCreatedResponse(EndpointResponse):
    """创建成功后的响应，额外包含两项只返回一次的信息。"""

    secret: str = Field(
        description="签名密钥明文，**仅此一次**返回。调用方用它计算 HMAC 签名。",
    )
    ingest_url: str = Field(
        description="入站接收地址，把第三方服务的 webhook 目标配到这里",
    )


class EndpointSecretResponse(BaseModel):
    """重置签名密钥后的响应。"""

    id: UUID
    secret: str = Field(description="新的签名密钥明文，**仅此一次**返回")
    secret_masked: str


class EventAcceptedResponse(BaseModel):
    """事件被接收后的响应。

    返回 202 而不是 200 是有意为之：202 的含义是“请求已接受处理，
    但尚未完成”。对调用方来说，拿到 202 只代表事件已安全落库，
    投递是稍后异步发生的事。
    """

    event_id: UUID
    endpoint_id: UUID
    status: str
    duplicate: bool = Field(
        description="true 表示该事件此前已经收到过，本次没有重复入队",
    )


class DeliveryAttemptResponse(BaseModel):
    """一次投递尝试的记录。"""

    model_config = ConfigDict(from_attributes=True)

    attempt_number: int
    status_code: int | None = Field(description="HTTP 状态码；连接失败或超时时为 null")
    response_body: str | None = Field(description="响应体前 1KB，便于排查对方返回了什么")
    error: str | None
    duration_ms: int | None
    created_at: datetime


class EventResponse(BaseModel):
    """事件摘要，用于列表展示。不含 payload，避免列表响应过大。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    endpoint_id: UUID
    status: str = Field(description="pending / delivering / succeeded / dead")
    attempt_count: int
    idempotency_key: str | None
    next_attempt_at: datetime | None = Field(description="下次可投递的时间，非 pending 时为 null")
    last_error: str | None
    created_at: datetime
    completed_at: datetime | None


class EventDetailResponse(EventResponse):
    """事件详情：额外包含原始内容与每一次投递的完整记录。"""

    payload: dict[str, Any]
    headers: dict[str, Any]
    attempts: list[DeliveryAttemptResponse]


class EventListResponse(BaseModel):
    """分页结果。

    同时返回 total，调用方不需要额外发一次请求才知道总数。
    """

    total: int
    limit: int
    offset: int
    items: list[EventResponse]


class EventReplayResponse(BaseModel):
    """死信重放的结果。"""

    event_id: UUID
    status: str
    scheduled_at: datetime = Field(description="重新入队的时间，Worker 下一轮就会取走")
    message: str


class StatsResponse(BaseModel):
    """账号维度的投递统计。"""

    endpoints: int = Field(description="接收地址数量")
    total_events: int = Field(description="累计接收的事件数")
    pending: int = Field(description="等待投递")
    delivering: int = Field(description="正在投递")
    succeeded: int = Field(description="投递成功")
    dead: int = Field(description="已放弃（死信）")
    success_rate: float = Field(
        description="成功率 = 成功 / （成功 + 死信）。进行中的事件不计入分母"
    )
    total_attempts: int = Field(description="累计投递尝试次数，含重试")
    avg_delivery_latency_ms: float | None = Field(
        default=None, description="平均投递耗时（毫秒），只统计真正发出请求的尝试"
    )
