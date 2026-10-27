def shortest(n: int, edges: list[list[int]], src: int) -> list[float]:
    """Bellman-Ford: correct for negative weights (graphs here have no negative cycles)."""
    INF = float("inf")
    dist = [INF] * n
    dist[src] = 0
    for _ in range(n - 1):
        for u, v, w in edges:
            if dist[u] + w < dist[v]:
                dist[v] = dist[u] + w
    return dist
