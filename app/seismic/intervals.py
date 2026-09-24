"""半开区间 [start, end) 上的纯函数：重叠合并与差集。

区间端点均为可比较的任意类型（实际使用 datetime 或整数刻度）。
合并时按 (start, end) 确定性排序，因此重叠窗口的结果不受录入顺序影响。
"""
from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


def merge_intervals(intervals: list[tuple[T, T]]) -> list[tuple[T, T]]:
    """合并相互重叠或相接的区间，按起点排序。"""
    ordered = sorted(((s, e) for s, e in intervals if e > s), key=lambda item: (item[0], item[1]))
    merged: list[tuple[T, T]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def subtract_intervals(base: list[tuple[T, T]], holes: list[tuple[T, T]]) -> list[tuple[T, T]]:
    """从已合并的 base 区间中减去 holes（holes 无需预先合并）。"""
    cut = sorted(merge_intervals(holes), key=lambda item: item[0])
    result: list[tuple[T, T]] = []
    for start, end in merge_intervals(base):
        cursor = start
        for hole_start, hole_end in cut:
            if hole_end <= cursor:
                continue
            if hole_start >= end:
                break
            if hole_start > cursor:
                result.append((cursor, min(hole_start, end)))
            cursor = max(cursor, hole_end)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
    return result


def intersect_intervals(left: list[tuple[T, T]], right: list[tuple[T, T]]) -> list[tuple[T, T]]:
    """两个已合并区间集合的交集。"""
    a, b = merge_intervals(left), merge_intervals(right)
    result: list[tuple[T, T]] = []
    i = j = 0
    while i < len(a) and j < len(b):
        start = max(a[i][0], b[j][0])
        end = min(a[i][1], b[j][1])
        if start < end:
            result.append((start, end))
        if a[i][1] <= b[j][1]:
            i += 1
        else:
            j += 1
    return result


def total_length(intervals: list[tuple[T, T]]) -> float:
    return sum((end - start).total_seconds() for start, end in intervals)
