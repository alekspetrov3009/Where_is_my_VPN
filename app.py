import asyncio
from itertools import islice
from contextlib import asynccontextmanager
import base64
import copy
import io
import json
import os
import sys
import tempfile
import time
import threading
import qrcode
import socket
import struct
import random
import platform
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import Response, FileResponse, HTMLResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

# ============ МОДУЛИ ПРИЛОЖЕНИЯ ============
from config import (SERVER_PORT, SERVER_HOST, ADMIN_PATH, ADMIN_USERNAME,
                    ADMIN_PASSWORD, OPEN_BROWSER, ONEKEY_LIMIT, ONEKEY_MIXED_PORT,
                    ONEKEY_TOKEN, GEOIP_ENABLED)
from paths import get_resource_path
from config_parser import URI_PREFIXES, parse_uri_to_singbox_outbound, extract_host
from storage import (STABLE_CHECKS, load_config, save_config, get_saved_singbox_path,
                     save_singbox_path, schedule_scope, load_check_schedule,
                     save_check_schedule, load_pool, save_pool as _save_pool_state,
                     load_dead_cache, save_dead_cache)
from singbox_manager import (find_singbox as _find_singbox, run_singbox_version,
                             download_singbox as _download_singbox)
from catalog import (FALLBACK_LISTS, GOIDA_FALLBACK_LISTS, EXTRA_FALLBACK_LISTS,
                     SOURCE_LABELS, list_source,
                     fetch_dynamic_lists, get_source_catalog, catalog_summary,
                     catalog_update_loop, canonical_endpoint, SOURCE_CATALOG)
from prober import (TEST_URL, TEST_URL_FALLBACK, CHECK_TIMEOUT, FAST_CHECK_TIMEOUT,
                    CHECK_CONCURRENCY, SERVICE_CONCURRENCY, POOL_CONCURRENCY,
                    POOL_RECHECK_SECONDS, SPEED_RECHECK_SECONDS, DISCOVERY_BATCH_SIZE,
                    CHECK_RETRIES_ON_TIMEOUT, SERVICE_TARGETS, REQUIRED_SERVICES,
                    _classify_error,
                    probe_uri_standalone, probe_services)
from check_scheduler import build_pool_plan, interleave_ranges
from clash_export import build_clash_config
from geoip import lookup_ip

# ============ НАСТРОЙКИ ============
# Порт, хост, путь админки и учётные данные читаются из config.py (переменные
# окружения VPN_*; значения по умолчанию — в .env.example).
APP_URL = f"http://localhost:{SERVER_PORT}"

# ============ ГЛОБАЛЬНОЕ СОСТОЯНИЕ ============
# Глобальное хранилище результатов
public_data = {
    "black": {"working": [], "failed_count": 0, "last_update": None},
    "white": {"working": [], "failed_count": 0, "last_update": None},
    "status": {
        "is_checking": False,
        "check_type": None,  # "black", "white" или "both"
        "progress": {"current": 0, "total": 0},
        "next_update_at": None,  # Unix timestamp следующего обновления
        "interval_seconds": 60
    }
}

# Свободные порты выделяет ОС (bind 127.0.0.1:0) прямо в _probe_outbound/probe_services.
# Старый пре-скан 500 портов удалён: он блокировал event loop и давал TOCTOU-гонку.
auto_update_settings = {
    "enabled": True,  # ✅ ВКЛЮЧЕНО ПО УМОЛЧАНИЮ
    "interval_seconds": 60,
    "task": None
}

# Изначально заполняем резервными списками, они будут обновлены при старте
AVAILABLE_LISTS = {**FALLBACK_LISTS, **GOIDA_FALLBACK_LISTS, **EXTRA_FALLBACK_LISTS}

# Статистика последней проверки по типам ошибок и протоколам (для диагностики
# различий между хостами, напр. Windows Server 2022 vs обычный ПК).
CHECK_STATS = {"black": {}, "white": {}}

# Кэш заведомо нерабочих точек: {каноническая точка: время последней неудачи}.
# Каталог содержит десятки тысяч конфигураций, и большинство мертвы. Без кэша
# каждый проход заново тратит на них полный таймаут. Заполняется при старте из
# .vpn_dead.json; точки из кэша пропускаются, пока запись не протухнет.
DEAD_CACHE = {}
# Точки, которые провалились в текущем проходе: сбрасываются в файл один раз
# в конце, а не на каждую неудачу (иначе запись доминирует над проверкой).
DEAD_PENDING = set()

# Переопределяем stdout/stderr на UTF-8, чтобы эмодзи работали в скомпилированном exe.
# Guard: под pythonw или перенаправленным выводом .buffer может отсутствовать.
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
except Exception:
    pass


# ============ ОБЩИЕ ХЕЛПЕРЫ (дедупликация) ============
def format_now():
    """Единый формат отметок времени в пуле и статусах."""
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S")


def same_endpoint(a, b):
    """Элементы пула — одна и та же точка: совпадают URI и источник."""
    return a["uri"] == b["uri"] and a.get("source") == b.get("source")


async def cancel_and_wait(tasks):
    """Отмена задач с ожиданием завершения; исключения проглатываются.

    Единая точка для повторяющегося паттерна cancel + gather(return_exceptions).
    """
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def save_pool():
    """Обёртка: сохраняет текущее глобальное состояние пула."""
    _save_pool_state(public_data, POOL_FILE)


def find_singbox():
    """Обёртка: singbox_manager.find_singbox с сохранённым путём из конфига."""
    return _find_singbox(get_saved_singbox_path)


def load_dead():
    """Загружает кэш нерабочих точек в глобальное состояние."""
    global DEAD_CACHE
    DEAD_CACHE = load_dead_cache(DEAD_FILE)
    return DEAD_CACHE


def flush_dead():
    """Сбрасывает накопленные неудачи текущего прохода в файл.

    Вызывается один раз в конце прохода: запись на каждую неудачу давала бы
    тысячи обращений к диску и конкурировала бы с самими проверками.
    """
    global DEAD_PENDING
    if not DEAD_PENDING:
        return 0
    now = time.time()
    for endpoint in DEAD_PENDING:
        DEAD_CACHE[endpoint] = now
    DEAD_PENDING = set()
    save_dead_cache(DEAD_CACHE, DEAD_FILE)
    return len(DEAD_CACHE)


def is_dead(uri):
    """True, если точка недавно провалилась и её можно пропустить."""
    if not DEAD_CACHE:
        return False
    return canonical_endpoint(uri) in DEAD_CACHE


def mark_dead(uri):
    """Помечает точку как нерабочую (применится в конце прохода)."""
    DEAD_PENDING.add(canonical_endpoint(uri))


def mark_alive(uri):
    """Точка ответила: убираем её из кэша и из очереди на запись.

    Без этого ожившая точка оставалась бы в кэше до истечения TTL, даже
    если её только что успешно проверили.
    """
    endpoint = canonical_endpoint(uri)
    DEAD_PENDING.discard(endpoint)
    if DEAD_CACHE.pop(endpoint, None) is not None:
        save_dead_cache(DEAD_CACHE, DEAD_FILE)


async def probe_uri_standalone(singbox_path, tag, uri, fast=False):
    """Обёртка: передаёт актуальный app._probe_outbound (точку патча тестов)."""
    return await _probe_uri_impl(singbox_path, tag, uri, fast=fast, probe=_probe_outbound)


def measure_download_speed(proxy_port, speed_bytes=None):
    """Обёртка: передаёт актуальный app.SPEED_BYTES (точку патча тестов)."""
    if speed_bytes is None:
        speed_bytes = SPEED_BYTES
    return _measure_speed_impl(proxy_port, speed_bytes=speed_bytes)


async def get_source_catalog(progress_callback=None):
    """Обёртка: синхронизирует app.SOURCE_CATALOG (точку патча тестов) с catalog
    и передаёт актуальный app.fetch_dynamic_lists."""
    global SOURCE_CATALOG
    import catalog as _catalog_mod
    _catalog_mod.SOURCE_CATALOG = SOURCE_CATALOG
    result = await _get_catalog_impl(progress_callback, fetch_lists=fetch_dynamic_lists)
    SOURCE_CATALOG = result
    return result


def load_pool():
    """Обёртка: передаёт актуальный app.POOL_FILE (точку патча тестов)."""
    return _load_pool_impl(POOL_FILE)


# ============ РЕЭКСПОРТ ДЛЯ ТЕСТОВ ============
# Тесты патчат эти имена на модуле app; сами функции живут в выделенных
# модулях, но вызовы внутри app идут через эти ссылки, чтобы patch.object
# подменял поведение. urllib/browser_requests нужны тестам как точки патча.
import urllib.request  # noqa: E402
import urllib.parse  # noqa: E402
from curl_cffi import requests as browser_requests  # noqa: E402
from prober import _probe_outbound  # noqa: E402
from prober import probe_uri_standalone as _probe_uri_impl  # noqa: E402
from prober import measure_download_speed as _measure_speed_impl  # noqa: E402
from prober import check_service_page  # noqa: E402
from catalog import assemble_catalog, urlopen_insecure  # noqa: E402
from catalog import get_source_catalog as _get_catalog_impl  # noqa: E402
from storage import load_pool as _load_pool_impl  # noqa: E402
from paths import POOL_FILE, SCHEDULE_FILE, DEAD_FILE  # noqa: E402
from prober import SPEED_BYTES  # noqa: E402


def open_browser_delayed():
    """Ждет 2 секунды, чтобы Uvicorn гарантированно занял порт, и открывает браузер.

    Выполняется только при VPN_OPEN_BROWSER=1 (по умолчанию — на Windows);
    в Docker/на сервере браузер не открывается.
    """
    time.sleep(2.0)
    if not OPEN_BROWSER:
        return
    try:
        # Используем динамический URL, собранный из переменной SERVER_PORT
        os.startfile(APP_URL)
    except Exception:
        # Если вдруг не получилось (например, нет браузера по умолчанию),
        # сервер продолжит работать, просто не откроет вкладку
        pass


# ============ ДИАЛОГ ВЫБОРА ФАЙЛА ============
def open_file_dialog():
    result = {"path": None}
    def _dialog():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            path = filedialog.askopenfilename(
                title="Выберите sing-box.exe",
                filetypes=[("Исполняемые файлы", "*.exe"), ("Все файлы", "*.*")],
                initialdir=str(Path.home() / "Downloads")
            )
            result["path"] = path if path else None
            root.destroy()
        except Exception:
            result["path"] = None
    thread = threading.Thread(target=_dialog, daemon=True)
    thread.start()
    thread.join(timeout=120)
    return result["path"]


# ============ ЯДРО ПРОВЕРКИ ============
CHECK_LOCK = asyncio.Lock()


def get_service_working(require_all_services=True):
    """Объединение текущих рабочих пулов без повторения URI.

    require_all_services=True — только серверы с доступом ко всем
    REQUIRED_SERVICES (секция ChatGPT); False — все рабочие (один ключ).
    Остальные SERVICE_TARGETS (yt/tg/ig) дают профиль доступности в записи
    пула, но выдачу не сужают: иначе расширение списка сервисов молча
    опустошило бы таблицу.
    """
    combined = {}
    for list_type in ("black", "white"):
        for config in public_data[list_type]["working"]:
            if require_all_services:
                services = config.get("services", {})
                results = services.get("results", {})
                if not all(results.get(name, {}).get("ok") is True for name in REQUIRED_SERVICES):
                    continue
            uri = config["uri"]
            if uri not in combined:
                combined[uri] = {**config, "lists": []}
            elif config.get("stable") and not combined[uri].get("stable"):
                combined[uri] = {**config, "lists": combined[uri]["lists"]}
            if list_type not in combined[uri]["lists"]:
                combined[uri]["lists"].append(list_type)
    return list(combined.values())


def pool_priority(item):
    results = item.get("services", {}).get("results", {})
    return (not results.get("gpt", {}).get("ok", False), not item.get("stable", False),
            item.get("last_checked_at", 0))


def build_onekey_config(limit=None):
    """Собирает sing-box конфиг «один ключ» из лучших рабочих серверов.

    Отбор: стабильные серверы из объединённого пула (get_service_working),
    дедупликация по конечной точке (canonical_endpoint), сортировка по пингу
    и свежему замеру скорости, top-N. Клиентская часть — urltest: sing-box
    сам выбирает сервер с минимальной задержкой и переключается при деградации.
    Возвращает (config_dict, error_message); ровно один из элементов не None.
    """
    if limit is None:
        limit = ONEKEY_LIMIT
    candidates = [c for c in get_service_working(require_all_services=False) if c.get("stable")]
    if not candidates:
        return None, "Нет стабильных серверов — ключ появится после проверки пула"

    # Дедуп по конечной точке: один сервер с разными remark — одна запись.
    unique = {}
    for config in candidates:
        key = canonical_endpoint(config["uri"])
        if key not in unique:
            unique[key] = config
    candidates = list(unique.values())

    candidates.sort(key=onekey_sort_key)

    outbounds = []
    seen_tags = set()
    for index, config in enumerate(candidates):
        if len(outbounds) >= limit:
            break
        outbound = parse_uri_to_singbox_outbound(config["uri"], f"srv-{index + 1}")
        if not outbound:
            continue
        if outbound["tag"] in seen_tags:
            continue
        seen_tags.add(outbound["tag"])
        outbounds.append(outbound)

    if not outbounds:
        return None, "Ни один стабильный URI не удалось преобразовать в конфиг sing-box"

    config = {
        "log": {"level": "warn", "timestamp": True},
        "dns": {
            "servers": [
                {"tag": "remote", "address": "tcp://8.8.8.8", "detour": "auto"},
                {"tag": "local", "address": "local", "detour": "direct"}
            ],
            "rules": [{"outbound": "any", "server": "local"}],
            "final": "remote"
        },
        "inbounds": [{
            "type": "mixed", "tag": "mixed-in",
            "listen": "127.0.0.1", "listen_port": ONEKEY_MIXED_PORT
        }],
        "outbounds": [{
            "type": "urltest", "tag": "auto",
            "outbounds": [o["tag"] for o in outbounds],
            "url": TEST_URL,
            "interval": "3m",
            "tolerance": 50
        }, {
            "type": "direct", "tag": "direct"
        }] + outbounds,
        "route": {
            "auto_detect_interface": True,
            "final": "auto"
        }
    }
    return config, None


def build_onekey_subscription(limit=None):
    """Собирает base64-подписку «один ключ» из лучших рабочих серверов.

    Универсальный формат для клиентов-подписок (v2rayNG, Nekoray, Streisand,
    Hiddify): base64 от списка URI по одному на строку. Автопереключение
    обеспечивает сам клиент (urltest/selector в его настройках).
    Возвращает (text, error_message); ровно один из элементов не None.
    """
    if limit is None:
        limit = ONEKEY_LIMIT
    candidates = [c for c in get_service_working(require_all_services=False) if c.get("stable")]
    if not candidates:
        return None, "Нет стабильных серверов — ключ появится после проверки пула"

    # Дедуп по конечной точке: один сервер с разными remark — одна запись.
    unique = {}
    for config in candidates:
        key = canonical_endpoint(config["uri"])
        if key not in unique:
            unique[key] = config
    candidates = list(unique.values())

    candidates.sort(key=onekey_sort_key)
    uris = [c["uri"] for c in candidates[:limit]]
    if not uris:
        return None, "Нет подходящих серверов для подписки"
    return base64.b64encode("\n".join(uris).encode()).decode(), None


def _onekey_format(request):
    """Определяет формат ответа /api/onekey: "clash", "json" или "base64".

    Явный ?format=... имеет приоритет; иначе — User-Agent. Клиенты Clash и
    Mihomo (Clash Verge, FlClash, Stash) ждут YAML, sing-box и его GUI-обёртки
    (SFA/SFI/SFM/SFT, Hiddify) — JSON, остальные подписки — base64-список URI.
    """
    fmt = request.query_params.get("format", "").strip().lower()
    if fmt in ("json", "base64", "clash", "yaml", "mihomo"):
        return "clash" if fmt in ("clash", "yaml", "mihomo") else fmt
    ua = (request.headers.get("user-agent") or "").lower()
    if any(marker in ua for marker in ("clash", "mihomo", "stash", "flclash")):
        return "clash"
    if any(marker in ua for marker in ("sing-box", "sfa/", "sfi/", "sfm/", "sft/", "hiddify")):
        return "json"
    return "base64"


def build_onekey_clash(limit=None):
    """Собирает Clash/Mihomo YAML из лучших рабочих серверов.

    Отбор тот же, что у base64-подписки (стабильные, дедуп по конечной точке,
    сортировка по пингу и свежей скорости), меняется только формат вывода.
    Возвращает (yaml_text, error_message); ровно один из элементов не None.
    """
    if limit is None:
        limit = ONEKEY_LIMIT
    candidates = [c for c in get_service_working(require_all_services=False) if c.get("stable")]
    if not candidates:
        return None, "Нет стабильных серверов — ключ появится после проверки пула"
    unique = {}
    for config in candidates:
        key = canonical_endpoint(config["uri"])
        if key not in unique:
            unique[key] = config
    candidates = list(unique.values())
    candidates.sort(key=onekey_sort_key)
    uris = [c["uri"] for c in candidates[:limit]]
    if not uris:
        return None, "Нет подходящих серверов для подписки"
    return build_clash_config(uris, mixed_port=ONEKEY_MIXED_PORT)


def _onekey_wants_json(request):
    """Совместимость: True, если клиент ожидает sing-box JSON."""
    return _onekey_format(request) == "json"


def onekey_sort_key(config):
    """Сортировка кандидатов: пинг, свежая скорость, число успешных проверок."""
    speed = (config.get("speed") or {})
    fresh_speed = speed.get("mbps") if (
        isinstance(speed.get("mbps"), (int, float))
        and speed.get("mbps") == speed.get("mbps")  # не NaN
        and time.time() - speed.get("checked_at", 0) <= SPEED_RECHECK_SECONDS
    ) else None
    return (config.get("latency") or 9999,
            -(fresh_speed or 0),
            -config.get("successful_checks", 0))


async def resolve_geo(uri, previous=None):
    """Геоданные сервера по URI; previous переиспользуется, если хост тот же.

    Возвращает dict geoip.lookup_ip или None. Сбой сети не считается ошибкой
    проверки: запись просто остаётся без геоданных.
    """
    if not GEOIP_ENABLED:
        return None
    try:
        host = extract_host(uri)
        if not host:
            return None
        if previous and previous.get("host") == host:
            return previous
        data = await asyncio.to_thread(lookup_ip, host)
        if not data:
            return None
        return {**data, "host": host}
    except Exception as exc:
        print(f"⚠️ Геопроверка {uri[:40]}: {exc}")
        return None


async def maintain_working_pool(singbox_path, first_sweep):
    """Актуализация не зависит от размера списков и не останавливает поиск."""
    while True:
        now = time.time()
        due = sorted(
            ((t, c) for t in ("black", "white") for c in public_data[t]["working"]
             if now - c.get("last_checked_at", 0) >= POOL_RECHECK_SECONDS),
            key=lambda pair: pool_priority(pair[1]))
        pending = iter(due)

        async def worker(worker_id):
            for list_type, item in pending:
                ok, latency = await probe_uri_standalone(
                    singbox_path, f"pool_{worker_id}", item["uri"])
                replacement = None
                if ok:
                    services = await probe_services(singbox_path, item["uri"])
                    successes = item.get("successful_checks", 0) + 1
                    replacement = {**item, "latency": latency, "services": services,
                                   "successful_checks": successes, "stable": successes >= STABLE_CHECKS,
                                   "last_checked_at": time.time()}
                    # Геоданные переиспользуются, пока хост тот же: внешний
                    # запрос на каждую перепроверку исчерпал бы лимит ip-api.
                    replacement["geo"] = await resolve_geo(item["uri"], item.get("geo"))
                # Замер скорости мог обновить объект во время сетевой проверки.
                current = public_data[list_type]["working"]
                if replacement is not None:
                    latest = next((c for c in current if same_endpoint(c, item)), item)
                    if "speed" in latest:
                        replacement["speed"] = latest["speed"]
                public_data[list_type]["working"] = [
                    replacement if same_endpoint(c, item) else c
                    for c in current
                    if replacement is not None or not same_endpoint(c, item)]
                if not ok:
                    public_data[list_type]["failed_count"] += 1
                public_data[list_type]["last_update"] = format_now()

        tasks = [asyncio.create_task(worker(i)) for i in range(POOL_CONCURRENCY)]
        try:
            await asyncio.gather(*tasks)
        finally:
            await cancel_and_wait(tasks)
        if due:
            save_pool()
        first_sweep.set()
        await asyncio.sleep(5)


async def maintain_pool_speed(singbox_path):
    """Один фоновый замер: не занимает слоты VPN/ChatGPT."""
    while True:
        now = time.time()
        candidates = sorted(
            ((t, c) for t in ("black", "white") for c in public_data[t]["working"]
             if c.get("stable") and now - (c.get("speed") or {}).get("checked_at", 0) >= SPEED_RECHECK_SECONDS),
            key=lambda pair: pool_priority(pair[1]))
        if not candidates:
            await asyncio.sleep(5)
            continue
        list_type, item = candidates[0]
        # Короткий проход поиска может закончиться раньше замера. Доводим уже
        # начатый замер до результата, иначе он будет бесконечно перезапускаться.
        task = asyncio.create_task(probe_services(singbox_path, item["uri"], measure_speed=True, speed_only=True))
        cancelled = False
        try:
            report = await asyncio.shield(task)
        except asyncio.CancelledError:
            report = await task
            cancelled = True
        public_data[list_type]["working"] = [
            {**c, "speed": report["speed"]} if same_endpoint(c, item) else c
            for c in public_data[list_type]["working"]]
        if cancelled:
            raise asyncio.CancelledError


async def run_check_core(check_type, progress_callback=None, source_id=None):
    if check_type not in ("black", "white", "both"):
        raise ValueError("Неизвестный тип списка")
    if source_id is not None and source_id not in SOURCE_LABELS:
        raise ValueError("Неизвестный источник")
    # Ручная и автоматическая проверки не должны перезаписывать прогресс друг друга.
    async with CHECK_LOCK:
        # Deepcopy: shallow dict() делил ссылку на список working и откат при
        # отмене SSE не работал бы при любом in-place изменении.
        previous_data = {t: copy.deepcopy(public_data[t]) for t in ("black", "white")}
        public_data["status"].update(
            is_checking=True, check_type=check_type,
            progress={"current": 0, "total": 0}
        )
        singbox_path, _ = find_singbox()
        first_sweep = asyncio.Event()
        maintenance = []
        try:
            if singbox_path:
                maintenance = [asyncio.create_task(maintain_working_pool(singbox_path, first_sweep)),
                               asyncio.create_task(maintain_pool_speed(singbox_path))]
            result = await _run_check_core(check_type, progress_callback, source_id)
            if maintenance:
                waiter = asyncio.create_task(first_sweep.wait())
                try:
                    await asyncio.wait([waiter, *maintenance], return_when=asyncio.FIRST_COMPLETED)
                    for task in maintenance:
                        if task.done():
                            task.result()
                finally:
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
            kinds = ("black", "white") if check_type == "both" else (check_type,)
            working = [c for kind in kinds for c in public_data[kind]["working"]
                       if source_id is None or c.get("source", "unknown") == source_id]
            return working, result[1], result[2]
        except BaseException:
            # Включая отмену SSE при закрытии вкладки: неполный проход
            # не должен подменять последний завершённый результат.
            await cancel_and_wait(maintenance)
            for t, data in previous_data.items():
                public_data[t] = data
            raise
        finally:
            await cancel_and_wait(maintenance)
            save_pool()
            public_data["status"].update(is_checking=False, check_type=None)


def _resolve_singbox_or_raise():
    """Диагностика и валидация пути sing-box перед проходом каталога."""
    singbox_path, source = find_singbox()
    if not singbox_path:
        error_msg = "sing-box не найден"
        print(f"❌ КРИТИЧЕСКАЯ ОШИБКА: {error_msg}")
        raise RuntimeError(error_msg)

    print(f"🔧 Диагностика sing-box:")
    print(f"   Путь: {singbox_path}")
    print(f"   Существует: {singbox_path.exists()}")
    print(f"   Это файл: {singbox_path.is_file()}")

    if not singbox_path.exists():
        error_msg = f"sing-box.exe не существует по пути: {singbox_path}"
        print(f"❌ {error_msg}")
        raise RuntimeError(error_msg)
    return singbox_path


def _collect_check_stats(kinds, check_type, failed_configs, source_id,
                         kind_checked_count, skipped_sources):
    """Диагностическая статистика: по ней видно, ЧЕМ сервер отличается от ПК
    (таймауты = медленный хост/сеть, dns = серверный DNS, tls/handshake =
    DPI/блок первого хопа, просадка hysteria2 = порезанный UDP)."""
    try:
        # Общий прогон "both" пишет статистику по каждому типу отдельно,
        # иначе плитки Black/White в админке показывали бы устаревшие данные.
        for kind in kinds:
            kind_failed = [i for i in failed_configs if i.get("list_type", kind) == kind]
            kind_working = [c for c in public_data[kind]["working"]
                            if source_id is None or c.get("source", "unknown") == source_id]
            err_counts, work_by_proto, fail_by_proto = {}, {}, {}
            for item in kind_failed:
                key = _classify_error(item.get("error", ""))
                err_counts[key] = err_counts.get(key, 0) + 1
                proto = (item.get("uri", "").split("://", 1)[0] or "unknown")
                fail_by_proto[proto] = fail_by_proto.get(proto, 0) + 1
            for item in kind_working:
                proto = (item.get("uri", "").split("://", 1)[0] or "unknown")
                work_by_proto[proto] = work_by_proto.get(proto, 0) + 1
            CHECK_STATS[kind] = {
                "checked": kind_checked_count[kind],
                "working": len(kind_working),
                "failed": len(kind_failed),
                "skipped_sources": skipped_sources,
                "errors": err_counts,
                "working_by_proto": work_by_proto,
                "failed_by_proto": fail_by_proto,
                # Частичная проверка (по одному источнику) даёт статистику только
                # по нему — помечаем scope, чтобы в /api/diag не читать как полную.
                "scope": source_id or ("all" if check_type == kind else check_type),
            }
            print(f"📊 Статистика {kind}: {CHECK_STATS[kind]}")
    except Exception as e:
        print(f"⚠️ Не удалось собрать статистику: {e}")


async def _run_check_core(check_type, progress_callback=None, source_id=None):
    singbox_path = _resolve_singbox_or_raise()

    kinds = ("black", "white") if check_type == "both" else (check_type,)
    failed_configs = []
    # Endpoint-уровень: точка из пула с другим remark — всё та же точка,
    # за её актуальность отвечает фоновая перепроверка, а не поиск.
    known = {(c.get("source", "unknown"), kind, canonical_endpoint(c["uri"]))
             for kind in kinds for c in public_data[kind]["working"] if c.get("uri")}
    checked = 0
    kind_checked_count = {kind: 0 for kind in kinds}
    completed = 0
    total_units = 0
    source_counts = {}
    source_completed = {}
    catalog = None
    source_progress = {"name": "Суточный каталог всех источников", "phase": "preparing", "pool": True,
                       "current": 0, "total": 0, "message": "Загрузка и подсчёт конфигураций"}
    for kind in kinds:
        public_data[kind]["failed_count"] = 0
    service_slots = asyncio.Semaphore(SERVICE_CONCURRENCY)

    async def publish_progress():
        progress = {
            "current": completed, "total": total_units, "checked": checked, "unit": "configs",
            "sources_done": sum(source_completed.get(s, 0) == n for s, n in source_counts.items()),
            "sources_total": len(source_counts),
            "sources_skipped": len(catalog["errors"]) if catalog else 0,
            "source": dict(source_progress),
            "allocation": {s: {"total": n, "current": source_completed.get(s, 0)} for s, n in source_counts.items()},
        }
        public_data["status"]["progress"] = progress
        if progress_callback:
            await progress_callback({"type": "progress", **progress})

    async def catalog_progress(event):
        source_progress.update(current=event["current"], total=event["total"],
                               message=f"Файлов: {event['current']}/{event['total']} — {event['name']}")
        await publish_progress()

    await publish_progress()
    catalog = await get_source_catalog(catalog_progress)

    def eligible(source, item):
        kind, uri = item
        if kind not in kinds or (source_id is not None and source != source_id):
            return False
        if (source, kind, canonical_endpoint(uri)) in known:
            return False
        # Недавно провалившиеся точки пропускаем: каталог огромен, и повторная
        # проверка заведомо мёртвых конфигураций съедает почти всё время.
        return not is_dead(uri)

    pools = catalog["plan"]["pools"] or []
    scope = schedule_scope(check_type, source_id)
    schedule = load_check_schedule(SCHEDULE_FILE)
    saved = schedule.get(scope, {})
    try:
        wished = int(saved.get("next_pool", 1))
    except (TypeError, ValueError):
        wished = 1
    # Возобновление только для того же каталога; без id (или при смене
    # содержимого) — всегда с первого пула.
    if saved.get("catalog_id") == catalog.get("id") and catalog.get("id"):
        start_pool = max(1, min(wished, len(pools) or 1))
    else:
        start_pool = 1
    if start_pool > 1:
        print(f"▶️ Продолжение {scope} с пула {start_pool}/{len(pools)}")
    # Смещения пропущенных пулов: нарезка ниже идёт с учётом уже пройденного.
    offsets = {}
    for quotas in pools[:start_pool - 1]:
        for source, quota in quotas.items():
            offsets[source] = offsets.get(source, 0) + quota

    def count_scope():
        # Прогресс честен при возобновлении: считаем только остаток.
        return {s: sum(eligible(s, item) for item in islice(items, offsets.get(s, 0), None))
                for s, items in catalog["groups"].items() if source_id is None or s == source_id}

    source_counts = await asyncio.to_thread(count_scope)
    total_units = sum(source_counts.values())
    skipped_sources = len(catalog["errors"])

    async def finish_config(source):
        nonlocal completed
        completed += 1
        source_completed[source] = source_completed.get(source, 0) + 1
        source_progress["current"] += 1
        await publish_progress()

    async def add_working(uri, latency, source, kind, previous=None):
        successes = (previous or {}).get("successful_checks", 0) + 1
        previous_added = (previous or {}).get("added_at")
        config = {"uri": uri, "latency": latency, "source": source,
                  "successful_checks": successes, "stable": successes >= STABLE_CHECKS,
                  "added_at": previous_added or format_now()}
        async with service_slots:
            config["services"] = await probe_services(singbox_path, uri)
        # Геоданные: сервер с российским IP для обхода блокировок бесполезен.
        # Запрос в отдельном потоке — urllib блокирующий, а лимит ip-api
        # заставляет ждать, и это не должно останавливать event loop.
        config["geo"] = await resolve_geo(uri)
        config["last_checked_at"] = time.time()
        public_data[kind]["working"].append(config)
        await finish_config(source)

    for index, quotas in enumerate(pools, 1):
        if index < start_pool:
            continue
        def prepare_pool():
            groups = {}
            for source, quota in quotas.items():
                start = offsets.get(source, 0)
                offsets[source] = start + quota
                if source_id is None or source == source_id:
                    groups[source] = tuple(item for item in islice(catalog["groups"][source], start, start + quota)
                                           if eligible(source, item))
            return groups

        pool_groups = await asyncio.to_thread(prepare_pool)
        pool_counts = {s: len(items) for s, items in pool_groups.items()}
        source_progress = {
            "index": index, "name": f"Пропорциональный пул {index}/{catalog['plan']['pool_count']}",
            "phase": "checking", "current": 0, "total": sum(pool_counts.values()),
            "pool_index": index, "pool_count": catalog["plan"]["pool_count"],
            "pool": True,
            "quotas": quotas, "scope_quotas": pool_counts,
            "duplicates": catalog["duplicates"], "message": "Чередование источников: VPN + ChatGPT"
        }
        await publish_progress()

        async def worker(fast=False):
            nonlocal checked
            for uri_index, (source, (kind, uri)) in pending:
                tag = f"proxy_{index}_{uri_index}"
                is_ok, result_data = await probe_uri_standalone(singbox_path, tag, uri, fast=fast)
                if fast and not is_ok and (_classify_error(str(result_data)) in ("timeout", "dns")
                                           or "Неожиданный ответ" in str(result_data)):
                    # Полная проверка сохраняет резервный URL и прежние таймауты.
                    # Быстрый проход лишь раньше доставляет кандидатов в ChatGPT.
                    # Перехват ответа (заглушка DPI) сюда не попадает: он
                    # детерминирован, полный проход дал бы тот же результат.
                    retry_items.append((uri_index, (source, (kind, uri))))
                    continue
                if is_ok:
                    mark_alive(uri)
                    service_tasks.append(asyncio.create_task(add_working(uri, result_data, source, kind)))
                else:
                    # Перехват DPI и ошибки парсинга детерминированы, как и
                    # обычная недоступность: кэшируем все неудачи одинаково.
                    mark_dead(uri)
                    failed_configs.append({"uri": uri, "source": source, "list_type": kind, "error": result_data})
                checked += 1
                kind_checked_count[kind] += 1
                if not is_ok:
                    public_data[kind]["failed_count"] += 1
                    await finish_config(source)
                await asyncio.sleep(0)

        candidates = enumerate(interleave_ranges(pool_groups, pool_counts, {}))
        while True:
            batch = list(islice(candidates, DISCOVERY_BATCH_SIZE))
            if not batch:
                break
            pending = iter(batch)
            service_tasks = []
            retry_items = []
            tasks = [asyncio.create_task(worker(fast=True)) for _ in range(min(CHECK_CONCURRENCY, len(batch)))]
            try:
                await asyncio.gather(*tasks)
                pending = iter(retry_items)
                source_progress["message"] = "Полная проверка оставшихся VPN + ChatGPT"
                tasks = [asyncio.create_task(worker()) for _ in range(min(CHECK_CONCURRENCY, len(retry_items)))]
                await asyncio.gather(*tasks)
                source_progress["message"] = "Завершение проверок ChatGPT"
                await publish_progress()
                await asyncio.gather(*service_tasks)
            finally:
                await cancel_and_wait(tasks + service_tasks)
        source_progress["phase"] = "done"
        await publish_progress()
        # Чекпоинт после каждого пула: отмена/падение теряет максимум один пул.
        schedule[scope] = {"catalog_id": catalog.get("id"), "next_pool": index + 1}
        save_check_schedule(schedule, SCHEDULE_FILE)

    # Все пулы пройдены — следующий цикл начинает с начала.
    if schedule.pop(scope, None) is not None:
        save_check_schedule(schedule, SCHEDULE_FILE)

    working_configs = [c for kind in kinds for c in public_data[kind]["working"]
                       if source_id is None or c.get("source", "unknown") == source_id]
    for kind in kinds:
        public_data[kind]["last_update"] = format_now()
    save_pool()
    # Кэш неудач сбрасываем один раз в конце прохода: запись на каждую
    # неудачу конкурировала бы с самими проверками.
    dead_total = flush_dead()
    _collect_check_stats(kinds, check_type, failed_configs, source_id,
                         kind_checked_count, skipped_sources)
    print(f"💾 Пул сохранён: {check_type} — рабочих {len(working_configs)}, проверено {checked}, "
          f"в кэше нерабочих точек: {dead_total}")
    return working_configs, failed_configs, checked

# ============ СЕРВЕРНОЕ АВТООБНОВЛЕНИЕ ============
async def server_auto_update_loop():
    """Фоновая задача автообновления с защитой от падений и логированием"""
    print("🔄 Фоновая задача автообновления ЗАПУЩЕНА!")
    await asyncio.sleep(5)  # Даем серверу 5 секунд на полный старт

    while True:
        if not auto_update_settings["enabled"]:
            await asyncio.sleep(5)
            continue

        # Дедлайн цикла фиксируем заранее: next_update_at честен даже во время
        # долгой проверки, а сон после — до дедлайна, а не interval после конца.
        interval = auto_update_settings["interval_seconds"]
        deadline = time.time() + interval
        public_data["status"]["next_update_at"] = deadline

        # Общий план чередует источники и типы списков внутри каждого пула.
        for check_type in ["both"]:
            if not auto_update_settings["enabled"]:
                break
            print(f"\n▶️ НАЧАЛО АВТОПРОВЕРКИ: {check_type.upper()}")
            try:
                # 2. Запускаем проверку
                working, failed, total = await run_check_core(check_type)

                print(f"✅ АВТОПРОВЕРКА ЗАВЕРШЕНА: {check_type.upper()} (Рабочих: {len(working)}, Всего проверено: {total})")

            except Exception as e:
                print(f"❌ КРИТИЧЕСКАЯ ОШИБКА в автообновлении ({check_type}): {e}")
                import traceback
                traceback.print_exc()  # Печатаем полный стек ошибки для отладки

        # 5. Планируем следующее обновление: интервал мог поменять в админке.
        interval = auto_update_settings["interval_seconds"]
        # Если проверка длилась дольше интервала — не копим отставание, стартуем
        # следующий цикл почти сразу, но с обновлённым next_update_at.
        wait = deadline - time.time()
        if wait < 0:
            deadline = time.time() + interval
            wait = 0
        public_data["status"]["next_update_at"] = deadline if wait else time.time() + interval
        print(f"⏳ Ожидание следующей проверки через {wait:.0f} сек...\n")

        await asyncio.sleep(wait)


# ============ LIFESPAN И ПРИЛОЖЕНИЕ ============
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Сервер запускается; каталог источников будет загружен перед проверкой.")

    # Загружаем сохранённый пул рабочих конфигураций
    pool = load_pool()
    for list_type in ("black", "white"):
        public_data[list_type]["working"] = pool[list_type]
    print(f"💾 Загружен сохранённый пул: black={len(pool['black'])}, white={len(pool['white'])}")

    # Кэш нерабочих точек: переживает перезапуски, избавляет от повторной
    # проверки заведомо мёртвых конфигураций из огромного каталога.
    dead = load_dead()
    print(f"🪦 Загружен кэш нерабочих точек: {len(dead)}")

    # Запускаем фоновые задачи
    auto_update_settings["task"] = asyncio.create_task(server_auto_update_loop())
    catalog_task = asyncio.create_task(catalog_update_loop())
    try:
        yield
    finally:
        await cancel_and_wait([auto_update_settings["task"], catalog_task])
        print("🛑 Фоновые задачи остановлены.")


app = FastAPI(lifespan=lifespan)
security = HTTPBasic()


# ============ АВТОРИЗАЦИЯ ============
def get_current_user(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != ADMIN_USERNAME or credentials.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Неверные учетные данные")
    return credentials.username


# ============ ENDPOINTS ============
@app.get("/", response_class=HTMLResponse)
async def read_root():
    # Получаем путь и сразу отдаем его через FileResponse (он сам всё прочитает)
    html_path = get_resource_path("index.html")
    return FileResponse(html_path, headers={"Cache-Control": "no-store"})

@app.get(ADMIN_PATH, response_class=HTMLResponse)
async def admin_page(username: str = Depends(get_current_user)):
    # Получаем путь и возвращаем файл
    admin_html_path = get_resource_path("admin.html")
    return FileResponse(admin_html_path, headers={"Cache-Control": "no-store"})

@app.get("/api/lists")
async def get_lists(username: str = Depends(get_current_user)):
    singbox_path, source = find_singbox()
    source_labels = {"user": "указан вручную", "system": "системный", "local": "папка приложения", "nekoray": "Nekoray/Nekobox", "": ""}
    return {
        "lists": {key: {**info, "source": list_source(info)} for key, info in AVAILABLE_LISTS.items()},
        "sources": SOURCE_LABELS,
        "catalog": catalog_summary(),
        "singbox_available": singbox_path is not None,
        "singbox_path": str(singbox_path) if singbox_path else "",
        "singbox_source": source_labels.get(source, ""),
        "pool": {
            "black": len(public_data["black"]["working"]),
            "white": len(public_data["white"]["working"])
        },
        "auto_update": {
            "enabled": auto_update_settings["enabled"],
            "interval_seconds": auto_update_settings["interval_seconds"]
        }
    }

@app.get("/api/admin/status")
async def get_admin_status(username: str = Depends(get_current_user)):
    """Статус для админки"""
    return {
        "auto_update": {key: auto_update_settings[key] for key in ("enabled", "interval_seconds")},
        "check_tuning": {
            "timeout": CHECK_TIMEOUT,
            "fast_timeout": FAST_CHECK_TIMEOUT,
            "concurrency": CHECK_CONCURRENCY,
            "service_concurrency": SERVICE_CONCURRENCY,
            "pool_concurrency": POOL_CONCURRENCY,
            "pool_recheck_seconds": POOL_RECHECK_SECONDS,
            "speed_recheck_seconds": SPEED_RECHECK_SECONDS,
            "retries_on_timeout": CHECK_RETRIES_ON_TIMEOUT,
            "required_successful_checks": 2,
            "test_url": TEST_URL,
            "test_url_fallback": TEST_URL_FALLBACK,
        },
        "stats": CHECK_STATS,
        "catalog": catalog_summary(),
        "public_data": {
            "black": {
                "working_count": len(public_data["black"]["working"]),
                "failed_count": public_data["black"]["failed_count"],
                "last_update": public_data["black"]["last_update"]
            },
            "white": {
                "working_count": len(public_data["white"]["working"]),
                "failed_count": public_data["white"]["failed_count"],
                "last_update": public_data["white"]["last_update"]
            }
        }
    }

def _raw_dns_time(server, name, timeout=4.0):
    """Один UDP DNS A-запрос напрямую к указанному серверу, без системного
    резолвера. Возвращает строку 'IP (N ms)' или 'ERROR: ...'. Нужен для
    сравнения: системный (локальный на DNS-сервере) vs публичный."""
    try:
        qid = random.randint(0, 65535)
        header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
        qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.split(".")) + b"\x00"
        packet = header + qname + struct.pack(">HH", 1, 1)
        t0 = time.perf_counter()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(packet, (server, 53))
            data, _ = s.recvfrom(512)
        ms = int((time.perf_counter() - t0) * 1000)
        if len(data) < 12 or struct.unpack(">H", data[0:2])[0] != qid:
            return "ERROR: bad reply"
        ancount = struct.unpack(">H", data[6:8])[0]
        if not ancount:
            return f"no answer ({ms} ms)"
        return f"{ancount} answer(s) ({ms} ms)"
    except Exception as e:
        return f"ERROR: {e}"

@app.get("/api/diag")
async def diag(username: str = Depends(get_current_user)):
    """Быстрая диагностика хоста: помогает понять, чем Windows Server
    отличается от обычного ПК (DNS, исходящие соединения, холодный старт
    sing-box, запись во временную папку)."""
    result = {
        "host": platform.node(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "check_tuning": {
            "timeout": CHECK_TIMEOUT,
            "concurrency": CHECK_CONCURRENCY,
            "retries_on_timeout": CHECK_RETRIES_ON_TIMEOUT,
        },
        "stats": CHECK_STATS,
    }
    # DNS до тестовых доменов
    for name in ("cp.cloudflare.com", "connectivitycheck.gstatic.com",
                 "raw.githack.com", "api.github.com"):
        try:
            t0 = time.perf_counter()
            ip = socket.gethostbyname(name)
            ms = int((time.perf_counter() - t0) * 1000)
            result[f"dns_{name}"] = f"{ip} ({ms} ms, системный DNS)"
        except Exception as e:
            result[f"dns_{name}"] = f"ERROR: {e}"
    # Тот же запрос напрямую к публичным DNS в обход системного (локального
    # на DNS-сервере). Если здесь быстро, а выше медленно/ошибка — виноват
    # именно локальный DNS Windows Server, а не сеть и не VPN-листы.
    result["dns_direct_8.8.8.8"] = _raw_dns_time("8.8.8.8", "cp.cloudflare.com")
    result["dns_direct_1.1.1.1"] = _raw_dns_time("1.1.1.1", "cp.cloudflare.com")
    # Прямое TCP-соединение наружу (первый хоп проверки без VPN)
    for host, port in (("cp.cloudflare.com", 80), ("www.google.com", 80)):
        key = f"tcp_{host}_{port}"
        try:
            with socket.create_connection((host, port), timeout=5):
                result[key] = "ok"
        except Exception as e:
            result[key] = f"ERROR: {e}"
    # sing-box: наличие, версия, время холодного старта
    try:
        singbox_path, source = find_singbox()
        result["singbox_path"] = str(singbox_path) if singbox_path else None
        result["singbox_source"] = source
        if singbox_path:
            t0 = time.perf_counter()
            ok, version_line = run_singbox_version(singbox_path, timeout=10)
            result["singbox_version"] = version_line or "unknown"
            result["singbox_startup_ms"] = int((time.perf_counter() - t0) * 1000)
    except Exception as e:
        result["singbox_error"] = str(e)
    # Запись во временную папку (там создаются конфиги для проверки)
    try:
        probe = os.path.join(tempfile.gettempdir(), "vpn_write_probe.tmp")
        with open(probe, "w") as f:
            f.write("ok")
        os.unlink(probe)
        result["temp_write"] = "ok"
    except Exception as e:
        result["temp_write"] = f"ERROR: {e}"
    return result

@app.post("/api/admin/set-interval")
async def set_interval(request: Request, username: str = Depends(get_current_user)):
    data = await request.json()
    try:
        minutes = int(data.get("minutes", 1))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Некорректный интервал")
    if minutes < 1:
        minutes = 1
    if minutes > 60:
        minutes = 60
    auto_update_settings["interval_seconds"] = minutes * 60
    return {"success": True, "interval_seconds": auto_update_settings["interval_seconds"]}

@app.post("/api/admin/toggle-auto-update")
async def toggle_auto_update(request: Request, username: str = Depends(get_current_user)):
    data = await request.json()
    auto_update_settings["enabled"] = data.get("enabled", False)
    return {"success": True, "enabled": auto_update_settings["enabled"]}

@app.get("/api/browse-singbox")
async def browse_singbox(username: str = Depends(get_current_user)):
    path = await asyncio.get_running_loop().run_in_executor(None, open_file_dialog)
    if not path:
        return {"success": False, "message": "Файл не выбран"}
    p = Path(path)
    if not p.exists():
        return {"success": False, "message": "Файл не существует"}
    if p.name.lower() != "sing-box.exe":
        return {"success": False, "message": f"Это не sing-box.exe (выбран {p.name})"}
    ok, version_line = run_singbox_version(p, timeout=5)
    if not ok:
        return {"success": False, "message": "Файл не является исполняемым sing-box"}
    save_singbox_path(path)
    return {"success": True, "path": path, "version": version_line}

@app.post("/api/set-singbox-path")
async def set_singbox_path(request: Request, username: str = Depends(get_current_user)):
    data = await request.json()
    path = data.get("path", "").strip()
    if not path:
        return {"success": False, "message": "Путь не указан"}
    p = Path(path)
    if not p.exists():
        return {"success": False, "message": "Файл не существует"}
    ok, version_line = run_singbox_version(p, timeout=5)
    if not ok:
        return {"success": False, "message": "Файл не является исполняемым sing-box"}
    save_singbox_path(path)
    return {"success": True, "path": path, "version": version_line}

@app.post("/api/clear-singbox-path")
async def clear_singbox_path(username: str = Depends(get_current_user)):
    config = load_config()
    config.pop("singbox_path", None)
    save_config(config)
    return {"success": True}

@app.post("/api/download-singbox")
async def download_singbox_endpoint(username: str = Depends(get_current_user)):
    async def event_generator():
        queue = asyncio.Queue()
        async def queue_progress(msg):
            await queue.put(msg)
        async def do_download():
            success, message = await _download_singbox(queue_progress)
            await queue.put(f"__DONE__:{json.dumps({'success': success, 'message': message})}")
        task = asyncio.create_task(do_download())
        while True:
            msg = await queue.get()
            if msg.startswith("__DONE__:"):
                result = json.loads(msg[9:])
                yield f"data: {json.dumps({'type': 'done', **result})}\n\n"
                break
            else:
                yield f"data: {json.dumps({'type': 'log', 'message': msg})}\n\n"
        await task
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/api/test-all")
async def test_all_configs(request: Request, username: str = Depends(get_current_user)):
    """Админский endpoint для ручного запуска проверки с SSE"""
    data = await request.json()
    check_type = data.get("type", "black")

    if check_type not in ("black", "white", "both"):
        raise HTTPException(status_code=400, detail="Неизвестный тип списка")
    source_id = data.get("source") or None
    if source_id is not None and source_id not in SOURCE_LABELS:
        raise HTTPException(status_code=400, detail="Неизвестный источник")

    async def event_generator():
        # Прогресс идёт через очередь: вложенная async-функция с yield
        # здесь не работала бы как callback (нужен был бы async-генератор).
        queue = asyncio.Queue()

        async def queue_callback(event):
            await queue.put(event)

        async def do_check():
            try:
                working, failed, total = await run_check_core(check_type, queue_callback, source_id)
                await queue.put({"type": "done", "working": working, "failed": failed, "total_checked": total})
            except Exception as e:
                await queue.put({"type": "error", "message": str(e)})

        task = asyncio.create_task(do_check())

        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/api/import-configs")
async def import_configs(request: Request, username: str = Depends(get_current_user)):
    """Разовая проверка вставленных вручную конфигураций.

    Нерабочие сразу забываются (в пул не попадают и циклично не проверяются).
    Рабочие добавляются в пул и живут по общему правилу — пока не станут недоступны.
    """
    data = await request.json()
    check_type = data.get("type", "black")
    if check_type not in ("black", "white"):
        raise HTTPException(status_code=400, detail="Неизвестный тип списка")
    text = data.get("text", "") or ""
    candidates = []
    seen_import = set()
    for line in text.splitlines():
        uri = line.strip()
        if uri.startswith(URI_PREFIXES) and uri not in seen_import:
            seen_import.add(uri)
            candidates.append(uri)
    if not candidates:
        return {"added": [], "failed": [], "total": 0, "duplicates": 0}
    singbox_path, _ = find_singbox()
    if not singbox_path or not singbox_path.exists():
        raise HTTPException(status_code=500, detail="sing-box не найден")
    async with CHECK_LOCK:
        existing = {c["uri"] for c in public_data[check_type].get("working", [])}
        fresh = [u for u in candidates if u not in existing]
        added, failed = [], []
        pending = iter(enumerate(fresh))

        async def import_worker(worker_id):
            for uri_index, uri in pending:
                is_ok, result = await probe_uri_standalone(
                    singbox_path, f"import_{worker_id}_{uri_index}", uri)
                if is_ok:
                    services = await probe_services(singbox_path, uri)
                    added.append({"uri": uri, "latency": result, "source": "manual",
                                  "services": services, "successful_checks": 1, "stable": False,
                                  "added_at": format_now()})
                else:
                    failed.append({"uri": uri, "error": result})

        tasks = [asyncio.create_task(import_worker(w)) for w in range(min(CHECK_CONCURRENCY, len(fresh)))]
        try:
            await asyncio.gather(*tasks)
        finally:
            await cancel_and_wait(tasks)
        if added:
            public_data[check_type]["working"].extend(added)
            public_data[check_type]["last_update"] = format_now()
            save_pool()
        print(f"📥 Ручной импорт ({check_type}): добавлено {len(added)}, отброшено {len(failed)}")
        return {"added": added, "failed": failed, "total": len(candidates),
                "duplicates": len(candidates) - len(fresh)}

@app.get("/api/public/status")
async def get_public_status():
    """Публичный endpoint - рабочие конфиги + статус обновления"""
    now = time.time()
    next_update_at = public_data["status"].get("next_update_at")
    seconds_until_next = None
    if next_update_at:
        seconds_until_next = max(0, int(next_update_at - now))

    return {
        "black": public_data["black"],
        "white": public_data["white"],
        "services": {"working": get_service_working()},
        "status": {
            "is_checking": public_data["status"]["is_checking"],
            "check_type": public_data["status"]["check_type"],
            "progress": public_data["status"]["progress"],
            "interval_seconds": auto_update_settings["interval_seconds"],
            "seconds_until_next": seconds_until_next,
            "auto_update_enabled": auto_update_settings["enabled"]
        }
    }

@app.get("/api/onekey")
async def onekey(request: Request, token: str = ""):
    """Один ключ с автопереключением: подписка из лучших серверов.

    Формат ответа: base64-список URI (универсальная подписка для v2rayNG,
    Nekoray, Streisand, Hiddify) — по умолчанию; sing-box JSON с urltest —
    при ?format=json или User-Agent sing-box/SFA/SFI/SFM/SFT/Hiddify;
    Clash/Mihomo YAML — при ?format=clash или User-Agent clash/mihomo.
    Защита: если задан VPN_ONEKEY_TOKEN, URL обязан содержать ?token=<значение>.
    """
    if ONEKEY_TOKEN and token != ONEKEY_TOKEN:
        raise HTTPException(status_code=403, detail="Неверный токен")
    fmt = _onekey_format(request)
    if fmt == "clash":
        text, error = build_onekey_clash()
        if error:
            raise HTTPException(status_code=503, detail=error)
        return Response(
            content=text,
            media_type="text/yaml; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="onekey-clash.yaml"',
                     "profile-title": "Where is my VPN",
                     "profile-update-interval": "1",
                     "Cache-Control": "no-store"}
        )
    if fmt == "json":
        config, error = build_onekey_config()
        if error:
            raise HTTPException(status_code=503, detail=error)
        return Response(
            content=json.dumps(config, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="onekey-singbox.json"',
                     "Cache-Control": "no-store"}
        )
    text, error = build_onekey_subscription()
    if error:
        raise HTTPException(status_code=503, detail=error)
    return Response(
        content=text,
        media_type="text/plain",
        headers={"profile-title": "Where is my VPN",
                 "profile-update-interval": "1",
                 "Cache-Control": "no-store"}
    )


@app.get("/api/public/events")
async def public_events(request: Request):
    """Первый пакет — снимок, затем только новые находки и текущий прогресс."""
    async def events():
        previous_lists = {}
        previous_services = None
        offsets = {"black": 0, "white": 0}
        last_payload = None
        last_sent = 0.0
        while not await request.is_disconnected():
            snapshot = await get_public_status()
            event = {"status": snapshot["status"]}
            for list_type in ("black", "white"):
                data = snapshot[list_type]
                working = data["working"]
                reset = previous_lists.get(list_type) is not working
                start = 0 if reset else offsets[list_type]
                event[list_type] = {
                    "reset": reset,
                    "working": working[start:],
                    "failed_count": data["failed_count"],
                    "last_update": data["last_update"]
                }
                previous_lists[list_type] = working
                offsets[list_type] = len(working)
            service_working = snapshot["services"]["working"]
            services_changed = service_working != previous_services
            event["services"] = {"reset": services_changed,
                                 "working": service_working if services_changed else []}
            previous_services = service_working
            # Опрос 4 раза/сек даёт до 3 дублей подряд (seconds_until_next
            # меняется раз в секунду): не гоняем одинаковые пакеты, но держим
            # heartbeat 15с, чтобы прокси не рвали висящий SSE.
            payload = f"data: {json.dumps(event)}\n\n"
            now_m = time.monotonic()
            if payload != last_payload or (now_m - last_sent) >= 15:
                yield payload
                last_payload = payload
                last_sent = now_m
            await asyncio.sleep(0.25)

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

# Простой in-memory rate-limit для QR (без новых зависимостей):
# генерация CPU-затратная (PIL), публичный endpoint иначе легко положить.
_QR_HITS: dict = {}
_QR_LIMIT = 60  # запросов
_QR_WINDOW = 60.0  # секунд

# Новый endpoint для генерации QR-кода
@app.get("/api/qr")
async def generate_qr(request: Request, uri: str):
    """Генерирует QR-код для URI конфигурации"""
    if len(uri) > 8000:
        return Response(content="Error: URI too long", status_code=400)
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    hits = _QR_HITS.get(client)
    if hits is None:
        hits = _QR_HITS[client] = []
    # Чистим окно и ограничиваем рост словаря.
    while hits and now - hits[0] > _QR_WINDOW:
        hits.pop(0)
    if len(_QR_HITS) > 1000:
        _QR_HITS.clear()
        hits = _QR_HITS.setdefault(client, [])
    if len(hits) >= _QR_LIMIT:
        return Response(content="Error: too many QR requests", status_code=429)
    hits.append(now)
    try:
        # Декодируем URI
        decoded_uri = uri

        # Создаём QR-код
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(decoded_uri)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")

        # Сохраняем в буфер
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)

        return Response(content=buffer.getvalue(), media_type="image/png")
    except Exception as e:
        return Response(content=f"Error: {str(e)}", status_code=500)

if __name__ == "__main__":
    import uvicorn

    # Проверяем, передан ли порт через аргумент командной строки
    if len(sys.argv) > 1:
        try:
            SERVER_PORT = int(sys.argv[1])
        except ValueError:
            print(f"Неверный порт: {sys.argv[1]}, используется {SERVER_PORT}")
    # APP_URL собран при импорте для порта по умолчанию — пересчитываем под итог.
    APP_URL = f"http://localhost:{SERVER_PORT}"

    singbox_path, source = find_singbox()
    print("=" * 60)
    print(" VPN Config Validator запускается...")
    if singbox_path:
        source_labels = {"user": "указан вручную", "system": "системный PATH", "local": "папка приложения", "nekoray": "Nekoray/Nekobox"}
        print(f"✅ sing-box найден ({source_labels.get(source, '')}): {singbox_path}")
    else:
        print("⚠️  sing-box НЕ найден")
    print(f"🌐 Публичная страница: http://localhost:{SERVER_PORT}")
    print(f" Админ-панель: http://localhost:{SERVER_PORT}{ADMIN_PATH}")
    print(f"   Логин: {ADMIN_USERNAME} / Пароль: {ADMIN_PASSWORD}")
    print("=" * 60)
    print(f"🌐 Подготовка к запуску. Веб-интерфейс будет доступен по адресу: {APP_URL}")

    # Запускаем открытие браузера в фоновом потоке ПЕРЕД стартом сервера
    threading.Thread(target=open_browser_delayed, daemon=True).start()

    # Запускаем сам сервер, используя те же переменные
    print("🚀 Запуск сервера...")
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
