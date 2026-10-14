"""Проверки заголовков подписки: ETag и Subscription-Userinfo.

Сеть не требуется: подписка собирается из подменённого пула, заголовки —
чистые функции. Формат Subscription-Userinfo сверен с реальным парсером
hiddify-app (lib/features/profile/data/profile_parser.dart): обязательны
upload и download, а total=0 / expire=0 означают «бесконечно».
"""
import unittest
from unittest.mock import patch

import app


def parse_like_hiddify(header):
    """Копия логики разбора из hiddify profile_parser.dart.

    Сегмент без '=' пропускается; значения могут быть дробными и усекаются
    до int; без upload/download заголовок игнорируется целиком.
    """
    parsed = {}
    for segment in header.split(";"):
        parts = segment.split("=")
        if len(parts) < 2:
            continue
        key = parts[0].strip()
        if not key:
            continue
        try:
            parsed[key] = int(float(parts[1].strip()))
        except ValueError:
            parsed[key] = None
    if not all(parsed.get(k) is not None for k in ("upload", "download", "total", "expire")):
        return None
    return parsed


POOL = [
    {"uri": "vless://a@1.2.3.4:443", "stable": True, "latency": 10, "successful_checks": 11},
    {"uri": "hysteria2://b@5.6.7.8:443", "stable": True, "latency": 20, "successful_checks": 11},
]


class SubscriptionUserinfoTests(unittest.TestCase):
    def setUp(self):
        self._black = app.public_data["black"]["working"]
        self._white = app.public_data["white"]["working"]
        app.public_data["black"]["working"] = [dict(c) for c in POOL]
        app.public_data["white"]["working"] = []

    def tearDown(self):
        app.public_data["black"]["working"] = self._black
        app.public_data["white"]["working"] = self._white

    def test_parses_with_real_client_logic(self):
        parsed = parse_like_hiddify(app.subscription_userinfo())
        self.assertIsNotNone(parsed, "клиент проигнорирует заголовок без upload/download")

    def test_required_keys_present(self):
        parsed = parse_like_hiddify(app.subscription_userinfo())
        for key in ("upload", "download", "total", "expire"):
            self.assertIn(key, parsed)

    def test_unlimited_and_perpetual(self):
        """total=0 и expire=0 клиент трактует как «бесконечно»."""
        parsed = parse_like_hiddify(app.subscription_userinfo())
        self.assertEqual(parsed["total"], 0)
        self.assertEqual(parsed["expire"], 0)

    def test_download_reflects_pool_size(self):
        small = parse_like_hiddify(app.subscription_userinfo())["download"]
        app.public_data["black"]["working"].append(
            {"uri": "vless://c@9.9.9.9:443", "stable": True, "latency": 30, "successful_checks": 11})
        bigger = parse_like_hiddify(app.subscription_userinfo())["download"]
        self.assertGreater(bigger, small)

    def test_empty_pool_still_valid(self):
        """Пустой пул не должен ломать формат: upload/download обязательны."""
        app.public_data["black"]["working"] = []
        parsed = parse_like_hiddify(app.subscription_userinfo())
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["download"], 0)

    def test_no_spaces_around_equals_in_keys(self):
        """Пробелы вокруг '=' допустимы, но ключи должны быть точными."""
        header = app.subscription_userinfo()
        keys = [seg.split("=")[0].strip() for seg in header.split(";")]
        self.assertEqual(keys, ["upload", "download", "total", "expire"])


class EtagTests(unittest.TestCase):
    def test_quoted_and_stable(self):
        etag = app.subscription_etag("payload")
        self.assertTrue(etag.startswith('"') and etag.endswith('"'))
        self.assertEqual(etag, app.subscription_etag("payload"))

    def test_changes_with_content(self):
        self.assertNotEqual(app.subscription_etag("a"), app.subscription_etag("b"))

    def test_different_formats_differ(self):
        """Один и тот же текст в разных форматах — разные ETag."""
        self.assertNotEqual(app.subscription_etag("base64:x"),
                            app.subscription_etag("clash:x"))


class EtagMatchesTests(unittest.TestCase):
    class _Req:
        def __init__(self, value=None):
            self.headers = {} if value is None else {"if-none-match": value}

    def test_no_header(self):
        self.assertFalse(app.etag_matches(self._Req(), '"abc"'))

    def test_exact_match(self):
        self.assertTrue(app.etag_matches(self._Req('"abc"'), '"abc"'))

    def test_mismatch(self):
        self.assertFalse(app.etag_matches(self._Req('"other"'), '"abc"'))

    def test_weak_etag(self):
        """Клиенты присылают слабые ETag с префиксом W/."""
        self.assertTrue(app.etag_matches(self._Req('W/"abc"'), '"abc"'))

    def test_list_of_candidates(self):
        self.assertTrue(app.etag_matches(self._Req('"x", "abc", "y"'), '"abc"'))

    def test_wildcard(self):
        self.assertTrue(app.etag_matches(self._Req("*"), '"abc"'))

    def test_whitespace_tolerated(self):
        self.assertTrue(app.etag_matches(self._Req('  "abc"  '), '"abc"'))


class OnekeyEndpointTests(unittest.IsolatedAsyncioTestCase):
    """Проверки реального ответа endpoint'а, включая 304."""

    class _Req:
        def __init__(self, query="", headers=None):
            self.query_params = dict(query)
            self.headers = headers or {}

    async def _call(self, request):
        return await app.onekey(request)

    def setUp(self):
        self._black = app.public_data["black"]["working"]
        self._white = app.public_data["white"]["working"]
        app.public_data["black"]["working"] = [dict(c) for c in POOL]
        app.public_data["white"]["working"] = []
        self._token = app.ONEKEY_TOKEN
        app.ONEKEY_TOKEN = ""

    def tearDown(self):
        app.public_data["black"]["working"] = self._black
        app.public_data["white"]["working"] = self._white
        app.ONEKEY_TOKEN = self._token

    async def test_base64_has_both_headers(self):
        response = await self._call(self._Req())
        self.assertEqual(response.status_code, 200)
        self.assertIn("etag", {k.lower() for k in response.headers})
        self.assertIn("subscription-userinfo", {k.lower() for k in response.headers})

    async def test_clash_has_both_headers(self):
        response = await self._call(self._Req({"format": "clash"}))
        self.assertEqual(response.status_code, 200)
        self.assertIn("etag", {k.lower() for k in response.headers})
        self.assertIn("subscription-userinfo", {k.lower() for k in response.headers})

    async def test_json_has_both_headers(self):
        response = await self._call(self._Req({"format": "json"}))
        self.assertEqual(response.status_code, 200)
        self.assertIn("etag", {k.lower() for k in response.headers})
        self.assertIn("subscription-userinfo", {k.lower() for k in response.headers})

    async def test_304_when_etag_matches(self):
        first = await self._call(self._Req())
        etag = first.headers["etag"]
        second = await self._call(self._Req(headers={"if-none-match": etag}))
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.body, b"", "304 не должен содержать тело")

    async def test_200_when_etag_differs(self):
        response = await self._call(self._Req(headers={"if-none-match": '"stale"'}))
        self.assertEqual(response.status_code, 200)

    async def test_etag_changes_when_pool_changes(self):
        first = await self._call(self._Req())
        app.public_data["black"]["working"].append(
            {"uri": "vless://new@7.7.7.7:443", "stable": True, "latency": 5, "successful_checks": 11})
        second = await self._call(self._Req())
        self.assertNotEqual(first.headers["etag"], second.headers["etag"])

    async def test_304_keeps_etag_header(self):
        """Клиент должен получить ETag и в 304, иначе не сможет кэшировать дальше."""
        first = await self._call(self._Req())
        etag = first.headers["etag"]
        second = await self._call(self._Req(headers={"if-none-match": etag}))
        self.assertEqual(second.headers["etag"], etag)

    async def test_userinfo_matches_header_helper(self):
        response = await self._call(self._Req())
        self.assertEqual(response.headers["subscription-userinfo"], app.subscription_userinfo())


if __name__ == "__main__":
    unittest.main()