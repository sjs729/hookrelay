# HookRelay 使用指南

本文档说明如何把 HookRelay 跑起来、以及如何完整地用一遍。

> 想直接看结果：跑起三个进程后执行 `uv run python scripts/demo.py`，
> 它会自动走完下面所有流程并逐步解释。本文是那份脚本的文字版。

---

## 一、它解决什么问题

假设你有两个系统：

- **上游**：产生事件的系统，比如电商下单
- **下游**：需要知道事件发生的系统，比如仓库发货

最直接的做法是上游在下单成功时直接调用下游接口。但这样下游一挂，
上游就得跟着处理失败、重试、补偿——这些逻辑会渗透进业务代码。

HookRelay 站在中间：

```text
   上游                     HookRelay                      下游
 下单成功  ──发送事件──▶  ① 验签           ──投递──▶  收到事件
                          ② 落库、立刻返回 202
                          ③ 后台重试直到成功
                             或进入死信
```

上游只负责"把事件发出去"，投递的可靠性由 HookRelay 承担。

---

## 二、跑起来

### 方式一：Docker（推荐，一条命令）

```bash
docker compose up --build
```

起来之后访问 <http://127.0.0.1:8000/docs>。

这个命令会拉起三个东西：PostgreSQL、Web 服务、投递 Worker。
数据库迁移由容器启动脚本自动执行，不需要手动跑。

### 方式二：本地进程（便于调试）

需要本机有 PostgreSQL 16。

```bash
# 1. 准备数据库
brew services start postgresql@16
createdb hookrelay_dev

# 2. 安装依赖
uv sync

# 3. 生成配置文件
cp .env.example .env
# 生成一个主密钥填进 SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(48))"

# 4. 建表
uv run alembic upgrade head

# 5. 启动 Web 服务（终端 A）
uv run uvicorn app.main:app --reload --port 8000

# 6. 启动投递 Worker（终端 B）
uv run python -m app.worker

# 7. 启动演示用的接收端（终端 C，可选）
uv run python scripts/demo_sink.py
```

### 关于 SECRET_KEY

**这个值必须保存好。** 它通过 HKDF 派生出加密密钥，数据库里存的
下游签名密钥是用它加密的。丢了就只能重置所有接收地址的密钥。

生产环境不要用 `.env` 文件，配在部署平台的环境变量里。

---

## 三、三个核心概念

| 概念 | 说明 |
| --- | --- |
| **接收地址（endpoint）** | 一个入站 token + 一个转发目标。创建时下发签名密钥 |
| **事件（event）** | 上游发来的一条 webhook，落库后进入投递队列 |
| **投递尝试（attempt）** | 每一次真正发出的 HTTP 请求，成功失败都记录 |

三者的关系：一个接收地址下有很多事件，一个事件下有很多次投递尝试。

---

## 四、完整流程

### 步骤 1：注册账号

```bash
curl -X POST http://127.0.0.1:8000/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email": "you@example.com", "password": "at-least-8-chars"}'
```

响应里的 `api_key` 是调用管理接口的凭据：

```json
{
  "id": "...",
  "email": "you@example.com",
  "api_key": "hr_xxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "api_key_prefix": "hr_xxxxxxxx",
  "created_at": "..."
}
```

> **`api_key` 只在这里返回一次。** 服务端只保存它的哈希，
> 之后无论怎么查询都拿不回来。丢了只能重新注册。

管理接口（`/api/*`）都要带上它：

```bash
-H "Authorization: Bearer hr_xxxxxxxx..."
```

### 步骤 2：创建接收地址

```bash
curl -X POST http://127.0.0.1:8000/api/endpoints \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "订单事件",
    "target_url": "https://your-downstream.example.com/webhook",
    "max_attempts": 5,
    "timeout_seconds": 10
  }'
```

响应里有两项**只返回这一次**：

```json
{
  "id": "...",
  "token": "ioNa85p5wgbuL_qAxMZ9jDqza_2qmlpk",
  "secret": "UPGz...（32 字节的签名密钥明文）",
  "secret_masked": "UPGz********p1N8",
  "ingest_url": "http://127.0.0.1:8000/ingest/ioNa85p5wgbuL_qAxMZ9jDqza_2qmlpk",
  "target_url": "https://your-downstream.example.com/webhook",
  "max_attempts": 5,
  "timeout_seconds": 10,
  "is_active": true
}
```

- `ingest_url`：配到上游的 webhook 地址栏里
- `secret`：用来计算请求签名

### 步骤 3：上游发送事件

对 `ingest_url` 发 POST，带上两个头：

| 请求头 | 值 |
| --- | --- |
| `X-HookRelay-Timestamp` | 当前 Unix 秒级时间戳 |
| `X-HookRelay-Signature` | `sha256=` + HMAC-SHA256(secret, `时间戳.原始请求体`) |

```python
import hashlib, hmac, json, time, httpx

secret = "步骤 2 拿到的 secret"
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
        "X-HookRelay-Idempotency-Key": "order-1001-created",  # 可选
    },
)
```

三点值得注意：

1. **签名对象包含时间戳**。这样旧请求无法原样重放——改时间戳签名就失效，
   不改就超出容忍窗口（默认 5 分钟）。
2. **签名必须基于原始请求体字节**，不能先把 JSON 解析再重新序列化。
   重新序列化后键序、空格都会变，签名就对不上了。
3. **`X-HookRelay-Idempotency-Key` 可选**。不传的话系统会自动取
   payload 里的 `event_id` / `id`，再退到请求体哈希。

成功返回 **202**：

```json
{
  "event_id": "...",
  "endpoint_id": "...",
  "status": "pending",
  "duplicate": false
}
```

> 返回 202 而不是 200：202 的含义是"请求已接受处理，但尚未完成"。
> 此刻事件只是安全落库了，真正的投递是后台异步发生的。
>
> `duplicate: true` 表示这个事件之前已经收到过，本次没有重复入队。
> 上游重试是常态，这个字段让上游知道自己的重发被认出来了。

### 步骤 4：查询事件与投递结果

```bash
# 列表（可按状态或接收地址过滤）
curl -H "Authorization: Bearer $API_KEY" \
  "http://127.0.0.1:8000/api/events?status=dead&limit=20"

# 详情：含完整 payload 与每一次投递记录
curl -H "Authorization: Bearer $API_KEY" \
  "http://127.0.0.1:8000/api/events/$EVENT_ID"
```

详情响应里的 `attempts` 是排查问题的关键：

```json
{
  "status": "dead",
  "attempt_count": 5,
  "last_error": "目标服务拒绝了请求（400）",
  "attempts": [
    {
      "attempt_number": 1,
      "status_code": 400,
      "response_body": "错误信息前 1KB",
      "error": null,
      "duration_ms": 3,
      "created_at": "..."
    }
  ]
}
```

### 步骤 5：重放死信

下游修好之后：

```bash
curl -X POST -H "Authorization: Bearer $API_KEY" \
  "http://127.0.0.1:8000/api/events/$EVENT_ID/replay"
```

事件会重新回到队列，Worker 下一轮取走投递。

> **重放会清零尝试次数**，因此需要区分"第几轮投递"——
> 这就是数据库里 `attempt_generation` 列的用途：
> `(event_id, generation, attempt_number)` 构成唯一约束，
> 既保证同一轮内尝试编号不重复，又允许重放后从 1 重新计数。
>
> 处于 `pending` / `delivering` 的事件不允许重放（返回 409）——
> 它还在队列里，重放只会造成重复投递。

### 步骤 6：查看统计

```bash
curl -H "Authorization: Bearer $API_KEY" http://127.0.0.1:8000/api/stats
```

```json
{
  "endpoints": 1,
  "total_events": 42,
  "pending": 0,
  "delivering": 0,
  "succeeded": 40,
  "dead": 2,
  "success_rate": 0.952,
  "total_attempts": 51,
  "avg_delivery_latency_ms": 12.4
}
```

`success_rate` 是**小数比例**（0.952 表示 95.2%），分母只算已终结的事件。

---

## 五、事件状态

| 状态 | 含义 |
| --- | --- |
| `pending` | 等待投递。**包括重试等待中的事件** |
| `delivering` | Worker 正在投递 |
| `succeeded` | 投递成功，终结 |
| `dead` | 重试耗尽，进入死信，等待人工处理 |

**注意：没有 "retrying" 这个状态。** 重试等待中的事件状态仍然是 `pending`，
靠 `last_error` 有值、`next_attempt_at` 在未来来区分。指标里那个
`result="retrying"` 是**投递结果**的标签，不是事件状态。

---

## 六、失败怎么处理

判断依据是下游返回的**状态码**：

| 情况 | 处理 | 原因 |
| --- | --- | --- |
| 2xx | 成功 | —— |
| 408、429 | 重试 | 对方明确表示"稍后再来" |
| 其他 4xx | **直接死信** | 请求本身有问题，重试再多次结果一样 |
| 3xx | **直接死信** | 重定向说明 `target_url` 配错了，重试无意义 |
| 5xx | 重试 | 对方自己出错了，通常重试就好 |
| 超时 | 重试 | 对方可能正在处理但来不及响应 |
| 连接失败 | 重试 | DNS、端口、TLS 问题，可能是暂时的 |

重试间隔是**指数退避 + 抖动**：`2s → 4s → 8s → 16s → ...`，
上限 1 小时，实际间隔在基准值的 ±20% 内随机浮动。

> 抖动的作用：如果 1000 个事件同时失败，没有抖动它们会在同一毫秒
> 全部重试，把刚恢复的下游再打垮一次。

---

## 七、接收入站时的限制

| 限制 | 默认值 | 说明 |
| --- | --- | --- |
| 请求体大小 | 1 MB | 边收边累计，超限立刻断开，不会先吃满内存 |
| 时间戳容忍窗口 | 300 秒 | 超出即判为重放 |
| 请求频率 | 120 次/分钟 | 滑动窗口，按接收地址计 |

超限分别返回 413 / 401 / 429。

---

## 八、接口速查

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/register` | 注册，返回 API Key |
| GET | `/api/auth/me` | 当前账号信息 |
| POST | `/ingest/{token}` | **接收事件**（不需要 API Key） |
| GET | `/api/endpoints` | 接收地址列表 |
| POST | `/api/endpoints` | 创建接收地址 |
| GET | `/api/endpoints/{id}` | 接收地址详情 |
| PATCH | `/api/endpoints/{id}` | 修改接收地址 |
| DELETE | `/api/endpoints/{id}` | 删除接收地址 |
| POST | `/api/endpoints/{id}/secret` | 重置签名密钥（旧密钥立刻失效） |
| GET | `/api/events` | 事件列表 |
| GET | `/api/events/{id}` | 事件详情（含每次投递记录） |
| POST | `/api/events/{id}/replay` | 重放死信 |
| GET | `/api/stats` | 账号统计 |
| GET | `/health` | 存活检查（不查数据库） |
| GET | `/health/ready` | 就绪检查（查数据库） |
| GET | `/metrics` | Prometheus 指标 |

交互式文档：`/docs`（Swagger UI）、`/redoc`。

---

## 九、关于投递语义

HookRelay 的投递语义是**至少一次（at-least-once）**，不是恰好一次。

原因是"恰好一次"需要下游配合做分布式事务，代价过高。实际做法是：

1. HookRelay 保证事件不丢，失败会重试
2. 每次投递带上 `X-HookRelay-Delivery-Id`（值等于事件 id）
3. **下游用它自己去重**

同一个事件被投递两次时，`X-HookRelay-Delivery-Id` 相同，
下游据此就能识别出这是重复投递。

---

## 十、常见问题

**Q：上游收到 401 "签名校验失败"**

检查签名是否基于**原始请求体字节**计算。常见错误是先把 body 解析成
字典，再 `json.dumps` 回去——键序和空格变了，签名自然对不上。

**Q：事件一直是 pending 不动**

Worker 没在跑。`uv run python -m app.worker` 起来了没有？
`docker compose` 方式下看日志里有没有 Worker 的输出。

**Q：事件一直是 delivering**

Worker 投递时崩了。看 Worker 日志里的异常堆栈。
正常情况下 Worker 每 60 秒回收一次超时未完成的任务（`locked_at`
超过 `worker_lock_timeout_seconds`），把事件放回队列。

**Q：怎么让事件真的进死信方便演示**

把 `target_url` 指向一个总是返回 400 的地址，
或者用 `scripts/demo_sink.py` 的 `/control?mode=fail&fail_status=400`。

**Q：重试很多次都不成功，但下游确实是好的**

看事件详情里 `attempts` 的 `response_body`——那里面有对方返回的错误内容
（保留前 1KB）。常见原因是对方要求特定的请求头，而它期望的头
没有被上游发过来。

---

## 十一、本地验证脚本

| 脚本 | 用途 |
| --- | --- |
| `scripts/demo.py` | 完整使用演示，逐步解释每一步 |
| `scripts/demo_sink.py` | 演示用的接收端，可切换成功/失败/慢/超时 |
| `scripts/verify_day3.py` | 事件接收入口验收 |
| `scripts/verify_day4.py` | 投递引擎与重放验收 |
| `scripts/verify_day5.py` | 可观测性验收 |
| `scripts/verify_docker.py` | 容器链路验收 |

跑测试：

```bash
uv run pytest --cov=app --cov-report=term
```
