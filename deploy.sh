#!/usr/bin/env bash
# Развёртывание «Where is my VPN» в Docker одной командой.
#
#   bash deploy.sh              # собрать и запустить
#   bash deploy.sh --local      # локальная сборка из этой папки
#   bash deploy.sh --github     # строго из GitHub, без автопереключения
#   bash deploy.sh --pull-singbox   # перекачать sing-box linux-amd64
#   bash deploy.sh --logs       # показать логи и выйти
#   bash deploy.sh --down       # остановить (данные в volume сохранятся)
#
# По умолчанию режим выбирается автоматически: если рядом лежит рабочий
# sing-box-bin/sing-box, сборка идёт локально (иначе Docker склонировал бы
# репозиторий и полез скачивать бинарник заново). Флаг --github отключает
# это автопереключение.
#
# Скрипт идемпотентен: повторный запуск обновляет код и пересобирает образ.
# Файл .env и volume с данными (пул, конфиг, чекпоинты) не затрагиваются.

set -euo pipefail

REPO_URL="https://github.com/alekspetrov3009/Where_is_my_VPN"
SB_VERSION="1.10.3"
SINGBOX_URL="https://github.com/SagerNet/sing-box/releases/download/v${SB_VERSION}/sing-box-${SB_VERSION}-linux-amd64.tar.gz"

MODE="auto"
PULL_SINGBOX=0
ACTION="up"

usage() {
    cat <<'USAGE'
Развёртывание «Where is my VPN» в Docker одной командой.

  bash deploy.sh                  собрать и запустить (режим выбирается сам)
  bash deploy.sh --local          локальная сборка из этой папки
  bash deploy.sh --github         строго из GitHub, без автопереключения
  bash deploy.sh --pull-singbox   перекачать sing-box linux-amd64
  bash deploy.sh --logs           показать логи и выйти
  bash deploy.sh --down           остановить (данные в volume сохранятся)

По умолчанию: если рядом есть рабочий sing-box-bin/sing-box — сборка
локальная, иначе из GitHub. Флаг --github отключает автопереключение.

Скрипт идемпотентен: повторный запуск обновляет код и пересобирает образ.
Файл .env и volume с данными (пул, конфиг, чекпоинты) не затрагиваются.
USAGE
}

for arg in "$@"; do
    case "$arg" in
        --local)        MODE="local" ;;
        --github)       MODE="github" ;;
        --pull-singbox) PULL_SINGBOX=1 ;;
        --logs)         ACTION="logs" ;;
        --down)         ACTION="down" ;;
        -h|--help)      usage; exit 0 ;;
        *) echo "Неизвестный аргумент: $arg (см. --help)" >&2; exit 2 ;;
    esac
done

# Скрипт лежит рядом с docker-compose.yml — работаем в своей папке.
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

# --- Проверки окружения -----------------------------------------------------

command -v docker >/dev/null 2>&1 || die "docker не найден. Установите Docker Engine."

# Поддерживаем и новый плагин (docker compose), и старый бинарь (docker-compose).
if docker compose version >/dev/null 2>&1; then
    DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    DC=(docker-compose)
else
    die "не найден 'docker compose'. Установите плагин compose."
fi

docker info >/dev/null 2>&1 || die "демон Docker недоступен. Запустите его: systemctl start docker"

# --- Действия без сборки ----------------------------------------------------

if [ "$ACTION" = "logs" ]; then
    exec "${DC[@]}" logs -f
fi

if [ "$ACTION" = "down" ]; then
    say "Останавливаю контейнер (данные в volume сохранятся)"
    "${DC[@]}" down
    exit 0
fi

# --- .env -------------------------------------------------------------------

if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        say "Создан .env из .env.example — смените VPN_ADMIN_PASSWORD"
    else
        die ".env не найден и нет .env.example для образца"
    fi
fi

# --- sing-box: определяемся до выбора режима --------------------------------
#
# Порядок важен. Если рядом лежит готовый linux-бинарник, сборка из GitHub его
# проигнорирует (Docker клонирует репозиторий заново) и полезет скачивать
# sing-box — а в сетях без доступа к GitHub это лишние минуты ожидания и
# падение. Поэтому наличие бинарника переводит нас в локальный режим.

mkdir -p sing-box-bin

have_singbox=0
if [ -x sing-box-bin/sing-box ]; then
    have_singbox=1
elif [ -f sing-box-bin/sing-box ]; then
    # Файл есть, но не исполняемый — починим права (частый случай после
    # распаковки архива или копирования через Windows-инструменты).
    chmod +x sing-box-bin/sing-box 2>/dev/null && have_singbox=1 || true
fi

# Проверяем, что это действительно linux-бинарник (ELF). Запускать его нельзя:
# deploy.sh может работать и на Windows (Git Bash), где ELF не выполняется,
# а решение о режиме сборки от этого зависеть не должно.
if [ "$have_singbox" = "1" ]; then
    magic="$(head -c 4 sing-box-bin/sing-box 2>/dev/null | od -An -tx1 | tr -d ' \n')"
    if [ "$magic" != "7f454c46" ]; then
        say "ВНИМАНИЕ: sing-box-bin/sing-box не является linux-бинарником (не ELF)"
        say "Похоже на windows-версию (sing-box.exe). Сборка его не использует."
        have_singbox=0
    fi
fi

if [ "$MODE" = "auto" ]; then
    if [ "$have_singbox" = "1" ]; then
        say "Найден рабочий sing-box-bin/sing-box — собираю локально"
        say "(иначе Docker клонировал бы репозиторий и качал бинарник заново;"
        say " чтобы всё равно собирать из GitHub, запустите с флагом --github)"
        MODE="local"
    else
        MODE="github"
    fi
fi

# --- Локальная сборка: нужен код рядом со скриптом ---------------------------

if [ "$MODE" = "local" ]; then
    [ -f Dockerfile ] || die "локальная сборка требует Dockerfile рядом с deploy.sh.
Скачайте код: curl -fL -o /tmp/vpn.zip ${REPO_URL}/archive/refs/heads/main.zip && unzip -oq /tmp/vpn.zip -d /tmp && cp -r /tmp/Where_is_my_VPN-main/. ."
    # Прописываем локальный контекст в .env, не трогая остальные настройки.
    if grep -q '^VPN_BUILD_CONTEXT=' .env; then
        sed -i 's|^VPN_BUILD_CONTEXT=.*|VPN_BUILD_CONTEXT=.|' .env
    else
        printf '\n# Сборка из локальной папки (добавлено deploy.sh)\nVPN_BUILD_CONTEXT=.\n' >> .env
    fi
    say "Режим: локальная сборка (VPN_BUILD_CONTEXT=.)"
else
    say "Режим: сборка из GitHub"
fi

# --- Загрузка sing-box, если его нет ----------------------------------------

need_singbox=0
if [ "$PULL_SINGBOX" = "1" ]; then
    need_singbox=1
elif [ "$have_singbox" = "0" ]; then
    # Бинарника нет. В режиме GitHub его скачает сам Dockerfile (внутри образа),
    # а для локальной сборки качаем заранее, чтобы не гадать, что произошло.
    if [ "$MODE" = "local" ]; then
        need_singbox=1
    else
        say "sing-box будет скачан на этапе сборки образа (Dockerfile)"
    fi
fi

if [ "$need_singbox" = "1" ]; then
    say "Скачиваю sing-box linux-amd64 ${SB_VERSION}"
    if [ "$MODE" = "github" ]; then
        say "ВНИМАНИЕ: режим GitHub — скачанный бинарник в образ не попадёт."
        say "Для использования своего sing-box запустите: bash deploy.sh --local"
    fi
    tmp_tar="$(mktemp -d)/sb.tar.gz"
    ok=0
    for base in "" "https://ghfast.top/" "https://mirror.ghproxy.com/" "https://gh-proxy.com/"; do
        url="${base}${SINGBOX_URL}"
        echo "    попытка: $url"
        if curl -fL --connect-timeout 20 --max-time 300 -o "$tmp_tar" "$url"; then
            ok=1; break
        fi
    done
    [ "$ok" = "1" ] || die "не удалось скачать sing-box ни с одного источника.
Положите бинарник вручную: sing-box-bin/sing-box (chmod +x)."

    tmp_dir="$(mktemp -d)"
    tar -xzf "$tmp_tar" -C "$tmp_dir"
    bin="$(find "$tmp_dir" -type f -name sing-box | head -n1)"
    [ -n "$bin" ] || die "в архиве не найден файл sing-box"
    cp "$bin" sing-box-bin/sing-box
    chmod +x sing-box-bin/sing-box
    rm -rf "$tmp_dir" "$(dirname "$tmp_tar")"
fi

if [ -f sing-box-bin/sing-box ]; then
    # Проверяем формат (ELF) — это работает и на Windows, и на Linux.
    magic="$(head -c 4 sing-box-bin/sing-box 2>/dev/null | od -An -tx1 | tr -d ' \n')"
    if [ "$magic" != "7f454c46" ]; then
        die "sing-box-bin/sing-box не является linux-бинарником (не ELF).
Похоже, это windows-версия (sing-box.exe). Перекачайте: bash deploy.sh --pull-singbox"
    fi
    # Версию печатаем только там, где бинарник реально исполняется.
    if ./sing-box-bin/sing-box version >/dev/null 2>&1; then
        say "sing-box: $(./sing-box-bin/sing-box version | head -n1)"
    else
        say "sing-box: ELF-бинарник на месте (запуск проверит сборка образа)"
    fi
fi

# --- Сборка и запуск --------------------------------------------------------

say "Собираю образ"
"${DC[@]}" build

say "Запускаю"
"${DC[@]}" up -d

# Ждём healthcheck, чтобы в конце показать реальный статус, а не «starting».
say "Ожидаю готовности (до 60 с)"
for _ in $(seq 1 30); do
    state="$(docker inspect --format '{{.State.Health.Status}}' where-is-my-vpn 2>/dev/null || echo unknown)"
    [ "$state" = "healthy" ] && break
    sleep 2
done

"${DC[@]}" ps

ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
port="$(grep -E '^VPN_SERVER_PORT=' .env | cut -d= -f2 || true)"
port="${port:-8079}"

cat <<EOF

Готово.
  Публичная страница: http://${ip:-<IP-сервера>}:${port}
  Админ-панель:       http://${ip:-<IP-сервера>}:${port}/vpn-control-panel-x7k9
  Логи:               bash deploy.sh --logs
  Остановить:         bash deploy.sh --down

Пароль админки берётся из .env (VPN_ADMIN_PASSWORD) — смените его на публичном сервере.
EOF

if [ "${state:-unknown}" != "healthy" ]; then
    printf '\nВНИМАНИЕ: контейнер ещё не healthy. Первый запуск может занимать время.\n'
    printf 'Посмотрите логи: bash deploy.sh --logs\n'
fi