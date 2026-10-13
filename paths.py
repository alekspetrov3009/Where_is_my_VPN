"""Единый источник путей приложения.

Работает и при обычном запуске `python app.py`, и в скомпилированном
Nuitka --onefile / PyInstaller, и в Docker: статика (HTML) разрешается
относительно исполняемого файла, а каталог изменяемых данных и папка
sing-box переопределяются переменными окружения VPN_DATA_DIR и
VPN_SINGBOX_DIR (см. config.py и .env.example).
"""
import os
import sys
from pathlib import Path

from config import DATA_DIR as _ENV_DATA_DIR
from config import SINGBOX_DIR as _ENV_SINGBOX_DIR
from config import IS_WINDOWS

# Правильное определение базовой директории для .exe и обычного Python
if getattr(sys, 'frozen', False):
    # Если запущен как скомпилированный .exe (Nuitka/PyInstaller)
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        # PyInstaller распаковывает файлы во временную папку
        APP_DIR = Path(meipass)
    else:
        # Nuitka: файлы рядом с exe или во временной папке
        APP_DIR = Path(sys.executable).parent
else:
    # Обычный запуск через python app.py
    APP_DIR = Path(__file__).parent

# Каталог изменяемых данных (пул, конфиг sing-box, чекпоинты).
# В Docker это volume /data; без переменной — каталог приложения.
DATA_DIR = Path(_ENV_DATA_DIR) if _ENV_DATA_DIR else APP_DIR

SINGBOX_DIR = Path(_ENV_SINGBOX_DIR) if _ENV_SINGBOX_DIR else APP_DIR / "sing-box-bin"
# Имя бинарника зависит от платформы: в Linux-контейнере расширения нет.
SINGBOX_BINARY_NAME = "sing-box.exe" if IS_WINDOWS else "sing-box"
SINGBOX_EXE = SINGBOX_DIR / SINGBOX_BINARY_NAME
CONFIG_FILE = DATA_DIR / ".singbox_config.json"
POOL_FILE = DATA_DIR / ".vpn_pool.json"
SCHEDULE_FILE = DATA_DIR / ".vpn_schedule.json"


def get_resource_path(relative_path):
    """Возвращает абсолютный путь к файлу. Работает и в Python, и в Nuitka --onefile"""
    if getattr(sys, 'frozen', False):
        # Для Nuitka --onefile и PyInstaller: берем папку временного exe
        base_path = os.path.dirname(sys.executable)
    else:
        # Для обычного запуска python app.py: берем папку со скриптом
        base_path = os.path.dirname(os.path.abspath(__file__))

    return os.path.join(base_path, relative_path)
