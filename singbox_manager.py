"""Поиск, валидация и скачивание sing-box.

Порядок поиска важен: сохранённый путь пользователя → системный PATH →
локальная папка sing-box-bin → nekoray/nekobox. Все admin-эндпоинты
валидируют бинарник запуском `sing-box version`.
"""
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

from paths import SINGBOX_DIR, SINGBOX_EXE
from config import GITHUB_MIRRORS, SINGBOX_VERSION, IS_WINDOWS

# Архив зависит от платформы: в Linux-контейнере качаем linux-amd64.
_SINGBOX_PLATFORM = "windows-amd64" if IS_WINDOWS else "linux-amd64"
SINGBOX_FILENAME = f"sing-box-{SINGBOX_VERSION}-{_SINGBOX_PLATFORM}.zip"

# Медленный rglob по дискам — кэшируем на час, иначе каждый run_check_core
# блокирует event loop на секунды.
_NEKORAY_CACHE = {"path": None, "found": False, "at": 0.0}
_NEKORAY_CACHE_TTL = 3600.0


def _creation_flags():
    """CREATE_NO_WINDOW на Windows, чтобы процессы sing-box не мигали окнами."""
    return subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0


def run_singbox_version(exe_path, timeout=10):
    """Запускает `sing-box version`; возвращает (ok, первая строка вывода).

    Единая точка валидации бинарника для admin-эндпоинтов и скачивания.
    """
    try:
        proc = subprocess.run(
            [str(exe_path), "version"],
            capture_output=True, text=True, timeout=timeout,
            creationflags=_creation_flags()
        )
        if proc.returncode != 0:
            return False, ""
        output = (proc.stdout or "").strip()
        return True, (output.split('\n')[0] if output else "unknown")
    except Exception:
        return False, ""


def find_singbox_in_nekoray():
    now = time.monotonic()
    if now - _NEKORAY_CACHE["at"] < _NEKORAY_CACHE_TTL and _NEKORAY_CACHE["found"]:
        cached = _NEKORAY_CACHE["path"]
        if cached is not None and Path(cached).is_file():
            return Path(cached)
        if cached is None:
            return None
    search_dirs = []
    for env_var in ["NEKORAY_DIR", "NEKOBOX_DIR", "ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"]:
        if env_var in os.environ:
            base = Path(os.environ[env_var])
            search_dirs.extend([base / "nekoray", base / "nekobox"])
    for drive in ["C:\\", "D:\\", "E:\\"]:
        search_dirs.extend([
            Path(drive) / "nekoray", Path(drive) / "nekobox",
            Path(drive) / "Program Files" / "nekoray",
            Path(drive) / "Program Files (x86)" / "nekoray",
        ])
    user_home = Path.home()
    search_dirs.extend([user_home / "Desktop", user_home / "Downloads"])

    for base_dir in search_dirs:
        if base_dir.exists():
            try:
                for exe_path in base_dir.rglob("sing-box.exe"):
                    if exe_path.is_file():
                        _NEKORAY_CACHE.update(path=str(exe_path), found=True, at=time.monotonic())
                        return exe_path
            except PermissionError:
                continue
    _NEKORAY_CACHE.update(path=None, found=True, at=time.monotonic())
    return None


def find_singbox(get_saved_path):
    """Возвращает (Path | None, источник: user/system/local/nekoray/'')."""
    saved_path = get_saved_path()
    if saved_path:
        return Path(saved_path), "user"
    system_path = shutil.which("sing-box")
    if system_path:
        return Path(system_path), "system"
    if SINGBOX_EXE.exists():
        return SINGBOX_EXE, "local"
    for exe_name in ["nekoray.exe", "nekobox.exe"]:
        nekoray_path = shutil.which(exe_name)
        if nekoray_path:
            parent = Path(nekoray_path).parent
            singbox_candidate = parent / "sing-box.exe"
            if singbox_candidate.exists():
                return singbox_candidate, "nekoray"
            core_singbox = parent / "core" / "sing-box.exe"
            if core_singbox.exists():
                return core_singbox, "nekoray"
    nekoray_path = find_singbox_in_nekoray()
    if nekoray_path:
        return nekoray_path, "nekoray"
    return None, ""


async def download_singbox(progress_callback=None):
    """Скачивает sing-box с GitHub (с зеркалами), распаковывает и валидирует.

    Возвращает (success, message). Бинарные загрузки идут через верифицированный
    urllib — без unverified-контекста.
    """
    import asyncio

    SINGBOX_DIR.mkdir(exist_ok=True)
    zip_path = SINGBOX_DIR / SINGBOX_FILENAME
    base_url = f"https://github.com/SagerNet/sing-box/releases/download/v{SINGBOX_VERSION}/{SINGBOX_FILENAME}"
    for i, mirror in enumerate(GITHUB_MIRRORS):
        url = mirror + base_url if mirror else base_url
        source_name = mirror.replace("https://", "").replace("/", "") or "GitHub"
        if progress_callback:
            await progress_callback(f"🔄 Попытка #{i+1}: скачивание через {source_name}...")
        try:
            def do_download():
                req = urllib.request.Request(url, headers={"User-Agent": "VPN-Validator/1.0"})
                with urllib.request.urlopen(req, timeout=30) as response:
                    with open(zip_path, 'wb') as f:
                        while True:
                            chunk = response.read(8192)
                            if not chunk:
                                break
                            f.write(chunk)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, do_download)
            if progress_callback:
                await progress_callback(f"📦 Распаковка...")
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                exe_name = None
                for name in zip_ref.namelist():
                    if name.endswith(SINGBOX_EXE.name):
                        exe_name = name
                        break
                if not exe_name:
                    raise Exception(f"{SINGBOX_EXE.name} не найден в архиве")
                with zip_ref.open(exe_name) as src, open(SINGBOX_EXE, 'wb') as dst:
                    dst.write(src.read())
            zip_path.unlink()
            # Проверяем, что это действительно рабочий sing-box, а не битый/подменённый файл.
            ok, _ = run_singbox_version(SINGBOX_EXE, timeout=10)
            if not ok:
                try:
                    if SINGBOX_EXE.exists():
                        SINGBOX_EXE.unlink()
                except Exception:
                    pass
                raise RuntimeError("скачанный файл не запускается как sing-box")
            return True, f"✅ sing-box успешно скачан через {source_name}"
        except Exception as e:
            if progress_callback:
                await progress_callback(f"️ {source_name} не сработал: {str(e)[:50]}")
            if zip_path.exists():
                try:
                    zip_path.unlink()
                except:
                    pass
            continue
    return False, "❌ Не удалось скачать sing-box ни с одного источника"
