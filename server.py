#!/usr/bin/env python3
"""
HTTP-сервер со стримингом ЖК через SSE:
- /                    → index.html
- /api/zhk             → весь список сразу (для отладки)
- /api/zhk/stream      → SSE-поток: meta → zhk* → done

Фильтры:
1. constructionTypeName.ru == "Новое строительство"
2. constructionObjectCategoryName.ru содержит одно из ключевых слов жилья
"""

import json
import os
import re
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ============================================================
# КОНФИГ
# ============================================================
QPORTAL_SEARCH_URL = "https://gateway.qportal.kz/api/main/ProjectRegistry/search"
NOMINATIM_URL      = "https://nominatim.openstreetmap.org/search"

# ЗАМЕНИТЕ на свой email — требование политики Nominatim
USER_AGENT = "ZhkMapBot/1.0 (contact: ultraresonance@gmail.com)"

QPORTAL_PAYLOAD = {
    "startDate": "2026-10-04T13:30:08.221Z",
    "userLanguage": "ru",
    "skip": 0,
    "take": 500,
    "sortBy": None,
    "sortDesc": False,
    "location": "Астана",
    "constructionTypeId": None,
    "projectName": "многоквартирный жилой комплекс",
    "projectTypeId": "constructionProject",
}

CACHE_FILE = "geocode_cache.json"
NOMINATIM_DELAY = 1.1
NOMINATIM_RETRIES = 2
NOMINATIM_RETRY_PAUSE = 5

# ============================================================
# ФИЛЬТРЫ
# ============================================================
# Только "Новое строительство"
NEW_CONSTRUCTION_LABEL = "новое строительство"

# Ключевые слова для "жилой" категории объекта.
# Если в constructionObjectCategoryName.ru встречается хотя бы одно —
# считаем объект жилым.
RESIDENTIAL_KEYWORDS = [
    "жилой",
    "жилые",
    "жилых",
    "жилое",
    "жилая",
    "многоквартирн",
    "многофункциональн",   # МЖК — тоже жильё; уберите, если нужны только чистые ЖК
]

# Ключевые слова-исключения: если попались — объект точно НЕ жилой.
# Нужны как страховка, если "жилой" случайно есть в названии служебного объекта.
NON_RESIDENTIAL_KEYWORDS = [
    "сооружени",
    "инженерн",
    "сетей",
    "сети ",
    "электроснабжен",
    "теплоснабжен",
    "водоснабжен",
    "водоотведен",
    "канализац",
    "котельн",
    "гидротехническ",
    "административны",
    "транспорт",
    "автомобильн",
    # "паркинг",       # отдельный паркинг — не ЖК
    "детский сад",   # отдельный садик
    "школ",          # отдельная школа
    "больниц",
    "поликлиник",
]


def is_new_construction(it):
    """True, если вид строительства = 'Новое строительство'."""
    ctype = (it.get("constructionTypeName") or {}).get("ru", "").strip().lower()
    return ctype == NEW_CONSTRUCTION_LABEL


def is_residential(it):
    """True, если категория объекта относится к жилью."""
    cat = (it.get("constructionObjectCategoryName") or {}).get("ru", "").lower()
    if not cat:
        return False

    # Сначала проверяем исключения — они важнее
    if any(kw in cat for kw in NON_RESIDENTIAL_KEYWORDS):
        return False

    # Затем — что есть хотя бы один жилой маркер
    return any(kw in cat for kw in RESIDENTIAL_KEYWORDS)


# ============================================================
# HTTP-ХЕЛПЕРЫ
# ============================================================
def _http_post_json(url, payload, timeout=60):
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }, method="POST")
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# Регулярки для извлечения компонентов из адреса вида
# "Республика Казахстан, город Астана, район Нұра, проспект Тұран"
# или "...улица Е 429"
def parse_kz_address(address):
    """
    Разбирает казахстанский адрес на компоненты.
    Возвращает dict: {country, city, street, housenumber}
    """
    result = {
        "country": "Казахстан",
        "city": "Астана",
        "street": None,
        "housenumber": None,
    }

    if not address:
        return result

    # Убираем "Республика Казахстан", "город", "район ..." — они не нужны
    # Nominatim ищет по стране/городу/улице

    # Улица: ищем "улица <название>" или "проспект <название>"
    # Форматы: "улица Е 429", "проспект Тұран", "ул. Ш. Қалдаяқов"
    street_match = re.search(
        r'(?:улица|ул\.?|проспект|пр\.?|шоссе|пер\.?)\s+([^,]+)',
        address, re.IGNORECASE
    )
    if street_match:
        result["street"] = street_match.group(1).strip()

    # Номер дома: "дом 30/1", "д. 17/1", "уч. 10", "участок 42"
    house_match = re.search(
        r'(?:дом|д\.|уч\.?|участок)\s*(\d+(?:/\d+)?[А-Яа-я]?)',
        address, re.IGNORECASE
    )
    if house_match:
        result["housenumber"] = house_match.group(1).strip()

    return result

def _nominatim_geocode(address):
    """
    Структурированный запрос к Nominatim.
    Точнее и быстрее, чем свободный q=.
    """
    if not address or not address.strip():
        return None, None, None

    parsed = parse_kz_address(address)

    # Формируем параметры
    params = {
        "format": "json",
        "limit": 1,
        "addressdetails": 1,
        "countrycodes": "kz",          # Только Казахстан — сильно повышает точность
    }

    # Если извлекли улицу — используем structured query
    if parsed["street"]:
        # street для Nominatim = "<housenumber> <streetname>"[citation:1]
        if parsed["housenumber"]:
            params["street"] = f"{parsed['housenumber']} {parsed['street']}"
        else:
            params["street"] = parsed["street"]
        params["city"] = parsed["city"]
        # countrycodes достаточно, country не обязателен
    else:
        # Fallback: свободный запрос, но с countrycodes
        params["q"] = address

    url = NOMINATIM_URL + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})

    for attempt in range(NOMINATIM_RETRIES + 1):
        try:
            with urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not data:
                return None, None, None
            hit = data[0]
            return float(hit["lat"]), float(hit["lon"]), hit.get("display_name", "")
        except HTTPError as e:
            if e.code == 429 and attempt < NOMINATIM_RETRIES:
                print(f"   [nominatim] 429, ждём {NOMINATIM_RETRY_PAUSE} с")
                time.sleep(NOMINATIM_RETRY_PAUSE)
                continue
            print(f"   [nominatim] HTTP ошибка: {e.code} {e.reason}")
            return None, None, None
        except (URLError, json.JSONDecodeError) as e:
            print(f"   [nominatim] ошибка: {e}")
            return None, None, None

    return None, None, None


# ============================================================
# КЭШ ГЕОКОДИРОВАНИЯ
# ============================================================
def _load_geocache():
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_geocache(cache):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[cache] не удалось сохранить: {e}")


# ============================================================
# НОРМАЛИЗАЦИЯ
# ============================================================
def _normalize_item(it, lat, lon, display):
    """Превращает элемент qportal в объект для клиента."""
    pname = it.get("projectName") or {}
    loc   = it.get("location") or {}
    cust  = it.get("customerName") or {}
    des   = it.get("designerName") or {}
    stage = it.get("projectStageName") or {}
    cat   = it.get("constructionObjectCategoryName") or {}

    return {
        "id": it.get("projectId"),
        "code": it.get("projectCode"),
        "name": (pname.get("ru") or "").strip(),
        "address": (loc.get("ru") or "").strip(),
        "lat": lat,
        "lon": lon,
        "display_name": display,
        "customer": (cust.get("ru") or "").strip(),
        "designer": (des.get("ru") or "").strip(),
        "stage": (stage.get("ru") or "").strip(),
        "category": (cat.get("ru") or "").strip(),
    }


def _fetch_raw_items():
    """
    Загружает список проектов и применяет два фильтра:
      1. Новое строительство
      2. Жилая категория объекта
    """
    print("[qportal] Запрос списка ...")
    payload = _http_post_json(QPORTAL_SEARCH_URL, QPORTAL_PAYLOAD)
    items = payload.get("data", [])
    print(f"[qportal] Получено: {len(items)}")

    # Счётчики для логов
    after_new = 0
    result = []
    rejected_categories = {}

    for it in items:
        if not is_new_construction(it):
            continue
        after_new += 1

        if not is_residential(it):
            cat = (it.get("constructionObjectCategoryName") or {}).get("ru", "—")
            rejected_categories[cat] = rejected_categories.get(cat, 0) + 1
            continue

        result.append(it)

    print(f"[filter] После 'Новое строительство': {after_new}")
    print(f"[filter] После фильтра по категории:  {len(result)}")

    if rejected_categories:
        print("[filter] Отклонено по категории:")
        for cat, cnt in sorted(rejected_categories.items(), key=lambda x: -x[1]):
            print(f"         {cnt:>3} × {cat}")

    return result


# ============================================================
# СТРИМИНГ
# ============================================================
def stream_zhk():
    """Генератор SSE-событий."""
    def sse(event, data):
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    try:
        items = _fetch_raw_items()
    except Exception as e:
        yield sse("error", {"message": f"Ошибка qportal: {e}"})
        return

    total = len(items)
    yield sse("meta", {"total": total})

    cache = _load_geocache()
    shown = 0

    for i, it in enumerate(items):
        loc = it.get("location") or {}
        address = (loc.get("ru") or "").strip()

        cached = cache.get(address)
        if cached and len(cached) == 3 and cached[0] is not None:
            lat, lon, display = cached
        else:
            yield sse("progress", {"index": i + 1, "total": total, "address": address[:80]})
            lat, lon, display = _nominatim_geocode(address)
            cache[address] = [lat, lon, display]
            _save_geocache(cache)
            time.sleep(NOMINATIM_DELAY)

        if lat is None or lon is None:
            yield sse("skip", {"index": i + 1, "address": address[:80]})
            continue

        zhk = _normalize_item(it, lat, lon, display)
        shown += 1
        yield sse("zhk", zhk)

    yield sse("done", {"shown": shown, "total": total})


def build_full_list():
    """Синхронный вариант — для /api/zhk."""
    items = _fetch_raw_items()
    cache = _load_geocache()
    result = []
    for it in items:
        address = ((it.get("location") or {}).get("ru") or "").strip()
        cached = cache.get(address)
        if cached and len(cached) == 3 and cached[0] is not None:
            lat, lon, display = cached
        else:
            lat, lon, display = _nominatim_geocode(address)
            cache[address] = [lat, lon, display]
            _save_geocache(cache)
            time.sleep(NOMINATIM_DELAY)
        if lat is None or lon is None:
            continue
        result.append(_normalize_item(it, lat, lon, display))
    return result


# ============================================================
# HTTP-ОБРАБОТЧИК
# ============================================================
class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/zhk/stream":
            self._handle_stream()
            return

        if self.path == "/api/zhk":
            try:
                data = build_full_list()
                self._send_json(200, {"success": True, "count": len(data), "data": data})
            except Exception as e:
                self._send_json(502, {"success": False, "message": str(e), "data": []})
            return

        return super().do_GET()

    def _handle_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            for chunk in stream_zhk():
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            print("[sse] клиент отключился")
        except Exception as e:
            print(f"[sse] ошибка: {e}")
            try:
                err = f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"
                self.wfile.write(err.encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


# ============================================================
# ЗАПУСК
# ============================================================
if __name__ == "__main__":
    from socketserver import ThreadingMixIn

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    port = 8000
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    httpd = ThreadedHTTPServer(("", port), Handler)
    print(f"Сервер:         http://localhost:{port}/")
    print(f"SSE-поток:      http://localhost:{port}/api/zhk/stream")
    print(f"Полный список:  http://localhost:{port}/api/zhk")
    print("Ctrl+C для остановки.\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
