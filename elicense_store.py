"""
Хранилище результатов elicense.kz.

Держит уведомления в JSON-файле, сгруппированные по БИН,
внутри каждой группы — строго в хронологическом порядке.

Структура файла:
    {
      "version": 1,
      "updated_at": "2026-10-10T09:15:00",
      "companies": {
        "250640027866": {
          "bin": "250640027866",
          "company": "Товарищество с ограниченной ответственностью \"QazDome Systems\"",
          "fetched_at": "2026-10-10T09:15:00",
          "attempts": 2,
          "stats": {"total": 3, "with_date": 3,
                    "first_date": "2024-02-11", "last_date": "2026-07-16"},
          "notifications": [ {...}, {...} ]
        }
      }
    }

Запись атомарная (через временный файл), поэтому оборванный процесс
не оставит после себя битый JSON. Обращения из потоков сервера
сериализованы блокировкой.
"""

import json
import os
import threading
from datetime import datetime

DEFAULT_FILE = "elicense_data.json"
SCHEMA_VERSION = 1

_lock = threading.RLock()


def _now():
    return datetime.now().isoformat(timespec="seconds")


def load_store(path=DEFAULT_FILE):
    """Читает хранилище. Отсутствующий или битый файл — пустой словарь."""
    with _lock:
        if not os.path.exists(path):
            return {"version": SCHEMA_VERSION, "updated_at": None, "companies": {}}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {"version": SCHEMA_VERSION, "updated_at": None, "companies": {}}

        if not isinstance(data, dict) or "companies" not in data:
            return {"version": SCHEMA_VERSION, "updated_at": None, "companies": {}}
        return data


def _atomic_write(path, data):
    """Пишем во временный файл рядом и подменяем — без «полузаписанного» JSON."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_store(store, path=DEFAULT_FILE):
    store["updated_at"] = _now()
    store.setdefault("version", SCHEMA_VERSION)
    with _lock:
        _atomic_write(path, store)
    return store


def save_result(result, path=DEFAULT_FILE):
    """
    Записывает результат search() в хранилище по его БИН.
    Уведомления пересортировываются по дате.
    Возвращает сохранённую запись компании.
    """
    bin_number = str(result.get("bin") or "").strip()
    if not bin_number:
        raise ValueError("В результате отсутствует БИН")

    notifs = result.get("notifications") or []

    with _lock:
        store = load_store(path)
        entry = store["companies"].get(bin_number, {})

        entry.update({
            "bin": bin_number,
            "company": result.get("company") or entry.get("company"),
            "fetched_at": _now(),
            "attempts": result.get("attempts"),
            "pages": result.get("pages"),
            "notifications": sorted(
                notifs,
                key=lambda n: (n.get("issue_date_iso") or "9999",
                               n.get("nikad") or ""),
            ),
        })
        entry["stats"] = {
            "total": len(entry["notifications"]),
            "with_date": sum(1 for n in entry["notifications"]
                             if n.get("issue_date_iso")),
            "first_date": min((n["issue_date_iso"] for n in entry["notifications"]
                               if n.get("issue_date_iso")), default=None),
            "last_date": max((n["issue_date_iso"] for n in entry["notifications"]
                              if n.get("issue_date_iso")), default=None),
        }

        store["companies"][bin_number] = entry
        save_store(store, path)
        return entry


def get_entry(bin_number, path=DEFAULT_FILE):
    """Запись одного БИН или None."""
    store = load_store(path)
    return store["companies"].get(str(bin_number).strip())


def list_bins(path=DEFAULT_FILE):
    """Все БИН, которые уже есть в хранилище."""
    store = load_store(path)
    return sorted(store["companies"].keys())


def timeline(bin_number, path=DEFAULT_FILE):
    """
    Хронология уведомлений по БИН — плоский список, по возрастанию даты.
    Пустой БИН или отсутствие записи даёт пустой список.
    """
    entry = get_entry(bin_number, path)
    if not entry:
        return []
    return list(entry.get("notifications") or [])


def summary(path=DEFAULT_FILE):
    """Короткая сводка по всему хранилищу — для /api/elicense."""
    store = load_store(path)
    companies = store["companies"]
    total_notifs = sum(
        len(c.get("notifications") or []) for c in companies.values()
    )
    return {
        "file": path,
        "exists": os.path.exists(path),
        "updated_at": store.get("updated_at"),
        "companies": len(companies),
        "notifications": total_notifs,
        "bins": sorted(companies.keys()),
    }