#!/usr/bin/env python3
"""
HTTP-сервер:
1. Отдаёт статику (index.html).
2. /api/zhk — список строящихся ЖК с датами начала и сдачи.
   Данные берутся из двух эндпоинтов homeportal.kz:
     - POST /api/v1/getobjects       — список ID
     - GET  /api/v1/objects-detail/{id} — детали каждого объекта
   Результат кэшируется в память и на диск.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
HOMEPORTAL_LIST_URL   = "https://api.homeportal.kz/api/v1/getobjects"
HOMEPORTAL_DETAIL_URL = "https://api.homeportal.kz/api/v1/objects-detail/{id}"

HOMEPORTAL_PAYLOAD = {
    "authority": "",
    "city_id": 12,          # Астана
    "is_paginate": False,
    "region_id": 1,
    "search": "",
    "build_status": "2",
    "status": "2,3",
}

CACHE_TTL_SECONDS = 600         # 10 минут — память
CACHE_FILE        = "cache.json"  # кэш на диск
MAX_WORKERS       = 10            # параллельных запросов к деталям

_memory_cache = {"data": None, "timestamp": 0}


# ============================================================
# HTTP-ПОМОЩНИКИ
# ============================================================
def _http_post_json(url, payload, timeout=20):
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; ZhkMapBot/1.0)",
            "Referer": "https://homeportal.kz/",
        },
        method="POST",
    )
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get_json(url, timeout=20):
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; ZhkMapBot/1.0)",
            "Referer": "https://homeportal.kz/",
        },
        method="GET",
    )
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ============================================================
# РАЗБОР ДАТ
# ============================================================
def _parse_date(s):
    """'23.09.2025' → datetime.date. Иначе None."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d.%m.%Y").date()
    except (ValueError, TypeError):
        return None


def _compute_status(start, end, today=None):
    """
    Возвращает словарь:
      {
        "color": "gray" | "green" | "orange" | "red" | "black",
        "months_left": int | None,
        "months_passed": int | None,
        "phase": str,   # человекочитаемая фаза
      }
    """
    today = today or date.today()
    result = {"color": "gray", "months_left": None,
              "months_passed": None, "phase": "нет данных"}

    if not start and not end:
        return result

    if start and today < start:
        # Стройка ещё не началась
        result["color"] = "gray"
        result["phase"] = "не начато"
        return result

    if start:
        result["months_passed"] = (today.year - start.year) * 12 + (today.month - start.month)

    if not end:
        result["color"] = "orange"
        result["phase"] = "строится"
        return result

    if today > end:
        result["color"] = "black"
        result["phase"] = "просрочено"
        return result

    months_left = (end.year - today.year) * 12 + (end.month - today.month)
    result["months_left"] = months_left

    if months_left <= 6:
        result["color"] = "red"
        result["phase"] = "скоро сдача"
    elif months_left <= 18:
        result["color"] = "orange"
        result["phase"] = "в процессе"
    else:
        result["color"] = "green"
        result["phase"] = "начальная стадия"

    return result


# ============================================================
# ЗАГРУЗКА СПИСКА + ДЕТАЛЕЙ
# ============================================================
def _fetch_ids():
    """Возвращает список (id, name) для детальной загрузки."""
    payload = _http_post_json(HOMEPORTAL_LIST_URL, HOMEPORTAL_PAYLOAD)
    objects = (payload.get("data", {})
                      .get("objects", {})
                      .get("data", []))
    result = []
    for obj in objects:
        oid = obj.get("id")
        if oid is not None:
            result.append(oid)
    return result


def _fetch_detail(oid):
    """Загружает детали одного объекта. Возвращает нормализованный dict или None."""
    try:
        payload = _http_get_json(HOMEPORTAL_DETAIL_URL.format(id=oid))
    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"[detail {oid}] ошибка: {e}")
        return None

    data = payload.get("data") or {}
    basic    = data.get("basicData") or {}
    location = data.get("locationData") or {}
    dev      = (data.get("companyData") or {}).get("developerData") or {}

    lat, lon = location.get("latitude"), location.get("longitude")
    if not lat or not lon:
        return None
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return None

    start = _parse_date(basic.get("start_date"))
    end   = _parse_date(basic.get("commissioning_date"))
    status_info = _compute_status(start, end)

    # authority приходит как объект {"id":..., "name":...}
    authority = basic.get("authority") or {}
    authority_name = authority.get("name") if isinstance(authority, dict) else authority

    return {
        "id": oid,
        "name": basic.get("name") or "—",
        "developer": dev.get("name") or "—",
        "address": basic.get("address") or "—",
        "lat": lat_f,
        "lon": lon_f,
        "authority": authority_name,
        "start_date": start.isoformat() if start else None,
        "end_date": end.isoformat() if end else None,
        "start_date_display": start.strftime("%d.%m.%Y") if start else None,
        "end_date_display":   end.strftime("%d.%m.%Y")   if end else None,
        # Готовые для клиента поля статуса:
        "phase": status_info["phase"],
        "color": status_info["color"],
        "months_left": status_info["months_left"],
        "months_passed": status_info["months_passed"],
        "construction_progress": basic.get("construction_progress"),
    }


def fetch_zhk_full():
    """Полный цикл: список + детали (параллельно). Возвращает список ЖК."""
    print("[api] Загружаем список ID ...")
    ids = _fetch_ids()
    print(f"[api] Получено ID: {len(ids)}")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_detail, oid): oid for oid in ids}
        for fut in as_completed(futures):
            item = fut.result()
            if item:
                results.append(item)

    # Сортируем по имени для стабильного порядка
    results.sort(key=lambda x: x["name"])
    print(f"[api] Успешно обработано: {len(results)}")
    return results


# ============================================================
# КЭШ (память + диск)
# ============================================================
def _load_disk_cache():
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if time.time() - obj.get("timestamp", 0) < CACHE_TTL_SECONDS:
            return obj
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _save_disk_cache(data):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"timestamp": time.time(), "data": data},
                      f, ensure_ascii=False)
    except OSError as e:
        print(f"[cache] не удалось сохранить: {e}")


def get_zhk(force_refresh=False):
    """Возвращает список ЖК из памяти / диска / сети."""
    now = time.time()

    if not force_refresh and _memory_cache["data"] is not None:
        age = now - _memory_cache["timestamp"]
        if age < CACHE_TTL_SECONDS:
            print(f"[cache] память (возраст {int(age)} с)")
            return _memory_cache["data"]

    if not force_refresh:
        disk = _load_disk_cache()
        if disk:
            print(f"[cache] диск (возраст {int(now - disk['timestamp'])} с)")
            _memory_cache["data"] = disk["data"]
            _memory_cache["timestamp"] = disk["timestamp"]
            return disk["data"]

    # Загружаем из сети
    data = fetch_zhk_full()
    _memory_cache["data"] = data
    _memory_cache["timestamp"] = now
    _save_disk_cache(data)
    return data


# ============================================================
# HTTP-ОБРАБОТЧИК
# ============================================================
class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/zhk"):
            # ?refresh=1 — принудительно обновить кэш
            force = "refresh=1" in self.path
            try:
                data = get_zhk(force_refresh=force)
                self._send_json(200, {
                    "success": True,
                    "count": len(data),
                    "data": data,
                })
            except Exception as e:
                print(f"[error] {e}")
                self._send_json(502, {
                    "success": False,
                    "message": f"Ошибка загрузки: {e}",
                    "data": [],
                })
            return
        return super().do_GET()

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # тихий режим


# ============================================================
# ЗАПУСК
# ============================================================
if __name__ == "__main__":
    port = 8000
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    httpd = HTTPServer(("", port), Handler)
    print(f"Сервер запущен:   http://localhost:{port}/")
    print(f"API ЖК:           http://localhost:{port}/api/zhk")
    print(f"Принудительный обновление кэша: /api/zhk?refresh=1")
    print("Ctrl+C для остановки.\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")