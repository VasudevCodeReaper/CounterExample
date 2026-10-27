import heapq


def shortest(n: int, edges: list[list[int]], src: int) -> list[float]:
    """Dijkstra, O((N + E) log N). Silently wrong once a negative weight appears."""
    adj = [[] for _ in range(n)]
    for u, v, w in edges:
        adj[u].append((v, w))
    dist = [float("inf")] * n
    dist[src] = 0
    done = [False] * n
    heap = [(0, src)]
    while heap:
        d, u = heapq.heappop(heap)
        if done[u]:
            continue
        done[u] = True
        for v, w in adj[u]:
            if not done[v] and d + w < dist[v]:
                dist[v] = d + w
                heapq.heappush(heap, (dist[v], v))
    return dist
