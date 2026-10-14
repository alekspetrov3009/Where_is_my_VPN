# 🌐 VPN Config Validator (Where is my VPN)

Репозиторий: <https://github.com/alekspetrov3009/Where_is_my_VPN>

Инструмент для автоматизированной проверки актуальности и работоспособности конфигураций VPN (VLESS, Trojan, Shadowsocks, VMess, Hysteria2, TUIC) из публичных списков. Приложение скачивает конфигурации, проверяет их через локальный движок `sing-box` и предоставляет удобный веб-интерфейс с актуальными рабочими ссылками и QR-кодами.

## 🚀 Ключевые возможности

- **Автоматическое обновление:** Фоновая проверка списков с настраиваемым интервалом (по умолчанию каждые 60 секунд).
- **Динамическая загрузка:** Автоматический поиск `.txt` файлов с конфигурациями в целевом GitHub-репозитории и их категоризация (⚫ Чёрный список / ⚪ Белый список).
- **Параллельная проверка:** Многопоточное тестирование конфигураций через `sing-box` с умным распределением свободных портов (исключает конфликты `address already in use`).
- **Портативность:** Полностью автономный дистрибутив (`Where is my VPN.exe`), не требующий установки Python и не оставляющий следов в реестре или `AppData`.
- **Один ключ с автопереключением:** Endpoint [`/api/onekey`](app.py) отдаёт подписку из лучших стабильных серверов. Форматы: base64-список URI (универсальный для v2rayNG, Nekoray, Streisand, Hiddify) — по умолчанию; sing-box JSON с `urltest` — при `?format=json` или User-Agent sing-box/SFA/SFI/SFM/SFT/Hiddify; **Clash/Mihomo YAML** — при `?format=clash` или User-Agent clash/mihomo (Clash Verge, FlClash, Stash). Кнопка «🔑 Один ключ» на публичной странице даёт ссылки и QR-код.
- **Веб-интерфейс:**
  - Публичная страница со статусом, рабочими конфигурациями и генерацией QR-кодов.
  - Защищённая админ-панель для управления интервалами, путём к `sing-box` и ручного запуска проверок.
- **Устойчивость к сетевым ограничениям:** Встроенный обход проблем с SSL-сертификатами на корпоративных серверах и использование зеркал GitHub для скачивания.
- **Проверка на перехват DPI:** Тестовый адрес обязан отвечать пустым телом (`generate_204`). Любое непустое тело анализируется на признаки заглушки (РКН, captive portal, страница блокировки) — такой сервер помечается как «перехват», а не как рабочий. Это убирает главный источник ложноположительных результатов.
- **Профиль блокировок по сервисам:** Каждый сервер проверяется на доступ к ChatGPT, YouTube, Telegram и Instagram. Секция «Сервисы» на публичной странице по-прежнему требует только ChatGPT, поэтому расширенный профиль не сужает выдачу, а показывает, что ещё открывается через данный сервер.
- **Поддержка обфускации:** Hysteria2 с `obfs=salamander` и TUIC (`congestion_control`, `udp_relay_mode`, `alpn`) разбираются полностью — без этих параметров рабочие серверы ложно попадали в «нерабочие».
- **Проверка QUIC/UDP:** Отдельный зонд подтверждает, что через сервер реально проходит HTTP/3. Hysteria2 и TUIC работают поверх QUIC, а ТСПУ часто режет именно UDP, оставляя TCP нетронутым: сервер с отличным TCP-пингом может не пропускать реальный трафик. Откат на TCP (`http_version=2`) успехом не считается.
- **Определение серверов в РФ:** По IP определяется страна и ASN (ip-api.com). Серверы с российским IP помечаются флагом `is_ru` — для обхода блокировок они бесполезны, так как трафик не выходит за периметр ТСПУ. Особенно актуально для белого списка (`WHITE-CIDR-RU`), который целиком состоит из таких серверов.
- **Гибкая конфигурация:** Все настраиваемые значения вынесены в переменные окружения (`VPN_*`, см. [.env.example](.env.example)) — без правок кода.
- **Docker:** Готовый образ с healthcheck и volume для данных — разворачивается одной командой.

---

## 📂 Структура проекта

```text
2_Where_is_my_VPN\
│
├── app.py                          # Основной код приложения (FastAPI + логика проверки)
├── config.py                       # Все настройки через переменные окружения VPN_*
├── paths.py                        # Разрешение путей (exe / Python / Docker)
├── storage.py                      # Персистентность: пул, конфиг, чекпоинты
├── catalog.py                      # Каталог источников списков (GitHub + fallback)
├── prober.py                       # Проверка конфигов через sing-box
├── config_parser.py                # Парсеры URI (vless/trojan/ss/vmess/hysteria2+obfs/tuic)
├── check_scheduler.py              # Математика пулов проверки
├── singbox_manager.py              # Поиск/валидация/скачивание sing-box
├── clash_export.py                 # Экспорт пула в Clash/Mihomo YAML
├── geoip.py                        # Страна и ASN сервера по IP (ip-api.com)
│
├── test_check_scheduling.py        # Тесты планировщика пулов
├── test_validation.py              # Тесты допуска в пул (6 предсуществующих падений)
├── test_dpi_hardening.py           # Тесты парсеров, детектора перехвата и схем sing-box
├── test_export_geo.py              # Тесты Clash YAML, QUIC-зонда и геопроверки
│
├── index.html                      # Фронтенд: Публичная страница для пользователей
├── admin.html                      # Фронтенд: Интерфейс панели администратора
│
├── sing-box-bin\                   # Папка с исполняемым файлом движка проверки
│   └── sing-box.exe                # Ядро для тестирования VPN-соединений (v1.10.3, Windows)
│
├── requirements.txt                # Python-зависимости (зафиксированные версии)
├── .env.example                    # Шаблон переменных окружения с комментариями
│
├── Dockerfile                      # Образ (python:3.13-slim + sing-box linux-amd64)
├── docker-compose.yml              # Сервис с volume, healthcheck, env_file
├── .dockerignore                   # Исключения из контекста сборки
├── DOCKER.md                       # Подробная инструкция по Docker
│
├── icon.svg                        # Исходный векторный файл иконки (глобус)
├── icon.ico                        # Иконка в формате Windows для встраивания в .exe
│
├── Инструкция по сборке дистрибутива.txt # Пошаговое руководство по компиляции
└── README.md                       # Этот файл
```

---

## 🖥️ Локальный запуск на компьютере (Windows)

### 1. Установить зависимости

Нужен Python 3.13+. Из корня проекта:

```bat
pip install -r requirements.txt
```

В [requirements.txt](requirements.txt): `fastapi`, `uvicorn`, `qrcode`, `pillow`, `curl_cffi`. `tkinter` входит в стандартную поставку Python (нужен только для диалога выбора sing-box.exe в админке).

### 2. Запустить сервер

```bat
python app.py
```

Или с другим портом:

```bat
python app.py 9000
```

Порт также задаётся переменной `VPN_SERVER_PORT` (аргумент командной строки имеет приоритет).

### 3. Открыть в браузере

- **Публичная страница:** <http://localhost:8079>
- **Админ-панель:** <http://localhost:8079/vpn-control-panel-x7k9>
  - Логин: `admin`
  - Пароль: `vpn2026`

Креды переопределяются переменными `VPN_ADMIN_USER` / `VPN_ADMIN_PASSWORD` (см. [config.py](config.py)).

### Что происходит при старте

1. Приложение ищет sing-box: сохранённый путь → системный PATH → `sing-box-bin\sing-box.exe` (в проекте уже лежит v1.10.3, скачивать ничего не нужно).
2. Загружается сохранённый пул из `.vpn_pool.json`.
3. Стартует фоновая авто-проверка (по умолчанию каждые 60 секунд) и открывается браузер (`VPN_OPEN_BROWSER=1` по умолчанию на Windows).

### Настройка через переменные окружения (необязательно)

Скопируйте шаблон и отредактируйте:

```bat
copy .env.example .env
```

Затем задайте переменные перед запуском, например:

```bat
set VPN_ADMIN_PASSWORD=мой-пароль
set VPN_SERVER_PORT=9000
python app.py
```

Полный список переменных с комментариями — в [.env.example](.env.example). Все они необязательны: без них используются значения по умолчанию, идентичные прежнему поведению.

### Проверка работоспособности

```bat
python -m unittest test_check_scheduling test_validation test_dpi_hardening test_export_geo
```

6 падений в `test_validation.py` — предсуществующие (описаны в [AGENTS.md](AGENTS.md)), на работу приложения не влияют. `test_dpi_hardening.py` проверяет парсеры hysteria2/tuic, детектор перехвата и схему конфигов через настоящий `sing-box check`. `test_export_geo.py` — генерацию Clash YAML (разбирается PyYAML, если установлен), QUIC-зонд и геопроверку с подставным API. Оба новых файла не требуют сети.

---

## 🐳 Запуск в Docker-контейнере

Подробная инструкция (volume, логи, диагностика, обновление, запуск без compose) — в [DOCKER.md](DOCKER.md). Ниже — краткий путь.

### Код берётся из GitHub при каждой сборке

[docker-compose.yml](docker-compose.yml) указывает репозиторий
<https://github.com/alekspetrov3009/Where_is_my_VPN> как контекст сборки:
при каждом `docker compose build` Docker клонирует свежую `main`-ветку.
Локальная копия исходников не нужна — достаточно `docker-compose.yml` и `.env`.
Закрепить версию можно фрагментом URL: `...Where_is_my_VPN.git#v1.0.0`;
для локальной разработки замените `build:` на `build: .`

### Быстрый старт без локальной копии проекта

Требуется Docker Engine 20.10+ / Docker Desktop. Скачиваются только файлы
развёртывания — код приложения при сборке придёт из GitHub.

Linux / Proxmox LXC / VM:

```bash
mkdir where-is-my-vpn && cd where-is-my-vpn
curl -LO https://raw.githubusercontent.com/alekspetrov3009/Where_is_my_VPN/main/docker-compose.yml
curl -LO https://raw.githubusercontent.com/alekspetrov3009/Where_is_my_VPN/main/.env.example
mv .env.example .env
nano .env   # обязательно смените VPN_ADMIN_PASSWORD
docker compose up -d --build
```

Windows (cmd):

```bat
mkdir where-is-my-vpn && cd where-is-my-vpn
curl -LO https://raw.githubusercontent.com/alekspetrov3009/Where_is_my_VPN/main/docker-compose.yml
curl -LO https://raw.githubusercontent.com/alekspetrov3009/Where_is_my_VPN/main/.env.example
ren .env.example .env
docker compose up -d --build
```

Проверка: `docker compose ps` и `docker compose logs -f`. После старта:

- Публичная страница: <http://localhost:8079>
- Админ-панель: <http://localhost:8079/vpn-control-panel-x7k9>
  (логин/пароль — `VPN_ADMIN_USER` / `VPN_ADMIN_PASSWORD` из `.env`, по умолчанию `admin` / `vpn2026`)

### Быстрый старт из локальной копии проекта

```bat
cd 2_Where_is_my_VPN
copy .env.example .env
docker compose up -d --build
```

Сборка всё равно пойдёт из GitHub (так настроен контекст в
[docker-compose.yml](docker-compose.yml)); чтобы собрать из локальных файлов —
замените строку `build:` на `build: .`

### Особенности контейнера

- **Код из GitHub** — пересборка (`docker compose up -d --build`) всегда подтягивает актуальную версию проекта.
- **sing-box linux-amd64** ставится на этапе сборки: [Dockerfile](Dockerfile) скачивает версию 1.10.3 (с зеркалами) и валидирует её запуском `sing-box version`. Windows-бинарник из репозитория в образ не попадает ([.dockerignore](.dockerignore)).
- **Данные** (пул, конфиг, чекпоинты) хранятся в volume `vpn-data` → `/data` и переживают пересоздание контейнера.
- **Healthcheck** опрашивает `/api/public/status` каждые 30 секунд.
- **Браузер не открывается** (`VPN_OPEN_BROWSER=0` в контейнере).
- **Логи** — `print()` в stdout, штатный механизм проекта: `docker compose logs -f`.

### Обновление до актуальной версии

```bash
docker compose up -d --build
```

При каждой сборке Docker заново клонирует `main`-ветку, поэтому пересборка
всегда подтягивает свежий код. Пул в volume сохраняется между пересозданиями.
Заодно обновить базовый образ: `docker compose build --pull`.

### Запуск без docker compose

```bash
docker build -t where-is-my-vpn https://github.com/alekspetrov3009/Where_is_my_VPN.git
docker run -d --name where-is-my-vpn \
  -p 8079:8079 \
  -e VPN_ADMIN_PASSWORD=мой-пароль \
  -v vpn-data:/data \
  where-is-my-vpn
```

`docker build` принимает git-URL как контекст: код клонируется из репозитория,
локальные файлы не нужны. Для сборки из локальной копии замените URL на `.`

---

## ⚙️ Основные переменные окружения

Полный список с комментариями — в [.env.example](.env.example). Приоритет: переменная окружения → значение по умолчанию из [config.py](config.py).

| Переменная | По умолчанию | Описание |
|-----------|--------------|----------|
| `VPN_SERVER_PORT` | `8079` | Порт веб-сервера |
| `VPN_SERVER_HOST` | `0.0.0.0` | Адрес привязки |
| `VPN_ADMIN_PATH` | `/vpn-control-panel-x7k9` | Путь админ-панели |
| `VPN_ADMIN_USER` | `admin` | Логин админки |
| `VPN_ADMIN_PASSWORD` | `vpn2026` | Пароль админки — **смените на публичном сервере** |
| `VPN_OPEN_BROWSER` | `1` на Windows | Открывать браузер при старте |
| `VPN_DATA_DIR` | каталог приложения | Каталог пула, конфига и чекпоинтов (в Docker — `/data`) |
| `VPN_SINGBOX_DIR` | `./sing-box-bin` | Каталог бинарника sing-box |
| `VPN_CHECK_CONCURRENCY` | `16` | Параллельных sing-box процессов при поиске |
| `VPN_CHECK_TIMEOUT` | `6.0` | Таймаут проверки одного URL, сек |
| `VPN_STABLE_CHECKS` | `11` | Успешных проверок до статуса «стабильный» |
| `VPN_SINGBOX_VERSION` | `1.10.3` | Версия sing-box для авто-скачивания |
| `VPN_GITHUB_MIRRORS` | 3 зеркала + GitHub | Зеркала загрузки sing-box через запятую |
| `VPN_ONEKEY_LIMIT` | `20` | Максимум серверов в ключе с автопереключением (`/api/onekey`) |
| `VPN_ONEKEY_MIXED_PORT` | `2080` | Порт mixed-инбаунда в генерируемом конфиге |
| `VPN_ONEKEY_TOKEN` | пусто | Секретный токен `/api/onekey` — **задайте на публичном сервере** |
| `VPN_QUIC_TEST_URL` | `https://cloudflare-quic.com/` | Адрес для проверки QUIC/HTTP3; пусто — отключить |
| `VPN_QUIC_TIMEOUT` | `10` | Таймаут QUIC-зонда, сек |
| `VPN_GEOIP_ENABLED` | `1` | Определение страны/ASN через ip-api.com; `0` — без внешних запросов |

---

## 📦 Сборка портативного .exe (Windows)

Полная команда — в файле `Инструкция по сборке дистрибутива.txt`. Кратко, из корня проекта после удаления `dist`:

```bat
python -m nuitka --standalone --onefile --output-filename="Where is my VPN.exe" --windows-icon-from-ico=icon.ico --include-data-files=index.html=index.html --include-data-files=admin.html=admin.html --include-data-dir=sing-box-bin=sing-box-bin --enable-plugin=anti-bloat --output-dir=dist app.py
```

Готовый файл: `dist\Where is my VPN.exe` — автономный дистрибутив без установки Python.
