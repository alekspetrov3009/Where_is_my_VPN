"""Пропорциональные пулы: только целочисленные квоты, без копирования URI."""
import heapq


def allocate_quotas(remaining, weights, capacity):
    """Ограниченные остатком квоты; округление методом наибольших остатков."""
    available = {s: n for s, n in remaining.items() if n > 0}
    budget = min(capacity, sum(available.values()))
    quotas = dict.fromkeys(remaining, 0)
    # Если места достаточно, даже самый маленький источник представлен в пуле.
    if budget >= len(available):
        for source in available:
            quotas[source] = 1
            available[source] -= 1
            budget -= 1
    while budget:
        active = {s: n for s, n in available.items() if n}
        denominator = sum(weights[s] for s in active)
        shares = {s: divmod(budget * weights[s], denominator) for s in active}
        exhausted = [s for s in active if shares[s][0] >= active[s]]
        if exhausted:
            for source in exhausted:
                take = available[source]
                quotas[source] += take
                budget -= take
                available[source] = 0
            continue
        for source, (take, _) in shares.items():
            quotas[source] += take
            available[source] -= take
            budget -= take
        for source in sorted(active, key=lambda s: (-shares[s][1], s))[:budget]:
            quotas[source] += 1
        break
    return quotas


def build_pool_plan(counts):
    total = sum(counts.values())
    pool_size = (total + 4) // 5
    weights = {s: min(n, pool_size) for s, n in counts.items()}
    remaining = dict(counts)
    pools = []
    while any(remaining.values()):
        quotas = allocate_quotas(remaining, weights, pool_size)
        pools.append(quotas)
        remaining = {s: n - quotas[s] for s, n in remaining.items()}
    return {"total": total, "pool_size": pool_size, "pool_count": len(pools),
            "counts": dict(counts), "weights": weights, "pools": pools}


def interleave_ranges(groups, quotas, offsets):
    """Плавное взвешенное чередование; каждый источник начинает с одного URI.

    offsets изменяются сразу на полные квоты, даже если потребитель фильтрует
    некоторые типы списков. Дополнительная память — O(количество источников).
    """
    heap = []
    for source, quota in quotas.items():
        start = offsets.get(source, 0)
        offsets[source] = start + quota
        if quota:
            heapq.heappush(heap, (0, source, start, 0, quota))
    while heap:
        _, source, start, used, quota = heapq.heappop(heap)
        yield source, groups[source][start + used]
        used += 1
        if used < quota:
            heapq.heappush(heap, (used / quota, source, start, used, quota))
