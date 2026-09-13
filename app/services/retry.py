"""投递重试的退避策略。

这个模块只放纯函数：不碰数据库、不发网络请求，输出完全由输入决定
（随机源可以注入）。这样做有一个很实际的理由——重试策略写错时，
症状是"任务在某个时刻集体重试把下游打死"或者"间隔太短等于没有退避"，
这类问题在运行时极难观察到，但用单元测试可以把边界全部钉死。
"""

import random

# 默认放大倍数：每隔一次失败，等待时间翻倍
DEFAULT_FACTOR = 2.0

# 这里用标准库的 random 而不是 secrets 是刻意的：
# 抖动的目的是打散重试时刻、避免流量撞在同一秒，不需要不可预测性，
# 因此不需要密码学随机源。用 secrets 反而会引入不必要的系统调用开销。
# 这一点需要和签名密钥、API Key 的生成区分开——那些必须用 secrets。


def compute_delay(
    attempt: int,
    *,
    base_delay: float = 2.0,
    factor: float = DEFAULT_FACTOR,
    max_delay: float = 3600.0,
    jitter_ratio: float = 0.2,
    rng: random.Random | None = None,
) -> float:
    """计算第 attempt 次尝试失败后，距下次重试应等待的秒数。

    参数：
        attempt      已经失败的次数，从 1 开始。1 表示首次投递刚失败。
        base_delay   首次失败后的基础等待秒数
        factor       每次失败后延迟的放大倍数
        max_delay    延迟上限
        jitter_ratio 抖动幅度（0~1），表示在延迟的 ±该比例内随机浮动
        rng          随机源；测试时注入固定种子以获得可重复结果

    公式：
        raw   = base_delay * factor ** (attempt - 1) * (1 ± jitter_ratio)
        delay = min(raw, max_delay)

    以默认参数（base=2, factor=2）为例，重试间隔依次为：
        2s → 4s → 8s → 16s → 32s ...

    为什么必须有抖动？
    假设下游服务在某一刻挂掉，同一秒里有 500 条事件的投递同时失败。
    如果退避是纯指数、没有随机成分，这 500 条任务会在完全相同的时刻
    一起重试（2 秒后、4 秒后……），形成一波脉冲流量。
    下游刚恢复就被这波脉冲再次打垮，于是进入下一轮"同时失败、同时重试"，
    这个循环很难自己走出来。抖动把重试时间摊到一个区间上，
    让流量变平缓，给下游留出恢复的余地。

    为什么是"先抖动、后封顶"而不是相反？
    这里先加抖动、最后取 min，保证返回值严格不超过 max_delay。
    如果反过来的话，抖动会把已经封顶的值再抬上去，上限就名不副实了。
    """
    if attempt < 1:
        raise ValueError("attempt 从 1 开始计数")

    source = rng if rng is not None else random
    raw = base_delay * (factor ** (attempt - 1))

    if jitter_ratio > 0:
        # uniform(-r, r) 得到 -r ~ +r 的乘数，即上下浮动 jitter_ratio
        raw *= 1 + source.uniform(-jitter_ratio, jitter_ratio)

    return max(0.0, min(raw, max_delay))
