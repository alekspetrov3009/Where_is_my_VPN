"""Проверки разбора подписок и реестра источников каталога.

Сеть не требуется: разбор текста — чистая функция, реестр источников —
статические данные. Живые URL проверялись вручную при добавлении; здесь
фиксируются инварианты, которые легко сломать правкой.
"""
import base64
import unittest

from catalog import (EXTRA_FALLBACK_LISTS, EXTRA_SOURCES, FALLBACK_LISTS,
                     GOIDA_FALLBACK_LISTS, SOURCE_LABELS, decode_subscription_bytes,
                     parse_subscription_text)


class SubscriptionParsingTests(unittest.TestCase):
    def test_plain_text(self):
        text = "vless://a@b:443\ntrojan://c@d:443\n# comment\n\n"
        uris, mode = parse_subscription_text(text)
        self.assertEqual(mode, "plain")
        self.assertEqual(uris, ("vless://a@b:443", "trojan://c@d:443"))

    def test_base64_body(self):
        """Epodonios/Splitted-By-Protocol и MatinGhanbari отдают base64."""
        raw = "vless://a@b:443\nvmess://xyz"
        encoded = base64.b64encode(raw.encode()).decode()
        uris, mode = parse_subscription_text(encoded)
        self.assertEqual(mode, "base64")
        self.assertEqual(uris, ("vless://a@b:443", "vmess://xyz"))

    def test_base64_with_line_breaks(self):
        """Подписки приходят с переносами внутри base64 — склеиваем."""
        raw = "vless://a@b:443\nvmess://xyz"
        encoded = base64.b64encode(raw.encode()).decode()
        wrapped = "\n".join(encoded[i:i + 40] for i in range(0, len(encoded), 40))
        uris, mode = parse_subscription_text(wrapped)
        self.assertEqual(mode, "base64")
        self.assertEqual(len(uris), 2)

    def test_plain_wins_over_base64_attempt(self):
        """Если URI нашлись в тексте, base64 не пробуем."""
        text = "vless://a@b:443\nnot base64 at all"
        uris, mode = parse_subscription_text(text)
        self.assertEqual(mode, "plain")
        self.assertEqual(len(uris), 1)

    def test_empty_input(self):
        self.assertEqual(parse_subscription_text(""), ((), "empty"))
        self.assertEqual(parse_subscription_text("   \n\n  "), ((), "empty"))

    def test_junk_without_uris(self):
        uris, mode = parse_subscription_text("hello world, not a config")
        self.assertEqual(uris, ())
        self.assertEqual(mode, "empty")

    def test_non_utf8_bytes_do_not_drop_file(self):
        """Один байт в другой кодировке не должен ронять весь источник."""
        raw = b"vless://a@b:443\n# \xb9 bad byte\ntrojan://c@d:443"
        text = decode_subscription_bytes(raw)
        uris, mode = parse_subscription_text(text)
        self.assertEqual(mode, "plain")
        self.assertEqual(len(uris), 2)

    def test_utf8_bom_stripped(self):
        raw = b"\xef\xbb\xbfvless://a@b:443"
        uris, _ = parse_subscription_text(decode_subscription_bytes(raw))
        self.assertEqual(uris, ("vless://a@b:443",))

    def test_unsupported_protocols_ignored(self):
        text = "http://example.com\nvless://a@b:443\nsocks5://1.2.3.4:1080"
        uris, _ = parse_subscription_text(text)
        self.assertEqual(uris, ("vless://a@b:443",))


class SourceRegistryTests(unittest.TestCase):
    def test_extra_sources_have_labels(self):
        """Без метки админка отклонит source_id с 400."""
        used = {info["source"] for info in EXTRA_FALLBACK_LISTS.values()}
        self.assertTrue(used <= set(SOURCE_LABELS), used - set(SOURCE_LABELS))

    def test_no_key_collisions_with_existing_sources(self):
        existing = set(FALLBACK_LISTS) | set(GOIDA_FALLBACK_LISTS)
        self.assertEqual(set(EXTRA_FALLBACK_LISTS) & existing, set())

    def test_every_entry_has_required_fields(self):
        for key, info in EXTRA_FALLBACK_LISTS.items():
            self.assertTrue(info.get("name"), key)
            self.assertTrue(info.get("url", "").startswith("https://"), key)
            self.assertIn(info.get("type"), ("black", "white"), key)
            self.assertTrue(info.get("source"), key)

    def test_urls_use_githack(self):
        """raw.githubusercontent в РФ часто режется — используем githack."""
        for key, info in EXTRA_FALLBACK_LISTS.items():
            self.assertIn("raw.githack.com", info["url"], key)

    def test_goida_disabled(self):
        """Репозиторий Goida удалён: 26 URL давали 26 ошибок 404 и ноль конфигов."""
        self.assertEqual(GOIDA_FALLBACK_LISTS, {})

    def test_extra_sources_nonempty(self):
        self.assertGreater(len(EXTRA_SOURCES), 0)
        self.assertEqual(len(EXTRA_SOURCES), len(EXTRA_FALLBACK_LISTS))

    def test_hysteria2_source_present(self):
        """Hysteria2 — ключевой протокол для обхода DPI, источник обязателен."""
        urls = " ".join(info["url"].lower() for info in EXTRA_FALLBACK_LISTS.values())
        self.assertIn("hysteria2", urls)


class AppWiringTests(unittest.TestCase):
    def test_available_lists_includes_extra_sources(self):
        import app
        for key in EXTRA_FALLBACK_LISTS:
            self.assertIn(key, app.AVAILABLE_LISTS)

    def test_available_lists_has_no_goida_entries(self):
        import app
        goida_keys = [k for k in app.AVAILABLE_LISTS if k.startswith("goida_")]
        self.assertEqual(goida_keys, [])


if __name__ == "__main__":
    unittest.main()