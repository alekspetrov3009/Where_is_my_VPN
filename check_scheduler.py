"""Пропорциональные пулы: только целочисленные квоты, без копирования URI."""
import heapq
import math


def source_weights(counts, cap):
    """Веса источников для пропорционального распределения.

    Чистая пропорция по размеру даёт перекос: один агрегатор на 15 000
    конфигураций забирал бы ~42% каждого пула, а остальные источники
    разбавлялись бы до единиц. Поэтому вес — корень из размера (сглаживает
    разрыв между 15 000 и 100), дополнительно ограниченный множителем cap
    относительно самого маленького источника.

    Источники с нулём конфигураций получают нулевой вес.
    """
    positive = {s: n for s, n in counts.items() if n > 0}
    if not positive:
        return dict.fromkeys(counts, 0)
    smallest = min(positive.values())
    weights = {}
    for source, count in counts.items():
        if count <= 0:
            weights[source] = 0
            continue
        weight = math.sqrt(count)
        if cap and smallest > 0:
            weight = min(weight, math.sqrt(smallest) * cap)
        weights[source] = weight
    return weights


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
        if denominator <= 0:
            # Все веса обнулились (например, cap=0): делим поровну, чтобы
            # пул не остался пустым.
            shares = {s: divmod(budget, len(active)) for s in active}
        else:
            # Дробные веса: считаем долю в float, целую часть берём, остаток
            # распределяем по наибольшим дробным частям. Целочисленное
            # деление здесь теряло бы доли и давало недобор квоты.
            exact = {s: budget * weights[s] / denominator for s in active}
            shares = {s: (int(exact[s]), exact[s] - int(exact[s])) for s in active}
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
        # budget здесь — реальный остаток после целых частей; он меньше числа
        # активных источников, поэтому срез по нему безопасен.
        for source in sorted(active, key=lambda s: (-shares[s][1], s))[:budget]:
            quotas[source] += 1
        budget = 0
        break
    return quotas


def build_pool_plan(counts, weight_cap=4.0):
    """План пулов: сглаженные веса источников вместо чистой пропорции.

    weight_cap ограничивает, во сколько раз вес источника может превышать
    вес самого маленького. 0 или None — без ограничения (старое поведение).
    """
    total = sum(counts.values())
    pool_size = (total + 4) // 5
    weights = source_weights(counts, weight_cap)
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
