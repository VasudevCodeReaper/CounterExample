def merge(intervals: list[list[int]]) -> list[list[int]]:
    """Sort + sweep, O(N log N). Bug: touching intervals ([1, 2], [2, 3]) are not merged."""
    out: list[list[int]] = []
    for lo, hi in sorted(intervals):
        if out and lo < out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out
