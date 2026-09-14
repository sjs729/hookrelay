# HookRelay

> Webhook 中继与可靠投递服务 —— 接收即返回，再可靠、幂等、可观测地异步转发。

[![CI](https://github.com/sjs729/hookrelay/actions/workflows/ci.yml/badge.svg)](https://github.com/sjs729/hookrelay/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.12-3776ab?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.141-009688?logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169e1?logo=postgresql&logoColor=white)
![Tests](https://img.shields.io/badge/tests-185%20passed-brightgreen)
![Coverage](https://img.shields.io/badge/coverage-95%25-brightgreen)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

---

## 这个项目解决什么问题

直接用 HTTP 请求同步转发 Webhook，会同时踩到四个坑：

1. **对端慢，你跟着慢。** 目标地址响应要 8 秒，你的接口就要挂 8 秒；上游早就超时重发了，于是同一个事件被处理两遍。
2. **对端挂了，事件就丢了。** 目标地址返回 502 的那一刻，那条事件没有任何记录留下，也没有第二次机会。
3. **无法追溯。** 事后对账时你想知道"3 号那条订单事件到底投出去没有、对端返回了什么"，答不上来。
4. **重复投递无法防御。** 上游超时重发是 Webhook 世界里的常态，但下游往往没做去重。

HookRelay 的做法是**把"接收"和"投递"拆开**：入站接口只负责校验签名、落库、立刻返回 `202`，实际转发交给后台 Worker 异步完成，失败自动退避重试，最终仍失败则进入死信等待人工重放。

## 核心特性

| 能力 | 说明 |
| --- | --- |
| **接收即返回** | 入站接口只做校验与落库，平均响应在毫秒级，不受目标地址快慢影响 |
| **签名验证** | HMAC-SHA256 签名 + 时间戳容忍窗口，拒绝伪造与重放 |
| **幂等去重** | 幂等键唯一索引 + 请求体 SHA-256 兜底，同一事件不会被重复接收 |
| **自动重试** | 指数退避 + 随机抖动，区分可重试与不可重试的失败类型 |
| **死信队列** | 重试耗尽后进入死信，不自动恢复，支持人工重放且不污染历史记录 |
| **完整可观测** | 结构化 JSON 日志带 `request_id` 贯穿链路，Prometheus 指标，投递历史逐次可查 |
| **崩溃安全** | 任务加锁带超时，进程被杀后遗留的僵尸任务会被自动回收重投 |

## 架构

```mermaid
flowchart TB
    SRC["第三方服务<br/>订单 / 支付 / CI"]
    WEB["Web 服务<br/>FastAPI · Uvicorn"]
    DB[("PostgreSQL 16<br/>events 表兼作任务队列")]
    WK["Worker 进程<br/>独立进程启动"]
    DST["目标地址<br/>你的业务接口"]

    SRC -->|"① 事件 + HMAC 签名"| WEB
    WEB -.->|"② 202 立即返回"| SRC
    WEB -->|"③ 落库 pending"| DB
    DB -->|"④ SKIP LOCKED 取任务"| WK
    WK -->|"⑤ HTTP 转发 + Delivery-Id"| DST
    WK -->|"⑥ 写投递结果"| DB

    classDef store fill:#f5f5f5,stroke:#666,stroke-width:2px
    classDef proc fill:#e3f2fd,stroke:#1976d2,stroke-width:2px
    class DB store
    class WEB,WK proc
```

两个进程共享同一个数据库，**队列就是 `events` 表本身**，不引入 Redis 或消息中间件。这个取舍的理由见[设计决策](#为什么不用-redis--celery--kafka)。

## 技术栈

| 层次 | 选型 | 说明 |
| --- | --- | --- |
| Web 框架 | FastAPI 0.141 | 原生 async，自带 OpenAPI 文档 |
| ASGI 服务器 | Uvicorn 0.52 | `uvicorn[standard]`，含 `uvloop` |
| ORM | SQLAlchemy 2.0 (async) | 全异步 session，配合 `asyncpg` 驱动 |
| 数据库 | PostgreSQL 16 | JSONB、部分索引、`FOR UPDATE SKIP LOCKED` |
| 迁移 | Alembic 1.20 | 版本化 schema 变更 |
| 密码哈希 | pwdlib + Argon2 | 抗 GPU 暴力破解 |
| 字段加密 | cryptography (Fernet) | 下游签名密钥在库里加密存储 |
| HTTP 客户端 | httpx 0.28 | 异步投递，可配超时 |
| 指标 | prometheus-client | `/metrics` 暴露 Prometheus 格式 |
| 依赖管理 | uv | 锁文件保证可复现构建 |
| 质量 | ruff + pytest | 静态检查 + 185 个测试用例 |

## 快速开始

### 方式一：Docker Compose（推荐）

只需要 Docker，无需本地安装 Python 和 PostgreSQL：

```bash
git clone https://github.com/sjs729/hookrelay.git
cd hookrelay
docker compose up -d
```

启动后：

| 地址 | 用途 |
| --- | --- |
| http://127.0.0.1:8000/docs | 交互式 API 文档 |
| http://127.0.0.1:8000/health | 存活检查 |
| http://127.0.0.1:8000/health/ready | 就绪检查（含数据库连通性） |
| http://127.0.0.1:8000/metrics | Prometheus 指标 |
| http://127.0.0.1:9101/metrics | Worker 进程指标（投递耗时等） |

`start.sh` 会自动跑迁移、在后台拉起 Worker，再把 Web 服务放在前台接管信号。

### 方式二：本地开发

需要 Python 3.12+ 和本地 PostgreSQL：

```bash
# 安装依赖（uv 会自动创建虚拟环境）
uv sync

# 准备配置
cp .env.example .env
# 编辑 .env，填入你的 DATABASE_URL 与 SECRET_KEY
# SECRET_KEY 生成方式：python -c "import secrets; print(secrets.token_urlsafe(48))"

# 建库并跑迁移
createdb hookrelay_dev
uv run alembic upgrade head

# 启动 Web 服务
uv run uvicorn app.main:app --reload

# 另开一个终端启动 Worker
uv run python -m app.worker
```

### 端到端演示

仓库自带一个 12 步的演示脚本，会用真实 HTTP 请求走完整条链路——包括成功投递、伪造签名被拒、幂等去重、503 退避重试、死信重放等：

```bash
# 终端 1：启动一个用于接收的本地服务
uv run python scripts/demo_sink.py --port 9000

# 终端 2：跑演示
uv run python scripts/demo.py
```

每一步都会打印"在做什么"和"为什么"，比读文档直观。详细说明见 [`docs/USAGE.md`](docs/USAGE.md)。

## 使用示例

### 1. 注册账号

```bash
curl -s -X POST http://127.0.0.1:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "password": "Your-Password-123"}'
```

响应里的 `api_key` **明文只出现这一次**，服务端只存哈希，请立即保存。后续所有管理接口都用它鉴权：

```text
Authorization: Bearer <api_key>
```

### 2. 创建接收地址

```bash
curl -s -X POST http://127.0.0.1:8000/api/endpoints \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "订单事件",
    "target_url": "https://your-app.example.com/webhooks/orders",
    "max_attempts": 5,
    "timeout_seconds": 10
  }'
```

响应会给出 `ingest_url`（接收地址）和 `secret`（签名密钥），**`secret` 同样只明文返回一次**。

### 3. 发送带签名的事件

签名对象是 `时间戳 + "." + 原始请求体`，算法 HMAC-SHA256：

```python
import hashlib
import hmac
import json
import time

import httpx

secret = "步骤 2 拿到的 secret"
ingest_url = "步骤 2 拿到的 ingest_url"

body = json.dumps({"order_id": 1001}, ensure_ascii=False).encode()
timestamp = str(int(time.time()))
signature = (
    "sha256="
    + hmac.new(
        secret.encode(),
        f"{timestamp}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()
)

httpx.post(
    ingest_url,
    content=body,
    headers={
        "Content-Type": "application/json",
        "X-HookRelay-Timestamp": timestamp,
        "X-HookRelay-Signature": signature,
        # 同一个幂等键重复提交只会入库一次
        "X-HookRelay-Idempotency-Key": "order-1001-created",
    },
)
```

返回 `202 Accepted` 表示事件已可靠入库，此时并未投递到目标地址。

### 4. 查看投递情况

```bash
# 事件列表（可按状态过滤）
curl -s "http://127.0.0.1:8000/api/events?status=dead" \
  -H "Authorization: Bearer $API_KEY"

# 单个事件的每一次投递尝试
curl -s http://127.0.0.1:8000/api/events/$EVENT_ID \
  -H "Authorization: Bearer $API_KEY"

# 汇总统计
curl -s http://127.0.0.1:8000/api/stats \
  -H "Authorization: Bearer $API_KEY"
```

### 5. 重放死信

目标地址修好之后，把死信事件重新放回队列：

```bash
curl -s -X POST http://127.0.0.1:8000/api/events/$EVENT_ID/replay \
  -H "Authorization: Bearer $API_KEY"
```

## 设计决策

这一节记录几个关键取舍。每个决策都附带代价——只讲好处不讲代价的设计说明没有参考价值。

#### 为什么不用 Redis / Celery / Kafka？

因为在这个规模下，额外的基础设施是**负债而不是资产**。

用消息中间件意味着：多一个必须部署、必须监控、必须处理故障的组件；队列里的消息与数据库里的业务数据处于两个系统，无法用同一个事务保证一致；本地开发要额外起一个容器。

而 PostgreSQL 的 `FOR UPDATE SKIP LOCKED` 恰好提供了队列需要的语义：**多消费者并发取任务且互不阻塞**。更关键的是，事件入库和取任务在同一个数据库里，天然共享事务边界——不会出现"任务入队成功但业务记录没写"这类需要补偿逻辑的中间状态。

**什么情况下该换掉它？** 当单表写入接近瓶颈时。队列表是持续 `INSERT` + `DELETE` 的模式，会产生大量死元组，需要靠 autovacuum 回收。当前压测下（见[性能](#性能)）单机入站约 380 事件/秒，离 PostgreSQL 单表写入上限还有距离，但这不是无限的。真要横向扩展，第一个改动点是把表按 `endpoint_id` 分区。

#### 为什么是 `FOR UPDATE SKIP LOCKED` 而不是普通 `FOR UPDATE`？

普通 `FOR UPDATE` 会让第二个 Worker **等待**第一个 Worker 释放行锁，多个 Worker 实际被串行化，并发能力归零。

`SKIP LOCKED` 让第二个 Worker **跳过**已被锁住的行，直接取下一批可用的。这是"跳过"而不是"等待"的差别，也是这条 SQL 能支撑多 Worker 并发的全部原因。

```sql
SELECT * FROM events
WHERE status = 'pending' AND next_attempt_at <= now()
ORDER BY next_attempt_at        -- 先到期的先投，避免老任务被饿死
LIMIT 50
FOR UPDATE SKIP LOCKED
```

SELECT 与后续的 UPDATE 标记**必须在同一个事务里**，否则中间会留下一个窗口：任务被选中但还没标记为 `delivering`，另一个 Worker 的查询会再次选中同一条。

#### 为什么投递语义定为"至少一次"而不是"恰好一次"？

因为**跨进程的恰好一次在工程上做不到**。

失败可能发生在任意一步：请求发出去了但对端没收到、对端处理成功了但响应在返回路上丢了、对端处理成功但本方写入结果前进程被杀。最后一种情况下，重投是对的还是不投是对的——本机无法判断。

所以做法是**保证送达（至少一次）**，把去重的责任和工具交给下游：每次投递都带一个 `X-HookRelay-Delivery-Id` 请求头，同一个事件的所有重试共享同一个 ID，下游按它做幂等即可。承认做不到的事情，比假装做到了更可靠。

#### 为什么重放要引入 `attempt_generation` 列？

表面上看，重放死信只需要把 `attempt_count` 清零、状态改回 `pending`。但 `delivery_attempts` 表上有唯一约束 `(event_id, generation, attempt_number)`——如果只是计数器清零，新产生的"第 1 次尝试"会和历史记录里的第 1 次撞车，插入直接失败。

引入 `generation` 作为轮次标记后，重放时把 `generation + 1`，新记录落在新轮次里，历史尝试记录完整保留。**排查问题时"这个事件被投过几次、每次都返回了什么"是核心信息，不能为了省一列把它覆盖掉。**

顺带一条约束：重放只允许在 `dead` 或 `succeeded` 状态下发起，`pending` / `delivering` 时返回 `409`——正在投递的事件被重放会导致同一事件被两个 Worker 同时处理。

#### 为什么幂等靠唯一索引而不是"先查后写"？

`SELECT` 判断不存在 → `INSERT` 这个写法有竞态：两个请求可能同时通过检查，然后都执行插入。加锁能解决，但会引入锁争用。

正确的做法是**把判断交给数据库**：在 `(endpoint_id, idempotency_key)` 上建唯一索引，直接插入，捕获唯一冲突后按已存在处理。数据库的唯一约束是原子的，不存在检查与写入之间的窗口。

另外，调用方可能不传幂等键，这时用请求体的 SHA-256 作为兜底——同一个 `endpoint` 收到字节完全相同的请求体，视为重复。

#### 为什么 Web 和 Worker 放在同一个容器里启动？

这是**为了适配免费部署平台**的妥协，不是架构上的理想形态。

Render、Koyeb 这类平台的免费层通常只给一个 Web Service，不提供独立的后台进程。但架构上 Worker 仍然是完全独立的进程（`python -m app.worker`），`start.sh` 只是把它们放在同一个容器内启动而已。

之所以坚持进程隔离而不是把 Worker 做成 Web 服务里的 `asyncio.create_task()`：两者故障域不同——发布 Web 新版本时不该打断正在进行的投递。当前用同一个容器，只是受部署环境限制；换到有独立 Worker 支持的平台时，把 `start.sh` 里那一行拆出去即可，代码不用改。

## 性能

在 Apple M1 Pro（10 核 / 32 GB）上，以 Docker Compose 单机跑 Web + PostgreSQL，容器配额 4 CPU / 6 GB，发 600 个事件测不同并发：

| 并发 | 吞吐（事件/秒） | P50 | P95 | P99 |
| ---: | ---: | ---: | ---: | ---: |
| 5 | **381** | 12.7 ms | 16.5 ms | 30.3 ms |
| 10 | 360 | 25.4 ms | 43.1 ms | 63.9 ms |
| 20 | 332 | 50.0 ms | 96.8 ms | 239.3 ms |
| 30 | 145 | 140.5 ms | 515.8 ms | 728.9 ms |
| 50 | 106 | 332.7 ms | 1311.7 ms | 2146.5 ms |
| 80 | 82 | 610.7 ms | 3198.1 ms | 4286.3 ms |

**拐点出现在并发 20 到 30 之间，吞吐腰斩。** 这个位置不是巧合，它正好等于连接池上限 `pool_size (10) + max_overflow (20) = 30`。超出之后请求拿不到连接，只能排队等 `pool_timeout`，排队时间直接变成请求延迟，同时吞吐反而下降——典型的过度并发。

把上限提到 80 后重测同一并发：

| 连接池上限 | 并发 | 吞吐 | P50 | P99 |
| ---: | ---: | ---: | ---: | ---: |
| 30 | 50 | 106 /s | 332.7 ms | 2146.5 ms |
| 80 | 50 | **219 /s** | 129.2 ms | 914.7 ms |

吞吐翻倍，P99 降到原来的 43%，假设得到验证。因此连接池规格被做成配置项（`DB_POOL_SIZE` / `DB_MAX_OVERFLOW`），而不是写死在代码里。

**队列查询的执行计划**（表内已有数据的情况下）：

```text
Limit (actual time=0.010..0.011 rows=0 loops=1)
  ->  LockRows (actual time=0.009..0.010 rows=0 loops=1)
        ->  Index Scan using ix_events_pending_due on events
              Index Cond: ((status = 'pending') AND (next_attempt_at <= now()))
              Buffers: shared hit=1
Execution Time: 0.035 ms
```

走的是部分索引 `ix_events_pending_due`（只索引 `pending` 状态的行），没有退化成全表扫描。这点很重要：队列表里绝大多数行是已完成的终态，部分索引让索引体积只与**待处理量**相关，而与历史总量无关。

复现方式：

```bash
INGEST_RATE_LIMIT_PER_MINUTE=1000000 docker compose up -d
uv run python scripts/loadtest.py --events 2000 --concurrency 50 \
  --database-url "postgresql+asyncpg://hookrelay:hookrelay@127.0.0.1:5433/hookrelay_dev"
```

需要放开限流，否则测到的是 120 次/分钟的限流阈值而不是服务容量；脚本会在报告里检测并提示这一点。

## 测试

```bash
# 全量测试 + 覆盖率
uv run pytest tests/ -q --cov=app

# 静态检查
uv run ruff check .
uv run ruff format --check .
```

当前：**185 个用例通过，覆盖率 95%**。

测试库必须以 `_test` 结尾，`conftest.py` 会校验这一点并在不符合时直接报错——避免测试把开发数据清掉。涉及数据库的测试跑在真实 PostgreSQL 上（CI 里用 service container 启动），不用 SQLite 替代：`SKIP LOCKED`、JSONB、部分索引都是 PostgreSQL 特有行为，用别的数据库测等于没测。

## 项目结构

```text
hookrelay/
├── app/
│   ├── main.py              # 应用组装、中间件、健康检查、/metrics
│   ├── config.py            # 配置与连接串规范化
│   ├── db.py                # 异步引擎、连接池、session 依赖
│   ├── models.py            # User / Endpoint / Event / DeliveryAttempt
│   ├── schemas.py           # 请求与响应模型
│   ├── security.py          # API Key、URL token、HMAC 签名、密钥加密
│   ├── observability.py     # JSON 日志格式、request_id 中间件
│   ├── metrics.py           # Prometheus 指标定义
│   ├── worker.py            # 投递 Worker 主循环
│   ├── api/                 # 路由：auth / endpoints / ingest / events / stats
│   └── services/
│       ├── delivery.py      # 单次投递：构造请求、判定失败类型
│       ├── retry.py         # 指数退避与抖动
│       └── ratelimit.py     # 滑动窗口限流
├── alembic/versions/        # 数据库迁移
├── scripts/
│   ├── demo.py              # 12 步端到端演示
│   ├── demo_sink.py         # 演示用接收端（可注入慢响应、超时、错误码）
│   ├── loadtest.py          # 压测与执行计划分析
│   └── verify_*.py          # 各天验收脚本
├── tests/                   # 185 个测试用例
├── docs/USAGE.md            # 详细使用文档
├── Dockerfile               # 多阶段构建，非 root 运行
├── start.sh                 # 容器入口：迁移 → 起 Worker → 前台 Web
├── docker-compose.yml       # 本地一键起 Web + PostgreSQL
└── render.yaml              # Render 部署蓝图
```

## 配置项

全部通过环境变量配置，完整清单见 [`.env.example`](.env.example)。常用的几项：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ENV` | `dev` | `prod` 时会关闭 CORS 通配、开启更严格的日志 |
| `SECRET_KEY` | — | **必须配置**。用于派生加密密钥，保护库里存的下游签名密钥 |
| `DATABASE_URL` | 本地地址 | 支持 `postgres://`、`postgresql://` 前缀与 `sslmode` 参数，会自动转成 asyncpg 可用形式 |
| `WORKER_CONCURRENCY` | `10` | 并发投递数，小内存容器建议降到 5 |
| `WORKER_BATCH_SIZE` | `50` | 每轮从队列取的任务数 |
| `WORKER_LOCK_TIMEOUT_SECONDS` | `120` | 超过该时长仍未完成的任务视为僵尸，回收重投 |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `10` / `20` | 连接池规格，直接决定并发拐点（见[性能](#性能)） |
| `INGEST_RATE_LIMIT_PER_MINUTE` | `120` | 单个接收地址的入站限流 |
| `MAX_INGEST_BODY_BYTES` | `1048576` | 入站请求体大小上限（1 MiB） |
| `SIGNATURE_TOLERANCE_SECONDS` | `300` | 签名时间戳容忍窗口 |

> `SECRET_KEY` 一旦丢失，数据库中已加密的 endpoint 签名密钥将无法解密，且无法恢复。部署时请单独保存。

## 已知局限

诚实列出边界，比宣称"生产级"更有用：

- **单实例部署。** 限流状态存在进程内存里（用 Redis 的 ZSET 可以无痛替换），多实例下实际额度会被放大到 N 倍。
- **入站吞吐约 380 事件/秒**（4 核容器）。瓶颈是每个请求一个事务的 `commit` 开销，批量提交或 `synchronous_commit=off` 可以提升，代价是损失部分持久性保证。
- **队列表会膨胀。** 事件表兼顾队列，长期运行需要靠 autovacuum 回收死元组；事件量真正大起来后应按 `endpoint_id` 分区或把终态事件归档到独立表。
- **死信不自动恢复。** 这是有意为之——目标地址修好之前，重试多少次都是浪费。需要人工确认后重放。
- **投递是按事件的 FIFO，不保证跨事件的顺序。** 同一 `endpoint` 的多个事件可能被不同 Worker 并发投递。如果下游对顺序敏感，需要在业务层处理。
- **免费平台会休眠。** Render 免费层 15 分钟无流量后实例休眠，下次请求需要几十秒冷启动。演示时请先访问一次 `/health` 预热。

## 后续计划

- [ ] 事件表按 `endpoint_id` 分区，解决长期膨胀
- [ ] 支持自定义投递请求头（目前转发原始入站头，并剥离 `Content-Length` 等逐跳头）
- [ ] 目标地址健康探测，连续失败时自动熔断而非逐个事件重试
- [ ] 提供 Webhook 签名验证的官方 SDK 片段（Python / Node）

## 许可证

[MIT](LICENSE)
