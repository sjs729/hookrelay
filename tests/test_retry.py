"""退避策略的单元测试。

这个模块是纯函数，因此测试可以做到完全确定：关掉抖动就是精确的指数序列，
打开抖动就注入固定种子的随机源，结果可重复。
"""

import random

import pytest

from app.services.retry import compute_delay

# 关掉抖动的公共参数，用来验证纯指数部分
NO_JITTER = {"jitter_ratio": 0.0}


class TestExponentialGrowth:
    """指数增长部分。"""

    @pytest.mark.parametrize(
        ("attempt", "expected"),
        [(1, 2.0), (2, 4.0), (3, 8.0), (4, 16.0), (5, 32.0)],
    )
    def test_delay_doubles_each_attempt(self, attempt: int, expected: float) -> None:
        """每次失败后等待时间翻倍。"""
        assert compute_delay(attempt, base_delay=2.0, factor=2.0, **NO_JITTER) == expected

    def test_custom_factor(self) -> None:
        """factor 决定放大倍数，改成 3 就是三倍增长。"""
        assert compute_delay(3, base_delay=1.0, factor=3.0, **NO_JITTER) == 9.0

    def test_custom_base_delay(self) -> None:
        """base_delay 决定首次失败的等待时长。"""
        assert compute_delay(1, base_delay=10.0, factor=2.0, **NO_JITTER) == 10.0


class TestMaxDelayCap:
    """封顶行为。"""

    def test_capped_at_max_delay(self) -> None:
        """指数增长到一定程度会被 max_delay 截住，不会无限膨胀。"""
        assert compute_delay(20, base_delay=2.0, factor=2.0, max_delay=60.0, **NO_JITTER) == 60.0

    def test_just_below_cap_is_not_capped(self) -> None:
        """还没到上限的值不应被改动。"""
        assert compute_delay(4, base_delay=2.0, factor=2.0, max_delay=60.0, **NO_JITTER) == 16.0

    def test_jitter_never_breaks_the_cap(self) -> None:
        """加了抖动也不能突破上限——这是先抖动后取 min 的意义。"""
        rng = random.Random(42)
        for attempt in range(1, 30):
            value = compute_delay(
                attempt, base_delay=2.0, max_delay=60.0, jitter_ratio=0.5, rng=rng
            )
            assert value <= 60.0


class TestJitter:
    """抖动行为。"""

    def test_jitter_stays_within_ratio(self) -> None:
        """结果落在基准值的 ±jitter_ratio 区间内。"""
        rng = random.Random(7)
        baseline = compute_delay(3, base_delay=2.0, factor=2.0, **NO_JITTER)
        for _ in range(200):
            value = compute_delay(3, base_delay=2.0, factor=2.0, jitter_ratio=0.2, rng=rng)
            assert baseline * 0.8 <= value <= baseline * 1.2

    def test_jitter_actually_varies(self) -> None:
        """抖动确实产生不同结果，否则等于没加。"""
        rng = random.Random(1)
        values = {compute_delay(3, base_delay=2.0, jitter_ratio=0.2, rng=rng) for _ in range(50)}
        assert len(values) > 1

    def test_zero_jitter_is_deterministic(self) -> None:
        """关掉抖动后结果恒定，这样才能用来做精确断言。"""
        values = {compute_delay(4, base_delay=1.0, **NO_JITTER) for _ in range(20)}
        assert len(values) == 1


class TestBoundaries:
    """边界与非法输入。"""

    def test_attempt_zero_rejected(self) -> None:
        """attempt 从 1 开始，0 属于调用方写错了。"""
        with pytest.raises(ValueError):
            compute_delay(0)

    def test_negative_attempt_rejected(self) -> None:
        with pytest.raises(ValueError):
            compute_delay(-3)

    def test_result_is_never_negative(self) -> None:
        """抖动幅度大于 1 时理论乘数可能为负，返回值仍需保证非负。"""
        rng = random.Random(3)
        for _ in range(200):
            assert compute_delay(1, base_delay=1.0, jitter_ratio=1.5, rng=rng) >= 0.0
