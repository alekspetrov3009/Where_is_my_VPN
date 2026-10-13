# Запуск «Where is my VPN» в Docker

Инструкция по запуску сервера проверки VPN-конфигураций в контейнере.
Все настраиваемые значения вынесены в переменные окружения (см. [config.py](config.py)
и [.env.example](.env.example)).

## Быстрый старт (одна команда)

Самый простой путь — скрипт [deploy.sh](deploy.sh): он сам создаст `.env`,
скачает sing-box, соберёт образ, запустит контейнер и дождётся статуса healthy.

```bash
bash deploy.sh              # собрать и запустить (режим выбирается сам)
bash deploy.sh --update     # скачать свежий код и пересобрать
bash deploy.sh --local      # то же, но строго из локальной папки
bash deploy.sh --github     # строго из GitHub, без автопереключения
bash deploy.sh --logs       # смотреть логи
bash deploy.sh --down       # остановить (данные сохранятся)
bash deploy.sh --pull-singbox   # перекачать sing-box linux-amd64
```

**Обновление кода — `bash deploy.sh --update`.** Скачивает архив `main`-ветки
и распаковывает поверх текущей папки. `.env`, `sing-box-bin/` и файлы данных
пула при этом сохраняются. Это работает, даже когда GitHub недоступен
**из контейнера**, — скачивание идёт с хоста обычным `curl`.

**Режим выбирается автоматически.** Если рядом лежит рабочий linux-бинарник
`sing-box-bin/sing-box`, скрипт собирает локально — иначе Docker склонировал бы
репозиторий и полез скачивать sing-box заново, что в сетях без доступа к GitHub
означает несколько минут ожидания и падение сборки. Если бинарника нет, сборка
идёт из GitHub. Флаг `--github` отключает автопереключение.

Скрипт идемпотентен: повторный запуск обновляет код и пересобирает образ,
не трогая `.env` и volume с данными. Ниже описано, что происходит «под капотом»
и как сделать то же самое вручную.

## Два режима сборки — выберите один

Ключевой момент, из-за которого чаще всего возникает путаница: **откуда Docker
берёт код**. Режим задаётся переменной `VPN_BUILD_CONTEXT` в `.env`, править
[docker-compose.yml](docker-compose.yml) не нужно.

| Режим | `VPN_BUILD_CONTEXT` | Что нужно на машине | Когда выбирать |
|-------|--------------------|---------------------|----------------|
| Из GitHub | не задано | только `docker-compose.yml` + `.env` | обычный сервер, нужна свежая `main` |
| Локальная сборка | `.` | **весь код** рядом с compose | свой sing-box, неопубликованные правки |

**Важно:** если вы собираете из GitHub, локальная папка `sing-box-bin/` не
используется — Docker клонирует репозиторий заново. Чтобы подложить свой
бинарник, нужна локальная сборка (`deploy.sh` включает её сам, если видит ELF).

## Режим 1: сборка из GitHub

Локальная копия исходников не нужна — достаточно двух файлов.

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

Закрепить версию вместо `main` — задайте в `.env`:

```bash
VPN_BUILD_CONTEXT=https://github.com/alekspetrov3009/Where_is_my_VPN.git#v1.0.0
```

(после `#` — тег, ветка или хэш коммита).

Проверка:

```bash
docker compose ps
docker compose logs -f
```

## Режим 2: локальная сборка

Нужен весь код проекта в одной папке с `docker-compose.yml`.

**Способ А — через git (аккуратно с вложенностью):**

```bash
cd ~
git clone https://github.com/alekspetrov3009/Where_is_my_VPN.git where-is-my-vpn
cd where-is-my-vpn
cp .env.example .env
nano .env
```

`git clone <url> <папка>` создаёт папку сам. Не делайте `mkdir where-is-my-vpn`
перед клонированием — иначе получите `where-is-my-vpn/Where_is_my_VPN/`
с двумя `docker-compose.yml`, и будет неочевидно, какой из них рабочий.

**Способ Б — из ZIP-архива:**

```bash
mkdir -p ~/where-is-my-vpn && cd ~/where-is-my-vpn
curl -fL -o /tmp/vpn.zip https://github.com/alekspetrov3009/Where_is_my_VPN/archive/refs/heads/main.zip
unzip -oq /tmp/vpn.zip -d /tmp
cp -r /tmp/Where_is_my_VPN-main/. .      # точка в конце обязательна
```

**Затем включите локальный режим:**

```bash
echo 'VPN_BUILD_CONTEXT=.' >> .env
grep -n "VPN_BUILD_CONTEXT" .env
docker compose up -d --build
```

**Проверка, что вы в правильной папке** — рядом должны лежать `Dockerfile`
и `app.py`:

```bash
ls Dockerfile app.py docker-compose.yml
```

## Как подложить свой sing-box

Нужно, когда сборка не может скачать бинарник (GitHub и зеркала недоступны),
но файл можно достать другим способом. **Требуется локальная сборка** —
`deploy.sh` включает её сам, как только видит ELF-бинарник рядом с собой.

```bash
cd ~/where-is-my-vpn

# 1. Скачать linux-amd64 (НЕ .exe — windows-версия в контейнере не работает)
curl -fL -o /tmp/sb.tar.gz \
  https://github.com/SagerNet/sing-box/releases/download/v1.10.3/sing-box-1.10.3-linux-amd64.tar.gz

# 2. Распаковать
tar -xzf /tmp/sb.tar.gz -C /tmp

# 3. Положить под именем ровно sing-box (без расширения)
mkdir -p sing-box-bin
cp /tmp/sing-box-1.10.3-linux-amd64/sing-box sing-box-bin/sing-box
chmod +x sing-box-bin/sing-box

# 4. Убедиться, что это linux-бинарник и он запускается
file sing-box-bin/sing-box      # ожидаем: ELF 64-bit LSB executable, x86-64
./sing-box-bin/sing-box version # ожидаем: sing-box version 1.10.3

# 5. Запустить — режим определится сам
bash deploy.sh
```

Если скачать на сервере нечем, положите файл любым другим способом (WinSCP,
FileZilla, общая папка) — важен только итоговый путь `sing-box-bin/sing-box`.
`deploy.sh` сам выставит права и проверит формат.

Признак успеха при сборке — в логах **нет** строки
«В sing-box-bin нет linux-бинарника — скачиваю linux-amd64».

Проверить бинарник внутри контейнера:

```bash
docker compose exec vpn-validator ./sing-box-bin/sing-box version
```

Если положили не тот файл (например, `sing-box.exe` от Windows), `deploy.sh`
предупредит: «не является linux-бинарником (не ELF)» и соберёт из GitHub,
а при локальной сборке Dockerfile упадёт на шаге `sing-box version`.

## Состав Docker-файлов

| Файл | Назначение |
|------|-----------|
| [Dockerfile](Dockerfile) | Образ на `python:3.13-slim`; sing-box linux-amd64 ставится на этапе сборки |
| [docker-compose.yml](docker-compose.yml) | Сервис с volume `/data`, healthcheck, `env_file: .env`; контекст сборки — `VPN_BUILD_CONTEXT` |
| [deploy.sh](deploy.sh) | Развёртывание одной командой: `.env`, sing-box, сборка, запуск, проверка |
| [docker/fetch_singbox.py](docker/fetch_singbox.py) | Загрузчик sing-box с таймаутами и зеркалами (используется в Dockerfile) |
| [.dockerignore](.dockerignore) | Исключает данные, тесты и Windows-бинарник из контекста сборки |
| [.env.example](.env.example) | Шаблон переменных окружения с комментариями |
| [requirements.txt](requirements.txt) | Зависимости с зафиксированными версиями |

## После старта

- Публичная страница: <http://localhost:8079>
- Админ-панель: <http://localhost:8079/vpn-control-panel-x7k9>
  (логин/пароль — `VPN_ADMIN_USER` / `VPN_ADMIN_PASSWORD` из `.env`, по умолчанию `admin` / `vpn2026`)

## Переменные окружения

Полный список с комментариями — в [.env.example](.env.example). Основные:

| Переменная | По умолчанию | Описание |
|-----------|--------------|----------|
| `VPN_BUILD_CONTEXT` | URL репозитория | Откуда брать код при сборке: URL, `.` (локально) или `URL#тег` |
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
bash deploy.sh --update     # скачать свежий код и пересобрать
```

Скрипт скачивает архив `main`-ветки с хоста и распаковывает поверх текущей
папки. Сохраняются: `.env` (пароль и настройки), `sing-box-bin/` (бинарник,
возможно положенный вручную), файлы данных пула.

Это работает и тогда, когда GitHub недоступен **из контейнера** — скачивание
идёт обычным `curl` на хосте. Если GitHub недоступен и с хоста, скрипт честно
сообщит об этом, а не будет ждать молча.

Другие способы:

```bash
docker compose up -d --build    # в режиме GitHub подтянет свежую main-ветку
git pull && bash deploy.sh      # если проект склонирован через git
```

Пул в volume сохраняется между пересозданиями контейнера. Заодно обновить
базовый образ (`python:3.13-slim`):

```bash
docker compose build --pull
```

В режиме локальной сборки (`.`) `docker compose up -d --build` использует ваши
локальные файлы — чтобы получить свежий код, сначала выполните
`bash deploy.sh --update` (или `git pull`).

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

1. **Код берётся из GitHub или из локальной папки** — задаётся переменной
   `VPN_BUILD_CONTEXT` в `.env` (см. «Два режима сборки»). По умолчанию —
   репозиторий <https://github.com/alekspetrov3009/Where_is_my_VPN>.
2. **sing-box linux-amd64**. В репозитории лежит `sing-box.exe` (Windows).
   [Dockerfile](Dockerfile) на этапе сборки скачивает linux-версию 1.10.3
   (с зеркалами, как в [singbox_manager.py](singbox_manager.py)) и проверяет
   её запуском `sing-box version`. Если положите `sing-box-bin/sing-box`
   (linux-бинарник) — скачивание не потребуется (нужна локальная сборка,
   см. «Как подложить свой sing-box»).
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

# 3. Самый надёжный путь: положить linux-бинарник заранее — тогда скачивание
#    не выполняется вообще (нужна локальная сборка, VPN_BUILD_CONTEXT=.)
bash deploy.sh --local --pull-singbox
```

Подробнее про вариант 3 — в разделе «Как подложить свой sing-box».

Если сборка «висит» на шаге `Попытка: https://github.com/...` без вывода —
это старая версия [Dockerfile](Dockerfile): там использовался `urlretrieve`
без таймаута. Обновите файл и [docker/fetch_singbox.py](docker/fetch_singbox.py).

## Запуск без docker compose

```bash
# из GitHub
docker build -t where-is-my-vpn https://github.com/alekspetrov3009/Where_is_my_VPN.git

# из локальной папки (нужен весь код рядом) — точка в конце
docker build -t where-is-my-vpn .

docker run -d --name where-is-my-vpn \
  -p 8079:8079 \
  -e VPN_ADMIN_PASSWORD=мой-пароль \
  -v vpn-data:/data \
  where-is-my-vpn
```

`docker build` умеет принимать git-URL как контекст: код клонируется из
репозитория, локальные файлы не нужны. Для сборки из локальной копии
используйте `.` — тогда работает и свой `sing-box-bin/sing-box`.

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
