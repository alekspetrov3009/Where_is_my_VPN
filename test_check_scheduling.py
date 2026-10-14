"""Изолированные проверки планировщика: без сети, процессов и записи пула."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original = copy.deepcopy(app.public_data)
        self.original_stats = copy.deepcopy(app.CHECK_STATS)
        self.original_catalog = app.SOURCE_CATALOG
        for kind in ("black", "white"):
            app.public_data[kind]["working"] = []
            app.public_data[kind]["failed_count"] = 0
        self.save = patch.object(app, "save_pool")
        self.save.start()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.schedule_patch = patch.object(
            app, "SCHEDULE_FILE", Path(self.tmpdir.name) / ".vpn_schedule.json")
        self.schedule_patch.start()
        self.addCleanup(self.schedule_patch.stop)

    def tearDown(self):
        self.save.stop()
        app.public_data.clear()
        app.public_data.update(self.original)
        app.CHECK_STATS.clear()
        app.CHECK_STATS.update(self.original_stats)
        app.SOURCE_CATALOG = self.original_catalog

    def make_catalog(self, counts, catalog_id=None):
        """Синтетический каталог: counts = {source: [(kind, uri), ...]}."""
        groups = {s: tuple(items) for s, items in counts.items()}
        plan = app.build_pool_plan({s: len(items) for s, items in counts.items()})
        return {"files": {}, "groups": groups, "plan": plan, "duplicates": 0,
                "id": catalog_id,
                "updated_at": app.time.time(), "errors": {}}

    def seed_schedule(self, scope, catalog_id, next_pool):
        with open(app.SCHEDULE_FILE, "w", encoding="utf-8") as f:
            json.dump({scope: {"catalog_id": catalog_id, "next_pool": next_pool}}, f)

    def read_schedule(self):
        try:
            with open(app.SCHEDULE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except OSError:
            return {}

    async def test_pool_prioritizes_chatgpt_and_removes_failed_entries(self):
        items = [
            {"uri": "ordinary", "successful_checks": 10},
            {"uri": "dead"},
            {"uri": "gpt", "services": {"results": {"gpt": {"ok": True}}}},
        ]
        app.public_data["black"]["working"] = items
        order = []

        async def probe(path, tag, uri):
            order.append(uri)
            return uri != "dead", 50

        completed = asyncio.Event()
        with patch.object(app, "POOL_CONCURRENCY", 1), \
             patch.object(app, "probe_uri_standalone", side_effect=probe), \
             patch.object(app, "probe_services", new=AsyncMock(return_value={"results": {"gpt": {"ok": False}}})):
            task = asyncio.create_task(app.maintain_working_pool(Path(__file__), completed))
            try:
                await asyncio.wait_for(completed.wait(), 2)
                self.assertEqual(order[0], "gpt")
                pool = {c["uri"]: c for c in app.public_data["black"]["working"]}
                self.assertNotIn("dead", pool)
                self.assertTrue(pool["ordinary"]["stable"])
                self.assertEqual(pool["ordinary"]["successful_checks"], 11)
                self.assertEqual(app.get_service_working(), [])
                self.assertTrue(all(c["last_checked_at"] > 0 for c in pool.values()))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_chatgpt_does_not_hold_discovery_slots_and_timeouts_retry(self):
        # 1000 URI одного источника: пул = 200, первый батч совпадает с пулом.
        # Гейт ждёт обнаружения всего первого пула: воркеры должны дойти до
        # конца батча, пока сервисные проверки его начала ещё висят.
        uris = [f"vless://test{i}" for i in range(1000)]
        catalog = self.make_catalog({"test": [("black", uri) for uri in uris]})
        first_pool = set(uris[:catalog["plan"]["pool_size"]])
        self.assertEqual(len(first_pool), 200)
        calls = []
        probed = set()
        first_pool_discovered = asyncio.Event()
        services_active = 0
        services_peak = 0

        async def probe(path, tag, uri, fast=False):
            calls.append((uri, fast))
            probed.add(uri)
            if first_pool <= probed:
                first_pool_discovered.set()
            if uri == uris[0] and fast:
                return False, "Тайм-аут проверки"
            return True, 50

        async def services(path, uri):
            nonlocal services_active, services_peak
            services_active += 1
            services_peak = max(services_peak, services_active)
            try:
                await first_pool_discovered.wait()
                await asyncio.sleep(0)
                return {"results": {"gpt": {"ok": True}}}
            finally:
                services_active -= 1

        with patch.object(app, "find_singbox", return_value=(Path(__file__), "test")), \
             patch.object(app, "get_source_catalog", new=AsyncMock(return_value=catalog)), \
             patch.object(app, "probe_uri_standalone", side_effect=probe), \
             patch.object(app, "probe_services", side_effect=services):
            working, failed, checked = await asyncio.wait_for(app._run_check_core("black"), 15)
        self.assertEqual(len(working), len(uris))
        self.assertEqual(checked, len(uris))
        self.assertEqual(failed, [])
        self.assertIn((uris[0], False), calls)
        self.assertGreater(services_peak, 1)
        self.assertLessEqual(services_peak, app.SERVICE_CONCURRENCY)
        self.assertEqual(len(app.get_service_working()), len(uris))

    async def test_recent_pool_is_not_rechecked(self):
        app.public_data["white"]["working"] = [{"uri": "recent", "last_checked_at": app.time.time()}]
        completed = asyncio.Event()
        with patch.object(app, "probe_uri_standalone", new=AsyncMock()) as probe:
            task = asyncio.create_task(app.maintain_working_pool(Path(__file__), completed))
            try:
                await asyncio.wait_for(completed.wait(), 2)
                probe.assert_not_awaited()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_finishing_cycle_preserves_inflight_speed_result(self):
        app.public_data["black"]["working"] = [{"uri": "stable", "stable": True}]
        started = asyncio.Event()
        finish = asyncio.Event()

        async def measure(path, uri, **kwargs):
            self.assertEqual(kwargs, {"measure_speed": True, "speed_only": True})
            started.set()
            await finish.wait()
            return {"speed": {"checked_at": app.time.time(), "mbps": 12}}

        with patch.object(app, "probe_services", side_effect=measure):
            task = asyncio.create_task(app.maintain_pool_speed(Path(__file__)))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assertEqual(app.public_data["black"]["working"][0]["speed"]["mbps"], 12)

    async def test_cancellation_stops_maintenance_before_rollback(self):
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def maintenance(path, first):
            try:
                app.public_data["white"]["working"].append({"uri": "temporary"})
                started.set()
                await asyncio.Future()
            finally:
                stopped.set()

        async def core(*args):
            await asyncio.Future()

        with patch.object(app, "CHECK_LOCK", asyncio.Lock()), \
             patch.object(app, "find_singbox", return_value=(Path(__file__), "test")), \
             patch.object(app, "maintain_working_pool", side_effect=maintenance), \
             patch.object(app, "maintain_pool_speed", side_effect=core), \
             patch.object(app, "_run_check_core", side_effect=core):
            task = asyncio.create_task(app.run_check_core("black"))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(stopped.is_set())
        self.assertEqual(app.public_data["white"]["working"], [])
        self.assertFalse(app.public_data["status"]["is_checking"])


    async def test_pool_plan_matches_proportional_example(self):
        # Пример из постановки: 1 000 + 10 000 + 1 000 000, пул = 1/5.
        plan = app.build_pool_plan({"s1": 1000, "s2": 10000, "s3": 1000000})
        self.assertEqual(plan["total"], 1011000)
        self.assertEqual(plan["pool_size"], 202200)
        self.assertEqual(plan["pool_count"], 5)
        # Веса сглажены (корень из размера) и ограничены множителем cap:
        # чистую пропорцию не проверяем — она и была источником перекоса.
        weights = plan["weights"]
        self.assertGreater(weights["s1"], 0)
        self.assertLess(weights["s1"], weights["s2"])
        self.assertLessEqual(weights["s2"], weights["s3"])
        # Главный инвариант: каждый источник выбран целиком, ничего не потеряно.
        totals = {s: sum(p[s] for p in plan["pools"]) for s in ("s1", "s2", "s3")}
        self.assertEqual(totals, {"s1": 1000, "s2": 10000, "s3": 1000000})
        # Маленькие источники представлены уже в первом пуле.
        first = plan["pools"][0]
        self.assertEqual(sum(first.values()), 202200)
        self.assertGreater(first["s1"], 0)
        self.assertGreater(first["s2"], 0)
        self.assertEqual(sum(plan["pools"][1].values()), 202200)

    async def test_weight_cap_limits_dominant_source(self):
        """Один агрегатор не должен забирать почти весь пул.

        Реальный случай: hidashimora даёт 15 768 конфигураций против 98 у
        matin. Чистая пропорция отдавала агрегатору ~42% каждого пула.
        """
        counts = {"big": 15768, "mid": 5291, "small": 98}
        capped = app.build_pool_plan(counts, weight_cap=4.0)
        big_share = capped["pools"][0]["big"] / capped["pool_size"]
        self.assertLess(big_share, 0.5, "агрегатор занял больше половины пула")
        # Ограничение не должно терять конфигурации.
        totals = {s: sum(p[s] for p in capped["pools"]) for s in counts}
        self.assertEqual(totals, counts)

    async def test_weight_cap_zero_means_unlimited(self):
        """cap=0 — прежнее поведение (чистая пропорция), для совместимости."""
        counts = {"big": 10000, "small": 100}
        plan = app.build_pool_plan(counts, weight_cap=0)
        self.assertGreater(plan["weights"]["big"], plan["weights"]["small"])
        totals = {s: sum(p[s] for p in plan["pools"]) for s in counts}
        self.assertEqual(totals, counts)

    async def test_all_zero_counts_terminates(self):
        """Источники с нулём не должны зациклить построение плана."""
        plan = app.build_pool_plan({"a": 0, "b": 0})
        self.assertEqual(plan["pools"], [])

    async def test_empty_plan_terminates(self):
        plan = app.build_pool_plan({})
        self.assertEqual(plan["pool_size"], 0)
        self.assertEqual(plan["pools"], [])

    async def test_interleave_starts_every_source_immediately(self):
        groups = {"a": tuple(f"a{i}" for i in range(6)),
                  "b": tuple(f"b{i}" for i in range(2)),
                  "c": ("c0",)}
        quotas = {"a": 6, "b": 2, "c": 1}
        offsets = {}
        order = [s for s, _ in app.interleave_ranges(groups, quotas, offsets)]
        # Каждый источник отдаёт по одному URI раньше, чем кто-либо — второй.
        self.assertEqual(order[:3], ["a", "b", "c"])
        self.assertEqual(len(order), 9)
        self.assertEqual(offsets, quotas)

    async def test_assemble_catalog_dedupes_within_source(self):
        files = {
            "f1": {"info": {"name": "a", "url": "u1", "type": "black"},
                   "uris": ("vless://x", "vless://y"), "error": None},
            "f2": {"info": {"name": "b", "url": "u2", "type": "black"},
                   "uris": ("vless://y", "vless://z"), "error": "boom"},
        }
        catalog = app.assemble_catalog(files)
        self.assertEqual(catalog["duplicates"], 1)
        self.assertEqual(catalog["errors"], {"f2": "boom"})
        self.assertEqual(len(catalog["groups"]["igareck"]), 3)
        self.assertEqual(catalog["plan"]["counts"], {"igareck": 3})

    async def test_canonical_endpoint_strips_only_fragment(self):
        self.assertEqual(app.canonical_endpoint("vless://id@host:1?a=1#name"),
                         "vless://id@host:1?a=1")
        self.assertEqual(app.canonical_endpoint("vless://id@host:1?a=1"), "vless://id@host:1?a=1")
        # Разные параметры — разные точки, не склеиваем.
        self.assertNotEqual(app.canonical_endpoint("vless://id@host:1?sni=a#x"),
                            app.canonical_endpoint("vless://id@host:1?sni=b#x"))



    async def test_assemble_catalog_dedupes_endpoints_across_sources(self):
        files = {
            "a1": {"info": {"name": "a", "url": "u1", "type": "black", "source": "goida"},
                   "uris": ("vless://id@host:1#remark-a", "vless://other@host:2#x"), "error": None},
            "b1": {"info": {"name": "b", "url": "u2", "type": "black"},
                   "uris": ("vless://id@host:1#remark-b", "vless://fresh@host:3#y"), "error": None},
        }
        catalog = app.assemble_catalog(files)
        # Точка host:1 осталась у первого источника с его remark, копия отброшена.
        self.assertEqual(catalog["duplicates"], 1)
        self.assertEqual([u for _, u in catalog["groups"]["goida"]],
                         ["vless://id@host:1#remark-a", "vless://other@host:2#x"])
        self.assertEqual([u for _, u in catalog["groups"]["igareck"]],
                         ["vless://fresh@host:3#y"])
        self.assertEqual(catalog["plan"]["counts"], {"goida": 2, "igareck": 1})

    async def test_known_pool_endpoint_skips_remark_variant(self):
        app.public_data["black"]["working"] = [
            {"uri": "vless://id@host:1#old", "source": "test"}]
        catalog = self.make_catalog({"test": [("black", "vless://id@host:1#new")]})
        with patch.object(app, "find_singbox", return_value=(Path(__file__), "test")), \
             patch.object(app, "get_source_catalog", new=AsyncMock(return_value=catalog)), \
             patch.object(app, "probe_uri_standalone", new=AsyncMock()) as probe:
            working, failed, checked = await asyncio.wait_for(app._run_check_core("black"), 3)
        probe.assert_not_awaited()
        self.assertEqual(checked, 0)
        self.assertEqual(len(working), 1)

    async def test_catalog_cached_for_24h(self):
        app.SOURCE_CATALOG = {"updated_at": app.time.time(), "plan": {"pools": []}}
        with patch.object(app, "fetch_dynamic_lists",
                          new=AsyncMock(side_effect=AssertionError("must use cache"))):
            result = await app.get_source_catalog()
        self.assertIs(result, app.SOURCE_CATALOG)

    async def test_both_run_splits_stats_per_kind(self):
        catalog = self.make_catalog({"src": [("black", "vless://b1"),
                                             ("white", "vless://w1")]})
        with patch.object(app, "find_singbox", return_value=(Path(__file__), "test")), \
             patch.object(app, "get_source_catalog", new=AsyncMock(return_value=catalog)), \
             patch.object(app, "probe_uri_standalone",
                          new=AsyncMock(return_value=(True, 50))), \
             patch.object(app, "probe_services",
                          new=AsyncMock(return_value={"results": {"gpt": {"ok": True}}})):
            working, failed, checked = await asyncio.wait_for(app._run_check_core("both"), 3)
        self.assertEqual(checked, 2)
        self.assertEqual(app.CHECK_STATS["black"]["checked"], 1)
        self.assertEqual(app.CHECK_STATS["white"]["checked"], 1)
        self.assertEqual(app.CHECK_STATS["black"]["working"], 1)
        self.assertEqual(app.CHECK_STATS["white"]["working"], 1)
        self.assertEqual(len(working), 2)


    async def test_resume_continues_from_checkpoint(self):
        # 4 URI одного источника: пул = 1, всего 4 пула. Чекпоинт — с 3-го.
        uris = [f"vless://r{i}" for i in range(4)]
        catalog = self.make_catalog({"a": [("black", uri) for uri in uris]}, catalog_id="cid1")
        self.assertEqual(catalog["plan"]["pool_count"], 4)
        self.seed_schedule("black|", "cid1", 3)
        probed = []

        async def probe(path, tag, uri, fast=False):
            probed.append(uri)
            return True, 50

        with patch.object(app, "find_singbox", return_value=(Path(__file__), "test")), \
             patch.object(app, "get_source_catalog", new=AsyncMock(return_value=catalog)), \
             patch.object(app, "probe_uri_standalone", side_effect=probe), \
             patch.object(app, "probe_services",
                          new=AsyncMock(return_value={"results": {"gpt": {"ok": True}}})):
            working, failed, checked = await asyncio.wait_for(app._run_check_core("black"), 5)
        # Первые два пула пропущены без единого зонда, остальные проверены.
        self.assertEqual(probed, uris[2:])
        self.assertEqual(checked, 2)
        self.assertEqual(len(working), 2)
        # Полный проход сбрасывает чекпоинт своего скоупа.
        self.assertNotIn("black|", self.read_schedule())

    async def test_stale_checkpoint_restarts_from_zero(self):
        uris = [f"vless://s{i}" for i in range(4)]
        catalog = self.make_catalog({"a": [("black", uri) for uri in uris]}, catalog_id="cid2")
        self.seed_schedule("black|", "cid1", 3)
        probed = []

        async def probe(path, tag, uri, fast=False):
            probed.append(uri)
            return True, 50

        with patch.object(app, "find_singbox", return_value=(Path(__file__), "test")), \
             patch.object(app, "get_source_catalog", new=AsyncMock(return_value=catalog)), \
             patch.object(app, "probe_uri_standalone", side_effect=probe), \
             patch.object(app, "probe_services",
                          new=AsyncMock(return_value={"results": {"gpt": {"ok": True}}})):
            working, failed, checked = await asyncio.wait_for(app._run_check_core("black"), 5)
        self.assertEqual(probed, uris)
        self.assertEqual(checked, 4)

    async def test_missing_schedule_starts_from_zero(self):
        self.assertEqual(app.load_check_schedule(), {})
        self.assertEqual(app.schedule_scope("both", None), "both|")
        self.assertEqual(app.schedule_scope("black", "goida"), "black|goida")


if __name__ == "__main__":
    unittest.main()
