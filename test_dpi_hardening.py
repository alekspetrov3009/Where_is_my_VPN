"""Проверки устойчивости к DPI: парсеры hysteria2/tuic и детектор перехвата.

Тесты без сети: парсеры — чистые функции, детектор работает по телу ответа.
Схема сгенерированных outbound'ов отдельно проверяется `sing-box check`
в ручном прогоне; здесь фиксируется форма конфигурации.
"""
import subprocess
import unittest
from pathlib import Path

from config_parser import parse_uri_to_singbox_outbound
from prober import _classify_error, _detect_interception


class Hysteria2ObfsTests(unittest.TestCase):
    """obfs (salamander) критичен: без него сервер с обфускацией не отвечает."""

    def test_obfs_dash_parameter(self):
        out = parse_uri_to_singbox_outbound(
            "hysteria2://pw@1.2.3.4:443?sni=a.com&obfs=salamander&obfs-password=secret", "t")
        self.assertEqual(out["obfs"], {"type": "salamander", "password": "secret"})

    def test_obfs_underscore_parameter(self):
        """Часть генераторов ссылок пишет obfs_password — принимаем оба."""
        out = parse_uri_to_singbox_outbound(
            "hysteria2://pw@1.2.3.4:443?sni=a.com&obfs=salamander&obfs_password=secret", "t")
        self.assertEqual(out["obfs"], {"type": "salamander", "password": "secret"})

    def test_no_obfs_omits_key(self):
        """Без obfs ключ не добавляется: сервер без обфускации отверг бы его."""
        out = parse_uri_to_singbox_outbound("hysteria2://pw@1.2.3.4:443?sni=a.com", "t")
        self.assertNotIn("obfs", out)

    def test_unknown_obfs_type_ignored(self):
        """Неизвестный тип обфускации не подставляем: sing-box отверг бы конфиг."""
        out = parse_uri_to_singbox_outbound(
            "hysteria2://pw@1.2.3.4:443?obfs=weird&obfs-password=x", "t")
        self.assertNotIn("obfs", out)

    def test_obfs_without_password_ignored(self):
        """salamander без пароля невалиден — не добавляем половину параметра."""
        out = parse_uri_to_singbox_outbound("hysteria2://pw@1.2.3.4:443?obfs=salamander", "t")
        self.assertNotIn("obfs", out)


class TuicParserTests(unittest.TestCase):
    """TUIC поверх QUIC проходит там, где TCP-протоколы режутся."""

    def test_credentials_in_userinfo(self):
        out = parse_uri_to_singbox_outbound(
            "tuic://11111111-2222-3333-4444-555555555555:pw@1.2.3.4:443?sni=a.com", "t")
        self.assertEqual(out["type"], "tuic")
        self.assertEqual(out["uuid"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(out["password"], "pw")
        self.assertEqual(out["server_port"], 443)

    def test_password_from_query(self):
        out = parse_uri_to_singbox_outbound(
            "tuic://11111111-2222-3333-4444-555555555555@1.2.3.4:443?password=pw2", "t")
        self.assertEqual(out["password"], "pw2")

    def test_congestion_and_udp_mode(self):
        out = parse_uri_to_singbox_outbound(
            "tuic://u:p@1.2.3.4:443?congestion_control=bbr&udp_relay_mode=native", "t")
        self.assertEqual(out["congestion_control"], "bbr")
        self.assertEqual(out["udp_relay_mode"], "native")

    def test_alpn_split_into_list(self):
        out = parse_uri_to_singbox_outbound("tuic://u:p@1.2.3.4:443?alpn=h3,h2", "t")
        self.assertEqual(out["tls"]["alpn"], ["h3", "h2"])

    def test_missing_password_rejected(self):
        self.assertIsNone(
            parse_uri_to_singbox_outbound("tuic://11111111-2222-3333-4444-555555555555@1.2.3.4:443", "t"))

    def test_missing_uuid_rejected(self):
        self.assertIsNone(parse_uri_to_singbox_outbound("tuic://:pw@1.2.3.4:443", "t"))

    def test_tuic_is_in_uri_prefixes(self):
        """tuic собирается источниками и допускается в пул (storage.load_pool)."""
        from config_parser import URI_PREFIXES
        self.assertIn("tuic://", URI_PREFIXES)


class InterceptionDetectionTests(unittest.TestCase):
    """Пустое тело = корректный generate_204; непустое — повод для анализа."""

    def test_empty_body_is_not_interception(self):
        self.assertIsNone(_detect_interception(b""))

    def test_whitespace_body_is_not_interception(self):
        self.assertIsNone(_detect_interception(b"   \n"))

    def test_rkn_stub_detected(self):
        body = b"<html><head><title>Access denied</title></head><body>blocked by roskomnadzor</body></html>"
        self.assertIn("перехват", _detect_interception(body))

    def test_captive_portal_detected(self):
        body = b"<html><head><title>Wi-Fi Login</title></head><body>captive portal</body></html>"
        self.assertIn("перехват", _detect_interception(body))

    def test_generic_html_reported_with_title(self):
        result = _detect_interception(b"<!DOCTYPE html><html><head><title>Error</title></head></html>")
        self.assertIn("HTML", result)
        self.assertIn("error", result)

    def test_plain_junk_is_not_interception(self):
        """Мусор без признаков заглушки — не перехват, а «неожиданный ответ»."""
        self.assertIsNone(_detect_interception(b"some random non-html garbage"))

    def test_legal_reasons_detected(self):
        self.assertIn("перехват", _detect_interception(b"Unavailable For Legal Reasons"))

    def test_body_sample_is_bounded(self):
        """Анализируется только начало тела: заглушки короткие."""
        body = b"x" * 100000 + b"blocked"
        self.assertIsNone(_detect_interception(body))


class ErrorClassificationTests(unittest.TestCase):
    def test_interception_classified_separately(self):
        """Перехват не должен сливаться с общей веткой blocked."""
        self.assertEqual(_classify_error("перехват ответа: обнаружена заглушка (blocked)"), "intercepted")

    def test_timeout_still_classified(self):
        self.assertEqual(_classify_error("Тайм-аут проверки (6 секунд)"), "timeout")

    def test_unexpected_answer_is_other(self):
        self.assertEqual(_classify_error("Неожиданный ответ тестового адреса"), "other")


class ServiceProfileTests(unittest.TestCase):
    """Расширение SERVICE_TARGETS не должно опустошать секцию «Сервисы»."""

    def test_required_services_is_subset_of_targets(self):
        from prober import REQUIRED_SERVICES, SERVICE_TARGETS
        self.assertTrue(set(REQUIRED_SERVICES) <= set(SERVICE_TARGETS))

    def test_gpt_remains_required(self):
        """Секция «Сервисы» на фронте читает именно results.gpt."""
        from prober import REQUIRED_SERVICES
        self.assertIn("gpt", REQUIRED_SERVICES)

    def test_extra_targets_do_not_gate_services_table(self):
        """Сервер с открытым ChatGPT, но закрытым Instagram остаётся в выдаче."""
        import copy
        from unittest.mock import patch
        import app
        from prober import REQUIRED_SERVICES, SERVICE_TARGETS

        base = {"uri": "vless://a@b:443", "source": "igareck",
                "services": {"checked_at": "01.01.2026 00:00:00",
                             "results": {name: {"ok": True} for name in SERVICE_TARGETS}}}
        # Instagram закрыт — на обязательный набор это влиять не должно.
        partial = copy.deepcopy(base)
        partial["uri"] = "vless://a@b:443#noig"
        partial["services"]["results"]["ig"] = {"ok": False}
        data = {"black": {"working": [partial]}, "white": {"working": []}}
        with patch.object(app, "public_data", data):
            result = app.get_service_working()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["uri"], "vless://a@b:443#noig")

    def test_missing_required_service_excludes_server(self):
        """А вот недоступный ChatGPT — по-прежнему повод исключить сервер."""
        import copy
        from unittest.mock import patch
        import app
        from prober import SERVICE_TARGETS

        bad = {"uri": "vless://a@b:443#nogpt", "source": "igareck",
               "services": {"checked_at": "01.01.2026 00:00:00",
                            "results": {name: {"ok": True} for name in SERVICE_TARGETS}}}
        bad["services"]["results"]["gpt"] = {"ok": False}
        data = {"black": {"working": [bad]}, "white": {"working": []}}
        with patch.object(app, "public_data", data):
            self.assertEqual(app.get_service_working(), [])


class SchemaValidationTests(unittest.TestCase):
    """Сгенерированные outbound'ы должны проходить `sing-box check`."""

    SINGBOX = Path(__file__).parent / "sing-box-bin" / "sing-box.exe"

    def _check(self, uri):
        import json
        import os
        import tempfile
        outbound = parse_uri_to_singbox_outbound(uri, "proxy")
        config = {"log": {"level": "error"},
                  "inbounds": [{"type": "socks", "listen": "127.0.0.1", "listen_port": 19999}],
                  "outbounds": [outbound]}
        path = os.path.join(tempfile.gettempdir(), "vpn_schema_check.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        return subprocess.run([str(self.SINGBOX), "check", "-c", path],
                              capture_output=True, text=True, timeout=30)

    def setUp(self):
        if not self.SINGBOX.exists():
            self.skipTest("sing-box.exe отсутствует — проверка схемы пропущена")

    def test_hysteria2_with_obfs_schema(self):
        result = self._check("hysteria2://pw@1.2.3.4:443?sni=a.com&obfs=salamander&obfs-password=s")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_tuic_full_schema(self):
        result = self._check("tuic://11111111-2222-3333-4444-555555555555:pw@1.2.3.4:443"
                             "?sni=a.com&congestion_control=bbr&alpn=h3,h2&udp_relay_mode=native")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()