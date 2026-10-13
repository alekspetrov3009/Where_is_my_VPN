"""Загрузка sing-box на этапе сборки Docker-образа.

Заменяет однострочный ``urllib.request.urlretrieve`` из Dockerfile, у которого
нет таймаута: при «зависшем» соединении (типовая ситуация при недоступности
GitHub) urlretrieve ждёт бесконечно, поэтому перебор зеркал не начинается и
сборка образа висит без диагностики.

Что делает скрипт:

* качает URL в файл с жёстким таймаутом на попытку (``--timeout``);
* делает несколько попыток на источник (``--retries``) с паузой между ними;
* учитывает прокси из окружения сборки (``HTTPS_PROXY`` / ``HTTP_PROXY``) —
  нужно за корпоративным MITM-прокси;
* проверяет, что скачался непустой файл (иначе HTML-страница ошибки
  принималась бы за архив);
* печатает понятную причину сбоя и завершается с кодом 1, чтобы Dockerfile
  перешёл к следующему зеркалу.

Запуск: ``python fetch_singbox.py <url> <dest> [--timeout N] [--retries N]``.
"""

import argparse
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

# Минимальный правдоподобный размер архива sing-box, байт. Реальный ~10 МБ;
# всё, что меньше, — почти наверняка страница ошибки прокси или редиректа.
MIN_ARCHIVE_BYTES = 1_000_000

CHUNK_SIZE = 64 * 1024


def build_opener():
    """Собрать opener с прокси из окружения и таймаутом по умолчанию.

    Возвращает OpenerDirector. Прокси берётся из HTTPS_PROXY/HTTP_PROXY
    (urllib уважает их автоматически), для недоверенных сертификатов
    предусмотрен флаг VPN_DL_INSECURE=1 — на случай корпоративного MITM,
    когда сборка идёт через перехватывающий прокси без корневого сертификата.
    """
    handlers = []
    if os.environ.get("VPN_DL_INSECURE") == "1":
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = [("User-Agent", "where-is-my-vpn-docker-build")]
    return opener


def download_once(opener, url, dest, timeout):
    """Скачать URL в dest одной попыткой.

    Возвращает (True, размер) при успехе и (False, текст ошибки) при сбое.
    Файл пишется потоково, поэтому таймаут действует и на «зависший» сокет
    посреди передачи, а не только на установку соединения.
    """
    try:
        with opener.open(url, timeout=timeout) as response:
            total = 0
            with open(dest, "wb") as out:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    out.write(chunk)
                    total += len(chunk)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, str(exc)

    if total < MIN_ARCHIVE_BYTES:
        return False, "получено всего %d байт — похоже на страницу ошибки" % total
    return True, "%d байт" % total


def download(url, dest, timeout, retries):
    """Скачать url в dest с повторами. True при успехе, иначе False."""
    opener = build_opener()
    for attempt in range(1, retries + 1):
        print("  попытка %d/%d (таймаут %ds)" % (attempt, retries, timeout))
        ok, info = download_once(opener, url, dest, timeout)
        if ok:
            print("  OK: %s" % info)
            return True
        print("  сбой: %s" % info)
        if attempt < retries:
            time.sleep(3)
    return False


def main(argv=None):
    """Точка входа CLI. Возвращает код выхода процесса."""
    parser = argparse.ArgumentParser(description="Скачать архив sing-box с таймаутами")
    parser.add_argument("url", help="URL архива")
    parser.add_argument("dest", help="куда сохранить файл")
    parser.add_argument("--timeout", type=int, default=60, help="таймаут попытки, сек")
    parser.add_argument("--retries", type=int, default=2, help="попыток на источник")
    args = parser.parse_args(argv)

    print("Скачивание: %s" % args.url)
    if download(args.url, args.dest, args.timeout, args.retries):
        return 0
    print("Не удалось скачать: %s" % args.url, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())