#!/usr/bin/env python3
"""
HTTP-сервер со стримингом ЖК через SSE + уведомления elicense.kz
о начале строительно-монтажных работ:
- /                    → index.html
- /api/zhk             → весь список сразу (для отладки)
- /api/zhk/stream      → SSE-поток: meta → zhk* → done

Хранение — SQLite (maptool.db, переопределяется MAPTOOL_DB):
кэш геокодирования, кэш БИН, компании и уведомления elicense.kz.
При первом запуске данные из старых JSON-файлов переносятся в базу
однократно, сами JSON остаются как резервная копия.

Уведомления elicense.kz:
- /api/elicense?bin=…            → уведомления по БИН (из кэша или живой поиск).
                                  Основной путь: по БИН сайт отдаёт полную
                                  историю.
- /api/elicense?licensiat=…      → поиск по названию заказчика, поле
                                  «Лицензиат». Запасной путь: сайт отдаёт
                                  для него только первую страницу выдачи, а
                                  БИН берётся из PDF уведомления.
- /api/elicense?bin=…&days=30    → срез за последние 30 дней (фильтр «Дата выдачи»)
- /api/elicense?bin=…&from=…&op=… → фильтр с даты, op: gte|gt|lte|lt|eq
- /api/elicense?bin=…&refresh=1  → принудительный живой поиск
- /api/elicense/stream?bin=…     → SSE-поток с прогрессом по попыткам капчи
- /api/elicense/enrich?bin=…     → дозагрузка адресов из PDF (без капчи)
- /api/elicense/match            → сопоставление уведомлений с ЖК
- /api/elicense/all              → сводка по накопленному хранилищу
- /api/elicense/timeline?bin=…   → хронология уведомлений
- /api/licenses                  → даты начала строительства по ЖК для карты

Проверка извлечения по паре «адрес + застройщик» (без геокодирования):
- /api/verify?address=…&developer=…  → нашлось ли уведомление о начале
  СМР по этому адресу у этого застройщика. Название разрешается в БИН
  (сначала кэш реестра qportal, затем DaData), далее поиск по БИН —
  сайт отдаёт по нему полную историю, тогда как по названию лишь
  первую страницу.
- /api/verify?…&bin=…               → если БИН известен наверняка

Фильтры ЖК:
1. constructionTypeName.ru == "Новое строительство"
2. constructionObjectCategoryName.ru содержит жилые ключевые слова

Поиск по названию заказчика на elicense.kz (поле «Лицензиат») доступен
отдельно — /api/elicense?licensiat=… — как запасной путь, если БИН
определить не удалось.
"""

import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import elicense_store
import zhk_match
import elicense_client
import refresh as refresh_mod
import telegram_bot as tg_mod
from storage import db, migrate_from_json

# ============================================================
# КОНФИГ
# ============================================================
QPORTAL_SEARCH_URL = "https://gateway.qportal.kz/api/main/ProjectRegistry/search"
NOMINATIM_URL      = "https://nominatim.openstreetmap.org/search"
DADATA_SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party_kz"

DADATA_API_KEY     = os.environ.get("DADATA_API_KEY", "")
USER_AGENT_CONTACT = os.environ.get("USER_AGENT_CONTACT", "")
USER_AGENT         = "ZhKhanBot/1.0 (contact: " + USER_AGENT_CONTACT + ")"


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

GEOCODE_CACHE_FILE = "geocode_cache.json"   # только для разового переноса
BIN_CACHE_FILE     = "bin_cache.json"       # только для разового переноса

NOMINATIM_DELAY         = 1.1
NOMINATIM_RETRIES       = 2
NOMINATIM_RETRY_PAUSE   = 5

DADATA_RETRIES          = 2
DADATA_RETRY_PAUSE      = 3    # если словим 429 — ждём 3 сек

# ============================================================
# ELICENSE
# ============================================================
ELICENSE_STORE_FILE = "elicense_data.json"   # только для разового переноса
ELICENSE_RETRIES    = int(os.environ.get("ELICENSE_RETRIES", "5"))   # попыток капчи на БИН
ELICENSE_MAX_PAGES  = int(os.environ.get("ELICENSE_MAX_PAGES", "20"))

# Окно «Дата выдачи» по умолчанию, в месяцах. 0 — без фильтра.
#
# Поиск по названию заказчика на elicense.kz отдаёт только первую
# страницу: пагинация нарисована на 8 страниц, но запрос ?page=2
# возвращает пустую страницу. Всё, что не влезло в первые 10 записей,
# просто не видно. Окно в три месяца укладывает выдачу в одну страницу.
# Переопределяется на запрос: ?months=6, ?days=30, ?from=… или ?all=1.
ELICENSE_MONTHS    = int(os.environ.get("ELICENSE_MONTHS", "3"))

# --- Тестовый режим -------------------------------------------------
# MAPTOOL_TEST=1: сервер не ходит в qportal, не геокодирует и не
# лезет на elicense.kz, а отдаёт заранее сохранённые ответы из
# testdata/. Нужен, чтобы проверять вёрстку и логику интерфейса
# без минутных ожиданий: обычный цикл проверки одной правки в JS —
# перезапуск сервера, прогрев кэша, ожидание SSE, и только потом
# браузер. Здесь всё это мгновенно и не зависит от внешних сервисов.
#
#   MAPTOOL_TEST=1 python server.py
#
# Фикстуры снимаются с живого сервера и кладутся рядом:
#   curl  http://localhost:8000/api/zhk     > testdata/zhk.json
#   curl  http://localhost:8000/api/licenses > testdata/licenses.json
TEST_MODE     = os.environ.get("MAPTOOL_TEST", "").strip().lower() \
                in ("1", "true", "yes", "on")
TESTDATA_DIR  = os.environ.get("MAPTOOL_TESTDATA", "testdata")
_fixture_cache = {}


def load_fixture(name):
    """Читает файл фикстуры (с кэшем на процесс). None, если файла нет."""
    if name in _fixture_cache:
        return _fixture_cache[name]

    path = os.path.join(TESTDATA_DIR, name)
    data = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[test] не прочитан {path}: {e}")
    else:
        print(f"[test] нет фикстуры {path}")
    _fixture_cache[name] = data
    return data


def fixture_zhk():
    """Список ЖК из фикстуры (тот же формат, что у build_full_list)."""
    return (load_fixture("zhk.json") or {}).get("data") or []


MATCH_RADIUS_M      = int(os.environ.get("MATCH_RADIUS_M", "300"))  # радиус гео-подбора
ZHK_CACHE_TTL       = int(os.environ.get("ZHK_CACHE_TTL", "600"))   # секунд жизни кэша ЖК

# --- Фоновое обновление и телеграм -----------------------------------
# Обновление данных раз в сутки по времени Астаны. Пока включено не
# всегда: REFRESH_ENABLED=0 выключает задание целиком.
REFRESH_ENABLED    = os.environ.get("REFRESH_ENABLED", "1").strip().lower() \
                     not in ("0", "false", "no", "off")
REFRESH_HOUR       = int(os.environ.get("REFRESH_HOUR", "3"))
REFRESH_MINUTE     = int(os.environ.get("REFRESH_MINUTE", "0"))

# Адрес для ссылок в сообщениях бота: без него ссылки не будет.
PUBLIC_URL         = (os.environ.get("PUBLIC_URL") or "").strip()

# При первом запуске обновления разослать нечего: объекты не новые,
# их просто раньше не показывали. Иначе при каждом деплое пользователям
# прилетала бы пачка «новых» объектов.
REFRESH_NOTIFY_ON_FIRST_RUN = os.environ.get(
    "REFRESH_NOTIFY_ON_FIRST_RUN", "0").strip().lower() in (
        "1", "true", "yes", "on")

_zhk_cache_data = None
_zhk_cache_time = 0.0
_zhk_cache_lock = threading.Lock()

# Браузер для eLicense нужен только при живом поиске, поднимаем лениво
_elicense_client = None


def _get_elicense_client():
    """Один общий клиент на процесс: Chromium тяжёлый, поднимать на каждый
    запрос дорого. Внутри клиента обращения сериализованы."""
    global _elicense_client
    if _elicense_client is None:
        _elicense_client = elicense_client.ElicenseClient(
            retries=ELICENSE_RETRIES,
            max_pages=ELICENSE_MAX_PAGES,
        )
    return _elicense_client


def _shutdown_elicense():
    """Закрывает браузер при остановке сервера."""
    global _elicense_client
    if _elicense_client is None:
        return
    print("Закрываю браузер eLicense...")
    try:
        _elicense_client.close()
    except Exception as e:
        print(f"  не удалось: {e}")
    _elicense_client = None


# ============================================================
# ФОНОВОЕ ОБНОВЛЕНИЕ И ТЕЛЕГРАМ
# ============================================================
_telegram_bot = None
_scheduler = None


def telegram_bot_instance():
    """Бот создаётся один раз на процесс."""
    global _telegram_bot
    if _telegram_bot is None:
        _telegram_bot = tg_mod.TelegramBot(db)
    return _telegram_bot


def start_telegram():
    """Запускает бота, если задан токен. Иначе сервер работает без него."""
    bot = telegram_bot_instance()
    if not bot.start():
        return None
    if not bot.token:
        return None

    # /status должен показывать реальное состояние, а не пустоту
    def summary():
        try:
            zhk = _zhk_cache_peek() or []
            lic = _licenses_snapshot()
            return {
                "zhk": len(zhk),
                "dated": len(lic.get("by_zhk") or {}),
                "developers": len({z.get("customer_bin")
                                   for z in zhk if z.get("customer_bin")}),
                "notifications": (lic.get("stats") or {}).get("notifications", 0),
                "last_refresh": _last_refresh_text(),
            }
        except Exception:
            return {}
    bot.status_provider = summary
    return bot


def _now_astana():
    return datetime.now(refresh_mod._tz())


def _last_refresh_text():
    row = db.query_one(
        "SELECT finished_at FROM refresh_log WHERE status='ok'"
        " ORDER BY id DESC LIMIT 1")
    if not row or not row["finished_at"]:
        return None
    try:
        return datetime.fromisoformat(row["finished_at"]).strftime(
            "%d.%m.%Y %H:%M")
    except ValueError:
        return row["finished_at"]


def _licenses_snapshot():
    """Сопоставление уведомлений с ЖК без обращения к сети."""
    zhk_list = _zhk_cache_peek()
    if not zhk_list:
        return {}
    try:
        return match_licenses_to_zhk(zhk_list, geocode=False)
    except Exception as e:
        print(f"[licenses] не собрать: {type(e).__name__}: {e}")
        return {}


def perform_refresh():
    """
    Один проход обновления: реестр проектов, затем уведомления.

    Порядок важен. Сначала обновляем список ЖК — из него берутся БИН
    застройщиков, по которым потом опрашивается elicense.kz.
    """
    global _zhk_cache_data, _zhk_cache_time

    print("[refresh] обновляю реестр проектов…")
    if TEST_MODE:
        zhk_list = fixture_zhk()
        print("[refresh] тестовый режим: беру фикстуру")
    else:
        # Сбрасываем кэш списка, иначе _zhk_cache вернёт старое
        with _zhk_cache_lock:
            _zhk_cache_data = None
            _zhk_cache_time = 0.0
        zhk_list = build_full_list()
        with _zhk_cache_lock:
            _zhk_cache_data = zhk_list
            _zhk_cache_time = time.time()
    print(f"[refresh] объектов: {len(zhk_list)}")

    # --- Уведомления о начале СМР по всем известным БИН ---
    bins = sorted({z["customer_bin"] for z in zhk_list
                   if z.get("customer_bin")})
    if bins:
        bins += [r["bin"] for r in db.query_all("SELECT bin FROM elicense_companies")
                 if r["bin"] not in bins]

    client = None if TEST_MODE else _get_elicense_client()
    total_new = 0
    for i, b in enumerate(bins, 1):
        if TEST_MODE:
            break
        print(f"[refresh] уведомления {i}/{len(bins)}: БИН {b}")
        try:
            before = db.query_one(
                "SELECT COUNT(*) c FROM elicense_notifications WHERE bin=?",
                (b,))["c"]
            # Полный поиск: он авторитетный и чистит исчезнувшие
            # документы. С фильтром даты данные бы просто дописывались.
            data = get_elicense_notifications(b, refresh=True)
            if not data.get("success"):
                print(f"[refresh]   не удалось: {data.get('error')}")
                continue
            after = db.query_one(
                "SELECT COUNT(*) c FROM elicense_notifications WHERE bin=?",
                (b,))["c"]
            if after > before:
                total_new += after - before
                # Новые уведомления почти всегда без адреса: подтягиваем PDF
                enrich_notifications(b)
        except Exception as e:
            print(f"[refresh]   БИН {b}: {type(e).__name__}: {e}")
    if not TEST_MODE and client is not None:
        try:
            client.close()
        except Exception:
            pass
        _elicense_client = None

    matched = _licenses_snapshot()
    notifications = elicense_store.all_notifications()

    return {
        "zhk": zhk_list,
        "notifications": notifications,
        "by_zhk": matched.get("by_zhk") or {},
        "stats": matched.get("stats") or {},
        "base_url": PUBLIC_URL,
    }


def start_background_jobs():
    """Поднимает планировщик и бота. В тестовом режиме — нет."""
    global _scheduler
    if TEST_MODE:
        print("[background] тестовый режим — фоновые задания не запускаются")
        return

    if not REFRESH_ENABLED:
        print("[background] обновление выключено (REFRESH_ENABLED=0)")
    else:
        _scheduler = refresh_mod.RefreshScheduler(
            refresh_fn=perform_refresh,
            bot=telegram_bot_instance(),
            db=db,
            hour=REFRESH_HOUR,
            minute=REFRESH_MINUTE,
        )
        if _scheduler.bot and not _scheduler.bot.token:
            _scheduler.bot = None
        _scheduler.first_run = not REFRESH_NOTIFY_ON_FIRST_RUN
        _scheduler.start()

    start_telegram()


def stop_background_jobs():
    global _scheduler
    if _scheduler:
        _scheduler.stop()
    if _telegram_bot:
        _telegram_bot.stop()


# ============================================================
# ФИЛЬТРЫ
# ============================================================
NEW_CONSTRUCTION_LABEL = "новое строительство"

RESIDENTIAL_KEYWORDS = [
    "жилой", "жилые", "жилых", "жилое", "жилая",
    "многоквартирн",
    "многофункциональн",
]

NON_RESIDENTIAL_KEYWORDS = [
    "сооружени", "инженерн", "сетей", "сети ",
    "электроснабжен", "теплоснабжен", "водоснабжен",
    "водоотведен", "канализац", "котельн", "гидротехническ",
    "административны", "транспорт", "паркинги",
    "детский сад", "школ", "больниц", "поликлиник",
]


def is_new_construction(it):
    ctype = (it.get("constructionTypeName") or {}).get("ru", "").strip().lower()
    return ctype == NEW_CONSTRUCTION_LABEL


def is_residential(it):
    cat = (it.get("constructionObjectCategoryName") or {}).get("ru", "").lower()
    if not cat:
        return False
    if any(kw in cat for kw in NON_RESIDENTIAL_KEYWORDS):
        return False
    return any(kw in cat for kw in RESIDENTIAL_KEYWORDS)


# ============================================================
# HTTP-ХЕЛПЕРЫ
# ============================================================
def _http_post_json(url, payload, headers=None, timeout=60):
    body = json.dumps(payload).encode("utf-8")
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if headers:
        h.update(headers)
    req = Request(url, data=body, headers=h, method="POST")
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
        # street для Nominatim = "<housenumber> <streetname>"
        if parsed["housenumber"]:
            params["street"] = f"{parsed['housenumber']} {parsed['street']}"
        else:
            params["street"] = parsed["street"]
        params["city"] = parsed["city"]
        # countrycodes достаточно, country не обязателен
    else:
        # Fallback: свободный запрос, но с countrycodes
        params["q"] = address

    # Контракт — всегда тройка: функцию используют и как геокодер для
    # zhk_match, где вызывающий делает `lat, lon, display = geocode(...)`.
    hit = _nominatim_request(params)
    return hit if hit else (None, None, None)


def _nominatim_request(params):
    """Один запрос к Nominatim. Возвращает (lat, lon, display) или None."""
    url = NOMINATIM_URL + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})

    for attempt in range(NOMINATIM_RETRIES + 1):
        try:
            with urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not data:
                return None
            hit = data[0]
            return (float(hit["lat"]), float(hit["lon"]),
                    hit.get("display_name", ""))
        except HTTPError as e:
            if e.code == 429 and attempt < NOMINATIM_RETRIES:
                time.sleep(NOMINATIM_RETRY_PAUSE)
                continue
            return None
        except (URLError, json.JSONDecodeError, KeyError, ValueError):
            return None
    return None


# Границы Астаны с запасом. Всё, что вне их, — ошибка геокодера:
# Байконыр это другой город (700 км), а «город Астана, район» без улицы
# Nominatim иногда отдаёт Синьцзян. Такой маркер хуже, чем отсутствие
# маркера, поэтому такие ответы отбрасываются.
ASTANA_BBOX = (50.70, 51.45, 71.00, 71.70)


def _in_astana(lat, lon):
    return (lat is not None and lon is not None
            and ASTANA_BBOX[0] <= lat <= ASTANA_BBOX[1]
            and ASTANA_BBOX[2] <= lon <= ASTANA_BBOX[3])


# Улица по-казахски. Суффиксы: көшесі — улица, даңғылы — проспект,
# жолы — шоссе. Формы «көшелерінің қиылысы» — пересечение улиц.
KK_STREET_SUFFIXES = (
    "көшелерінің", "көшелері", "көшесі", "даңғылы", "даңғылдары",
    "шағын жолы", "тас жолы", "жолы",
)

# Слова, которые в казахском названии идут перед улицей и адреса
# не обозначают: «Астана қаласы, Есіл ауданы, …»
KK_PREFIX_NOISE = re.compile(
    r"^(?:[\w.]+\s+)?(?:қаласы|ауданы|аудан|республикасы)\b[^,]*$",
    re.IGNORECASE)

# Адрес внутри projectName.ru — по той же формулировке, что и в PDF
RE_IN_NAME = re.compile(r"расположенн\w*\s+(?:по\s+адресу\s*:?|в\s+)", re.I)

RU_STREET_RE = re.compile(
    r"(?:улица|проспект|шоссе|переулок)\s+([^,]+)", re.IGNORECASE)


def _tr(value):
    return (value or {}).get("ru") if isinstance(value, dict) else (value or "")


def _tk(value):
    return (value or {}).get("kk") if isinstance(value, dict) else (value or "")


def kk_street_from_name(name_kk):
    """
    Вытаскивает казахское название улицы из projectName.kk.

    Строка вида «Астана қаласы, Есіл ауданы, Тұрар Рысқұлов көшесі,
    4-учаске…» режется по запятым и берётся кусок с суффиксом улицы.
    Разбор по запятым надёжнее регулярки: в названии много слов через
    пробел, а `\w` пробел не берёт, и регулярка вытаскивала один
    только суффикс — «көшесі» вместо «Тұрар Рысқұлов көшесі».
    """
    if not name_kk:
        return None

    for part in name_kk.split(","):
        part = re.sub(r"\s+", " ", part).strip(" .«»\"'")
        if not part:
            continue
        if any(part.endswith(sfx) for sfx in KK_STREET_SUFFIXES):
            if not KK_PREFIX_NOISE.match(part):
                return part

    return None


def geocode_candidates(item):
    """
    Варианты адреса для геокодирования — от точного к приблизительному.

    Нужен потому, что поле location часто содержит только район:
    у 38 объектов из 135 адреса не геокодировались, причём точный
    адрес лежал в projectName — по-русски и по-казахски.

    Русские названия улиц Nominatim по Казахстану почти не знает
    («Турара Рыскулова» не находит), а казахские находит («Тұрар
    Рысқұлов көшесі»). Поэтому казахский вариант идёт первым.
    """
    loc_ru = _tr(item.get("location")).strip()
    name_ru = _tr(item.get("projectName"))
    name_kk = _tk(item.get("projectName"))

    out = []

    # 1. Казахская улица из названия проекта — Nominatim её знает.
    street_kk = kk_street_from_name(name_kk)
    if street_kk:
        district = re.search(r"([^,]+)\s+ауданы", name_kk or "")
        q = street_kk + (f", {district.group(1)} ауданы" if district else "")
        out.append(q + ", Астана")

    # 2. Точный адрес из названия проекта, если это действительно адрес.
    m = RE_IN_NAME.search(name_ru)
    if m:
        addr = re.split(r"[»\"]", name_ru[m.end():])[0].strip(" .,")
        # Короткий фрагмент с улицей; длинный — это описание проекта,
        # и Nominatim по нему уходит куда угодно.
        if addr and len(addr) <= 120 and RU_STREET_RE.search(addr):
            out.append(addr)

    # 3. Поле location как есть.
    if loc_ru:
        out.append(loc_ru)

    # 4. Район — грубо, но лучше, чем ничего.
    district = loc_ru.split("город Астана,")[-1].strip(" ,")
    district = re.sub(r"^Республика Казахстан,?\s*", "", district)
    if district:
        out.append(f"Астана, {district}")

    # Убираем повторы, сохраняя порядок
    seen, uniq = set(), []
    for q in out:
        key = q.lower()
        if q and key not in seen:
            seen.add(key)
            uniq.append(q)
    return uniq


def geocode_zhk(item):
    """
    Геокодирует объект реестра, перебирая варианты адреса.
    Ответы вне границ Астаны отбрасываются.
    """
    for query in geocode_candidates(item):
        hit = _nominatim_request({
            "q": query,
            "format": "json",
            "limit": 1,
            "countrycodes": "kz",
            "accept-language": "ru",
        })
        if not hit:
            continue
        lat, lon, display = hit
        if _in_astana(lat, lon):
            return lat, lon, display, query
        # Вне Астаны — пробуем следующий вариант
    return None, None, None, None


def _dadata_get_bin(customer_name):
    """
    Возвращает БИН по названию заказчика через DaData,
    либо None, если не удалось найти.
    """
    if not customer_name or not customer_name.strip():
        return None

    # Ограничение API: query ≤ 300 символов
    query = customer_name.strip()[:300]

    headers = {
        "Authorization": f"Token {DADATA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"query": query, "count": 1}

    for attempt in range(DADATA_RETRIES + 1):
        try:
            data = _http_post_json(DADATA_SUGGEST_URL, payload,
                                   headers=headers, timeout=15)
            suggestions = data.get("suggestions") or []
            if not suggestions:
                return None
            bin_val = (suggestions[0].get("data") or {}).get("bin")
            return bin_val
        except HTTPError as e:
            if e.code == 429 and attempt < DADATA_RETRIES:
                print(f"   [dadata] 429, ждём {DADATA_RETRY_PAUSE} с")
                time.sleep(DADATA_RETRY_PAUSE)
                continue
            print(f"   [dadata] HTTP ошибка: {e.code} {e.reason}")
            return None
        except (URLError, json.JSONDecodeError) as e:
            print(f"   [dadata] ошибка: {e}")
            return None
    return None


# ============================================================
# КЭШИ (SQLite)
# ============================================================
# Раньше кэши жили в JSON-файлах и целиком перечитывались и переписывались
# после каждой вставки — это O(n) на операцию плюс гонка между потоками.
# Теперь это точечные UPSERT-запросы к индексированным таблицам.
def cache_get_geo(address):
    """(lat, lon, display) из кэша либо None."""
    if not address:
        return None
    row = db.query_one(
        "SELECT lat, lon, display FROM geocode_cache WHERE address = ?",
        (address,))
    if row is None or row["lat"] is None:
        return None
    return row["lat"], row["lon"], row["display"]


def cache_get_geo_checked(address):
    """
    Координаты из кэша, только если они внутри Астаны.

    Проверка нужна и на чтении: однажды в кэш попала точка в
    Мангистау (43.64, 51.40) — Nominatim так ищет «улица А 103»
    в Астане, а находит одноимённую улицу в другом регионе. Если
    такую запись не отбросить при чтении, она будет вечно выводиться
    на карту мимо всех проверок.
    """
    cached = cache_get_geo(address)
    if not cached:
        return None
    lat, lon, display = cached
    if not _in_astana(lat, lon):
        return None
    return cached


def cache_put_geo(address, lat, lon, display):
    db.execute(
        "INSERT INTO geocode_cache (address, lat, lon, display, updated_at)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(address) DO UPDATE SET lat=excluded.lat,"
        " lon=excluded.lon, display=excluded.display,"
        " updated_at=excluded.updated_at",
        (address, lat, lon, display, _now_iso()))


def cache_get_bin(customer):
    """
    ИИН по названию заказчика. Возвращает (значение, найдено):
    found=True нужна даже при None — чтобы не опрашивать DaData повторно.
    """
    if not customer:
        return None, False
    row = db.query_one("SELECT bin FROM bin_cache WHERE customer = ?",
                       (customer,))
    if row is None:
        return None, False
    return row["bin"], True


def cache_put_bin(customer, bin_value):
    db.execute(
        "INSERT INTO bin_cache (customer, bin, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(customer) DO UPDATE SET bin=excluded.bin,"
        " updated_at=excluded.updated_at",
        (customer, bin_value, _now_iso()))


def _now_iso():
    from datetime import datetime, timedelta
    return datetime.now().isoformat(timespec="seconds")


# ============================================================
# НОРМАЛИЗАЦИЯ
# ============================================================
def _normalize_item(it, lat, lon, display, customer_bin):
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
        "customer_bin": customer_bin,
        "designer": (des.get("ru") or "").strip(),
        "stage": (stage.get("ru") or "").strip(),
        "category": (cat.get("ru") or "").strip(),
    }


def _fetch_raw_items():
    print("[qportal] Запрос списка ...")
    payload = _http_post_json(QPORTAL_SEARCH_URL, QPORTAL_PAYLOAD)
    items = payload.get("data", [])
    print(f"[qportal] Получено: {len(items)}")

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
    def sse(event, data):
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    # Тестовый режим: отдаём фикстуру, никуда не ходя
    if TEST_MODE:
        items = fixture_zhk()
        yield sse("meta", {"total": len(items), "test": True})
        for i, zhk in enumerate(items, 1):
            yield sse("progress", {"index": i, "total": len(items),
                                   "address": (zhk.get("address") or "")[:80],
                                   "stage": "geo"})
            yield sse("zhk", zhk)
        _store_zhk_cache(items)
        yield sse("done", {"shown": len(items), "total": len(items)})
        return

    try:
        items = _fetch_raw_items()
    except Exception as e:
        yield sse("error", {"message": f"Ошибка qportal: {e}"})
        return

    total = len(items)
    yield sse("meta", {"total": total})

    shown = 0
    collected = []

    for i, it in enumerate(items):
        loc = it.get("location") or {}
        address = (loc.get("ru") or "").strip()
        customer = ((it.get("customerName") or {}).get("ru") or "").strip()

        # --- Геокодирование ---
        cached = cache_get_geo_checked(address)
        if cached:
            lat, lon, display = cached
        else:
            yield sse("progress", {"index": i + 1, "total": total,
                                   "address": address[:80], "stage": "geo"})
            # Каскад вариантов: поле location часто содержит только
            # район, и точная улица лежит в projectName — по-русски
            # и по-казахски. Плюс проверка, что попали в Астану.
            lat, lon, display, _used = geocode_zhk(it)
            cache_put_geo(address, lat, lon, display)
            time.sleep(NOMINATIM_DELAY)

        # --- Определение БИН заказчика ---
        bin_val = None
        if customer:
            bin_val, cached_bin = cache_get_bin(customer)
            if not cached_bin:
                yield sse("progress", {"index": i + 1, "total": total,
                                       "address": customer[:80], "stage": "bin"})
                bin_val = _dadata_get_bin(customer)
                cache_put_bin(customer, bin_val)
                # DaData лимит 30 запросов/сек — пауза не нужна,
                # но ставим небольшую для вежливости
                time.sleep(0.15)

        if lat is None or lon is None:
            yield sse("skip", {"index": i + 1, "address": address[:80]})
            continue

        zhk = _normalize_item(it, lat, lon, display, bin_val)
        shown += 1
        collected.append(zhk)
        yield sse("zhk", zhk)

    # Кладём собранный список в кэш: фронтенд сразу после события done
    # дёргает /api/licenses, и ему не приходится ждать повторной сборки
    # (это ещё один заход по Nominatim с паузами на каждый новый адрес).
    _store_zhk_cache(collected)

    yield sse("done", {"shown": shown, "total": total})


def build_full_list():
    if TEST_MODE:
        return fixture_zhk()

    items = _fetch_raw_items()
    result = []
    for it in items:
        address = ((it.get("location") or {}).get("ru") or "").strip()
        customer = ((it.get("customerName") or {}).get("ru") or "").strip()

        cached = cache_get_geo_checked(address)
        if cached:
            lat, lon, display = cached
        else:
            # Тот же каскад, что и в stream_zhk: поле location часто
            # содержит только район, а точный адрес лежит в projectName.
            lat, lon, display, _used = geocode_zhk(it)
            cache_put_geo(address, lat, lon, display)
            time.sleep(NOMINATIM_DELAY)

        bin_val, cached_bin = cache_get_bin(customer)
        if not cached_bin:
            bin_val = _dadata_get_bin(customer)
            cache_put_bin(customer, bin_val)
            time.sleep(0.15)

        if lat is None or lon is None:
            continue
        result.append(_normalize_item(it, lat, lon, display, bin_val))
    return result


def _store_zhk_cache(items):
    """Кладёт готовый список ЖК в кэш, чтобы /api/licenses не пересобирал."""
    global _zhk_cache_data, _zhk_cache_time
    with _zhk_cache_lock:
        _zhk_cache_data = items
        _zhk_cache_time = time.time()


def _zhk_cache_peek():
    """Возвращает список из кэша или None — без постройки."""
    with _zhk_cache_lock:
        if _zhk_cache_data is None:
            return None
        if (time.time() - _zhk_cache_time) >= ZHK_CACHE_TTL:
            return None
        return _zhk_cache_data


def _zhk_cache():
    """Кэш списка ЖК в памяти: сборка стоит запроса к qportal плюс
    геокодирование, а нужна она и SSE, и /api/licenses."""
    global _zhk_cache_data, _zhk_cache_time

    with _zhk_cache_lock:
        if _zhk_cache_data is not None and (
                time.time() - _zhk_cache_time) < ZHK_CACHE_TTL:
            return _zhk_cache_data

    data = build_full_list()

    with _zhk_cache_lock:
        _zhk_cache_data = data
        _zhk_cache_time = time.time()

    return data


# ============================================================
# ELICENSE: УВЕДОМЛЕНИЯ О НАЧАЛЕ СМР
# ============================================================
class _SseQueue:
    """
    Мост между потоком браузера и потоком отправки SSE.

    Поиск идёт в отдельном потоке (он блокирующий и долгий), а генератор
    SSE в это время раздаёт клиенту события прогресса.
    """

    def __init__(self):
        self._q = queue.Queue()

    def put(self, **item):
        self._q.put(item)

    def progress(self, info):
        """Вызывается из ElicenseClient.search() после каждой попытки."""
        self._q.put({"progress": info})

    def get(self):
        return self._q.get()


def _parse_date_filter(query):
    """
    Достаёт фильтр «Дата выдачи» из параметров запроса.

    Поддерживаются записи — удобнее для ручной отладки:
        ?days=30            — за последние 30 дней
        ?months=3           — за последние N месяцев (по календарю)
        ?from=2026-09-11    — начиная с конкретной даты
        ?from=11.09.2026    — то же в привычном формате
        ?all=1              — без фильтра, вся история

    Если ничего не задано и ?all= не указан, подставляется окно по
    умолчанию ELICENSE_MONTHS (по умолчанию 3 месяца).

    Причина умолчания: поиск по названию заказчика на elicense.kz
    отдаёт только первую страницу выдачи — вторая не открывается,
    хотя пагинация на сайте нарисована. Записи старше одной страницы
    просто не видны. Ограничение окна делает результат заведомо
    помещающимся в одну страницу.

    Возвращает 'ГГГГ-ММ-ДД' или None.
    """
    from elicense_client import days_ago, iso_date, months_ago

    if (query.get("all") or [""])[0].strip().lower() in ("1", "true", "yes"):
        return None

    days = (query.get("days") or [""])[0].strip()
    if days:
        try:
            return days_ago(int(days))
        except (TypeError, ValueError):
            pass

    months = (query.get("months") or [""])[0].strip()
    if months:
        try:
            return months_ago(int(months))
        except (TypeError, ValueError):
            pass

    raw = (query.get("from") or [""])[0].strip()
    if raw:
        return iso_date(raw)

    if ELICENSE_MONTHS > 0:
        return months_ago(ELICENSE_MONTHS)
    return None


def _slice_by_date(entry, date_from, date_equality):
    """
    Отрезает запись кэша по «Дате выдачи».
    Операторы те же, что у фильтра на сайте.
    """
    notifications = entry.get("notifications") or []
    if not date_from:
        return notifications

    def keep(item):
        d = item.get("issue_date_iso") or ""
        if not d:
            return False
        if date_equality == "eq":
            return d == date_from
        if date_equality == "gt":
            return d > date_from
        if date_equality == "lt":
            return d < date_from
        if date_equality == "lte":
            return d <= date_from
        return d >= date_from

    return [n for n in notifications if keep(n)]


def get_elicense_notifications(bin_number, refresh=False, date_from=None,
                               date_equality="gte"):
    """
    Отдаёт уведомления о начале СМР по БИН.

    Сначала смотрим хранилище: кэш содержит полную историю, и срез по
    дате проще отрезать из него — тогда запрос не требует ни браузера,
    ни капчи. Живой поиск делаем только когда кэша нет или задан
    refresh: сайт при таком поиске тоже отдаёт лишь первую страницу
    выдачи, поэтому кэш надёжнее.
    """
    bin_number = str(bin_number or "").strip()

    if not refresh:
        cached = elicense_store.get_entry(bin_number)
        if cached is not None:
            notifications = _slice_by_date(cached, date_from, date_equality)
            if notifications or not date_from:
                # **cached идёт первым: перечисленные ниже поля должны
                # перекрыть полную историю срезом по дате.
                return {
                    **cached,
                    "success": True,
                    "source": "cache",
                    "notifications": notifications,
                    "date_from": date_from,
                    "date_equality": date_equality if date_from else None,
                    "stored_total": len(cached.get("notifications") or []),
                }

    client = _get_elicense_client()
    result = client.search(bin_number, date_from=date_from,
                           date_equality=date_equality)

    if not result.get("success"):
        return result

    if date_from:
        # Отвечаем именно срезом, который запросили, а не всей историей.
        # В хранилище при этом данные дописываются, а не заменяются —
        # иначе запрос «за 30 дней» стёр бы накопленное.
        elicense_store.save_result(result)
        entry = elicense_store.get_entry(bin_number) or {}
        return {
            "success": True,
            "source": "live",
            "bin": bin_number,
            "company": result.get("company"),
            "date_from": result.get("date_from"),
            "date_equality": result.get("date_equality"),
            "attempts": result.get("attempts"),
            "pages": result.get("pages"),
            "stats": result.get("stats"),
            "notifications": result.get("notifications") or [],
            "stored_total": len(entry.get("notifications") or []),
        }

    elicense_store.save_result(result)

    entry = elicense_store.get_entry(bin_number) or {}
    return {"success": True, "source": "live", **entry}


def get_elicense_by_licensiat(licensiat, date_from=None, date_equality="gte",
                              with_pdf=True):
    """
    Уведомления по наименованию заказчика — без знания БИН.

    Название берётся прямо из реестра проектов qportal (поле customer),
    поэтому определять БИН заранее не нужно: он извлекается из PDF.
    Это снимает промежуточный шаг с DaData.

    В выдаче сайта БИН заказчика нет — только название, поэтому БИН
    появляется лишь после разбора PDF. Хранилище ключуется по БИН,
    и результат раскладывается по БИН уже на этом шаге.

    Возвращает уведомления вместе с resolved_bins: сколько уведомлений
    легло на каждый БИН.
    """
    licensiat = str(licensiat or "").strip()
    if not licensiat:
        raise ValueError("Пустое наименование заказчика")

    client = _get_elicense_client()
    result = client.search(licensiat=licensiat, date_from=date_from,
                           date_equality=date_equality)

    if not result.get("success"):
        return result

    notifications = result.get("notifications") or []

    # Поиск по названию на elicense.kz нечёткий: под «Asyll Dala» сайт
    # отдаёт ещё и «Gold Stroy (ЖК Астана)». Настоящий заказчик виден
    # только в PDF — там печатается полное наименование и БИН.
    #
    # БИН заявителя есть лишь в PDF. Страница карточки и файлы отдают
    # документы без капчи, поэтому добираем их отдельно.
    if with_pdf and notifications:
        notifications = client.enrich_notifications(notifications,
                                                    only_smr=True)

    # Оставляем только уведомления о начале СМР: остальные виды
    # деятельности для даты начала строительства не нужны.
    smr = [n for n in notifications
           if elicense_client.NOTIFICATION_MARKER
           in (n.get("activity") or "").lower()]

    # Поиск по названию на elicense.kz нечёткий: под «Asyll Dala» сайт
    # отдаёт ещё и уведомления «Gold Stroy (ЖК Астана)». Настоящий
    # заказчик виден только в PDF — там печатается полное наименование.
    own, foreign = [], {}
    for n in smr:
        applicant = n.get("applicant")
        if not applicant or zhk_match.name_similarity(licensiat,
                                                       applicant) >= 0.6:
            own.append(n)
        else:
            bin_val = re.sub(r"\D", "", str(n.get("applicant_bin") or ""))
            foreign[bin_val or "без БИН"] = \
                foreign.get(bin_val or "без БИН", 0) + 1

    by_bin = {}
    without_bin = []
    for n in own:
        bin_val = re.sub(r"\D", "", str(n.get("applicant_bin") or ""))
        if bin_val:
            by_bin.setdefault(bin_val, []).append(n)
        else:
            without_bin.append(n)

    # Название из PDF — источник истины, а не то, что стояло в запросе.
    for bin_val, items in by_bin.items():
        names = [n.get("applicant") for n in items if n.get("applicant")]
        company = names[0] if names else (result.get("company") or licensiat)
        # merge=True: выдача уже подрезана фильтром «только начало СМР»
        # и не является полной историей застройщика.
        elicense_store.save_result({
            **result,
            "bin": bin_val,
            "notifications": items,
            "company": company,
        }, merge=True)

    return {
        "success": True,
        "source": "live",
        "licensiat": licensiat,
        "attempts": result.get("attempts"),
        "pages": result.get("pages"),
        "stats": result.get("stats"),
        "date_from": result.get("date_from"),
        "date_equality": result.get("date_equality"),
        "resolved_bins": {b: len(v) for b, v in by_bin.items()},
        "without_bin": len(without_bin),
        "foreign_companies": foreign,
        "truncated": bool(result.get("truncated")),
        "truncated_note": (
            "Сайт отдал только первую страницу выдачи: пагинация обещает "
            "больше, но ?page=2 с фильтром «Лицензиат» возвращает пустую "
            "страницу. Уже выданные записи верны, но более старые могут "
            "не попасть в результат — сузьте окно даты."
            if result.get("truncated") else None),
        "notifications": own,
    }


def resolve_developer_bin(developer):
    """
    Название застройщика -> БИН.

    Сначала кэш (в том числе bin_cache, который наполняется при сборе ЖК
    из реестра qportal), затем DaData.

    Это основной путь. Поиск по названию на elicense.kz (поле «Лицензиат»)
    оставлен запасным: он отбирает компанию точно, но сайт отдаёт для
    него только первую страницу выдачи — ?page=2 возвращает пустую
    страницу, хотя пагинация нарисована на все. По БИН сайт отдаёт
    полную историю, поэтому поиск по БИН надёжнее.
    """
    developer = str(developer or "").strip()
    if not developer:
        return None, None

    bin_val, cached = cache_get_bin(developer)
    if cached:
        return bin_val, "cache"

    bin_val = _dadata_get_bin(developer)
    if bin_val:
        cache_put_bin(developer, bin_val)
    return bin_val, "dadata"


def verify_address(address, developer, refresh=False, date_from=None,
                   date_equality="gte", threshold=None, bin_number=None):
    """
    Проверка: есть ли уведомление о начале СМР по заданному адресу
    у заданного застройщика.

    Шаги:
      1. название застройщика -> БИН (кэш реестра qportal, затем DaData);
      2. поиск уведомлений по этому БИН (кэш базы или живой поиск);
      3. дозагрузка данных из PDF: там наименование объекта и адрес;
      4. сверка адреса из запроса с адресом каждого уведомления.

    Поиск по БИН, а не по названию: elicense отдаёт для фильтра
    «Лицензиат» только первую страницу выдачи — пагинация обещает
    больше, но ?page=2 возвращает пустую страницу. По БИН сайт отдаёт
    полную историю.

    licensiat передаётся только если БИН определить не удалось: тогда
    ищем по названию, а БИН заявителя берём из PDF.

    Геокодирование не используется намеренно: сверяются тексты адресов,
    а не координаты — это проверка извлечения, а не построение карты.

    Возвращает подробный отчёт: что нашлось, с какой оценкой совпадения
    и почему признано несовпадением.
    """
    address = str(address or "").strip()
    developer = str(developer or "").strip()

    report = {
        "success": False,
        "query": {"address": address, "developer": developer},
        "resolved": None,
        "search": None,
        "verdict": None,
        "matches": [],
        "others": [],
    }

    if not address:
        report["error"] = "Не указан параметр ?address="
        return report

    # --- 1. БИН застройщика ---
    bin_val = str(bin_number or "").strip() or None
    live = False
    data = None

    if bin_val:
        source = "БИН из запроса"
    elif developer:
        # Основной путь: название -> БИН через кэш реестра или DaData.
        bin_val, source = resolve_developer_bin(developer)
        if not bin_val:
            report["error"] = (
                f"Не удалось определить БИН по названию {developer!r}. "
                f"Укажите ?bin= явно.")
            return report
    else:
        report["error"] = ("Нужен застройщик: передайте название "
                           "заказчика или БИН явно.")
        return report

    if refresh or elicense_store.get_entry(bin_val) is None:
        data = get_elicense_notifications(bin_val, refresh=refresh,
                                          date_from=date_from,
                                          date_equality=date_equality)
        if not data.get("success"):
            report["error"] = data.get("error") or "Поиск не удался"
            return report
        live = True

    # --- 2. собираем уведомления по найденному БИН ---
    notifications = []
    companies = {}
    attempts = None

    entry = elicense_store.get_entry(bin_val) or {}
    items = entry.get("notifications") or []
    if items and not any(n.get("address") for n in items):
        items = enrich_notifications(bin_val).get("notifications") or items
    notifications.extend(items)
    if entry.get("company"):
        companies[bin_val] = entry["company"]
    attempts = entry.get("attempts")

    report["resolved"] = {
        "bin": bin_val,
        "bins": [bin_val],
        "companies": companies,
        "source": source,
    }

    report["search"] = {
        "source": "live" if live else "cache",
        "total_records": len(notifications),
        "with_address": sum(1 for n in notifications if n.get("address")),
        "attempts": attempts,
    }

    if not notifications:
        report["verdict"] = {
            "matched": False,
            "reason": f"У застройщика {developer or bin_val} нет уведомлений "
                      f"о начале СМР",
            "similarity": 0.0,
            "checked": 0,
        }
        report["success"] = True
        return report

    # --- 4. сверка адресов ---
    thr = (threshold if threshold is not None
           else zhk_match.ADDRESS_MATCH_THRESHOLD)

    # Веса слов строим по корпусу самих уведомлений: без них общие слова
    # района («Астана», «Есиль»�� дают ложные совпадения между разными
    # улицами.
    comparable = [n["address"] for n in notifications if n.get("address")]
    weights = zhk_match.build_token_weights(comparable)

    found = zhk_match.best_address_match(address, notifications,
                                         threshold=thr, weights=weights)

    def _describe(notification):
        return {
            "nikad": notification.get("nikad"),
            "unique_number": notification.get("unique_number"),
            "issue_date": notification.get("issue_date_iso"),
            "issue_date_text": notification.get("issue_date"),
            "received_date": notification.get("received_date"),
            "status": notification.get("status"),
            "activity": notification.get("activity"),
            "object_name": notification.get("object_name"),
            "address": notification.get("address"),
            "cadastral": notification.get("cadastral"),
            "similarity": round(
                zhk_match.weighted_similarity(address,
                                              notification.get("address"),
                                              weights), 3),
            "similarity_plain": round(
                zhk_match.address_similarity(address,
                                             notification.get("address")), 3),
            "detail_url": notification.get("detail_url"),
        }

    matches = [_describe(n) for n in found["matched_list"]]

    matched_ids = {n.get("unique_number") or n.get("nikad")
                   for n in found["matched_list"]}
    others = [_describe(n) for n in notifications
              if (n.get("unique_number") or n.get("nikad")) not in matched_ids]

    if found["matched"]:
        reason = (f"Адрес совпадает с уведомлением от {found['similarity']} "
                  f"по схожести (порог {thr})")
    elif not comparable:
        reason = ("У застройщика есть уведомления о начале СМР, но ни в одном "
                  "не удалось извлечь адрес из PDF")
    elif found["best"] is None:
        reason = ("Адреса из уведомлений есть, но ни одно слово не совпало с "
                  "запрошенным адресом — скорее всего это другой объект")
    else:
        reason = (f"Ближайшее совпадение лишь {found['similarity']} "
                  f"(порог {thr}) — адрес, вероятно, другой")

    report["verdict"] = {
        "matched": found["matched"],
        "reason": reason,
        "similarity": found["similarity"],
        "threshold": thr,
        "checked": len(notifications),
        "comparable": len(comparable),
    }
    report["matches"] = matches
    report["others"] = others[:20]
    report["success"] = True
    return report


def enrich_notifications(bin_number, on_progress=None, only_smr=True):
    """
    Дозагружает данные из PDF-уведомлений по уже сохранённому БИН.

    Капча не нужна: страница карточки и файлы документов отдают их
    и без неё. Сначала качает русский PDF, потом разбирает его.

    only_smr=True — качаем PDF только для уведомлений о начале СМР:
    для даты начала стройки остальные виды деятельности не нужны,
    а запросов получается в разы меньше.
    """
    entry = elicense_store.get_entry(bin_number)
    if entry is None:
        raise KeyError(f"Нет сохранённых данных по БИН {bin_number}")

    notifications = entry.get("notifications") or []
    if not notifications:
        return {"bin": bin_number, "enriched": 0, "notifications": [],
                "with_address": 0, "errors": []}

    client = _get_elicense_client()

    def progress(i, total, item):
        if on_progress:
            on_progress(i, total, item)

    enriched = client.enrich_notifications(notifications,
                                          on_item=progress, only_smr=only_smr)
    elicense_store.update_notifications(bin_number, enriched)

    errors = [{"nikad": n.get("nikad"), "error": n["pdf_error"]}
              for n in enriched if n.get("pdf_error")]

    return {
        "bin": bin_number,
        "enriched": sum(1 for n in enriched if n.get("address")),
        "checked": len(notifications),
        "with_address": sum(1 for n in enriched if n.get("address")),
        "errors": errors,
        "notifications": enriched,
    }


def match_licenses_to_zhk(zhk_list=None, geocode=True):
    """
    Сопоставляет накопленные уведомления с ЖК.
    Основной путь — БИН, запасной — геокодирование адреса из PDF.
    """
    if zhk_list is None:
        zhk_list = _zhk_cache()

    notifications = elicense_store.all_notifications()

    # Геокодер нужен только когда БИН не совпал — подключаем лениво,
    # чтобы не платить за сетевые запросы, если все нашлись по БИН.
    geocoder = _nominatim_geocode if geocode else None

    return zhk_match.match_notifications(notifications, zhk_list,
                                        geocoder, MATCH_RADIUS_M)


def stream_elicense(bin_number):
    """
    SSE-версия живого поиска. События:
        progress    — каждая попытка ввода капчи (статус: solving/ok/
                      captcha_failed/empty/no_results/error)
        meta        — итог: БИН, компания, число страниц, всего уведомлений
        notification— по одному уведомлению, уже в хронологическом порядке
        done        — сколько отдано
    """
    def sse(event, data):
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    try:
        client = _get_elicense_client()
    except Exception as e:
        yield sse("error", {"message": f"Не удалось запустить браузер: {e}"})
        return

    # Пишем события прямо в сокет из колбэка — поток вызова блокируется
    # на время поиска, но клиент видит прогресс по мере попыток.
    queue = _SseQueue()

    def worker():
        try:
            queue.put(result=client.search(bin_number, on_attempt=queue.progress))
        except Exception as e:
            queue.put(result={"success": False, "error": str(e)})
        finally:
            queue.put(done=True)

    threading.Thread(target=worker, daemon=True).start()

    while True:
        item = queue.get()
        if item.get("done"):
            break

        if item.get("progress"):
            yield sse("progress", item["progress"])
            continue

        result = item.get("result") or {}

        if not result.get("success"):
            yield sse("error", {"message": result.get("error") or "Поиск не удался",
                                "history": result.get("history", [])})
            return

        elicense_store.save_result(result)

        yield sse("meta", {
            "bin": result["bin"],
            "company": result.get("company"),
            "attempts": result.get("attempts"),
            "pages": result.get("pages"),
            "total": len(result.get("notifications") or []),
            "history": result.get("history", []),
        })

        for n in result.get("notifications") or []:
            yield sse("notification", n)

        yield sse("done", {"shown": len(result.get("notifications") or [])})
        return


# ============================================================
# HTTP-ОБРАБОТЧИК
# ============================================================
class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        # Роутим по path без query-строки, чтобы /api/zhk/?bin=… работали так же
        path = urlsplit(self.path).path.rstrip("/") or "/"

        if path == "/api/zhk/stream":
            self._handle_stream()
            return
        if path == "/api/zhk":
            try:
                data = build_full_list()
                self._send_json(200, {"success": True, "count": len(data), "data": data})
            except Exception as e:
                self._send_json(502, {"success": False, "message": str(e), "data": []})
            return
        if path == "/api/elicense/stream":
            self._handle_elicense_stream()
            return
        if path in ("/api/elicense", "/api/elicense/all",
                    "/api/elicense/timeline", "/api/elicense/enrich",
                    "/api/elicense/match"):
            self._handle_elicense()
            return
        if path == "/api/verify":
            self._handle_verify()
            return
        if path == "/api/licenses":
            self._handle_licenses()
            return
        if path in ("/api/refresh", "/api/telegram"):
            self._handle_refresh()
            return
        return super().do_GET()

    # ---------------------------------------------------------
    # ФОНОВОЕ ОБНОВЛЕНИЕ И ТЕЛЕГРАМ
    # ---------------------------------------------------------
    def _handle_refresh(self):
        """
        /api/telegram                 — состояние подписок и бота
        /api/refresh                  — журнал последних запусков
        /api/refresh?run=1            — прогнать обновление сейчас
        /api/telegram?test=1          — отправить тестовое сообщение
        """
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        flag = lambda name: (query.get(name) or ["0"])[0].strip().lower() \
            in ("1", "true", "yes", "on")

        if path == "/api/telegram" and flag("test"):
            bot = telegram_bot_instance()
            if not bot.token:
                self._send_json(503, {"success": False,
                                      "message": "TELEGRAM_BOT_TOKEN не задан"})
                return
            try:
                sent = bot.broadcast(
                    "<b>Проверка связи</b>\nЭто тестовое сообщение бота.")
            except Exception as e:
                self._send_json(502, {"success": False,
                                      "message": f"{type(e).__name__}: {e}"})
                return
            self._send_json(200, {"success": True, "sent": sent,
                                  "subscribers": bot.stats()})
            return

        if path == "/api/telegram":
            bot = telegram_bot_instance()
            users = [dict(r) for r in db.query_all(
                "SELECT chat_id, username, active, created_at FROM"
                " telegram_users ORDER BY created_at DESC LIMIT 200")]
            self._send_json(200, {
                "success": True,
                "bot_configured": bool(bot.token),
                "bot_username": bot._bot_username,
                "subscribers": bot.stats(),
                "last_refresh": _last_refresh_text(),
                "schedule": (f"{REFRESH_HOUR:02d}:{REFRESH_MINUTE:02d}"
                             f" {refresh_mod.ASTANA_TZ}") if REFRESH_ENABLED
                            else "выключено",
                "users": users,
            })
            return

        # /api/refresh
        if flag("run"):
            if TEST_MODE:
                self._send_json(200, {"success": True, "message":
                                      "тестовый режим: обновление не выполняется"})
                return
            if _scheduler is None:
                self._send_json(409, {"success": False,
                                      "message": "планировщик не запущен"})
                return
            try:
                result = _scheduler.run_now(force=True)
            except Exception as e:
                self._send_json(502, {"success": False,
                                      "message": f"{type(e).__name__}: {e}"})
                return
            self._send_json(200, {"success": True, "result": {
                "zhk": len((result or {}).get("zhk") or []),
                "notifications": len((result or {}).get("notifications") or []),
            }})
            return

        rows = [dict(r) for r in db.query_all(
            "SELECT started_at, finished_at, status, new_zhk, new_smr,"
            " notified, message FROM refresh_log ORDER BY id DESC LIMIT 30")]
        self._send_json(200, {
            "success": True,
            "enabled": REFRESH_ENABLED and not TEST_MODE,
            "schedule": (f"{REFRESH_HOUR:02d}:{REFRESH_MINUTE:02d}"
                         f" {refresh_mod.ASTANA_TZ}"),
            "next_run_in_s": (refresh_mod.seconds_until_next_run()
                              if REFRESH_ENABLED and not TEST_MODE else None),
            "log": rows,
        })

    # ---------------------------------------------------------
    # ELICENSE
    # ---------------------------------------------------------
    def _handle_elicense(self):
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        bin_number = (query.get("bin") or [""])[0].strip()
        licensiat = (query.get("licensiat") or [""])[0].strip()
        refresh = (query.get("refresh") or ["0"])[0] in ("1", "true", "yes")

        if path == "/api/elicense/all":
            self._send_json(200, {"success": True,
                                  **elicense_store.summary()})
            return

        # Этот маршрут не требует БИН — проверяем его до общей валидации
        if path == "/api/elicense/match":
            try:
                data = match_licenses_to_zhk(geocode=not refresh)
            except Exception as e:
                self._send_json(502, {"success": False, "message": str(e)})
                return
            self._send_json(200, {"success": True, **data})
            return

        if not bin_number and not licensiat:
            self._send_json(400, {"success": False,
                                  "message": "Нужен параметр ?bin= "
                                             "или ?licensiat="})
            return

        # Поиск по названию заказчика: БИН заранее не нужен,
        # он извлекается из PDF уведомления.
        if licensiat and not bin_number:
            try:
                data = get_elicense_by_licensiat(
                    licensiat,
                    date_from=_parse_date_filter(query),
                    date_equality=(query.get("op") or ["gte"])[0])
            except Exception as e:
                self._send_json(502, {"success": False, "message": str(e)})
                return
            status = 200 if data.get("success") else 504
            self._send_json(status, data)
            return

        if path == "/api/elicense/timeline":
            entry = elicense_store.get_entry(bin_number)
            if entry is None:
                self._send_json(404, {"success": False,
                                      "message": f"Нет данных по БИН {bin_number}"})
                return
            self._send_json(200, {"success": True, "source": "cache", **entry})
            return

        if path == "/api/elicense/enrich":
            try:
                data = enrich_notifications(bin_number)
            except KeyError as e:
                self._send_json(404, {"success": False, "message": str(e)})
                return
            except Exception as e:
                self._send_json(502, {"success": False, "message": str(e)})
                return
            data.pop("success", None)
            self._send_json(200, {"success": True, **data})
            return

        try:
            data = get_elicense_notifications(
                bin_number, refresh=refresh,
                date_from=_parse_date_filter(query),
                date_equality=(query.get("op") or ["gte"])[0],
            )
        except Exception as e:
            self._send_json(502, {"success": False, "message": str(e)})
            return

        status = 200 if data.get("success") else 504
        self._send_json(status, data)

    def _handle_verify(self):
        """
        Проверка уведомления о начале СМР по паре «адрес + застройщик».

        /api/verify?address=<адрес>&developer=<название>
        Необязательно: &bin=<БИН> (если известен — поиск идёт по нему),
                      &days=N — срез по дате выдачи,
                      &refresh=1 — живой поиск и дозагрузка PDF,
                      &threshold=0.3 — порог схожести адресов.

        Застройщика можно задать названием: поиск идёт по полю «Лицензиат»
        прямо на elicense.kz, а БИН берётся из PDF уведомления.
        """
        query = parse_qs(urlsplit(self.path).query)
        address = (query.get("address") or [""])[0].strip()
        developer = (query.get("developer") or [""])[0].strip()
        refresh = (query.get("refresh") or ["0"])[0] in ("1", "true", "yes")

        threshold = None
        raw_threshold = (query.get("threshold") or [""])[0].strip()
        if raw_threshold:
            try:
                threshold = float(raw_threshold)
            except ValueError:
                self._send_json(400, {"success": False,
                                      "message": "threshold должен быть числом"})
                return

        if not address:
            self._send_json(400, {"success": False,
                                  "message": "Не указан параметр ?address="})
            return

        bin_direct = (query.get("bin") or [""])[0].strip()

        if not developer and not bin_direct:
            self._send_json(400, {
                "success": False,
                "message": "Укажите ?developer= (название застройщика) "
                           "или ?bin= (БИН напрямую)",
            })
            return

        if not developer:
            # БИН известен — подставляем название из базы, чтобы отчёт был полным
            entry = elicense_store.get_entry(bin_direct)
            developer = (entry or {}).get("company") or ""

        try:
            report = verify_address(
                address, developer, refresh=refresh,
                date_from=_parse_date_filter(query),
                threshold=threshold, bin_number=bin_direct or None)
        except Exception as e:
            self._send_json(502, {"success": False, "message": str(e)})
            return

        status = 200 if report.get("success") else 404
        self._send_json(status, report)

    def _handle_licenses(self):
        """
        Даты начала строительства по ЖК — то, что рисуется на карте.

        Отдаёт словарь {id ЖК: {start_date, bin, company, notifications}}.
        Сопоставление идёт по БИН, геокодирование — только как запасной
        путь (отключается параметром geocode=0).
        """
        query = parse_qs(urlsplit(self.path).query)
        use_geo = (query.get("geocode") or ["1"])[0] not in ("0", "false", "no")

        # Тестовый режим: отдаём сохранённый ответ целиком
        if TEST_MODE:
            fixed = load_fixture("licenses.json")
            if fixed:
                self._send_json(200, {"success": True, "test": True, **fixed})
                return

        # Сборка списка ЖК занимает минуты (Nominatim с паузами). Обычно
        # список уже в кэше — его положил SSE-поток. Если кэш холодный,
        # честно отвечаем 503, а не висим молча.
        if _zhk_cache_peek() is None and (query.get("wait") or ["0"])[0] not in (
                "1", "true", "yes"):
            self._send_json(503, {
                "success": False,
                "message": "Список ЖК ещё не собран. Дождись окончания "
                           "SSE-потока /api/zhk/stream или добавь ?wait=1.",
            })
            return

        try:
            zhk_list = _zhk_cache()
        except Exception as e:
            self._send_json(502, {"success": False, "message": f"qportal: {e}"})
            return

        try:
            matched = match_licenses_to_zhk(zhk_list, geocode=use_geo)
        except Exception as e:
            self._send_json(502, {"success": False, "message": str(e)})
            return

        self._send_json(200, {
            "success": True,
            "source": db.path,
            "stats": matched["stats"],
            "unmatched": matched["unmatched"],
            "licenses": matched["by_zhk"],
        })

    def _handle_elicense_stream(self):
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        bin_number = (query.get("bin") or [""])[0].strip()

        if not bin_number:
            self._send_json(400, {"success": False,
                                  "message": "Не указан параметр ?bin="})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            for chunk in stream_elicense(bin_number):
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            print("[sse] клиент отключился")
        except Exception as e:
            print(f"[sse] ошибка: {e}")

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

    # Вывод сразу в UTF-8: консоль Windows по умолчанию cp1251, и любой
    # сторонний модуль с нелатинским приветствием роняет процесс.
    elicense_client._ensure_utf8_stdout()

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    # Порт задаётся переменной окружения: на Windows старый экземпляр
    # сервера может держать 8000 (HTTPServer разрешает повторную привязку),
    # и запросы уходят к нему, а не к свежезапущенному. Тогда поднимаем
    # новый экземпляр на свободном порту.
    port = int(os.environ.get("MAPTOOL_PORT") or 8000)
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    httpd = ThreadedHTTPServer(("", port), Handler)
    print(f"Сервер:         http://localhost:{port}/")

    # --- Хранилище: создаём схему и один раз переносим накопленные JSON ---
    print("Инициализация базы...")
    db.init_schema()
    moved = migrate_from_json(db, GEOCODE_CACHE_FILE, BIN_CACHE_FILE,
                              ELICENSE_STORE_FILE)
    if any(moved.values()):
        print(f"  перенесено из JSON: {moved}")

    db_info = db.stats()
    print(f"  база: {db.path} ({db_info['size_bytes'] // 1024} КБ)")
    for table, count in db_info["tables"].items():
        print(f"    {table:24} {count}")

    if TEST_MODE:
        n_zhk = len(fixture_zhk())
        has_lic = load_fixture("licenses.json") is not None
        print(f"\n*** ТЕСТОВЫЙ РЕЖИМ: данные из {TESTDATA_DIR}/ "
              f"(ЖК: {n_zhk}, даты: {'есть' if has_lic else 'нет'}).")
        print("*** qportal, геокодирование и elicense.kz не вызываются.")

    print(f"SSE-поток:      http://localhost:{port}/api/zhk/stream")
    print(f"Полный список:  http://localhost:{port}/api/zhk")
    print()
    print(f"eLicense:       http://localhost:{port}/api/elicense?bin=<БИН>"
          f"   (попыток капчи: {ELICENSE_RETRIES})")
    print(f"  срез 30 дней:  http://localhost:{port}/api/elicense?bin=<БИН>&days=30")
    print(f"eLicense SSE:   http://localhost:{port}/api/elicense/stream?bin=<БИН>")
    print(f"Хранилище:      {elicense_store.summary()['companies']}"
          f" компаний в {db.path}")
    print()

    if TEST_MODE:
        print("Фоновые задания: отключены (тестовый режим)")
    else:
        if REFRESH_ENABLED:
            nxt = _now_astana() + timedelta(
                seconds=refresh_mod.seconds_until_next_run())
            print(f"Обновление:    ежедневно в {REFRESH_HOUR:02d}:"
                  f"{REFRESH_MINUTE:02d} {refresh_mod.ASTANA_TZ}"
                  f" (ближайшее {nxt:%Y-%m-%d %H:%M})")
        else:
            print("Обновление:    выключено (REFRESH_ENABLED=0)")

        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        print(f"Телеграм-бот:  "
              f"{'настроен' if token else 'не настроен (нужен TELEGRAM_BOT_TOKEN)'}")
        print(f"Состояние бота: http://localhost:{port}/api/telegram")
        print(f"Журнал обновл.: http://localhost:{port}/api/refresh")

    print("Ctrl+C для остановки.\n")

    start_background_jobs()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
    finally:
        stop_background_jobs()
        httpd.shutdown()
        _shutdown_elicense()

