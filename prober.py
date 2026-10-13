"""Проверка VPN-конфигураций через изолированные процессы sing-box.

probe_uri_standalone: две независимые пробы через `sing-box tools fetch`
(SOCKS-инбаунд, порт выделяет ОС). probe_services: долгоживущий sing-box
с HTTP-инбаундом для проверки страницы ChatGPT (curl_cffi с TLS-профилем
Chrome, чтобы избежать ложных 403 от Cloudflare) и замера скорости.
"""
import asyncio
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime

from config_parser import parse_uri_to_singbox_outbound
from config import (TEST_URL, TEST_URL_FALLBACK, CHECK_TIMEOUT, FAST_CHECK_TIMEOUT,
                    CHECK_CONCURRENCY, SERVICE_CONCURRENCY, POOL_CONCURRENCY,
                    POOL_RECHECK_SECONDS, SPEED_RECHECK_SECONDS, DISCOVERY_BATCH_SIZE,
                    SPEED_BYTES, SPEED_TIMEOUT, CHECK_RETRIES_ON_TIMEOUT, SERVICE_TIMEOUT)

# ============ ТАЙМАУТЫ И ЛИМИТЫ ПРОВЕРКИ ============
# Все значения читаются из config.py (переменные окружения VPN_*).
SPEED_URL = f"https://speed.cloudflare.com/__down?bytes={SPEED_BYTES}"
# Замер качает 5 МиБ через чужой VPN: полная параллельность положила бы канал,
# а строгий Lock(1) при десятках стабильных растягивал recheck на десятки минут.
# Компромисс: до 2 параллельных замеров.
SPEED_LOCK = asyncio.Semaphore(2)

# Проверка сервисов через туннель: профиль блокировок, а не один флаг.
# gpt — исторический таргет и обязательный (REQUIRED_SERVICES): именно он
# определяет секцию «Сервисы» на публичной странице. Остальные таргеты
# добавляют к записи пула профиль доступности (что ещё открывается через
# этот сервер) и НЕ сужают выдачу: сервер без YouTube остаётся рабочим.
SERVICE_TARGETS = {
    "gpt": ("https://chatgpt.com/", (b"chatgpt", b"openai")),
    "yt": ("https://www.youtube.com/", (b"youtube",)),
    "tg": ("https://web.telegram.org/", (b"telegram",)),
    "ig": ("https://www.instagram.com/", (b"instagram",)),
}

# Подмножество SERVICE_TARGETS, обязательное для секции «Сервисы».
# Расширение SERVICE_TARGETS не должно опустошать таблицу: сервер, у которого
# открыт ChatGPT, но закрыт Instagram, остаётся пригодным для работы.
REQUIRED_SERVICES = ("gpt",)

SERVICE_BODY_LIMIT = 512 * 1024

from curl_cffi import requests as browser_requests  # noqa: E402


def _creation_flags():
    """CREATE_NO_WINDOW на Windows, чтобы процессы sing-box не мигали окнами."""
    return subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0


def _classify_error(err):
    """Грубая категоризация ошибки проверки для диагностики (сервер vs ПК)."""
    e = (err or "").lower()
    if "тайм-аут" in e or "timeout" in e or "timed out" in e or "deadline" in e:
        return "timeout"
    if "dns" in e or "no such host" in e or "name resolution" in e or "getaddrinfo" in e:
        return "dns"
    if "tls" in e or "handshake" in e or "certificate" in e or "reality" in e:
        return "tls/handshake"
    if "refused" in e or "unreachable" in e or "reset by peer" in e or "network is unreachable" in e:
        return "connection_refused/unreachable"
    # Перехват DPI идёт раньше общей ветки "blocked": для статистики важно
    # отличать подмену ответа от прочих блокировок.
    if "перехват" in e or "заглушк" in e:
        return "intercepted"
    if "blocked" in e or "forbidden" in e or "filter" in e:
        return "blocked"
    if "не удалось распарсить" in e or "не поддерживается" in e:
        return "unparsable"
    return "other"


# Признаки подмены ответа DPI/РКН: тестовый адрес обязан отвечать пустым телом
# (generate_204), поэтому ЛЮБОЕ непустое тело — уже аномалия. Маркеры ниже
# отделяют осмысленную заглушку от прочего мусора, чтобы в отчёте было видно,
# что именно вернул провайдер.
INTERCEPTION_MARKERS = (
    b"blocked", b"access denied", b"zapret", b"roskomnadzor", b"rkn.gov.ru",
    b"captive", b"<title>403", b"<title>451", b"warning.rt.ru", b"blocklist",
    b"unavailable for legal reasons", b"restricted", b"not available in your country",
    b"blackhole", b"connection reset by peer",
)
# Сколько байт тела читаем для анализа: заглушки короткие, полные страницы
# ошибок читать целиком незачем.
_INTERCEPTION_BODY_LIMIT = 8192


def _detect_interception(body):
    """Определяет подмену ответа тестового адреса (DPI/РКН/captive portal).

    Возвращает короткое описание вида "перехват: <маркер>" либо None, если
    явных признаков заглушки нет. Вызывается только когда тело непустое:
    корректный ответ generate_204 всегда пуст.
    """
    sample = (body or b"")[:_INTERCEPTION_BODY_LIMIT].lower()
    if not sample:
        return None
    for marker in INTERCEPTION_MARKERS:
        if marker in sample:
            return f"перехват ответа: обнаружена заглушка ({marker.decode('ascii', 'replace')})"
    # Непустое тело без известных маркеров: либо HTML-страница провайдера,
    # либо captive portal. Пробуем достать <title> для понятного отчёта.
    if sample.lstrip().startswith((b"<!doctype html", b"<html")):
        title = None
        start = sample.find(b"<title>")
        if start != -1:
            end = sample.find(b"</title>", start)
            if end != -1:
                title = sample[start + 7:end].strip()[:80].decode("utf-8", "replace")
        return f"перехват ответа: HTML-страница вместо пустого тела" + (f" ({title})" if title else "")
    return None


def _allocate_local_port():
    """Порт выделяет ОС: bind(0) почти всегда успешен; None при экзотической нехватке."""
    for _ in range(3):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                return sock.getsockname()[1]
        except OSError:
            continue
    return None


def _build_probe_config(outbound_config, unique_port):
    """Конфиг sing-box для одноразовой проверки: SOCKS-инбаунд + публичный DNS.

    Явные публичные DNS вместо системного резолвера. Без этого sing-box
    резолвит тестовый домен через системный DNS хоста: на обычном ПК
    это быстрый DNS роутера/провайдера, а на Windows Server с ролью
    DNS-сервера — локальный 127.0.0.1 с рекурсией/форвардерами, который
    часто медленный или фильтрует. Итог: на сервере пачка ложных
    "таймаутов", хотя сам VPN рабочий. DNS-трафик по-прежнему идёт
    напрямую (direct), но уже к 8.8.8.8 / 1.1.1.1, а не к локальному DNS.
    """
    return {
        "log": {"level": "error"},
        "dns": {
            "servers": [
                {"tag": "dns-pub1", "address": "8.8.8.8"},
                {"tag": "dns-pub2", "address": "1.1.1.1"}
            ],
            "final": "dns-pub1",
            "strategy": "prefer_ipv4"
        },
        "inbounds": [{"type": "socks", "listen": "127.0.0.1", "listen_port": unique_port}],
        "outbounds": [outbound_config, {"type": "direct", "tag": "direct"}],
        "route": {"rules": [{"protocol": "dns", "outbound": "direct"}]}
    }


async def _probe_outbound(singbox_path, tag, uri, outbound_config, fast=False):
    """Проверяет один outbound через sing-box. Возвращает (tag, uri, is_ok, result).

    Основной и резервный URL имеют отдельные таймауты. Полный цикл
    по умолчанию не повторяется; задержка считается для успешного URL.
    """
    last_error = "failed"
    last_was_timeout = False
    attempts = 1 + CHECK_RETRIES_ON_TIMEOUT
    timeout = FAST_CHECK_TIMEOUT if fast else CHECK_TIMEOUT
    # +2 итерации резервируем только под TOCTOU-конфликт локального порта;
    # таймауты/прочие ошибки выходят раньше через return.
    for attempt in range(attempts + 2):
        unique_port = _allocate_local_port()
        if unique_port is None:
            last_error = "Не удалось выделить локальный порт"
            last_was_timeout = False
            continue
        process = None
        config_path = None

        singbox_config = _build_probe_config(outbound_config, unique_port)

        try:
            temp_dir = tempfile.gettempdir()
            config_path = os.path.join(temp_dir, f"singbox_test_{os.getpid()}_{tag}_{unique_port}.json")

            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(singbox_config, f)

            # Пробуем основной URL, при неуспехе — резервный (частый случай на
            # сервере: cp.cloudflare.com не резолвится/режется серверным DNS).
            last_err_this_attempt = "failed"
            timeout_this_attempt = False
            port_conflict = False
            for test_url in ((TEST_URL,) if fast else (TEST_URL, TEST_URL_FALLBACK)):
                start_time = time.perf_counter()
                cmd = [str(singbox_path), "tools", "fetch", "-c", config_path, test_url]
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=_creation_flags()
                )
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await process.communicate()
                    except Exception:
                        pass
                    process = None
                    last_err_this_attempt = f"Тайм-аут проверки ({timeout:g} секунд)"
                    timeout_this_attempt = True
                    continue  # Резервный адрес — последняя попытка, без повтора цикла.
                end_time = time.perf_counter()
                latency_ms = int((end_time - start_time) * 1000)

                if process.returncode == 0:
                    # Оба адреса должны отвечать пустым телом (generate_204).
                    # tools fetch 1.10.3 не проверяет HTTP-статус: код 0
                    # означает лишь скачивание тела, включая страницы блокировки.
                    if not stdout.strip():
                        return tag, uri, True, latency_ms
                    # Непустое тело: отличаем подмену ответа DPI от прочего
                    # мусора. Заглушка — детерминированный признак, что канал
                    # перехвачен, поэтому резервный URL уже не поможет.
                    interception = _detect_interception(stdout)
                    if interception:
                        return tag, uri, False, interception
                    last_err_this_attempt = "Неожиданный ответ тестового адреса (ожидалось пустое тело)"
                    process = None
                    continue
                err = stderr.decode('utf-8', errors='ignore').strip()
                last_err_this_attempt = err if err else "failed"
                process = None
                # TOCTOU: порт, выданный ОС через bind(0), могли занять между
                # close() и стартом sing-box. Это не вина VPN — повторяем с новым.
                if "already in use" in last_err_this_attempt.lower() or "address in use" in last_err_this_attempt.lower():
                    port_conflict = True
                    break
                if _classify_error(last_err_this_attempt) in ("timeout", "dns"):
                    if _classify_error(last_err_this_attempt) == "timeout":
                        timeout_this_attempt = True
                    continue
                # Ошибка протокола/авторизации одинакова для обоих URL — второй не пробуем
                break
            # TOCTOU-конфликт порта — не вина VPN: повторяем с новым портом
            # (до 2 доп. попыток сверх attempts, именно для этого случая).
            if port_conflict:
                last_error = f"{last_err_this_attempt} (локальный порт занят, повтор с новым портом)"
                last_was_timeout = False
                if attempt < attempts - 1 + 2:
                    continue
                return tag, uri, False, last_error
            last_error = last_err_this_attempt
            last_was_timeout = timeout_this_attempt or _classify_error(str(last_error)) == "timeout"
            # Повторяем только таймаут (транзиентная перегрузка сервера); остальные
            # ошибки детерминированы и повтор не поможет, только замедлит проверку.
            if not last_was_timeout:
                return tag, uri, False, last_error
            if attempt >= attempts - 1:
                # Лимит ретраев по таймауту исчерпан (резервные итерации —
                # только для конфликта порта).
                return tag, uri, False, last_error
        except asyncio.TimeoutError:
            last_error = f"Тайм-аут проверки ({CHECK_TIMEOUT:g} секунд)"
            last_was_timeout = True
            break
        except Exception as e:
            print(f"❌ Исключение в test_single_outbound: {e}")
            return tag, uri, False, str(e)
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await process.communicate()
                except Exception:
                    pass
            if config_path and os.path.exists(config_path):
                try:
                    os.unlink(config_path)
                except Exception:
                    pass
    return tag, uri, False, last_error


async def probe_uri_standalone(singbox_path, tag, uri, fast=False, probe=None):
    """Две успешные независимые пробы перед попаданием URI в рабочий пул.

    probe — заменяемая функция одной пробы (по умолчанию _probe_outbound);
    параметр позволяет вызывающему коду подставить свою реализацию.
    """
    if probe is None:
        probe = _probe_outbound
    outbound = parse_uri_to_singbox_outbound(uri, tag)
    if not outbound:
        return False, "Не удалось распарсить"
    latencies = []
    for pass_number in (1, 2):
        _, _, is_ok, result = await probe(
            singbox_path, f"{tag}_pass{pass_number}", uri, outbound, fast=fast)
        if not is_ok:
            return False, f"Проверка {pass_number}/2: {result}"
        latencies.append(result)
    return True, max(latencies)


def check_service_page(proxy_port, url, markers):
    """HTTPS с TLS-профилем Chrome через явный CONNECT-прокси."""
    started = time.monotonic()
    try:
        # urllib получает ложный 403 от Cloudflare даже с браузерным User-Agent:
        # важен также TLS/HTTP-профиль. Окружение не должно обходить выбранный VPN.
        with browser_requests.Session(trust_env=False) as session:
            response = session.get(
                url, proxy=f"http://127.0.0.1:{proxy_port}", impersonate="chrome",
                timeout=SERVICE_TIMEOUT, allow_redirects=True, stream=True)
            try:
                if response.status_code != 200:
                    return {"ok": False, "error": f"ChatGPT вернул HTTP {response.status_code}; доступ к странице не подтверждён"}
                if "text/html" not in response.headers.get("Content-Type", "").lower():
                    return {"ok": False, "error": "Ожидалась HTML-страница ChatGPT"}
                body = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    if time.monotonic() - started > SERVICE_TIMEOUT:
                        raise TimeoutError("Превышено время проверки сервиса")
                    body.extend(chunk[:SERVICE_BODY_LIMIT - len(body)])
                    if len(body) >= SERVICE_BODY_LIMIT:
                        break
            finally:
                response.close()
            content = bytes(body).lower()
            blocked = any(marker in content for marker in (
                b"cf-chl-", b"just a moment", b"unusual traffic", b"unsupported_country",
                b"access denied", b"not available in your country"))
            ok = not blocked and any(marker in content for marker in markers)
            if not ok:
                return {"ok": False, "error": "Страница сервиса не подтверждена или требуется антибот-проверка"}
            return {"ok": True, "latency": int((time.monotonic() - started) * 1000)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:250]}


def measure_download_speed(proxy_port, speed_bytes=None):
    """Короткий HTTPS-замер через VPN; время включает установку соединения.

    speed_bytes — объём тестовой загрузки (по умолчанию модульный SPEED_BYTES);
    параметр позволяет вызывающему коду подставить своё значение.
    """
    if speed_bytes is None:
        speed_bytes = SPEED_BYTES
    result = {"checked_at": time.time(), "mbps": None, "bytes": 0}
    started = time.monotonic()
    try:
        with browser_requests.Session(trust_env=False) as session:
            response = session.get(SPEED_URL, proxy=f"http://127.0.0.1:{proxy_port}",
                                   impersonate="chrome", timeout=SPEED_TIMEOUT,
                                   allow_redirects=False, stream=True,
                                   headers={"Accept-Encoding": "identity", "Cache-Control": "no-cache"})
            try:
                if response.status_code != 200:
                    raise ValueError(f"HTTP {response.status_code}")
                if "application/octet-stream" not in response.headers.get("Content-Type", "").lower():
                    raise ValueError("Тестовый сервер не вернул бинарные данные")
                for chunk in response.iter_content(chunk_size=65536):
                    if time.monotonic() - started > SPEED_TIMEOUT:
                        raise TimeoutError("Истекло время замера скорости")
                    result["bytes"] += len(chunk)
                    if result["bytes"] >= speed_bytes:
                        break
                if result["bytes"] != speed_bytes:
                    raise ValueError("Неполная или некорректная загрузка тестового файла")
            finally:
                response.close()
        elapsed = max(time.monotonic() - started, 0.001)
        result.update(mbps=round(result["bytes"] * 8 / elapsed / 1_000_000, 2), seconds=round(elapsed, 3))
    except Exception as exc:
        result["error"] = str(exc)[:250]
    return result


async def probe_services(singbox_path, uri, measure_speed=False, speed_only=False):
    """Один изолированный sing-box на URI, затем HTTPS-проверка ChatGPT."""
    process = None
    config_path = None
    results = {}
    speed = None
    try:
        outbound = parse_uri_to_singbox_outbound(uri, "service-proxy")
        if not outbound:
            raise ValueError("Не удалось распарсить конфигурацию")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config = {
            "log": {"level": "error"},
            "dns": {"servers": [{"tag": "public1", "address": "8.8.8.8"},
                                {"tag": "public2", "address": "1.1.1.1"}],
                    "final": "public1", "strategy": "prefer_ipv4"},
            "inbounds": [{"type": "http", "listen": "127.0.0.1", "listen_port": port}],
            "outbounds": [outbound],
            "route": {"final": "service-proxy"},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", prefix="vpn_services_",
                                         encoding="utf-8", delete=False) as file:
            config_path = file.name
            json.dump(config, file)
        process = await asyncio.create_subprocess_exec(
            str(singbox_path), "run", "-c", config_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            creationflags=_creation_flags())
        deadline = time.monotonic() + 5
        while True:
            if process.returncode is not None:
                raise RuntimeError("sing-box завершился до проверки сервисов")
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 0.25)
                writer.close()
                await writer.wait_closed()
                break
            except (OSError, asyncio.TimeoutError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("sing-box не запустил прокси за 5 секунд")
                await asyncio.sleep(0.1)
        # Таргеты проверяются параллельно: последовательный обход при четырёх
        # сервисах растянул бы проверку одного сервера до 4*SERVICE_TIMEOUT.
        if not speed_only:
            probes = {name: asyncio.create_task(
                asyncio.to_thread(check_service_page, port, url, markers))
                for name, (url, markers) in SERVICE_TARGETS.items()}
            try:
                for name, task in probes.items():
                    results[name] = await asyncio.shield(task)
            except asyncio.CancelledError:
                # Дожидаемся всех потоков до остановки sing-box: иначе они
                # обратятся к уже закрытому прокси-порту.
                await asyncio.gather(*probes.values(), return_exceptions=True)
                raise
        if measure_speed:
            async with SPEED_LOCK:
                task = asyncio.create_task(asyncio.to_thread(measure_download_speed, port))
                try:
                    speed = await asyncio.shield(task)
                except asyncio.CancelledError:
                    # Поток должен завершиться до освобождения лимита и остановки прокси.
                    await task
                    raise
    except Exception as exc:
        for name in SERVICE_TARGETS:
            results.setdefault(name, {"ok": False, "error": str(exc)[:250]})
    finally:
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
        if config_path:
            try:
                os.unlink(config_path)
            except OSError:
                pass
    report = {"checked_at": datetime.now().strftime("%d.%m.%Y %H:%M:%S"), "results": results}
    if measure_speed:
        report["speed"] = speed or {"checked_at": time.time(), "mbps": None, "error": "Не удалось запустить замер"}
    return report
