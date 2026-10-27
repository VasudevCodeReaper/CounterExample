def lengthOfLIS(nums: list[int]) -> int:
    """O(2^N)-free brute force: O(N^2) DP that is easy to verify by hand."""
    if not nums:
        return 0
    best = [1] * len(nums)
    for i in range(len(nums)):
        for j in range(i):
            if nums[j] < nums[i]:
                best[i] = max(best[i], best[j] + 1)
    return max(best)
