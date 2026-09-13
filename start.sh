#!/usr/bin/env bash
#
# 容器启动脚本：先执行数据库迁移，再同时拉起 Web 与 Worker。
#
# 为什么两个进程放在一个容器里：
# 免费托管平台通常只给一个 Web Service，不提供独立的后台进程或 Cron。
# 要在免费层跑起来，只能同容器启动。
#
# 但代码层面 Worker 依旧是独立进程（python -m app.worker），
# 不与 Web 绑定在同一个进程里。所以本地开发时它们可以分开重启——
# 发布 Web 新版本不会打断正在进行的投递。
#
# 如果以后换成给独立进程的平台，把这两段拆成两个 Service 即可，
# 应用代码一行都不用改。

set -euo pipefail

PORT="${PORT:-8000}"

echo "[start] 执行数据库迁移"
alembic upgrade head

echo "[start] 启动投递 Worker"
python -m app.worker &
worker_pid=$!

echo "[start] 启动 Web 服务，监听 0.0.0.0:${PORT}"
# 必须监听 0.0.0.0。绑定 127.0.0.1 的话容器外部无法访问，
# 表现为"部署成功但打不开"。
uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" &
web_pid=$!

# 收到停止信号时转发给子进程，让它们走各自的优雅关闭流程：
# Worker 会把在投递中的事件放回队列，而不是留下永远停在 delivering 的僵尸任务。
shutdown() {
    kill -TERM "${worker_pid}" "${web_pid}" 2>/dev/null || true
}
trap shutdown TERM INT

# 任一进程退出就整体退出。
# 保留一个"Web 已经挂了但 Worker 还在跑"的容器没有任何好处，
# 编排系统会一直认为这个实例是健康的。
wait -n "${web_pid}" "${worker_pid}"
exit_code=$?

shutdown
wait

exit "${exit_code}"
