# Запуск «Where is my VPN» в Docker

Инструкция по запуску сервера проверки VPN-конфигураций в контейнере.
Все настраиваемые значения вынесены в переменные окружения (см. [config.py](config.py)
и [.env.example](.env.example)).

## Состав Docker-файлов

| Файл | Назначение |
|------|-----------|
| [Dockerfile](Dockerfile) | Образ на `python:3.13-slim`; sing-box linux-amd64 ставится на этапе сборки |
| [docker-compose.yml](docker-compose.yml) | Сервис с volume `/data`, healthcheck, `env_file: .env` |
| [.dockerignore](.dockerignore) | Исключает данные, тесты и Windows-бинарник из контекста сборки |
| [.env.example](.env.example) | Шаблон переменных окружения с комментариями |
| [requirements.txt](requirements.txt) | Зависимости с зафиксированными версиями |

## Быстрый старт

Требуется Docker Desktop (Windows) или Docker Engine 20.10+.

```bat
:: 1. Создать .env из шаблона (необязательно — есть значения по умолчанию)
copy .env.example .env

:: 2. Собрать образ
docker compose build

:: 3. Запустить
docker compose up -d

:: 4. Проверить
docker compose ps
docker compose logs -f
```

После старта:

- Публичная страница: <http://localhost:8079>
- Админ-панель: <http://localhost:8079/vpn-control-panel-x7k9>
  (логин/пароль — `VPN_ADMIN_USER` / `VPN_ADMIN_PASSWORD` из `.env`, по умолчанию `admin` / `vpn2026`)

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

```bat
:: Посмотреть, что лежит в volume
docker run --rm -v where-is-my-vpn_vpn-data:/data alpine ls -la /data

:: Скопировать пул из контейнера на хост
docker run --rm -v where-is-my-vpn_vpn-data:/data -v %cd%:/backup alpine cp /data/.vpn_pool.json /backup/

:: Полностью стереть данные (пул будет собран заново)
docker compose down -v
```

## Обновление

```bat
docker compose down
docker compose build
docker compose up -d
```

Пул в volume сохраняется между пересозданиями контейнера.

## Логи и диагностика

`print()` в stdout — штатный механизм логирования проекта (портативный exe
показывает консоль), поэтому в Docker всё видно через:

```bat
docker compose logs -f          # следить за логами
docker compose logs --tail 100  # последние 100 строк
```

Healthcheck опрашивает `/api/public/status` каждые 30 секунд:

```bat
docker inspect --format "{{.State.Health.Status}}" where-is-my-vpn
```

Диагностика хоста (DNS, TCP, sing-box) — endpoint `/api/diag` в админке.

## Особенности работы в контейнере

1. **sing-box linux-amd64**. В репозитории лежит `sing-box.exe` (Windows).
   [Dockerfile](Dockerfile) на этапе сборки скачивает linux-версию 1.10.3
   (с зеркалами, как в [singbox_manager.py](singbox_manager.py)) и проверяет
   её запуском `sing-box version`. Если положите `sing-box-bin/sing-box`
   (linux-бинарник) в репозиторий — скачивание не потребуется.
2. **Браузер не открывается**: `VPN_OPEN_BROWSER=0` в контейнере
   ([app.py](app.py) вызывает `os.startfile` только при включённой опции).
3. **Сеть**: контейнеру нужен доступ к `api.github.com`, `raw.githack.com`,
   `chatgpt.com`, `speed.cloudflare.com` и GitHub-зеркалам. Проверка VPN
   порождает множество исходящих соединений — ограничивать их не нужно.
4. **Процессы**: sing-box запускается как дочерний процесс; в compose включён
   `init: true` (tini), чтобы зомби-процессы не накапливались.
5. **Порт занят на хосте**: измените левую часть в `ports` файла
   [docker-compose.yml](docker-compose.yml) или `VPN_SERVER_PORT` в `.env`.

## Запуск без docker compose

```bat
docker build -t where-is-my-vpn .
docker run -d --name where-is-my-vpn ^
  -p 8079:8079 ^
  -e VPN_ADMIN_PASSWORD=мой-пароль ^
  -v vpn-data:/data ^
  where-is-my-vpn
```

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
