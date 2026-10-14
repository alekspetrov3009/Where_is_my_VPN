"""Каталог источников: динамическое обнаружение списков и суточный кэш.

Источники (igareck, Goida VPN) обнаруживаются через GitHub API; при
недоступности сети используются резервные списки. Файлы списков скачиваются
с условными GET (ETag / If-Modified-Since), дедуплицируются по конечной
точке (URI без #fragment) и нарезаются на пропорциональные пулы.

urlopen_insecure: точечный unverified-контекст для корпоративных MITM-прокси.
Глобальный ssl._create_default_https_context НЕ переопределяем, чтобы
скачивание бинарей и прочие чувствительные запросы оставались верифицированными.
"""
import asyncio
import base64
import hashlib
import json
import ssl
import time
import urllib.request

from check_scheduler import build_pool_plan, interleave_ranges
from config import CATALOG_REFRESH_SECONDS, CATALOG_DOWNLOAD_CONCURRENCY, GOIDA_BASE_URL

_UNVERIFIED_SSL_CONTEXT = ssl._create_unverified_context()


def urlopen_insecure(req, timeout=10):
    """urllib с отключённой проверкой сертификата (только для списков/GitHub API).

    Используется там, где корпоративный MITM иначе ломает TLS.
    Скачивание sing-box намеренно идёт через обычный верифицированный контекст.
    Тесты патчат urllib.request.urlopen — обёртка продолжает его вызывать."""
    return urllib.request.urlopen(req, timeout=timeout, context=_UNVERIFIED_SSL_CONTEXT)


# Резервный список на случай, если GitHub API временно недоступен
FALLBACK_LISTS = {
    "black_mobile": {"name": "⚫ Чёрный список (мобильный)", "url": "https://raw.githack.com/igareck/vpn-configs-for-russia/main/BLACK_VLESS_RUS_mobile.txt", "type": "black"},
    "white_cidr_checked": {"name": "⚪ Белый список (проверенный)", "url": "https://raw.githack.com/igareck/vpn-configs-for-russia/main/WHITE-CIDR-RU-checked.txt", "type": "white"}
}

def make_goida_list(name):
    return {
        "name": f"⚫ Goida VPN — {name}",
        "url": f"{GOIDA_BASE_URL}{name}",
        "type": "black",
        "source": "goida"
    }


# Goida VPN отключён: репозиторий AvenCores/goida-vpn-configs удалён (GitHub API
# отдаёт 404 на сам репозиторий), поэтому 26 захардкоженных URL давали 26
# ошибок 404 на каждое обновление каталога и ноль конфигураций. Список оставлен
# пустым, а не удалён вместе с fetch_goida_lists: если репозиторий вернётся,
# достаточно снова наполнить его — код обнаружения и метка источника целы.
GOIDA_FALLBACK_LISTS = {}

SOURCE_LABELS = {"igareck": "igareck", "goida": "Goida VPN",
                 "epodonios": "Epodonios", "barryfar": "barry-far",
                 "solispirit": "SoliSpirit", "argh94": "Argh94",
                 "hidashimora": "Anti-RKN",
                 "delta": "Delta-Kronecker", "radikal": "0xRadikal",
                 "matin": "MatinGhanbari",
                 "manual": "Ручной импорт", "unknown": "Старый пул (источник неизвестен)"}

# Дополнительные публичные источники. Каждый проверен вручную: URL отвечает и
# отдаёт разбираемые URI (часть репозиториев содержит только README или
# метаданные, а некоторые «ss.txt» на деле наполнены vless/vmess — поэтому
# тип списка ниже задан по фактическому содержимому, а не по имени файла).
#
# Все URL идут через raw.githack.com: raw.githubusercontent в РФ часто режется,
# а githack отдаёт тот же файл с корректным Content-Type.
#
# Формат: (source_id, имя, путь в репозитории, тип списка)
EXTRA_SOURCES = [
    # Крупные агрегаторы (обновляются каждые 5-15 минут).
    ("epodonios", "⚫ Epodonios — vless", "Epodonios/v2ray-configs/main/Splitted-By-Protocol/vless.txt", "black"),
    ("epodonios", "⚫ Epodonios — trojan", "Epodonios/v2ray-configs/main/Splitted-By-Protocol/trojan.txt", "black"),
    ("epodonios", "⚫ Epodonios — ss", "Epodonios/v2ray-configs/main/Splitted-By-Protocol/ss.txt", "black"),
    ("epodonios", "⚫ Epodonios — vmess", "Epodonios/v2ray-configs/main/Splitted-By-Protocol/vmess.txt", "black"),
    ("barryfar", "⚫ barry-far — все конфиги", "barry-far/V2ray-Config/main/All_Configs_Sub.txt", "black"),
    ("solispirit", "⚫ SoliSpirit — все конфиги", "SoliSpirit/v2ray-configs/main/all_configs.txt", "black"),
    # Разбивка по протоколам: hysteria2 и tuic важны для обхода DPI.
    ("argh94", "⚫ Argh94 — Hysteria2", "Argh94/Proxy-List/main/Hysteria2.txt", "black"),
    ("argh94", "⚫ Argh94 — Trojan", "Argh94/Proxy-List/main/Trojan.txt", "black"),
    ("argh94", "⚫ Argh94 — Shadowsocks (фактически vless/vmess)", "Argh94/Proxy-List/main/ShadowSocks.txt", "black"),
    # TGParse намеренно отсутствует: его splitted/* (1206 URI, все base64)
    # на 100% дублируются Epodonios/barry-far/argh94 — те же Telegram-каналы.
    # Проверено: assemble_catalog отдаёт все его точки другим источникам,
    # поэтому загрузка 1.2 МБ не давала ни одного нового сервера.
    # Источники, ориентированные на РФ.
    ("hidashimora", "⚫ Anti-RKN — крупный набор", "Hidashimora/free-vpn-anti-rkn/main/configs/1.14.txt", "black"),
    ("hidashimora", "⚫ Anti-RKN — свежий набор", "Hidashimora/free-vpn-anti-rkn/main/configs/1.21.txt", "black"),
    ("delta", "⚫ Delta-Kronecker — все конфиги", "Delta-Kronecker/V2ray-Config/main/config/all_configs.txt", "black"),
    ("radikal", "⚫ 0xRadikal — все конфиги", "0xRadikal/Free-v2ray-Configs/main/all/configs.txt", "black"),
    ("matin", "⚫ MatinGhanbari — супер-подписка", "MatinGhanbari/v2ray-configs/main/subscriptions/v2ray/super-sub.txt", "black"),
]


def make_extra_list(source_id, display_name, path, list_type):
    """Запись источника в формате каталога (ключ — source_id + имя файла)."""
    filename = path.rsplit("/", 1)[-1]
    return {
        f"{source_id}_{filename}": {
            "name": display_name,
            "url": f"https://raw.githack.com/{path}",
            "type": list_type,
            "source": source_id,
        }
    }


EXTRA_FALLBACK_LISTS = {}
for _source_id, _display, _path, _kind in EXTRA_SOURCES:
    EXTRA_FALLBACK_LISTS.update(make_extra_list(_source_id, _display, _path, _kind))


def list_source(info):
    return info.get("source", "igareck")


def fetch_igareck_lists():
    """Динамически получает актуальный список .txt файлов из репозитория"""
    api_url = "https://api.github.com/repos/igareck/vpn-configs-for-russia/contents/"
    try:
        req = urllib.request.Request(
            api_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/vnd.github.v3+json"
            }
        )
        with urlopen_insecure(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))

        dynamic_lists = {}
        for item in data:
            if item["type"] == "file" and item["name"].endswith(".txt"):
                name = item["name"]
                # Формируем URL через githack для обхода блокировок raw.githubusercontent
                url = f"https://raw.githack.com/igareck/vpn-configs-for-russia/main/{name}"

                # Автоматическая категоризация по имени файла
                name_upper = name.upper()
                if "BLACK" in name_upper or "ЧЕРН" in name_upper:
                    list_type = "black"
                    icon = "⚫"
                elif "WHITE" in name_upper or "БЕЛ" in name_upper or "CIDR" in name_upper or "SNI" in name_upper:
                    list_type = "white"
                    icon = "⚪"
                else:
                    continue  # Пропускаем файлы типа README, LICENSE и т.д.

                readable_name = name.replace(".txt", "").replace("_", " ").replace("-", " ")
                dynamic_lists[name] = {
                    "name": f"{icon} {readable_name}",
                    "url": url,
                    "type": list_type
                }

        print(f"✅ Динамически загружено {len(dynamic_lists)} конфигурационных файлов с GitHub")
        return dynamic_lists
    except Exception as e:
        print(f"⚠️ Не удалось получить список файлов с GitHub API: {e}")
        print("⚠️ Использую резервный (fallback) список.")
        return FALLBACK_LISTS


def fetch_goida_lists():
    """Получает TXT-подписки из каталога githubmirror источника Goida VPN."""
    api_url = "https://api.github.com/repos/AvenCores/goida-vpn-configs/contents/githubmirror?ref=main"
    try:
        req = urllib.request.Request(
            api_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/vnd.github.v3+json"
            }
        )
        with urlopen_insecure(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))

        dynamic_lists = {}
        for item in data:
            name = item["name"]
            if item["type"] == "file" and name.endswith(".txt"):
                dynamic_lists[f"goida_{name}"] = make_goida_list(name)

        if not dynamic_lists:
            raise ValueError("В githubmirror не найдены TXT-подписки")
        print(f"✅ Goida VPN: загружено {len(dynamic_lists)} списков")
        return dynamic_lists
    except Exception as e:
        print(f"⚠️ Goida VPN: использую резервный список, GitHub API недоступен: {e}")
        return GOIDA_FALLBACK_LISTS.copy()


async def fetch_dynamic_lists():
    """Объединяет источники с независимым резервным списком для каждого.

    igareck и Goida обнаруживаются через GitHub API (список файлов меняется),
    дополнительные источники заданы статически в EXTRA_SOURCES: их набор
    стабилен, а лишний API-запрос на каждый репозиторий упирался бы в лимит
    GitHub (60 запросов/час без токена).
    """
    igareck, goida = await asyncio.gather(
        asyncio.to_thread(fetch_igareck_lists), asyncio.to_thread(fetch_goida_lists))
    lists = dict(igareck)
    lists.update(goida)
    lists.update(EXTRA_FALLBACK_LISTS)
    return lists


def decode_subscription_bytes(raw):
    """Байты подписки → текст, устойчиво к не-UTF-8 мусору.

    Часть публичных файлов содержит одиночные байты в другой кодировке
    (например, cp1251 в remark). Строгий decode ронял весь файл целиком:
    источник с тысячами конфигураций молча пропадал из-за одного байта,
    поэтому ошибки заменяются, а не прерывают разбор.
    """
    return raw.decode("utf-8-sig", errors="replace")


def parse_subscription_text(text):
    """Извлекает URI из тела подписки: обычный текст или base64.

    Часть публичных источников отдаёт подписку целиком в base64
    (Epodonios/Splitted-By-Protocol, MatinGhanbari): без декодирования такой
    файл выглядит непустым, но даёт ноль URI и молча теряется. Сначала
    пробуем обычный текст — он дешевле и покрывает большинство источников;
    base64 разбираем только если в тексте не нашлось ни одного URI.

    Возвращает (tuple_uri, режим), где режим — "plain", "base64" или "empty".
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    plain = tuple(line for line in lines if line.startswith(URI_PREFIXES))
    if plain:
        return plain, "plain"
    # Склеиваем без пробелов: подписки приходят и с переносами внутри base64.
    body = "".join(lines)
    if not body:
        return (), "empty"
    padded = body + "=" * ((4 - len(body) % 4) % 4)
    try:
        decoded = base64.b64decode(padded, validate=False).decode("utf-8", errors="replace")
    except Exception:
        return (), "empty"
    uris = tuple(line.strip() for line in decoded.splitlines()
                 if line.strip().startswith(URI_PREFIXES))
    return uris, "base64" if uris else "empty"


def download_catalog_file(info, previous=None):
    """Суточный GET с условными заголовками; старый файл сохраняется при сбое."""
    headers = {"User-Agent": "Mozilla/5.0"}
    if previous:
        if previous.get("etag"):
            headers["If-None-Match"] = previous["etag"]
        if previous.get("last_modified"):
            headers["If-Modified-Since"] = previous["last_modified"]
    try:
        req = urllib.request.Request(info["url"], headers=headers)
        with urlopen_insecure(req, timeout=15) as response:
            deadline = time.monotonic() + 60
            chunks = []
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Превышено время загрузки списка (60 секунд)")
                read1 = getattr(response, "read1", None)
                chunk = read1(65536) if callable(read1) else response.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            uris, mode = parse_subscription_text(decode_subscription_bytes(b''.join(chunks)))
            return {"info": info, "uris": uris, "encoding": mode,
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"), "error": None,
                    "fetched_at": time.time(), "stale": False}
    except Exception as exc:
        if getattr(exc, "code", None) == 304 and previous:
            return {**previous, "info": info, "error": None, "stale": False, "fetched_at": time.time()}
        return {**(previous or {}), "info": info, "uris": (previous or {}).get("uris", ()),
                "error": str(exc), "stale": previous is not None}


def canonical_endpoint(uri):
    """Конечная точка без #fragment: один сервер с разными remark — один ключ.

    Запрос (хост, порт, ключи, sni, path) входит в ключ целиком и сравнивается
    как есть: различающиеся параметры могут давать разный результат проверки,
    такие URI сознательно НЕ склеиваем. Отрезаем только отображаемое имя.
    """
    return uri.split("#", 1)[0]


def assemble_catalog(files):
    """Дедупликация по конечной точке: повторы внутри источника и между
    источниками не увеличивают вес. Владелец точки — первый источник в порядке
    каталога; у него остаётся первый увиденный полный URI (с его remark).
    Чужие копии отбрасываются и в проверку не идут."""
    typed = {}
    owner = {}
    duplicates = 0
    for record in files.values():
        info = record["info"]
        source, kind = list_source(info), info["type"]
        group = typed.setdefault(source, {}).setdefault(kind, [])
        for uri in record["uris"]:
            key = (kind, canonical_endpoint(uri))
            if key in owner:
                duplicates += 1
                continue
            owner[key] = source
            group.append(uri)
    groups = {}
    for source, kinds in typed.items():
        # Black/White одного источника тоже перемешиваются, а не идут длинными блоками.
        groups[source] = tuple(interleave_ranges(kinds, {k: len(v) for k, v in kinds.items()}, {}))
    plan = build_pool_plan({s: len(items) for s, items in groups.items()})
    # Идентификатор содержимого: файлы обновились — чекпоинт старого
    # каталога недействителен, начинаем с первого пула. Порядок детерминирован.
    digest = hashlib.blake2b(digest_size=16)
    for source in sorted(groups):
        for kind, uri in groups[source]:
            digest.update(f"{source}\0{kind}\0{uri}\0".encode("utf-8"))
    return {"files": files, "groups": groups, "plan": plan, "duplicates": duplicates,
            "id": digest.hexdigest(),
            "updated_at": time.time(), "errors": {key: r["error"] for key, r in files.items() if r.get("error")}}


# Суточный TTL каталога и параллельная загрузка файлов — из config.py.
CATALOG_LOCK = asyncio.Lock()
SOURCE_CATALOG = None

from config_parser import URI_PREFIXES  # noqa: E402  (нужен download_catalog_file)


async def get_source_catalog(progress_callback=None, fetch_lists=None):
    """Суточный каталог источников.

    fetch_lists — заменяемый поставщик списков (по умолчанию fetch_dynamic_lists);
    параметр позволяет вызывающему коду подставить свою реализацию.
    """
    global SOURCE_CATALOG
    if fetch_lists is None:
        fetch_lists = fetch_dynamic_lists
    async with CATALOG_LOCK:
        if SOURCE_CATALOG is not None and time.time() - SOURCE_CATALOG["updated_at"] < CATALOG_REFRESH_SECONDS:
            return SOURCE_CATALOG
        lists = await fetch_lists()
        old_files = SOURCE_CATALOG["files"] if SOURCE_CATALOG else {}
        files = {}
        pending = iter(lists.items())

        async def worker():
            for key, info in pending:
                previous = old_files.get(key)
                if previous and previous["info"]["url"] != info["url"]:
                    previous = None
                task = asyncio.create_task(asyncio.to_thread(download_catalog_file, info, previous))
                try:
                    files[key] = await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
                if progress_callback:
                    await progress_callback({"current": len(files), "total": len(lists), "name": info["name"]})

        tasks = [asyncio.create_task(worker()) for _ in range(min(CATALOG_DOWNLOAD_CONCURRENCY, len(lists)))]
        try:
            await asyncio.gather(*tasks)
            # Стабильный порядок независимо от скорости скачивания файлов.
            catalog = await asyncio.to_thread(assemble_catalog, {key: files[key] for key in lists})
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        SOURCE_CATALOG = catalog
        return catalog


def catalog_summary(catalog=None):
    catalog = catalog if catalog is not None else SOURCE_CATALOG
    if catalog is None:
        return None
    return {**catalog["plan"], "updated_at": catalog["updated_at"],
            "next_update_at": catalog["updated_at"] + CATALOG_REFRESH_SECONDS,
            "files": len(catalog["files"]), "duplicates": catalog["duplicates"],
            "errors": catalog["errors"]}


async def catalog_update_loop():
    while True:
        await asyncio.sleep(60)
        try:
            await get_source_catalog()
        except Exception as exc:
            print(f"⚠️ Обновление суточного каталога: {exc}")
