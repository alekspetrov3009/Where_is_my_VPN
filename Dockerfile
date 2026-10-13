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

# Код и статика
COPY app.py config.py paths.py storage.py catalog.py prober.py \
     config_parser.py check_scheduler.py singbox_manager.py ./
COPY index.html admin.html ./

# sing-box: сначала пробуем локальный linux-бинарник, иначе скачиваем с GitHub
COPY sing-box-bin/ ./sing-box-bin/
RUN set -eux; \
    if [ ! -x ./sing-box-bin/sing-box ] && [ -f ./sing-box-bin/sing-box.exe ]; then \
        echo "В sing-box-bin только Windows-бинарник — скачиваю linux-amd64"; \
        apt-get update && apt-get install -y --no-install-recommends unzip ca-certificates && rm -rf /var/lib/apt/lists/*; \
        curl -fsSL -o /tmp/sing-box.zip \
            "https://github.com/SagerNet/sing-box/releases/download/v1.10.3/sing-box-1.10.3-linux-amd64.zip" \
        || { for m in https://ghfast.top/ https://mirror.ghproxy.com/ https://gh-proxy.com/; do \
                curl -fsSL -o /tmp/sing-box.zip "${m}https://github.com/SagerNet/sing-box/releases/download/v1.10.3/sing-box-1.10.3-linux-amd64.zip" && break; \
            done; }; \
        unzip -o /tmp/sing-box.zip -d /tmp/sb; \
        find /tmp/sb -name sing-box -type f -exec mv {} ./sing-box-bin/sing-box \; ; \
        rm -rf /tmp/sb /tmp/sing-box.zip; \
    fi; \
    chmod +x ./sing-box-bin/sing-box* 2>/dev/null || true; \
    ./sing-box-bin/sing-box version

# Каталог изменяемых данных (пул, конфиг, чекпоинты) — volume
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8079

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:%s/api/public/status' % __import__('os').environ.get('VPN_SERVER_PORT','8079'), timeout=4)" || exit 1

CMD ["python", "app.py"]
