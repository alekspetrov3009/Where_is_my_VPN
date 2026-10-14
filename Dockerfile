# Образ "Where is my VPN" — FastAPI-сервер проверки VPN-конфигураций.
# sing-box скачивается на этапе сборки (linux-amd64), чтобы контейнер
# стартовал без зависимости от GitHub в рантайме.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Значения по умолчанию; переопределяются через docker-compose/.env
    VPN_SERVER_HOST=0.0.0.0 \
    VPN_SERVER_PORT=8079 \
    VPN_DATA_DIR=/data \
    VPN_SINGBOX_DIR=/app/sing-box-bin \
    VPN_OPEN_BROWSER=0

WORKDIR /app

# Зависимости отдельным слоем — кэш сборки не инвалидируется правками кода
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код и статика.
# Модули копируются маской, а не списком: при добавлении нового файла
# (clash_export.py, geoip.py) перечисление по именам забывали обновить, и
# контейнер падал с ModuleNotFoundError уже после сборки. Тесты и прочие
# *.py в образ не нужны — их отсекает .dockerignore.
COPY *.py ./
COPY index.html admin.html ./

# sing-box: если в репозитории нет linux-бинарника — скачиваем с GitHub
# (сначала официальный релиз, затем зеркала). ВАЖНО: релизы sing-box для
# linux распространяются как .tar.gz (не .zip). Скачивание через python
# urllib (tarfile тоже встроен) — curl в slim-образе отсутствует.
#
# Загрузчик написан отдельным скриптом (а не urlretrieve в одну строку) ради
# таймаутов: при «зависшем» соединении с GitHub urlretrieve без timeout ждёт
# бесконечно и до зеркал дело не доходит. Здесь на каждую попытку отведено
# VPN_DL_TIMEOUT секунд, попыток VPN_DL_RETRIES, и учитывается прокси из
# окружения сборки (HTTPS_PROXY/HTTP_PROXY) — полезно за корпоративным MITM.
COPY sing-box-bin/ ./sing-box-bin/
COPY docker/fetch_singbox.py /tmp/fetch_singbox.py
# Таймаут одной попытки (сек) и число попыток на источник.
ARG VPN_DL_TIMEOUT=60
ARG VPN_DL_RETRIES=2
RUN set -eux; \
    if [ ! -x ./sing-box-bin/sing-box ]; then \
        echo "В sing-box-bin нет linux-бинарника — скачиваю linux-amd64"; \
        SB_URL="https://github.com/SagerNet/sing-box/releases/download/v1.10.3/sing-box-1.10.3-linux-amd64.tar.gz"; \
        ok=0; \
        for u in "$SB_URL" \
                 "https://ghfast.top/$SB_URL" \
                 "https://mirror.ghproxy.com/$SB_URL" \
                 "https://gh-proxy.com/$SB_URL"; do \
            echo "Попытка: $u"; \
            if python /tmp/fetch_singbox.py "$u" /tmp/sing-box.tar.gz \
                   --timeout "$VPN_DL_TIMEOUT" --retries "$VPN_DL_RETRIES"; then \
                ok=1; break; \
            fi; \
        done; \
        if [ "$ok" != "1" ]; then \
            echo "ERROR: не удалось скачать sing-box ни с одного источника" >&2; \
            echo "Подсказка: положите linux-бинарник в sing-box-bin/sing-box либо" >&2; \
            echo "передайте прокси: docker compose build --build-arg HTTPS_PROXY=..." >&2; \
            exit 1; \
        fi; \
        python -c "import tarfile, glob, shutil; \
t = tarfile.open('/tmp/sing-box.tar.gz'); \
t.extractall('/tmp/sb'); \
shutil.move(glob.glob('/tmp/sb/**/sing-box', recursive=True)[0], './sing-box-bin/sing-box')"; \
        rm -rf /tmp/sb /tmp/sing-box.tar.gz; \
    fi; \
    chmod +x ./sing-box-bin/sing-box; \
    ./sing-box-bin/sing-box version

# Каталог изменяемых данных (пул, конфиг, чекпоинты) — volume
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8079

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:%s/api/public/status' % __import__('os').environ.get('VPN_SERVER_PORT','8079'), timeout=4)" || exit 1

CMD ["python", "app.py"]
