# Запуск «Where is my VPN» в Docker

Инструкция по запуску сервера проверки VPN-конфигураций в контейнере.
Все настраиваемые значения вынесены в переменные окружения (см. [config.py](config.py)
и [.env.example](.env.example)).

## Как устроено развёртывание

При сборке образа Docker клонирует актуальный код прямо из GitHub-репозитория
<https://github.com/alekspetrov3009/Where_is_my_VPN>: [docker-compose.yml](docker-compose.yml)
указывает репозиторий как контекст сборки. Локальная копия исходников не нужна —
достаточно двух файлов: `docker-compose.yml` и `.env`.

- `docker compose up -d --build` — каждая сборка подтягивает свежую `main`-ветку.
- Закрепить конкретную версию — фрагмент URL в compose:
  `build: https://github.com/alekspetrov3009/Where_is_my_VPN.git#v1.0.0`
  (после `#` — тег, ветка или хэш коммита).
- Для локальной разработки замените в compose строку `build:` на `build: .` —
  соберётся из локальных файлов.

## Состав Docker-файлов

| Файл | Назначение |
|------|-----------|
| [Dockerfile](Dockerfile) | Образ на `python:3.13-slim`; sing-box linux-amd64 ставится на этапе сборки |
| [docker-compose.yml](docker-compose.yml) | Сервис с volume `/data`, healthcheck, `env_file: .env`; контекст сборки — GitHub-репозиторий |
| [.dockerignore](.dockerignore) | Исключает данные, тесты и Windows-бинарник из контекста сборки |
| [.env.example](.env.example) | Шаблон переменных окружения с комментариями |
| [requirements.txt](requirements.txt) | Зависимости с зафиксированными версиями |

## Быстрый старт без локальной копии проекта

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

Проверка:

```bash
docker compose ps
docker compose logs -f
```

После старта:

- Публичная страница: <http://localhost:8079>
- Админ-панель: <http://localhost:8079/vpn-control-panel-x7k9>
  (логин/пароль — `VPN_ADMIN_USER` / `VPN_ADMIN_PASSWORD` из `.env`, по умолчанию `admin` / `vpn2026`)

## Быстрый старт из локальной копии проекта

Если репозиторий уже склонирован:

```bat
cd 2_Where_is_my_VPN
copy .env.example .env
docker compose up -d --build
```

Сборка всё равно пойдёт из GitHub (так настроен контекст в
[docker-compose.yml](docker-compose.yml)). Чтобы собрать из локальных файлов —
замените строку `build:` на `build: .`.

## Переменные окружения

Полный список с комментариями — в [.env.example](.env.example). Основные:

| Переменная | По умолчанию | Описание |
|-----------|--------------|----------|
| `VPN_SERVER_PORT` | `8079` | Порт веб-сервера (левая часть `ports` в compose) |
| `VPN_SERVER_HOST` | `0.0.0.0` | Адрес привязки (в контейнере не менять) |
| `VPN_ADMIN_PATH` | `/vpn-control-panel-x7k9` | Путь админ-панели |
| `VPN_ADMIN_USER` | `admin` | Логин админки |
| `VPN_ADMIN_PASSWORD` | `vpn2026` | Пароль админки — **смените на публичном сервере** |
| `VPN_DATA_DIR` | `/data` (в контейнере) | Каталог пула, конфига и чекпоинтов (volume) |
| `VPN_SINGBOX_DIR` | `/app/sing-box-bin` | Каталог бинарника sing-box |
| `VPN_CHECK_CONCURRENCY` | `16` | Параллельных sing-box процессов при поиске |
| `VPN_CHECK_TIMEOUT` | `6.0` | Таймаут проверки одного URL, сек |
| `VPN_STABLE_CHECKS` | `11` | Успешных проверок до статуса «стабильный» |
| `VPN_SINGBOX_VERSION` | `1.10.3` | Версия sing-box для авто-скачивания |
| `VPN_GITHUB_MIRRORS` | 3 зеркала + GitHub | Зеркала загрузки sing-box через запятую |

Приоритет значений: переменная окружения → значение по умолчанию из [config.py](config.py).
Порт также можно передать аргументом (`python app.py 9000`) — как и раньше.

## Данные и volume

Изменяемые данные (пул `.vpn_pool.json`, конфиг `.singbox_config.json`,
чекпоинты `.vpn_schedule.json`) хранятся в volume `vpn-data` → `/data`
внутри контейнера ([paths.py](paths.py) читает `VPN_DATA_DIR`).

```bash
# Посмотреть, что лежит в volume
docker run --rm -v where-is-my-vpn_vpn-data:/data alpine ls -la /data

# Скопировать пул из контейнера на хост
docker run --rm -v where-is-my-vpn_vpn-data:/data -v $(pwd):/backup alpine cp /data/.vpn_pool.json /backup/

# Полностью стереть данные (пул будет собран заново)
docker compose down -v
```

## Обновление до актуальной версии

```bash
docker compose up -d --build
```

При каждой сборке Docker заново клонирует `main`-ветку репозитория, поэтому
пересборка всегда подтягивает свежий код. Пул в volume сохраняется между
пересозданиями контейнера. Заодно обновить базовый образ (`python:3.13-slim`):

```bash
docker compose build --pull
```

## Логи и диагностика

`print()` в stdout — штатный механизм логирования проекта (портативный exe
показывает консоль), поэтому в Docker всё видно через:

```bash
docker compose logs -f          # следить за логами
docker compose logs --tail 100  # последние 100 строк
```

Healthcheck опрашивает `/api/public/status` каждые 30 секунд:

```bash
docker inspect --format "{{.State.Health.Status}}" where-is-my-vpn
```

Диагностика хоста (DNS, TCP, sing-box) — endpoint `/api/diag` в админке.

## Особенности работы в контейнере

1. **Код берётся из GitHub** — контекст сборки в
   [docker-compose.yml](docker-compose.yml) указывает на
   <https://github.com/alekspetrov3009/Where_is_my_VPN>; пересборка = свежая версия.
2. **sing-box linux-amd64**. В репозитории лежит `sing-box.exe` (Windows).
   [Dockerfile](Dockerfile) на этапе сборки скачивает linux-версию 1.10.3
   (с зеркалами, как в [singbox_manager.py](singbox_manager.py)) и проверяет
   её запуском `sing-box version`. Если положите `sing-box-bin/sing-box`
   (linux-бинарник) в репозиторий — скачивание не потребуется.
   Загрузкой занимается [docker/fetch_singbox.py](docker/fetch_singbox.py):
   у каждой попытки есть таймаут, поэтому недоступный GitHub не подвешивает
   сборку навсегда, а перебор зеркал идёт дальше. Настройки — build-args
   `VPN_DL_TIMEOUT` (по умолчанию 60 с) и `VPN_DL_RETRIES` (2).
3. **Браузер не открывается**: `VPN_OPEN_BROWSER=0` в контейнере
   ([app.py](app.py) вызывает `os.startfile` только при включённой опции).
4. **Сеть**: контейнеру нужен доступ к `api.github.com`, `raw.githack.com`,
   `chatgpt.com`, `speed.cloudflare.com` и GitHub-зеркалам. Проверка VPN
   порождает множество исходящих соединений — ограничивать их не нужно.
5. **Процессы**: sing-box запускается как дочерний процесс; в compose включён
   `init: true` (tini), чтобы зомби-процессы не накапливались.
6. **Порт занят на хосте**: измените левую часть в `ports` файла
   [docker-compose.yml](docker-compose.yml) или `VPN_SERVER_PORT` в `.env`.

## Сборка за прокси и медленным GitHub

Скачивание sing-box идёт на этапе сборки, поэтому при недоступном GitHub
сборка падает с `ERROR: не удалось скачать sing-box ни с одного источника`.
Варианты:

```bash
# 1. Прокси для сборки (корпоративный MITM — добавьте VPN_DL_INSECURE=1)
docker compose build --build-arg HTTPS_PROXY=http://proxy:3128 --build-arg HTTP_PROXY=http://proxy:3128

# 2. Увеличить таймаут и число попыток на источник
docker compose build --build-arg VPN_DL_TIMEOUT=180 --build-arg VPN_DL_RETRIES=3

# 3. Самый надёжный путь: положить linux-бинарник рядом с исходниками,
#    тогда скачивание не выполняется вообще
curl -L -o sing-box-bin/sing-box \
  https://github.com/SagerNet/sing-box/releases/download/v1.10.3/sing-box-1.10.3-linux-amd64.tar.gz
tar -xzf sing-box-bin/sing-box -C /tmp   # внутри sing-box-1.10.3-linux-amd64/sing-box
mv /tmp/sing-box-1.10.3-linux-amd64/sing-box sing-box-bin/sing-box
chmod +x sing-box-bin/sing-box
```

Если сборка «висит» на шаге `Попытка: https://github.com/...` без вывода —
это старая версия [Dockerfile](Dockerfile): там использовался `urlretrieve`
без таймаута. Обновите файл и [docker/fetch_singbox.py](docker/fetch_singbox.py).

## Запуск без docker compose

```bash
docker build -t where-is-my-vpn https://github.com/alekspetrov3009/Where_is_my_VPN.git
docker run -d --name where-is-my-vpn \
  -p 8079:8079 \
  -e VPN_ADMIN_PASSWORD=мой-пароль \
  -v vpn-data:/data \
  where-is-my-vpn
```

`docker build` умеет принимать git-URL как контекст: код клонируется из
репозитория, локальные файлы не нужны. Для сборки из локальной копии
замените URL на `.`

## Локальный запуск без Docker (как раньше)

```bat
pip install -r requirements.txt
python app.py
```

Без переменных окружения всё работает со значениями по умолчанию —
поведение идентично прежнему.

## Тесты

```bat
python -m unittest test_check_scheduling test_validation
```

Известные предсуществующие падения в `test_validation.py` (7 шт.) описаны
в [AGENTS.md](AGENTS.md): тесты патчат `urllib.request.urlopen`, тогда как
код каталога вызывает `urlopen_insecure` — чинить нужно тесты, а не код.
