# syntax=docker/dockerfile:1

# ============================================================
# 构建阶段
# ============================================================
# 单独一个阶段装依赖，运行阶段只复制装好的虚拟环境。
# 这样最终镜像里不含编译器、wheel 缓存和构建工具链，
# 体积能小一半以上，被攻击的面也小。
FROM python:3.12-slim AS builder

# uv 严格按 uv.lock 安装，本地测试通过的那一套版本会被原样搬到线上。
# 用 pip 装的话，锁文件形同虚设，线上可能拿到与本地不同的次版本。
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 先只复制依赖清单。只要依赖没变，这一层就命中缓存；
# 改业务代码不会触发重新下载安装依赖，构建从几分钟降到几秒。
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# 再复制源码并装项目本身
COPY . .
RUN uv sync --frozen --no-dev

# ============================================================
# 运行阶段
# ============================================================
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8000

# curl 只用于 HEALTHCHECK。slim 镜像不带它，
# 而没有健康检查的容器在编排系统里等于无法判断是否可用。
# 装完立刻清掉 apt 缓存，避免把索引文件留在镜像层里。
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 不用 root 运行应用。容器隔离并不牢固，
# 一旦被攻破，攻击者拿到的是普通用户而不是宿主机的 root 上下文。
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

# 虚拟环境里的可执行文件用绝对路径互相引用，
# 所以这里必须放在与构建阶段相同的位置（/app/.venv）。
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --chown=appuser:appuser . .

USER appuser

EXPOSE 8000

# start-period 给迁移和冷启动留时间。免费平台休眠后唤醒较慢，
# 这段时间内的失败不该被计成"服务不健康"。
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# 用 exec 形式，让 start.sh 直接成为 PID 1，能正常收到 SIGTERM
CMD ["./start.sh"]
