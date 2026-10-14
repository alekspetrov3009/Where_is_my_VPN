"""Проверки кэша нерабочих точек и приоритизации источников.

Сеть не требуется: кэш — чистые функции над файлом, веса — чистая математика.
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app
from check_scheduler import build_pool_plan, source_weights
from config import DEAD_CACHE_SECONDS
from storage import load_dead_cache, save_dead_cache


class DeadCacheStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "dead.json"

    def tearDown(self):
        for path in (self.tmp, self.tmp.with_suffix(".tmp")):
            if path.exists():
                path.unlink()
        self.tmp.parent.rmdir()

    def test_roundtrip(self):
        save_dead_cache({"vless://a@b:443": time.time()}, self.tmp)
        cache = load_dead_cache(self.tmp)
        self.assertEqual(len(cache), 1)

    def test_missing_file_is_empty(self):
        self.assertEqual(load_dead_cache(self.tmp), {})

    def test_corrupt_file_is_empty(self):
        self.tmp.write_text("{not json", encoding="utf-8")
        self.assertEqual(load_dead_cache(self.tmp), {})

    def test_non_dict_json_is_empty(self):
        self.tmp.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(load_dead_cache(self.tmp), {})

    def test_expired_entries_dropped(self):
        """Протухшие записи отбрасываются: точка могла ожить."""
        old = time.time() - DEAD_CACHE_SECONDS - 60
        save_dead_cache({"old://x": old, "fresh://y": time.time()}, self.tmp)
        cache = load_dead_cache(self.tmp)
        self.assertIn("fresh://y", cache)
        self.assertNotIn("old://x", cache)

    def test_bad_values_skipped(self):
        self.tmp.write_text(json.dumps({"a": "not-a-number", "b": None, "": 1,
                                        "c": time.time()}), encoding="utf-8")
        cache = load_dead_cache(self.tmp)
        self.assertEqual(list(cache), ["c"])

    def test_write_is_atomic(self):
        """Прерванная запись не должна оставлять битый файл."""
        save_dead_cache({"a://b": time.time()}, self.tmp)
        save_dead_cache({"c://d": time.time()}, self.tmp)
        self.assertEqual(list(load_dead_cache(self.tmp)), ["c://d"])
        self.assertFalse(self.tmp.with_suffix(".tmp").exists())


class DeadCacheBehaviourTests(unittest.TestCase):
    def setUp(self):
        self._cache = dict(app.DEAD_CACHE)
        self._pending = set(app.DEAD_PENDING)
        app.DEAD_CACHE.clear()
        app.DEAD_PENDING.clear()

    def tearDown(self):
        app.DEAD_CACHE.clear()
        app.DEAD_CACHE.update(self._cache)
        app.DEAD_PENDING.clear()
        app.DEAD_PENDING.update(self._pending)

    def test_mark_then_flush_persists(self):
        app.mark_dead("vless://a@b:443#tag")
        self.assertEqual(len(app.DEAD_PENDING), 1)
        with patch.object(app, "save_dead_cache"):
            app.flush_dead()
        self.assertEqual(app.DEAD_PENDING, set())
        # Ключ — каноническая точка, без #fragment.
        self.assertIn("vless://a@b:443", app.DEAD_CACHE)

    def test_is_dead_matches_remark_variants(self):
        """Один сервер с разными remark — одна точка."""
        app.DEAD_CACHE["vless://a@b:443"] = time.time()
        self.assertTrue(app.is_dead("vless://a@b:443#other-remark"))

    def test_mark_alive_removes_from_cache(self):
        app.DEAD_CACHE["vless://a@b:443"] = time.time()
        with patch.object(app, "save_dead_cache"):
            app.mark_alive("vless://a@b:443#x")
        self.assertFalse(app.is_dead("vless://a@b:443"))

    def test_mark_alive_cancels_pending(self):
        """Ожившая точка не должна попасть в файл в конце прохода."""
        app.mark_dead("vless://a@b:443")
        app.mark_alive("vless://a@b:443")
        self.assertEqual(app.DEAD_PENDING, set())

    def test_flush_without_pending_is_noop(self):
        with patch.object(app, "save_dead_cache") as save:
            app.flush_dead()
        save.assert_not_called()

    def test_empty_cache_never_skips(self):
        self.assertFalse(app.is_dead("vless://anything"))


class DeadCacheSkipsInCheckTests(unittest.IsolatedAsyncioTestCase):
    """Пропуск мёртвых точек должен реально уменьшать объём проверки."""

    async def test_dead_uri_not_probed(self):
        import copy
        dead_uri = "vless://dead@1.2.3.4:443"
        live_uri = "vless://live@5.6.7.8:443"
        catalog = {
            "groups": {"src": (("black", dead_uri), ("black", live_uri))},
            "plan": {"pools": [{"src": 2}], "pool_count": 1, "pool_size": 2, "total": 2,
                     "counts": {"src": 2}, "weights": {"src": 1.0}},
            "duplicates": 0, "errors": {}, "id": "test", "files": {}, "updated_at": time.time(),
        }
        probed = []

        async def fake_probe(singbox_path, tag, uri, fast=False):
            probed.append(uri)
            return True, 10

        data = copy.deepcopy(app.public_data)
        for kind in ("black", "white"):
            data[kind]["working"] = []
        app.DEAD_CACHE["vless://dead@1.2.3.4:443"] = time.time()
        try:
            with patch.object(app, "public_data", data), \
                 patch.object(app, "CHECK_LOCK", __import__("asyncio").Lock()), \
                 patch.object(app, "get_source_catalog", AsyncMock(return_value=catalog)), \
                 patch.object(app, "find_singbox", return_value=(Path(app.__file__), "test")), \
                 patch.object(app, "probe_uri_standalone", fake_probe), \
                 patch.object(app, "probe_services", AsyncMock(return_value={})), \
                 patch.object(app, "resolve_geo", AsyncMock(return_value=None)), \
                 patch.object(app, "save_pool"), patch.object(app, "save_dead_cache"), \
                 patch.object(app, "save_check_schedule"), \
                 patch.object(app, "load_check_schedule", return_value={}), \
                 patch.object(app, "CHECK_STATS", {}):
                await app._run_check_core("black")
        finally:
            app.DEAD_CACHE.pop("vless://dead@1.2.3.4:443", None)
        self.assertIn(live_uri, probed)
        self.assertNotIn(dead_uri, probed, "мёртвая точка была проверена повторно")


class SourceWeightTests(unittest.TestCase):
    def test_sqrt_smoothing(self):
        """Корень из размера сглаживает разрыв между 15 000 и 100."""
        weights = source_weights({"big": 15768, "small": 98}, cap=0)
        self.assertLess(weights["big"] / weights["small"], 15768 / 98)

    def test_cap_binds_on_large_gap(self):
        weights = source_weights({"big": 1000000, "small": 100}, cap=4.0)
        self.assertAlmostEqual(weights["big"] / weights["small"], 4.0, places=6)

    def test_cap_inert_on_similar_sizes(self):
        """При близких размерах ограничение не должно вмешиваться."""
        raw = source_weights({"a": 10000, "b": 9000}, cap=0)
        capped = source_weights({"a": 10000, "b": 9000}, cap=4.0)
        self.assertEqual(raw, capped)

    def test_zero_counts_get_zero_weight(self):
        weights = source_weights({"a": 100, "b": 0}, cap=4.0)
        self.assertEqual(weights["b"], 0)
        self.assertGreater(weights["a"], 0)

    def test_all_zero(self):
        self.assertEqual(source_weights({"a": 0, "b": 0}, cap=4.0), {"a": 0, "b": 0})


class PoolPlanInvariantTests(unittest.TestCase):
    """Главный инвариант: ни одна конфигурация не теряется и не дублируется."""

    CASES = [
        {"s1": 1000, "s2": 10000, "s3": 1000000},
        {"hidashimora": 15768, "solispirit": 5301, "matin": 98},
        {"a": 1, "b": 1, "c": 1},
        {"only": 500},
        {"a": 100000, "b": 1},
    ]

    def test_totals_preserved(self):
        for counts in self.CASES:
            plan = build_pool_plan(counts, weight_cap=4.0)
            totals = {s: sum(p[s] for p in plan["pools"]) for s in counts}
            self.assertEqual(totals, counts, counts)

    def test_pool_sizes_never_exceed_budget(self):
        for counts in self.CASES:
            plan = build_pool_plan(counts, weight_cap=4.0)
            for pool in plan["pools"]:
                self.assertLessEqual(sum(pool.values()), plan["pool_size"], counts)

    def test_quotas_never_exceed_remaining(self):
        for counts in self.CASES:
            plan = build_pool_plan(counts, weight_cap=4.0)
            for source in counts:
                taken = sum(p[source] for p in plan["pools"])
                self.assertLessEqual(taken, counts[source], counts)

    def test_dominant_source_capped(self):
        """Агрегатор не должен забирать больше половины первого пула."""
        counts = {"big": 15768, "mid": 5291, "small": 98}
        plan = build_pool_plan(counts, weight_cap=4.0)
        share = plan["pools"][0]["big"] / plan["pool_size"]
        self.assertLess(share, 0.5)

    def test_small_sources_present_in_first_pool(self):
        counts = {"big": 100000, "small": 5}
        plan = build_pool_plan(counts, weight_cap=4.0)
        self.assertGreater(plan["pools"][0]["small"], 0)


if __name__ == "__main__":
    unittest.main()