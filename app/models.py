"""数据模型：四张表及其约束。

表结构按数据流向设计：

    users              使用本服务的账号，靠 API Key 鉴权
      └─ endpoints     一个接收地址 = 一个入站 token + 一个转发目标
           └─ events   收到的事件，同时也是投递队列里的一条"任务"
                └─ delivery_attempts   每一次投递尝试的结果记录

两个关键约束（面试高频话题）：

1. UNIQUE(endpoint_id, idempotency_key) —— 幂等的实现基础
   把唯一性交给数据库、而不是应用代码，是因为应用层的"先查询再插入"在并发下
   存在竞态：两个相同事件同时到达，两边都查到"不存在"，然后都插入成功。
   数据库唯一约束由存储引擎保证原子性，绕不过去。

   另外注意 PostgreSQL 的 UNIQUE 语义：NULL 与 NULL 不相等，因此没有提供
   idempotency_key 的多条事件可以共存。这正好符合"调用方没给幂等键就不去重"
   的预期行为，不需要额外写条件逻辑。

2. 部分索引 (status, next_attempt_at) WHERE status = 'pending' —— 队列扫描优化
   Worker 需要反复扫描"到期待投递"的事件。部分索引只收录 pending 状态的行，
   已投递完成的事件（占绝大多数历史数据）根本不进索引，于是索引体积小、
   写入时维护成本低、查询命中率高。MySQL 不支持部分索引，这是本项目选
   PostgreSQL 的实际理由之一。
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """所有模型的基类。

    它持有的 metadata 是 Alembic 自动生成迁移脚本的依据：
    只要模型类继承了 Base，建表语句就能被自动推导出来。
    """


class UUIDPrimaryKeyMixin:
    """UUID 主键。

    选 UUID 而不是自增整数，理由有两条：

    - 对外暴露的 ID 不泄露业务量。自增 ID 会让调用方推算出一共有多少条数据
      （我创建的事件 ID 是 8，说明系统总共只有 8 个事件）。
    - 多实例、多 Worker 并发写入时不会冲突，不依赖中心化的序列生成器。

    代价也要说清楚：UUID 的值分布随机，B-tree 索引的插入位置会散落在各处，
    高写入量下比顺序自增主键产生更多页分裂。本项目规模可以忽略；若 events 表
    增长到千万级，可以换成 UUIDv7 或 ULID 这类"时间有序"的变体把顺序性找回来。
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        # 数据库层默认值：即使有人直接写 SQL 插入，也不会漏掉主键
        server_default=text("gen_random_uuid()"),
        # Python 层默认值：对象构造后立刻拿得到 id，无需先 flush
        default=uuid.uuid4,
    )


class CreatedAtMixin:
    """创建时间。

    统一使用带时区的时间戳（timestamptz）。不带时区的 datetime 在跨时区部署、
    夏令时切换时会产生静默歧义，是生产事故的常见来源。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class TimestampMixin(CreatedAtMixin):
    """创建时间 + 更新时间。配置类数据会改，追踪改动时间便于排查。"""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class EventStatus(StrEnum):
    """事件在投递生命周期中的状态。

    用字符串枚举而不是 PostgreSQL 原生 ENUM 类型：原生 ENUM 增加取值需要
    ALTER TYPE，在迁移和回滚时都更麻烦。String + CHECK 约束同样能保证取值合法，
    且改起来只是改一行约束。
    """

    PENDING = "pending"
    """待投递，或等待下一次重试。Worker 扫描的就是这个状态。"""

    DELIVERING = "delivering"
    """已被某个 Worker 取走、正在投递。持有租约超时会被重新放回 pending。"""

    SUCCEEDED = "succeeded"
    """投递成功，终态。"""

    DEAD = "dead"
    """重试次数耗尽，进入死信，终态。可被人工重放。"""


class User(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """使用本服务的账号。"""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # 只存 API Key 的哈希：数据库万一泄露，攻击者拿到哈希也无法反推出可用的 Key
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # 明文前缀，仅用于在界面上辨识"这是哪一把 Key"，本身不具备鉴权能力
    api_key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    endpoints: Mapped[list["Endpoint"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        # 删除动作交给数据库的外键 ON DELETE CASCADE 执行，
        # 避免 ORM 把每条子记录先加载到内存再逐条删除
        passive_deletes=True,
    )


class Endpoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """接收地址：一个入站 token 对应一个转发目标。"""

    __tablename__ = "endpoints"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 拼接在接收 URL 里（/ingest/{token}），是定位这个 endpoint 的唯一凭据
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    target_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # HMAC 签名密钥用 Fernet 对称加密后入库：
    # 即使数据库被拖走，没有 SECRET_KEY 也解不开原文
    secret_encrypted: Mapped[str] = mapped_column(String(512), nullable=False)

    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("10"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    user: Mapped["User"] = relationship(back_populates="endpoints")
    events: Mapped[list["Event"]] = relationship(
        back_populates="endpoint",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(
            "max_attempts >= 1 AND max_attempts <= 20",
            name="ck_endpoints_max_attempts_range",
        ),
        CheckConstraint(
            "timeout_seconds >= 1 AND timeout_seconds <= 120",
            name="ck_endpoints_timeout_range",
        ),
        CheckConstraint(
            "length(name) > 0",
            name="ck_endpoints_name_not_empty",
        ),
    )


class Event(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """收到的事件，同时是投递队列中的一条任务。"""

    __tablename__ = "events"

    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("endpoints.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 用 JSONB 而不是 JSON：JSONB 以二进制存储，支持索引和按内部字段查询，
    # 写入时多做一次解析是值得的
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    headers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # 调用方通过请求头传入；为空表示这次调用不要求去重
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'pending'")
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # 下次可以投递的时间。首次入队时立即置为"现在"，让 Worker 马上取走
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 租约字段：Worker 取走任务时打上标记，防止多个 Worker 同时投递同一条，
    # 也用于回收"Worker 崩溃后卡在 delivering 状态"的僵尸任务
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    endpoint: Mapped["Endpoint"] = relationship(back_populates="events")
    attempts: Mapped[list["DeliveryAttempt"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        # 【约束一】幂等基础：同一 endpoint 下，同一个幂等键只允许存在一条事件
        UniqueConstraint(
            "endpoint_id",
            "idempotency_key",
            name="uq_events_endpoint_idempotency_key",
        ),
        # 状态取值约束：防止代码写错状态值悄悄入库
        CheckConstraint(
            "status IN ('pending', 'delivering', 'succeeded', 'dead')",
            name="ck_events_status_valid",
        ),
        # 【约束二】部分索引：只收录待投递的行。
        # Worker 的查询是：
        #   WHERE status = 'pending' AND next_attempt_at <= now()
        #   ORDER BY next_attempt_at
        #   FOR UPDATE SKIP LOCKED
        # 该索引让"找下一批要投递的任务"从全表扫描变成索引范围扫描。
        Index(
            "ix_events_pending_due",
            "status",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )


class DeliveryAttempt(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """单次投递尝试的记录。

    这张表只增不改，用来回答两类问题：
    - 排查：这次为什么失败？上游返回了什么？
    - 展示：这条事件总共试了几次、每次耗时多久？
    """

    __tablename__ = "delivery_attempts"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # 以下字段允许为空：连接超时、DNS 失败这类情况根本没有 HTTP 状态码
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    event: Mapped["Event"] = relationship(back_populates="attempts")

    __table_args__ = (
        # 同一事件下第几次尝试是唯一的，避免重试逻辑出 bug 时重复记录
        UniqueConstraint("event_id", "attempt_number", name="uq_attempts_event_number"),
    )
