"""纯计算模块：几个样本统计函数。

无外部依赖，可单独测试。
"""

from typing import Iterable


def mean(values: Iterable[float]) -> float:
    """算术平均。空序列返回 0.0。"""
    nums = list(values)
    if not nums:
        return 0.0
    return sum(nums) / len(nums)


def median(values: Iterable[float]) -> float:
    """中位数。空序列返回 0.0。"""
    nums = sorted(values)
    if not nums:
        return 0.0
    n = len(nums)
    mid = n // 2
    if n % 2 == 1:
        return float(nums[mid])
    return (nums[mid - 1] + nums[mid]) / 2.0


def variance(values: Iterable[float]) -> float:
    """总体方差（除以 n，不是 n-1）。"""
    nums = list(values)
    if not nums:
        return 0.0
    m = mean(nums)
    return sum((x - m) ** 2 for x in nums) / len(nums)
