def max_subarray(nums: list[int]) -> int:
    """Kadane's algorithm with a classic bug: an all-negative array returns 0."""
    best = cur = 0
    for x in nums:
        cur = max(0, cur + x)
        best = max(best, cur)
    return best
