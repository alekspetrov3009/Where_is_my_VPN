"""Проверки экспорта в Clash/Mihomo, геопроверки и QUIC-зонда.

Сеть не требуется: YAML разбирается PyYAML (если установлен), гео-API
подменяется заглушкой, QUIC-зонд проверяется на подставном HTTP-клиенте.
"""
import json
import unittest
from unittest.mock import Mock, patch

from clash_export import build_clash_config, uri_to_clash_proxy
from config_parser import extract_host
from prober import measure_quic_reachability

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML не в зависимостях проекта
    yaml = None


VLESS_REALITY = ("vless://11111111-2222-3333-4444-555555555555@1.2.3.4:443"
                 "?security=reality&sni=a.com&pbk=KEY&sid=ab&fp=chrome&type=grpc&serviceName=gs#Berlin")
HY2_OBFS = "hysteria2://pw@5.6.7.8:443?sni=b.com&obfs=salamander&obfs-password=sec#Hy2"
TUIC = ("tuic://11111111-2222-3333-4444-555555555555:pw@9.9.9.9:443"
        "?sni=c.com&congestion_control=bbr&alpn=h3,h2#TUIC")
TROJAN_WS = "trojan://pass@2.2.2.2:443?sni=d.com&type=ws&path=/ws#Trojan"
SS_B64 = "ss://YWVzLTI1Ni1nY206cGFzcw==@3.3.3.3:8388#SS"
VMESS = ("vmess://eyJhZGQiOiI0LjQuNC40IiwiYWlkIjoiMCIsImlkIjoiMTExMTExMTEtMjIyMi0zMzMz"
         "LTQ0NDQtNTU1NTU1NTU1NTU1IiwibmV0Ijoid3MiLCJwYXRoIjoiL3dzIiwicG9ydCI6NDQzLCJz"
         "Y3kiOiJhdXRvIiwidGxzIjoidGxzIiwiaG9zdCI6ImUuY29tIn0=#VMess")

ALL_URIS = [VLESS_REALITY, HY2_OBFS, TUIC, TROJAN_WS, SS_B64, VMESS]


class ClashProxyTests(unittest.TestCase):
    def test_vless_reality_fields(self):
        proxy = uri_to_clash_proxy(VLESS_REALITY, 1)
        self.assertEqual(proxy["type"], "vless")
        self.assertTrue(proxy["tls"])
        self.assertEqual(proxy["reality-opts"]["public-key"], "KEY")
        self.assertEqual(proxy["grpc-opts"]["grpc-service-name"], "gs")
        self.assertEqual(proxy["client-fingerprint"], "chrome")

    def test_hysteria2_obfs_preserved(self):
        """obfs обязан дойти до Clash, иначе сервер не подключится."""
        proxy = uri_to_clash_proxy(HY2_OBFS, 1)
        self.assertEqual(proxy["obfs"], "salamander")
        self.assertEqual(proxy["obfs-password"], "sec")

    def test_tuic_options(self):
        proxy = uri_to_clash_proxy(TUIC, 1)
        self.assertEqual(proxy["congestion-controller"], "bbr")
        self.assertEqual(proxy["alpn"], ["h3", "h2"])

    def test_ss_uses_cipher_key(self):
        """В Clash поле называется cipher, в sing-box — method."""
        proxy = uri_to_clash_proxy(SS_B64, 1)
        self.assertEqual(proxy["cipher"], "aes-256-gcm")
        self.assertEqual(proxy["password"], "pass")
        self.assertNotIn("method", proxy)

    def test_vmess_uses_alterid_and_cipher(self):
        proxy = uri_to_clash_proxy(VMESS, 1)
        self.assertEqual(proxy["alterId"], 0)
        self.assertEqual(proxy["cipher"], "auto")

    def test_unsupported_protocol_returns_none(self):
        self.assertIsNone(uri_to_clash_proxy("wireguard://x@1.2.3.4:51820", 1))

    def test_name_sanitised(self):
        """Двоеточия в remark ломают разбор Clash — они вычищаются.

        Кавычки удалять не нужно: _yaml_str их экранирует, и YAML остаётся
        валидным (проверяется в ClashConfigTests.test_hostile_name_*).
        """
        proxy = uri_to_clash_proxy('vless://u@1.2.3.4:443#He said "hi": ok', 1)
        self.assertNotIn(":", proxy["name"])
        self.assertIn("He said", proxy["name"])


class ClashConfigTests(unittest.TestCase):
    def test_all_protocols_exported(self):
        text, error = build_clash_config(ALL_URIS)
        self.assertIsNone(error)
        self.assertIn("proxies:", text)
        for kind in ("vless", "hysteria2", "tuic", "trojan", "ss", "vmess"):
            self.assertIn(f'type: "{kind}"', text)

    def test_empty_input_is_error_not_empty_file(self):
        text, error = build_clash_config([])
        self.assertIsNone(text)
        self.assertIsNotNone(error)

    def test_all_unparsable_is_error(self):
        text, error = build_clash_config(["garbage://x", "nonsense"])
        self.assertIsNone(text)
        self.assertIsNotNone(error)

    def test_duplicate_names_disambiguated(self):
        """Дубли имён ломают ссылки групп — разводятся суффиксом."""
        text, _ = build_clash_config([VLESS_REALITY, VLESS_REALITY])
        self.assertIn("#2", text)

    @unittest.skipIf(yaml is None, "PyYAML не установлен")
    def test_yaml_is_parseable(self):
        text, _ = build_clash_config(ALL_URIS, mixed_port=7890)
        doc = yaml.safe_load(text)
        self.assertEqual(doc["mixed-port"], 7890)
        self.assertEqual(len(doc["proxies"]), len(ALL_URIS))

    @unittest.skipIf(yaml is None, "PyYAML не установлен")
    def test_nested_opts_are_mappings_not_strings(self):
        """repr Python-dict давал невалидный YAML — проверяем реальные dict."""
        text, _ = build_clash_config(ALL_URIS)
        doc = yaml.safe_load(text)
        found = 0
        for proxy in doc["proxies"]:
            for key in ("ws-opts", "grpc-opts", "reality-opts"):
                if key in proxy:
                    self.assertIsInstance(proxy[key], dict, f"{key} не mapping")
                    found += 1
        self.assertGreater(found, 0, "ни одной вложенной опции не проверено")

    @unittest.skipIf(yaml is None, "PyYAML не установлен")
    def test_group_references_are_valid(self):
        text, _ = build_clash_config(ALL_URIS)
        doc = yaml.safe_load(text)
        names = {proxy["name"] for proxy in doc["proxies"]}
        for group in doc["proxy-groups"]:
            for ref in group["proxies"]:
                self.assertIn(ref, names)

    @unittest.skipIf(yaml is None, "PyYAML не установлен")
    def test_hostile_name_does_not_break_yaml(self):
        text, _ = build_clash_config(['vless://u@1.2.3.4:443#a"b: c, d\ne'])
        doc = yaml.safe_load(text)
        self.assertEqual(len(doc["proxies"]), 1)


class HostExtractionTests(unittest.TestCase):
    def test_hosts_for_all_protocols(self):
        expected = {VLESS_REALITY: "1.2.3.4", HY2_OBFS: "5.6.7.8", TUIC: "9.9.9.9",
                    TROJAN_WS: "2.2.2.2", SS_B64: "3.3.3.3", VMESS: "4.4.4.4"}
        for uri, host in expected.items():
            self.assertEqual(extract_host(uri), host, uri[:40])

    def test_garbage_returns_none(self):
        self.assertIsNone(extract_host("not-a-uri"))


class QuicProbeTests(unittest.TestCase):
    """QUIC-зонд: успех только при фактически согласованном HTTP/3."""

    def _run(self, status=200, version="3", exc=None):
        response = Mock(status_code=status, http_version=version)
        response.close = Mock()
        session = Mock()
        session.get.return_value = response
        if exc:
            session.get.side_effect = exc
        factory = Mock()
        factory.return_value.__enter__ = Mock(return_value=session)
        factory.return_value.__exit__ = Mock(return_value=False)
        with patch("prober.browser_requests.Session", factory):
            return measure_quic_reachability(12345)

    def test_http3_success(self):
        result = self._run(status=200, version="3")
        self.assertTrue(result["ok"])
        self.assertEqual(result["http_version"], "3")

    def test_tcp_fallback_is_not_success(self):
        """Откат на HTTP/2 означает, что QUIC не прошёл — это не успех."""
        result = self._run(status=200, version="2")
        self.assertFalse(result["ok"])
        self.assertIn("QUIC не согласован", result["error"])

    def test_non_200_is_failure(self):
        result = self._run(status=503, version="3")
        self.assertFalse(result["ok"])

    def test_exception_is_captured(self):
        result = self._run(exc=RuntimeError("connection reset"))
        self.assertFalse(result["ok"])
        self.assertIn("connection reset", result["error"])


class GeoLookupTests(unittest.TestCase):
    def setUp(self):
        import geoip
        geoip.clear_cache()
        self.geoip = geoip

    def _respond(self, payload):
        response = Mock()
        response.read.return_value = json.dumps(payload).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_russian_ip_flagged(self):
        payload = {"status": "success", "country": "Russia", "countryCode": "RU",
                   "as": "AS9123 JSC TIMEWEB", "query": "194.87.118.184"}
        with patch("geoip.urllib.request.urlopen", return_value=self._respond(payload)):
            data = self.geoip.lookup_ip("194.87.118.184")
        self.assertTrue(data["is_ru"])
        self.assertEqual(data["asn"], 9123)
        self.assertEqual(data["as_name"], "JSC TIMEWEB")

    def test_foreign_ip_not_flagged(self):
        payload = {"status": "success", "country": "Germany", "countryCode": "DE",
                   "as": "AS207957 SERV.HOST GROUP LTD", "query": "91.196.32.163"}
        with patch("geoip.urllib.request.urlopen", return_value=self._respond(payload)):
            data = self.geoip.lookup_ip("91.196.32.163")
        self.assertFalse(data["is_ru"])
        self.assertEqual(data["country_code"], "DE")

    def test_api_failure_returns_none(self):
        with patch("geoip.urllib.request.urlopen", side_effect=OSError("no network")):
            self.assertIsNone(self.geoip.lookup_ip("1.2.3.4"))

    def test_cache_avoids_second_request(self):
        payload = {"status": "success", "country": "Italy", "countryCode": "IT",
                   "as": "AS8968 RETELIT", "query": "85.20.0.1"}
        with patch("geoip.urllib.request.urlopen",
                   return_value=self._respond(payload)) as call:
            self.geoip.lookup_ip("85.20.0.1")
            self.geoip.lookup_ip("85.20.0.1")
        self.assertEqual(call.call_count, 1)

    def test_empty_ip_returns_none(self):
        self.assertIsNone(self.geoip.lookup_ip(""))


if __name__ == "__main__":
    unittest.main()