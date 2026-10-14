"""Единая конфигурация приложения через переменные окружения.

Все настраиваемые значения собраны здесь с префиксом VPN_. Каждая переменная
имеет значение по умолчанию, равное прежнему хардкоду: локальный запуск
`python app.py` работает без .env-файла, а в Docker значения задаются через
docker-compose/.env. Полный список переменных с комментариями — в .env.example.
"""
import os
import sys

IS_WINDOWS = sys.platform == "win32"


def _env_str(name, default):
    """Строковая переменная окружения; отсутствующая или пустая = default."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_int(name, default):
    """Целочисленная переменная; некорректное значение = default с предупреждением."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        # Без эмодзи: импорт может идти до UTF-8-обёртки stdout в app.py,
        # и на Windows-консоли cp1251 эмодзи уронили бы UnicodeEncodeError.
        print(f"WARNING: {name}={raw!r} не является целым числом, использую {default}")
        return default


def _env_float(name, default):
    """Дробная переменная; некорректное значение = default с предупреждением."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        print(f"WARNING: {name}={raw!r} не является числом, использую {default}")
        return default


def _env_bool(name, default):
    """Булева переменная: 1/true/yes/on — истина; 0/false/no/off — ложь."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name, default):
    """Список строк через запятую; пустые элементы сохраняются (могут нести смысл)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return list(default)
    return [item.strip() for item in raw.split(",")]


# ============ СЕРВЕР ============
# Порт и адрес веб-сервера (порт также можно передать аргументом: python app.py 9000)
SERVER_PORT = _env_int("VPN_SERVER_PORT", 8079)
SERVER_HOST = _env_str("VPN_SERVER_HOST", "0.0.0.0")
# Путь админ-панели (сделайте менее очевидным для публичного сервера)
ADMIN_PATH = _env_str("VPN_ADMIN_PATH", "/vpn-control-panel-x7k9")
if not ADMIN_PATH.startswith("/"):
    ADMIN_PATH = "/" + ADMIN_PATH
# Учётные данные админ-панели
ADMIN_USERNAME = _env_str("VPN_ADMIN_USER", "admin")
ADMIN_PASSWORD = _env_str("VPN_ADMIN_PASSWORD", "vpn2026")
# Автооткрытие браузера имеет смысл только на рабочем столе; в контейнере выключено
OPEN_BROWSER = _env_bool("VPN_OPEN_BROWSER", IS_WINDOWS)

# ============ ПУТИ ============
# Каталог изменяемых данных (пул, конфиг, чекпоинты); пусто = каталог приложения
DATA_DIR = _env_str("VPN_DATA_DIR", "")
# Каталог с бинарником sing-box; пусто = ./sing-box-bin рядом с приложением
SINGBOX_DIR = _env_str("VPN_SINGBOX_DIR", "")

# ============ ПРОВЕРКА: ТАЙМАУТЫ И ЛИМИТЫ (prober) ============
# Основной URL проверки VPN-канала
TEST_URL = _env_str("VPN_TEST_URL", "http://cp.cloudflare.com")
# Резервный URL (если captive-portal Cloudflare недоступен из сети сервера)
TEST_URL_FALLBACK = _env_str("VPN_TEST_URL_FALLBACK", "http://connectivitycheck.gstatic.com/generate_204")
# Таймаут одного URL: полный проход и быстрый проход
CHECK_TIMEOUT = _env_float("VPN_CHECK_TIMEOUT", 6.0)
FAST_CHECK_TIMEOUT = _env_float("VPN_FAST_CHECK_TIMEOUT", 2.5)
# Параллельные sing-box процессы: поиск, проверка сервисов, перепроверка пула
CHECK_CONCURRENCY = _env_int("VPN_CHECK_CONCURRENCY", 16)
SERVICE_CONCURRENCY = _env_int("VPN_SERVICE_CONCURRENCY", 6)
POOL_CONCURRENCY = _env_int("VPN_POOL_CONCURRENCY", 3)
# Период перепроверки рабочих конфигов и повторного замера скорости, секунды
POOL_RECHECK_SECONDS = _env_int("VPN_POOL_RECHECK_SECONDS", 10 * 60)
SPEED_RECHECK_SECONDS = _env_int("VPN_SPEED_RECHECK_SECONDS", 3 * 60 * 60)
# Размер батча поиска
DISCOVERY_BATCH_SIZE = _env_int("VPN_DISCOVERY_BATCH_SIZE", 200)
# Объём тестовой загрузки при замере скорости, байт, и его таймаут
SPEED_BYTES = _env_int("VPN_SPEED_BYTES", 5 * 1024 * 1024)
SPEED_TIMEOUT = _env_int("VPN_SPEED_TIMEOUT", 15)
# Повторы полной проверки при таймауте (резервный URL уже даёт вторую попытку)
CHECK_RETRIES_ON_TIMEOUT = _env_int("VPN_CHECK_RETRIES_ON_TIMEOUT", 0)
# Таймаут проверки страницы сервиса (ChatGPT), секунды
SERVICE_TIMEOUT = _env_int("VPN_SERVICE_TIMEOUT", 10)
# Проверка QUIC/HTTP3 через туннель: hysteria2 и tuic работают поверх UDP,
# и ТСПУ часто режет именно UDP, оставляя TCP нетронутым. Пустая строка
# отключает проверку (тогда quic в записи пула не появляется).
QUIC_TEST_URL = _env_str("VPN_QUIC_TEST_URL", "https://cloudflare-quic.com/")
QUIC_TIMEOUT = _env_int("VPN_QUIC_TIMEOUT", 10)
# Геопроверка серверов через ip-api.com: определяет страну и ASN, чтобы
# помечать серверы с российским IP (для обхода блокировок они бесполезны).
# Выключается переменной VPN_GEOIP_ENABLED=0 — тогда внешних запросов нет.
GEOIP_ENABLED = _env_bool("VPN_GEOIP_ENABLED", True)

# ============ ПУЛ ============
# Успешных проверок подряд до статуса "стабильный"
STABLE_CHECKS = _env_int("VPN_STABLE_CHECKS", 11)

# ============ ОДИН КЛЮЧ (sing-box urltest) ============
# Максимум серверов в ключе с автопереключением
ONEKEY_LIMIT = _env_int("VPN_ONEKEY_LIMIT", 20)
# Порт mixed-инбаунда в генерируемом конфиге (локальный прокси клиента)
ONEKEY_MIXED_PORT = _env_int("VPN_ONEKEY_MIXED_PORT", 2080)
# Секретный токен /api/onekey; пусто = endpoint без токена (только для
# локального запуска; на публичном сервере обязательно задайте свой токен)
ONEKEY_TOKEN = _env_str("VPN_ONEKEY_TOKEN", "")

# ============ КАТАЛОГ ИСТОЧНИКОВ ============
# TTL суточного каталога, секунды
CATALOG_REFRESH_SECONDS = _env_int("VPN_CATALOG_REFRESH_SECONDS", 24 * 60 * 60)
# Параллельная загрузка файлов списков
CATALOG_DOWNLOAD_CONCURRENCY = _env_int("VPN_CATALOG_DOWNLOAD_CONCURRENCY", 4)
# Базовый URL каталога Goida VPN
GOIDA_BASE_URL = _env_str("VPN_GOIDA_BASE_URL", "https://raw.githack.com/AvenCores/goida-vpn-configs/main/githubmirror/")

# ============ SING-BOX ============
# Версия sing-box для авто-скачивания
SINGBOX_VERSION = _env_str("VPN_SINGBOX_VERSION", "1.10.3")
# Зеркала GitHub через запятую; ПУСТОЙ элемент списка = официальный GitHub
GITHUB_MIRRORS = _env_list("VPN_GITHUB_MIRRORS",
                           ["https://ghfast.top/", "https://mirror.ghproxy.com/",
                            "https://gh-proxy.com/", ""])
