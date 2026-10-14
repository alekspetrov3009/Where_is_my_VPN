"""Определение страны и ASN сервера по IP — признак «сервер внутри РФ».

Зачем: ключ с российским IP бесполезен для обхода блокировок — трафик не
выходит за периметр ТСПУ, и блокировки применяются к нему так же, как к
обычному домашнему подключению. Белый список (WHITE-CIDR-RU) специально
собирает такие серверы, поэтому без этой проверки они выглядят «рабочими».

Источник — ip-api.com (бесплатный, без ключа, 45 запросов/мин). Ответы
кэшируются в памяти процесса: один и тот же IP в пуле встречается десятки раз,
а лимит бесплатного тарифа общий. Сетевой сбой не должен ломать проверку —
при ошибке возвращается None, и запись просто остаётся без геоданных.
"""
import json
import threading
import time
import urllib.request

# Бесплатный тариф ip-api: 45 запросов в минуту с одного IP.
GEO_API_URL = "http://ip-api.com/json/{ip}?fields=status,message,country,countryCode,as,org,query"
GEO_TIMEOUT = 6
# Кэш ответов: IP → (данные, время). TTL сутки — геоданные почти не меняются.
GEO_CACHE_TTL = 24 * 60 * 60
# Минимальный интервал между сетевыми запросами: 45/мин ≈ 1.33 сек.
GEO_MIN_INTERVAL = 1.4

_CACHE = {}
_LOCK = threading.Lock()
_LAST_REQUEST = [0.0]


def _cached(ip):
    """Свежий ответ из кэша или None."""
    entry = _CACHE.get(ip)
    if entry and time.time() - entry[1] < GEO_CACHE_TTL:
        return entry[0]
    return None


def _throttle():
    """Держит темп запросов в пределах бесплатного тарифа."""
    with _LOCK:
        wait = GEO_MIN_INTERVAL - (time.monotonic() - _LAST_REQUEST[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST[0] = time.monotonic()


def lookup_ip(ip, timeout=GEO_TIMEOUT):
    """Страна и ASN по IP. Возвращает dict или None при сбое.

    Поля: country, country_code, asn (номер), as_name, is_ru.
    """
    if not ip:
        return None
    cached = _cached(ip)
    if cached is not None:
        return cached
    _throttle()
    try:
        request = urllib.request.Request(
            GEO_API_URL.format(ip=ip), headers={"User-Agent": "WhereIsMyVPN/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    if data.get("status") != "success":
        # Приватные/зарезервированные адреса и исчерпанный лимит попадают сюда.
        _CACHE[ip] = (None, time.time())
        return None
    as_text = str(data.get("as") or "")
    asn = None
    as_name = as_text
    if as_text.startswith("AS"):
        head, _, tail = as_text.partition(" ")
        try:
            asn = int(head[2:])
        except ValueError:
            asn = None
        as_name = tail or as_text
    result = {
        "country": data.get("country") or "",
        "country_code": (data.get("countryCode") or "").upper(),
        "asn": asn,
        "as_name": as_name,
        # Ключевой признак для обхода блокировок.
        "is_ru": (data.get("countryCode") or "").upper() == "RU",
    }
    _CACHE[ip] = (result, time.time())
    return result


def clear_cache():
    """Полный сброс кэша (используется тестами и админкой)."""
    with _LOCK:
        _CACHE.clear()
        _LAST_REQUEST[0] = 0.0