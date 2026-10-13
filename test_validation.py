"""Проверки допуска в пул и изоляции источников, без сетевых запросов."""
import asyncio
import copy
import io
import tempfile
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import app


URI = "vless://test@example.com:443"


class ValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_speed_measured_for_all_stable_on_every_recheck(self):
        data = copy.deepcopy(app.public_data)
        fresh_speed = {"mbps": 10.0, "checked_at": 9999999999}
        data["black"]["working"] = [
            {"uri": URI + "#stable-fresh", "source": "igareck", "successful_checks": 11,
             "speed": dict(fresh_speed)},
            {"uri": URI + "#unstable", "source": "igareck", "successful_checks": 1},
        ]
        data["white"]["working"] = []
        data["status"]["new_since_recheck"] = 999
        response = io.BytesIO((URI + "#new").encode())
        response.headers = {}
        with patch.object(app, "public_data", data), \
             patch.object(app, "CHECK_LOCK", asyncio.Lock()), \
             patch.object(app, "AVAILABLE_LISTS", {"a": {"name": "A", "type": "black", "url": "https://example.com"}}), \
             patch.object(app, "find_singbox", return_value=(Path(app.__file__), "test")), \
             patch.object(app.urllib.request, "urlopen", return_value=response), \
             patch.object(app, "probe_uri_standalone", AsyncMock(return_value=(True, 10))), \
             patch.object(app, "probe_services", AsyncMock(return_value={})) as services, \
             patch.object(app, "save_pool"), patch.object(app, "CHECK_STATS", {}):
            await app.run_check_core("black")
        speed_calls = [c.args[1] for c in services.await_args_list if c.kwargs.get("measure_speed")]
        # Свежий замер не освобождает: скорость перепроверяется у всех стабильных.
        self.assertIn(URI + "#stable-fresh", speed_calls)
        self.assertNotIn(URI + "#unstable", speed_calls)
        self.assertNotIn(URI + "#new", speed_calls)

    def test_speed_download_validation_and_proxy(self):
        for status, content_type, chunks, expected in [
            (200, "application/octet-stream", [b"x" * 500000], 2.0),
            (200, "application/octet-stream", [b"x" * 100], None),
            (200, "text/html", [b"blocked"], None),
            (403, "application/octet-stream", [], None),
        ]:
            response = Mock(status_code=status, headers={"Content-Type": content_type})
            response.iter_content.return_value = chunks
            with patch.object(app.browser_requests, "Session") as factory, \
                 patch.object(app, "SPEED_BYTES", 500000), \
                 patch.object(app.time, "monotonic", side_effect=[10, 11, 12]):
                session = factory.return_value.__enter__.return_value
                session.get.return_value = response
                result = app.measure_download_speed(12345)
            # Округление до сотых Мбит/с.
            self.assertEqual(result["mbps"], round(expected, 2) if expected is not None else None)
            self.assertEqual("error" in result, expected is None)
            factory.assert_called_once_with(trust_env=False)
            self.assertEqual(session.get.call_args.kwargs["proxy"], "http://127.0.0.1:12345")
            response.close.assert_called_once()

    @unittest.skipUnless(shutil.which("node"), "Node.js нужен для проверки JavaScript")
    def test_speed_sorting_and_expiration(self):
        html = Path(app.__file__).with_name("index.html").read_text(encoding="utf-8")
        script = re.findall(r"<script>(.*?)</script>", html, re.S)[0]
        script = script[:script.index('// === ЗАПУСК ===')]
        script += '''
            const assert = require('node:assert/strict');
            const fresh = {stable: true, latency: 500, speed: {mbps: 20, checked_at: Date.now()/1000}};
            const fast = {stable: false, latency: 10, speed: {mbps: 50, checked_at: Date.now()/1000}};
            const stale = {speed: {mbps: 100, checked_at: Date.now()/1000 - 3601}};
            assert.equal(currentSpeed(stale), null);
            assert.equal(currentSpeed({}), null);
            assert.ok(compareConfigs(fresh, fast, 'services', 'speed') < 0);
            assert.ok(compareConfigs(fast, fresh, 'black', 'speed') < 0);
            assert.ok(compareConfigs(fresh, stale, 'stable', 'speed') < 0);
            assert.ok(speedCell(stale).includes('Устарело'));
            const checkedAt = '20.09.2026 12:34:56';
            const config = {uri: 'vless://test@203.0.113.123:443#Test', stable: true,
                source: 'igareck', services: {checked_at: checkedAt, results: {gpt: {ok: true}}}};
            publicData = {black: {working: [config, {...config, source: 'goida'}]},
                white: {working: [config]}, services: {working: [config]}};
            const elements = {};
            global.document = {getElementById(id) {
                return elements[id] ||= {value: id.startsWith('speedFilter') ? '0' : 'all', checked: false};
            }};
            for (const type of tableTypes) {
                renderResults(type);
                const suffix = type.charAt(0).toUpperCase() + type.slice(1);
                const row = elements['working' + suffix].innerHTML;
                assert.ok(row.includes(checkedAt), type);
                assert.ok(row.includes('203.0.113.123:443'), type);
                assert.ok(row.includes('server-address'), type);
                assert.ok(row.includes('actions-cell'), type);
            }
            assert.ok(renderServiceDetails({services: {checked_at: checkedAt,
                results: {gpt: {ok: false}}}}).includes(checkedAt));
            assert.ok(renderServiceDetails({}).includes('ещё не выполнялась'));
        '''
        result = subprocess.run(["node"], input=script, text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js нужен для проверки JavaScript")
    def test_public_javascript_syntax(self):
        html = Path(app.__file__).with_name("index.html").read_text(encoding="utf-8")
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertTrue(scripts)
        result = subprocess.run(["node", "--check"], input="\n".join(scripts),
                                text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    async def test_requires_two_successes(self):
        for results, expected, calls in [
            ([(True, 10), (True, 20)], True, 2),
            ([(True, 10), (False, "timeout")], False, 2),
            ([(False, "timeout")], False, 1),
        ]:
            probe = AsyncMock(side_effect=[("tag", URI, ok, result) for ok, result in results])
            with patch.object(app, "_probe_outbound", probe):
                ok, result = await app.probe_uri_standalone(Path(app.__file__), "test", URI)
            self.assertEqual(ok, expected)
            self.assertEqual(probe.await_count, calls)
            if ok:
                self.assertEqual(result, 20)
            elif calls == 2:
                self.assertIn("2/2", result)

    async def test_selected_source_preserves_other_pool(self):
        data = copy.deepcopy(app.public_data)
        other = {"uri": URI, "latency": 42, "source": "goida"}
        data["black"]["working"] = [other]
        lists = {"a": {"name": "A", "type": "black", "url": "https://example.com/a"},
                 "b": {"name": "B", "type": "black", "source": "goida", "url": "https://example.com/b"}}
        response = io.BytesIO(URI.encode())
        response.headers = {}
        with patch.object(app, "public_data", data), \
             patch.object(app, "CHECK_LOCK", asyncio.Lock()), \
             patch.object(app, "AVAILABLE_LISTS", lists), \
             patch.object(app, "find_singbox", return_value=(Path(app.__file__), "test")), \
             patch.object(app.urllib.request, "urlopen", return_value=response) as fetch, \
             patch.object(app, "probe_uri_standalone", AsyncMock(return_value=(True, 20))), \
             patch.object(app, "probe_services", AsyncMock(return_value={})), \
             patch.object(app, "save_pool"), patch.object(app, "CHECK_STATS", {}):
            working, failed, total = await app.run_check_core("black", source_id="igareck")
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[0].full_url, "https://example.com/a")
        self.assertEqual(len(working), 1)
        self.assertEqual({k: v for k, v in working[0].items() if k != "added_at"},
                         {"uri": URI, "latency": 20, "source": "igareck", "services": {},
                          "successful_checks": 1, "stable": False})
        self.assertRegex(working[0]["added_at"], r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}")
        self.assertEqual(data["black"]["working"], [other, *working])
        self.assertEqual((failed, total), ([], 1))

    async def test_failed_confirmation_never_published(self):
        data = copy.deepcopy(app.public_data)
        data["black"]["working"] = []
        response = io.BytesIO(URI.encode())
        response.headers = {}
        probe = AsyncMock(side_effect=[("t", URI, True, 10), ("t", URI, False, "timeout")])
        with patch.object(app, "public_data", data), \
             patch.object(app, "CHECK_LOCK", asyncio.Lock()), \
             patch.object(app, "AVAILABLE_LISTS", {"a": {"name": "A", "type": "black", "url": "https://example.com"}}), \
             patch.object(app.urllib.request, "urlopen", return_value=response), \
             patch.object(app, "find_singbox", return_value=(Path(app.__file__), "test")), \
             patch.object(app, "_probe_outbound", probe), \
             patch.object(app, "save_pool"), patch.object(app, "CHECK_STATS", {}):
            working, failed, total = await app.run_check_core("black")
        self.assertEqual(working, [])
        self.assertEqual(data["black"]["working"], [])
        self.assertEqual(total, 1)
        self.assertIn("2/2", failed[0]["error"])

    async def test_thousand_new_uris_rechecks_both_pools_before_next_candidate(self):
        data = copy.deepcopy(app.public_data)
        existing = {"uri": URI, "latency": 1, "source": "igareck", "successful_checks": 10}
        dead = {"uri": URI + "#dead", "source": "goida"}
        data["black"]["working"] = [existing]
        data["white"]["working"] = [dead]
        candidates = [URI + f"#new{i}" for i in range(1001)]
        # Уже добавленный URI и дубликаты не должны приблизить границу.
        response = io.BytesIO(('\n'.join([URI] * 10 + candidates[:1000] * 2 + candidates[1000:])).encode())
        response.headers = {}
        calls = []

        async def probe(path, tag, uri):
            calls.append(uri)
            return (False, "timeout") if uri == dead["uri"] else (True, 10)

        with patch.object(app, "public_data", data), \
             patch.object(app, "CHECK_LOCK", asyncio.Lock()), \
             patch.object(app, "AVAILABLE_LISTS", {"a": {"name": "A", "type": "black", "url": "https://example.com"}}), \
             patch.object(app, "find_singbox", return_value=(Path(app.__file__), "test")), \
             patch.object(app.urllib.request, "urlopen", return_value=response), \
             patch.object(app, "probe_uri_standalone", side_effect=probe), \
             patch.object(app, "probe_services", AsyncMock(return_value={})) as services, \
             patch.object(app, "save_pool"), patch.object(app, "CHECK_STATS", {}):
            working, _, _ = await app.run_check_core("black")
        self.assertEqual(calls.count(URI), 1)
        self.assertEqual(set(calls[:1000]), set(candidates[:1000]))
        self.assertEqual(data["status"]["new_since_recheck"], 1)
        self.assertEqual(calls.count(candidates[999]), 2)
        self.assertEqual(calls.count(candidates[1000]), 1)
        self.assertLess(calls.index(dead["uri"]), calls.index(candidates[1000]))
        self.assertEqual(data["white"]["working"], [])
        self.assertTrue(next(c for c in working if c["uri"] == URI)["stable"])
        self.assertFalse(next(c for c in working if c["uri"] == candidates[0])["stable"])
        speed_calls = [call for call in services.await_args_list if call.kwargs.get("measure_speed")]
        self.assertEqual(len(speed_calls), 1)
        self.assertEqual(speed_calls[0].args[1], URI)

    async def test_counter_continues_between_lists_without_startup_recheck(self):
        data = copy.deepcopy(app.public_data)
        data["black"]["working"] = [{"uri": URI, "source": "igareck"}]
        data["white"]["working"] = []
        data["status"]["new_since_recheck"] = 998
        lists = {t: {"name": t, "type": t, "url": f"https://example.com/{t}"}
                 for t in ("black", "white")}
        responses = [io.BytesIO((URI + "#" + t).encode()) for t in lists]
        for response in responses:
            response.headers = {}
        probe = AsyncMock(return_value=(True, 10))
        progress = []

        async def report(event):
            progress.append(event)

        with patch.object(app, "public_data", data), \
             patch.object(app, "CHECK_LOCK", asyncio.Lock()), \
             patch.object(app, "AVAILABLE_LISTS", lists), \
             patch.object(app, "find_singbox", return_value=(Path(app.__file__), "test")), \
             patch.object(app.urllib.request, "urlopen", side_effect=responses), \
             patch.object(app, "probe_uri_standalone", probe), \
             patch.object(app, "probe_services", AsyncMock(return_value={})), \
             patch.object(app, "save_pool"), patch.object(app, "CHECK_STATS", {}):
            await app.run_check_core("black", report)
            self.assertEqual(probe.await_count, 1)
            self.assertEqual(data["status"]["new_since_recheck"], 999)
            self.assertEqual(len(data["black"]["working"]), 2)
            await app.run_check_core("white", report)
        self.assertEqual(probe.await_count, 5)  # Две новые + три актуальные.
        self.assertEqual(data["status"]["new_since_recheck"], 0)
        self.assertTrue(all(p["total"] == 1 for p in progress))
        pool_progress = [p for p in progress if p["source"].get("pool")]
        self.assertTrue(pool_progress)
        self.assertTrue(all(p["current"] == 1 for p in pool_progress))

    async def test_recheck_refreshes_services_across_all_tables_and_sources(self):
        data = copy.deepcopy(app.public_data)
        old = {"checked_at": "старое время", "results": {"gpt": {"ok": True}}}
        data["black"]["working"] = [
            {"uri": URI + "#first", "source": "igareck", "successful_checks": 11, "services": old},
            {"uri": URI + "#second", "source": "goida", "successful_checks": 1, "services": old},
        ]
        data["white"]["working"] = [
            {"uri": URI + "#white", "source": "igareck", "successful_checks": 11, "services": old}]
        data["status"]["new_since_recheck"] = 999
        response = io.BytesIO((URI + "#new").encode())
        response.headers = {}
        fresh = {"checked_at": "20.09.2026 12:34:56", "results": {"gpt": {"ok": True}}}

        async def services(path, uri, **kwargs):
            report = copy.deepcopy(fresh)
            # Сервер из второго источника потерял доступ к ChatGPT, но VPN работает.
            if uri.endswith("#second"):
                report["results"]["gpt"]["ok"] = False
            return report

        with patch.object(app, "public_data", data), \
             patch.object(app, "CHECK_LOCK", asyncio.Lock()), \
             patch.object(app, "AVAILABLE_LISTS", {"a": {"name": "A", "type": "black", "url": "https://example.com"}}), \
             patch.object(app, "find_singbox", return_value=(Path(app.__file__), "test")), \
             patch.object(app.urllib.request, "urlopen", return_value=response), \
             patch.object(app, "probe_uri_standalone", AsyncMock(return_value=(True, 10))), \
             patch.object(app, "probe_services", side_effect=services) as probes, \
             patch.object(app, "save_pool"), patch.object(app, "CHECK_STATS", {}):
            await app.run_check_core("black", source_id="igareck")
            snapshot = await app.get_public_status()
        for t in ("black", "white", "services"):
            self.assertTrue(snapshot[t]["working"])
            for config in snapshot[t]["working"]:
                self.assertEqual(config["services"]["checked_at"], fresh["checked_at"])
        self.assertEqual(probes.call_count, 5)  # Новая конфигурация + весь пул из четырёх.
        self.assertEqual(len(snapshot["black"]["working"]), 3)
        self.assertNotIn(URI + "#second", [c["uri"] for c in snapshot["services"]["working"]])

    async def test_pool_roundtrip_preserves_sources_and_legacy(self):
        data = copy.deepcopy(app.public_data)
        data["black"]["working"] = [
            {"uri": URI, "source": "igareck", "latency": 10, "successful_checks": 11,
             "speed": {"mbps": 25.5, "checked_at": 12345},
             "services": {"checked_at": "20.09.2026 12:34:56", "results": {"gpt": {"ok": True}}}},
            {"uri": URI, "source": "goida", "latency": 20, "successful_checks": 10},
            {"uri": URI, "latency": 30},
        ]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app, "POOL_FILE", Path(directory) / "pool.json"), \
             patch.object(app, "public_data", data):
            app.save_pool()
            pool = app.load_pool()
        self.assertEqual([c["source"] for c in pool["black"]], ["igareck", "goida", "unknown"])
        self.assertTrue(pool["black"][0]["stable"])
        self.assertEqual(pool["black"][0]["speed"], {"mbps": 25.5, "checked_at": 12345})
        self.assertEqual(pool["black"][0]["services"], data["black"]["working"][0]["services"])
        self.assertFalse(pool["black"][1]["stable"])
        # added_at backfill для старого пула + roundtrip.
        for c in pool["black"]:
            self.assertRegex(c["added_at"], r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}")

    async def test_service_list_requires_all_services_and_deduplicates(self):
        good = {"uri": URI, "source": "igareck", "services": {
            "checked_at": "15.09.2026 12:00:00",
            "results": {name: {"ok": True} for name in app.SERVICE_TARGETS}}}
        partial = copy.deepcopy(good)
        partial["uri"] = URI + "#partial"
        partial["services"]["results"]["gpt"] = {"ok": False}
        data = {"black": {"working": [good, partial, {"uri": URI + "#untested"}]},
                "white": {"working": [copy.deepcopy(good)]}}
        with patch.object(app, "public_data", data):
            result = app.get_service_working()
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["lists"], ["black", "white"])
            self.assertNotIn("lists", good)
            data["black"]["working"] = []
            data["white"]["working"] = []
            self.assertEqual(app.get_service_working(), [])

    async def test_service_response_validation_and_forced_proxy(self):
        for status, content_type, body, expected in [
            (200, "text/html", b"<html>ChatGPT</html>", True),
            (403, "text/html", b"ChatGPT", False),
            (200, "text/html", b"ChatGPT Just a moment cf-chl-test", False),
            (200, "text/html", b"Provider blocking page", False),
            (200, "application/json", b"ChatGPT", False),
        ]:
            response = Mock()
            response.status_code = status
            response.headers = {"Content-Type": content_type}
            response.iter_content.return_value = [body]
            session = Mock()
            session.get.return_value = response
            with patch.object(app.browser_requests, "Session") as factory:
                factory.return_value.__enter__.return_value = session
                result = app.check_service_page(12345, "https://chatgpt.com/", (b"chatgpt",))
            self.assertEqual(result["ok"], expected)
            factory.assert_called_once_with(trust_env=False)
            self.assertEqual(session.get.call_args.kwargs["proxy"], "http://127.0.0.1:12345")
            self.assertEqual(session.get.call_args.kwargs["impersonate"], "chrome")
            response.close.assert_called_once()

    async def test_service_probe_cleans_up_process_on_cancellation(self):
        process = Mock(returncode=None)
        process.wait = AsyncMock()
        writer = Mock()
        writer.wait_closed = AsyncMock()
        config_paths = []

        async def spawn(*args, **kwargs):
            config_paths.append(Path(args[3]))
            return process

        with patch.object(app.asyncio, "create_subprocess_exec", side_effect=spawn), \
             patch.object(app.asyncio, "open_connection", AsyncMock(return_value=(None, writer))), \
             patch.object(app.asyncio, "to_thread", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await app.probe_services(Path(app.__file__), URI)
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
        self.assertTrue(config_paths)
        self.assertFalse(config_paths[0].exists())


if __name__ == "__main__":
    unittest.main()
