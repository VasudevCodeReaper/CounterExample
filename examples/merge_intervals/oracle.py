def merge(intervals: list[list[int]]) -> list[list[int]]:
    """Brute force over integer points: two intervals belong together when they
    share a point. O(range * N), obviously correct for closed integer intervals."""
    if not intervals:
        return []
    groups = [[lo, hi] for lo, hi in intervals]
    changed = True
    while changed:
        changed = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                a, b = groups[i], groups[j]
                if a[0] <= b[1] and b[0] <= a[1]:
                    groups[i] = [min(a[0], b[0]), max(a[1], b[1])]
                    del groups[j]
                    changed = True
                    break
            if changed:
                break
    return sorted(groups)
