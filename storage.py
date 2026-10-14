"""Персистентность: конфиг sing-box, пул рабочих конфигураций, чекпоинты пулов.

Все записи атомарные (tmp + os.replace), чтобы прерванный процесс не оставлял
битых JSON. Пул переживает перезапуски; чекпоинты позволяют продолжить
проход каталога с последнего завершённого пула. Кэш нерабочих точек
(.vpn_dead.json) избавляет от повторной проверки заведомо мёртвых URI.
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path

from paths import CONFIG_FILE, POOL_FILE, SCHEDULE_FILE, DEAD_FILE
from config import STABLE_CHECKS, DEAD_CACHE_SECONDS


def load_config():
    """Читает .singbox_config.json; отсутствующий/битый файл = пустой конфиг."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_saved_singbox_path():
    config = load_config()
    path = config.get("singbox_path")
    if path and Path(path).exists() and Path(path).is_file():
        return path
    return None


def save_singbox_path(path):
    config = load_config()
    config["singbox_path"] = path
    save_config(config)


def schedule_scope(check_type, source_id):
    """Ключ чекпоинта: ручные частичные прогоны не сдвигают общий график."""
    return f"{check_type}|{source_id or ''}"


def load_check_schedule(schedule_file=None):
    schedule_file = schedule_file or SCHEDULE_FILE
    try:
        with open(schedule_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_check_schedule(state, schedule_file=None):
    schedule_file = schedule_file or SCHEDULE_FILE
    try:
        tmp_path = Path(schedule_file).with_suffix(".tmp")
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp_path, schedule_file)
    except Exception as e:
        print(f"⚠️ Не удалось сохранить позицию пулов: {e}")


def load_pool(pool_file=None):
    """Загружает пул с нормализацией записей и дедупликацией (source, uri)."""
    pool_file = pool_file or POOL_FILE
    if not pool_file.exists():
        return {"black": [], "white": []}
    try:
        with open(pool_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        pool = {}
        for list_type in ("black", "white"):
            items = data.get(list_type, [])
            clean = []
            seen_pool = set()
            for item in items:
                uri = item.get("uri", "").strip() if isinstance(item, dict) else ""
                source = item.get("source", "unknown") if isinstance(item, dict) else "unknown"
                if not uri.startswith(('vless://', 'trojan://', 'ss://', 'hysteria2://', 'tuic://', 'vmess://')):
                    continue
                key = (source, uri)
                if key in seen_pool:
                    continue
                seen_pool.add(key)
                successes = max(0, int(item.get("successful_checks", 0)))
                added_at = item.get("added_at") if isinstance(item.get("added_at"), str) else None
                if not added_at:
                    # Старый пул без даты: считаем датой добавления момент загрузки.
                    added_at = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                clean.append({"uri": uri, "latency": item.get("latency", 0), "source": source,
                              "successful_checks": successes, "stable": successes >= STABLE_CHECKS,
                              "added_at": added_at,
                              "services": item.get("services") if isinstance(item.get("services"), dict) else {},
                              "geo": item.get("geo") if isinstance(item.get("geo"), dict) else None,
                              "speed": item.get("speed") if isinstance(item.get("speed"), dict) else None})
            pool[list_type] = clean
        return pool
    except Exception as e:
        print(f"⚠️ Не удалось загрузить пул ({pool_file}): {e}")
        return {"black": [], "white": []}


def save_pool(public_data, pool_file=None):
    """Сохраняет текущий пул из public_data; запись атомарная."""
    pool_file = pool_file or POOL_FILE
    try:
        data = {
            list_type: [{"uri": c["uri"], "latency": c.get("latency", 0), "source": c.get("source", "unknown"),
                         "successful_checks": c.get("successful_checks", 0), "added_at": c.get("added_at"),
                         "speed": c.get("speed"),
                         "geo": c.get("geo"),
                         "services": c.get("services", {})} for c in public_data[list_type].get("working", [])]
            for list_type in ("black", "white")
        }
        tmp_path = pool_file.with_suffix(".tmp")
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, pool_file)
    except Exception as e:
        print(f"⚠️ Не удалось сохранить пул: {e}")


def load_dead_cache(dead_file=None):
    """Читает кэш нерабочих точек, отбрасывая протухшие записи.

    Ключ — каноническая точка (URI без #fragment), значение — время последней
    неудачи. Каталог содержит десятки тысяч конфигураций, и большинство из них
    мертвы: без кэша каждый проход заново тратит на них полный таймаут.
    Записи старше DEAD_CACHE_SECONDS выбрасываются, чтобы точка могла ожить.

    Возвращает dict {endpoint: timestamp}.
    """
    dead_file = dead_file or DEAD_FILE
    try:
        with open(dead_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    cutoff = time.time() - DEAD_CACHE_SECONDS
    cache = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key:
            continue
        try:
            stamp = float(value)
        except (TypeError, ValueError):
            continue
        if stamp >= cutoff:
            cache[key] = stamp
    return cache


def save_dead_cache(cache, dead_file=None):
    """Сохраняет кэш нерабочих точек; запись атомарная."""
    dead_file = dead_file or DEAD_FILE
    try:
        tmp_path = Path(dead_file).with_suffix(".tmp")
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp_path, dead_file)
    except Exception as e:
        print(f"⚠️ Не удалось сохранить кэш нерабочих точек: {e}")
