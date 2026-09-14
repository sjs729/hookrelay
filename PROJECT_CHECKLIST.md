# HookRelay 项目完整清单

> Webhook 中继与可靠投递服务 ｜ Python 后端简历项目
> 版本 v1.0 ｜ 制定时间：2026-09-12

---

## 一、项目概况

| 项目 | 内容 |
| --- | --- |
| 项目名 | HookRelay |
| 一句话定位 | 接收第三方 Webhook 事件并可靠、幂等、可观测地异步投递到目标地址 |
| 目标岗位 | Python 后端 / 服务端开发 |
| 核心难度 | 并发消费、可靠性投递、数据库索引优化 |
| 时间预算 | 7 天 |
| 交付平台 | GitHub + Gitee 双平台 |
| 上线方式 | Render 免费层 + Neon 免费 PostgreSQL |
| 演示形式 | 公网可访问的 `/docs` 交互式 API 文档 |

### 技术栈

```text
Python 3.12 + uv          运行环境与依赖管理
FastAPI + Uvicorn         异步 Web 框架
SQLAlchemy 2.0 + asyncpg  异步 ORM
PostgreSQL 16             数据库兼任务队列
Alembic                   数据库迁移
httpx                     出站投递
pwdlib[argon2]           密码哈希
cryptography              出站密钥加密存储
prometheus-client         监控指标
pytest + ruff             测试与代码质量
Docker + GitHub Actions   容器化与 CI
```

### 优先级说明

清单中每项标注优先级，时间不够时按此顺序砍：

- **P0** 核心链路，缺了项目不成立，必须完成
- **P1** 明显提升简历含金量，尽量完成
- **P2** 锦上添花，可延后

---

## 二、依赖清单

### Python 运行依赖

```toml
fastapi
uvicorn[standard]
pydantic-settings
sqlalchemy[asyncio]
asyncpg
alembic
httpx
cryptography
pwdlib[argon2]
prometheus-client
```

### Python 开发依赖

```toml
pytest
pytest-asyncio
pytest-cov
ruff
```

### 系统依赖

```text
uv                 依赖与 Python 版本管理
PostgreSQL 16      本地开发数据库（Homebrew 安装）
Docker Desktop     仅 Day 6 需要
```

### 环境变量清单

```bash
# 基础
ENV=dev                              # dev | prod
LOG_LEVEL=INFO
SECRET_KEY=                          # 用于加密 endpoint secret，必填

# 数据库
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/hookrelay_dev

# Worker
WORKER_CONCURRENCY=10                # 并发投递数
WORKER_BATCH_SIZE=50                 # 每轮取任务数
WORKER_POLL_INTERVAL_SECONDS=1.0     # 空转轮询间隔

# 入站
INGEST_RATE_LIMIT_PER_MINUTE=120     # 每 endpoint 每分钟上限
MAX_INGEST_BODY_BYTES=1048576        # 请求体上限 1MB
SIGNATURE_TOLERANCE_SECONDS=300      # 防重放时间窗

# 投递
DEFAULT_MAX_ATTEMPTS=5
DEFAULT_TIMEOUT_SECONDS=10
RETRY_BASE_DELAY_SECONDS=2
RETRY_MAX_DELAY_SECONDS=3600
RETRY_JITTER_RATIO=0.2
```

---

## 三、Day 1 · 环境与骨架（P0）

### 1.1 环境准备

- [x] 安装 uv：`brew install uv`
- [x] 用 uv 安装 Python 3.12：`uv python install 3.12`
- [x] 配置国内镜像源（避免依赖下载卡住）
- [x] 验证 `uv --version` 与 `uv run python --version`

### 1.2 项目初始化

- [x] 生成 `pyproject.toml`，声明运行依赖与开发依赖
- [x] 创建目录：`app/`、`app/api/`、`app/services/`、`tests/`、`scripts/`
- [x] 编写 `.gitignore`（Python 标准模板 + `.env` + `.venv`）
- [x] 编写 `.env.example`（只放变量名，不放真实密钥）
- [x] 编写 `LICENSE`（MIT）
- [x] 创建 `README.md` 占位

### 1.3 基础代码

- [x] `app/config.py`：用 pydantic-settings 读取环境变量，提供类型化配置对象
- [x] `app/main.py`：创建 FastAPI 实例，配置标题、版本、描述
- [x] `app/main.py`：实现 lifespan（启动/关闭钩子骨架，为后续 Worker 预留）
- [x] `GET /health`：返回服务存活状态
- [x] 全局异常处理器：统一错误响应格式
- [x] CORS 中间件配置

### 1.4 验证

- [x] `uv run uvicorn app.main:app --reload` 启动成功
- [x] 浏览器打开 `http://127.0.0.1:8000/docs` 看到接口文档页
- [x] `curl http://127.0.0.1:8000/health` 返回 200

### 1.5 Git 与双远端

- [x] 确认 `.env` 未被纳入版本控制
- [x] 首次提交，commit message 遵循规范（如 `chore: 初始化项目骨架`）
- [x] 在 GitHub 创建仓库并配置为 `upstream` 或 `github` 远端
- [x] 在 Gitee 创建仓库并配置为 `origin` 或 `gitee` 远端
- [x] 推送到两个平台，确认两边都能看到代码

**完成标志**：本地能打开接口文档页，两个远程仓库都有代码。

---

## 四、Day 2 · 数据层（P0）

### 2.1 数据库

- [x] `brew install postgresql@16`
- [x] `brew services start postgresql@16`
- [x] 创建开发库 `hookrelay_dev`
- [x] 创建测试库 `hookrelay_test`
- [x] 验证能连上

### 2.2 连接层

- [x] `app/db.py`：创建异步 engine（含连接池参数）
- [x] `app/db.py`：创建 async session 工厂
- [x] `app/db.py`：提供 FastAPI 依赖注入用的 session 获取函数
- [x] `app/models.py`：定义 `Base` 与公共字段 mixin（id、created_at）

### 2.3 数据模型

- [x] `users` 表：id、email（唯一）、password_hash、api_key_hash（唯一）、created_at
- [x] `endpoints` 表：id、user_id、name、token（唯一）、target_url、secret_encrypted、max_attempts、timeout_seconds、active、created_at
- [x] `events` 表：id、endpoint_id、payload（JSONB）、headers（JSONB）、idempotency_key、status、attempt_count、next_attempt_at、locked_at、locked_by、created_at、completed_at
- [x] `delivery_attempts` 表：id、event_id、attempt_number、status_code、response_body、error、duration_ms、created_at
- [x] 唯一索引：`UNIQUE(endpoint_id, idempotency_key)` —— 幂等的实现基础
- [x] 部分索引：`(status, next_attempt_at) WHERE status = 'pending'` —— 队列扫描优化
- [x] 外键与级联删除策略
- [x] status 字段取值约束：`pending | delivering | succeeded | dead`

### 2.4 迁移

- [x] `alembic init` 生成迁移目录
- [x] 改造 `alembic/env.py` 支持异步引擎
- [x] 让 Alembic 自动发现模型的 metadata
- [x] 生成首个迁移脚本
- [x] 人工检查生成的 SQL 是否符合预期（索引、约束是否都在）
- [x] `alembic upgrade head` 建表成功
- [x] 验证能 `downgrade` 回滚

### 2.5 认证体系

- [x] `app/security.py`：密码哈希与校验（argon2）
- [x] `app/security.py`：API Key 生成、哈希与校验
- [x] `POST /api/auth/register`：注册并返回 API Key（仅此一次明文返回）
- [x] `app/api/deps.py`：从 `Authorization: Bearer` 解析当前用户
- [x] `app/schemas.py`：注册请求/响应模型

### 2.6 验证

- [x] 注册用户成功，拿到 API Key
- [x] 用 API Key 访问受保护接口通过
- [x] 用错误 Key 访问返回 401
- [x] 数据库里能看到迁移建出的表和索引

**完成标志**：迁移建表成功，可注册用户并用 API Key 鉴权。

---

## 五、Day 3 · 入站接收（P0）

### 5.1 Endpoint 管理

- [x] `POST /api/endpoints`：创建接收地址，自动生成 token 与签名密钥
- [x] `GET /api/endpoints`：列出当前用户的接收地址
- [x] `GET /api/endpoints/{id}`：查看详情
- [x] `PATCH /api/endpoints/{id}`：修改目标 URL、重试次数、超时
- [x] `DELETE /api/endpoints/{id}`：删除
- [x] 接收地址的签名密钥用 Fernet 加密后入库，接口只回显掩码
- [x] 提供「重置密钥」接口

### 5.2 签名与防重放（P0 核心安全点）

- [x] `app/security.py`：生成 HMAC-SHA256 签名（签名对象：时间戳 + 请求体）
- [x] `app/security.py`：校验签名，使用 `hmac.compare_digest` 恒定时间比较
- [x] 校验时间戳头，超出容差窗口（默认 300 秒）判定为过期请求
- [x] 签名格式设计：`X-HookRelay-Signature: sha256=<hex>` 与 `X-HookRelay-Timestamp`
- [x] 失败返回 401 并记录原因

### 5.3 接收接口

- [x] `POST /ingest/{token}`：按 token 定位 endpoint，未找到返回 404
- [x] 校验 endpoint 是否启用，禁用返回 403
- [x] 校验签名与时间戳
- [x] 校验请求体大小，超限返回 413
- [x] 提取幂等键：优先 `Idempotency-Key` 头，其次 payload 内 `event_id`，都没有则用请求体哈希
- [x] 幂等判定：依赖唯一索引，捕获冲突异常后返回已存在事件的 ID
- [x] 入站限流：按 endpoint 滑动窗口计数，超限返回 429 并带 `Retry-After`
- [x] 写入 events 表，状态 `pending`，`next_attempt_at` 设为当前时间
- [x] 返回 `202 Accepted`，响应体含 `event_id` 与查询链接

### 5.4 验证

- [x] 脚本发送带正确签名的请求，返回 202
- [x] 签名错误返回 401
- [x] 时间戳过期返回 401
- [x] 同一幂等键连发两次，只产生一条事件记录
- [x] 超过限流阈值返回 429
- [x] 超大请求体返回 413

**完成标志**：重复事件只入队一次，伪造签名被拒绝，超限被拦截。

---

## 六、Day 4 · 投递引擎（P0 · 项目核心）

### 6.1 退避策略

- [x] `app/services/retry.py`：退避延迟纯函数
- [x] 公式：`delay = min(base * factor^(attempt-1), max_delay)`
- [x] 加入 ±jitter 抖动，避免大量任务同时重试造成尖峰
- [x] 覆盖边界：首次失败、最大延迟封顶、随机性范围
- [x] 用参数化测试验证（纯函数，最容易测好）

### 6.2 单次投递

- [x] `app/services/delivery.py`：用 httpx 异步发起 POST
- [x] 设置超时（来自 endpoint 配置）
- [x] 出站请求带 `X-HookRelay-Signature`（用 endpoint 密钥签名）
- [x] 出站请求带 `X-HookRelay-Delivery-Id`（供下游去重）
- [x] 出站请求带 `X-HookRelay-Attempt`（第几次尝试）
- [x] 记录响应码、耗时、响应体（截断 1KB）
- [x] 区分失败类型：连接错误、超时、4xx、5xx
- [x] 4xx 与 5xx 的重试策略区分（如 410 直接判死）

### 6.3 Worker 主循环（技术核心）

- [x] `app/worker.py`：循环拉取到期任务
- [x] 取任务 SQL 使用 `FOR UPDATE SKIP LOCKED`，批量取、限并发
- [x] 取出后标记 `delivering` 并记录 `locked_at`、`locked_by`
- [x] 并发投递：`asyncio.gather` + Semaphore 限制并发数
- [x] 投递成功：状态置 `succeeded`，写 delivery_attempts，记录 completed_at
- [x] 投递失败：`attempt_count + 1`，按退避计算 `next_attempt_at`，状态回 `pending`
- [x] 超过 `max_attempts`：状态置 `dead`，写 delivery_attempts
- [x] 空转处理：无任务时按 `WORKER_POLL_INTERVAL_SECONDS` 休眠，避免空转打满 CPU
- [x] 优雅关闭：捕获 SIGTERM/SIGINT，等待进行中任务结束
- [x] 崩溃恢复：处理卡在 `delivering` 超时的僵尸任务（锁超时回滚为 pending）

### 6.4 死信处理

- [x] `GET /api/events?status=dead`：查询死信事件
- [x] `POST /api/events/{id}/replay`：重置状态为 pending，重置尝试次数，立即投递
- [x] 重放操作写审计日志

### 6.5 演示接收端

- [x] `scripts/demo_sink.py`：一个本地 HTTP 服务，收到请求后打印并在页面展示
- [x] 支持模拟故障（按比例返回 500 / 超时），用于演示重试

### 6.6 端到端验证

- [x] 发事件 → 自动投递到 sink 成功
- [x] 关闭 sink → 看到重试次数递增、间隔变长
- [x] 重试超过上限 → 进入死信
- [x] 手动重放死信 → 投递成功
- [x] 同时开两个 Worker → 任务不重复、不冲突（验证 SKIP LOCKED）

**完成标志**：完整链路可演示，能证明多 Worker 并发不重复消费。

---

## 七、Day 5 · 可观测与测试（P1）

### 7.1 日志

- [x] `app/observability.py`：JSON 结构化日志 formatter
- [x] 请求日志中间件，生成并透传 `request_id`
- [x] 关键节点打点：接收、入队、投递成功、投递失败、进入死信、重放

### 7.2 指标

- [x] `prometheus-client` 初始化
- [x] 计数器：接收事件数、投递成功数、投递失败数
- [x] 直方图：投递耗时分布
- [x] 仪表：待处理队列积压量、死信数量
- [x] `GET /metrics` 暴露指标

### 7.3 查询与统计接口

- [x] `GET /api/events`：分页，支持按 status、endpoint_id 过滤
- [x] `GET /api/events/{id}`：详情 + 完整投递历史
- [x] `GET /api/stats`：接收总数、成功数、失败数、成功率、平均投递延迟
- [x] `GET /health/ready`：就绪检查（探测数据库连通性）

### 7.4 测试

- [x] `tests/conftest.py`：测试库、事件循环、HTTP 客户端 fixture
- [x] `test_security.py`：签名正确/错误/过期、密码哈希、API Key
- [x] `test_retry.py`：退避计算边界与抖动范围
- [x] `test_ingest.py`：幂等、限流、大小限制、鉴权失败
- [x] `test_delivery.py`：成功、失败重试、超限死信、重放
- [x] 用 httpx mock 或本地 sink 模拟下游
- [x] 覆盖率报告，核心模块覆盖率目标 ≥ 80%

### 7.5 验证

- [x] `pytest` 全绿
- [x] `ruff check` 无告警
- [x] 覆盖率报告生成
- [x] `/metrics` 有真实数据

**完成标志**：测试全绿，指标端点有数据。

---

## 八、Day 6 · 容器化、CI 与上线（P1）

### 8.1 容器化

- [x] `Dockerfile`：多阶段构建，最终镜像用非 root 用户
- [x] `.dockerignore`
- [x] `docker-compose.yml`：app + postgres 两个服务，含健康检查
- [x] `start.sh`：同一容器内同时启动 web 与 worker（兼容免费平台）
- [x] 本地 `docker compose up` 验证完整链路

### 8.2 CI

- [x] `.github/workflows/ci.yml`：ruff 检查
- [x] CI 中启动 PostgreSQL service 容器
- [x] CI 中跑 pytest
- [x] CI 中构建 Docker 镜像验证可构建
- [x] 推送后确认 Actions 通过（显示绿色徽章）
- [x] README 添加 CI 状态徽章

### 8.3 部署

- [ ] 注册 Render 与 Neon 账号
- [ ] Neon 创建 PostgreSQL 实例，获取连接串
- [ ] Render 创建 Web Service，关联仓库
- [ ] 配置全部环境变量（`SECRET_KEY`、`DATABASE_URL` 等）
- [ ] 部署成功，公网 `/docs` 可访问
- [ ] 线上跑通完整链路：创建 endpoint → 发事件 → 投递成功
- [ ] 记录线上地址，写入 README

### 8.4 验证

- [ ] 公网能打开接口文档页
- [ ] 线上完整链路可演示
- [x] GitHub Actions 绿色
- [ ] 免费实例休眠后的唤醒说明写入 README

**完成标志**：公网可访问的 Demo 链接 + 在线 API 文档。

---

## 九、Day 7 · 文档、压测与简历（P0）

### 9.1 README

- [x] 项目简介与解决的问题
- [x] 架构图（Mermaid 绘制完整数据流）
- [x] 功能特性列表
- [x] 技术栈说明
- [x] 快速开始：本地运行
- [x] 快速开始：Docker 一键启动
- [x] API 使用示例（含签名生成的 curl 脚本）
- [x] 设计决策章节：为什么不用消息中间件、为什么是至少一次投递、为什么 web 与 worker 同容器
- [x] 压测数据章节
- [x] 已知局限与后续规划
- [ ] 在线 Demo 链接
- [x] 许可证

### 9.2 压测

- [x] `scripts/loadtest.py`：基于 asyncio + httpx 的并发压测脚本
- [x] 压测入站接口：记录吞吐（events/s）与 P50/P95/P99 延迟
- [x] 压测投递吞吐：记录单位时间投递完成数
- [x] 对比优化前后（如加索引前 vs 加索引后队列扫描耗时）
- [x] 记录测试环境配置（本机规格、并发数、数据量）

### 9.3 演示素材

- [ ] 关键界面截图（接口文档页、事件详情、统计）
- [ ] 端到端演示录屏或 GIF
- [ ] 图片存入 `docs/images/` 并在 README 引用

### 9.4 简历与面试

- [x] 简历项目描述文案（4 条，含量化数据）
- [x] 面试问答稿：为什么不用 Kafka/Celery
- [x] 面试问答稿：SKIP LOCKED 的取舍
- [x] 面试问答稿：至少一次投递语义与下游去重
- [x] 面试问答稿：部分索引与队列表膨胀优化
- [x] 补充问答稿：幂等为何靠唯一索引而非先查后写
- [x] 补充问答稿：如何做崩溃恢复与僵尸任务处理

### 9.5 收尾

- [x] 检查仓库无敏感信息泄露（`.env`、密钥、连接串）
- [ ] 打 tag（如 `v1.0.0`）
- [x] 推送 GitHub 与 Gitee，确认两边同步
- [ ] 检查 Gitee 端 README 图片外链是否正常（GitHub 图床可能无法访问，必要时改用仓库内相对路径）

**完成标志**：压测数据 + 简历文案 + 双平台同步完成。

---

## 十、最终仓库结构

```text
hookrelay/
├── app/
│   ├── __init__.py
│   ├── main.py                 应用装配与生命周期
│   ├── config.py               配置管理
│   ├── db.py                   数据库连接层
│   ├── models.py               SQLAlchemy 模型
│   ├── schemas.py              Pydantic 模型
│   ├── security.py             密码哈希、API Key、HMAC 签名
│   ├── observability.py        日志与指标
│   ├── worker.py               Worker 主循环
│   ├── api/
│   │   ├── deps.py             依赖注入
│   │   ├── auth.py             注册与鉴权
│   │   ├── endpoints.py        Endpoint CRUD
│   │   ├── ingest.py           入站接收
│   │   └── events.py           事件查询、统计、重放
│   └── services/
│       ├── ingest.py           幂等与入队
│       ├── delivery.py         单次投递执行
│       ├── retry.py            退避策略
│       └── stats.py            统计聚合
├── alembic/
│   ├── env.py
│   └── versions/
├── tests/
│   ├── conftest.py
│   ├── test_security.py
│   ├── test_retry.py
│   ├── test_ingest.py
│   └── test_delivery.py
├── scripts/
│   ├── demo_sink.py            演示用接收端
│   ├── send_event.py           发送测试事件
│   └── loadtest.py             压测脚本
├── docs/
│   └── images/                 截图与演示图
├── .github/workflows/ci.yml
├── Dockerfile
├── docker-compose.yml
├── start.sh
├── pyproject.toml
├── alembic.ini
├── .env.example
├── .gitignore
├── LICENSE
└── README.md
```

---

## 十一、验收清单

项目完成后必须同时满足：

- [ ] 公网可访问，`/docs` 打开即为可交互的 API 文档
- [ ] 完整链路可现场演示：发事件 → 幂等去重 → 异步投递 → 失败重试 → 进死信 → 重放成功
- [ ] 双 Worker 并发消费不重复、不冲突
- [ ] 伪造签名、过期时间戳、超限请求均被正确拒绝
- [ ] `pytest` 全绿，核心模块覆盖率 ≥ 80%
- [ ] `ruff check` 无告警
- [ ] `docker compose up` 一键起完整服务
- [ ] GitHub Actions 流水线绿色
- [ ] README 含架构图、快速开始、设计决策、压测数据
- [ ] GitHub 与 Gitee 双平台代码同步
- [ ] 仓库无任何敏感信息
- [ ] 你能独立讲清 6 个面试技术话题

---

## 十二、交付物清单

| 交付物 | 形式 | 用途 |
| --- | --- | --- |
| 可运行的项目源码 | Git 仓库 | 双平台展示 |
| 在线 Demo | 公网链接 | 简历直接放链接 |
| API 文档 | `/docs` 在线页面 | 面试演示 |
| README | Markdown | 项目门面 |
| 架构图 | Mermaid / 图片 | README 与面试讲解 |
| 压测报告 | README 章节 | 量化证明性能优化能力 |
| 测试与覆盖率 | CI 报告 | 证明工程规范 |
| 演示 GIF / 截图 | docs/images | README 与简历附加材料 |
| 简历项目描述 | 文本 | 直接用 |
| 面试问答稿 | 文本 | 面试准备 |

---

## 十三、风险与应对

| 风险 | 应对 |
| --- | --- |
| 免费平台政策变动 | 架构不依赖平台特性，可换 Koyeb / Fly.io / Hugging Face Spaces |
| 国内下载依赖慢 | Day 1 配置 uv 与 pip 国内镜像源 |
| GitHub 访问不稳定 | 以 Gitee 为主推，GitHub 同步；CI 用 GitHub Actions |
| 环境安装卡住 | Day 1–2 用 Homebrew 装 PostgreSQL，Docker 推迟到 Day 6 |
| 时间不足 | 严格按 P0 → P1 → P2 顺序砍范围，砍尾不砍头 |
| 理解跟不上 | 每块先讲设计再写代码，关键处加中文注释 |

---

## 十四、每日完成标志速查

| 天 | 一句话完成标志 |
| --- | --- |
| Day 1 | 本地能打开接口文档页，双平台仓库有代码 |
| Day 2 | 迁移建表成功，能注册用户并鉴权 |
| Day 3 | 重复事件只入队一次，伪造签名被拒绝 |
| Day 4 | 完整链路可演示，双 Worker 并发不重复 |
| Day 5 | pytest 全绿，指标端点有数据 |
| Day 6 | 公网 Demo 链接可访问 |
| Day 7 | 压测数据 + 简历文案定稿，双平台同步 |
