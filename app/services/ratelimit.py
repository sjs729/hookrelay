"""入站限流。

按 endpoint 维度计数，防止单个来源把服务刷爆，也避免恶意调用方
靠大量请求把数据库写入打满。

这一类保护属于"可用性防御"：它不阻止单次攻击，但保证一个坏掉的
或恶意的调用方不会拖垮整个服务，影响其他用户。
"""

import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """按 key 的滑动窗口限流器。

    为什么用滑动窗口，而不是固定窗口计数器？
    固定窗口（例如"每分钟 120 次"，按自然分钟重置）有个边界漏洞：
    第 59 秒打满 120 次，第 61 秒进入新窗口又能打满 120 次——
    实际两秒内放行了 240 次。滑动窗口记录每次请求的时间戳，
    判定的是"过去 60 秒内发生了多少次"，没有这个边界问题。

    存储选择与代价（必须说清楚）：
    状态保存在进程内存里，多实例部署时各实例独立计数，
    实际可用额度会被放大到 N 倍。本项目单实例部署，用内存实现
    可以少引入一个 Redis 依赖。若将来要横向扩容，本类接口无需改动，
    把 _hits 换成 Redis 的 ZSET 即可——用 ZREMRANGEBYSCORE 清理过期成员、
    ZCARD 取当前计数，语义完全对应。
    """

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        # 用锁保护计数状态：主流程虽然跑在单线程事件循环里，
        # 但 FastAPI 的同步依赖会在线程池中执行，加锁避免将来引入同步调用时踩坑
        self._lock = threading.Lock()

    def check(self, key: str) -> float | None:
        """记录一次请求，并判定是否超限。

        返回 None 表示放行。
        返回大于 0 的秒数表示超限，该值即建议客户端等待多久后重试，
        会作为 Retry-After 响应头返回。

        用 time.monotonic() 而不是 time.time()：
        单调时钟不受系统时间被调整（NTP 校时、手动改时间）的影响，
        否则时钟回拨会让窗口计算错乱。
        """
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]

            # 先清理已经滑出窗口的历史记录
            cutoff = now - self._window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= self._limit:
                # 最早那次请求滑出窗口之后就有额度了
                retry_after = bucket[0] + self._window - now
                return max(retry_after, 0.0)

            bucket.append(now)
            return None

    def reset(self, key: str | None = None) -> None:
        """清空计数。测试里用来隔离用例，避免相互干扰。"""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)
