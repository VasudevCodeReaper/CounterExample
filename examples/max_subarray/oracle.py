def max_subarray(nums: list[int]) -> int:
    """Brute force: try every subarray. O(N^2), obviously correct."""
    best = nums[0]
    for i in range(len(nums)):
        total = 0
        for j in range(i, len(nums)):
            total += nums[j]
            best = max(best, total)
    return best
