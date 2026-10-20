"""
Клиент elicense.kz: поиск уведомлений по БИН с автоматическим
решением капчи через ddddocr.

Сценарий серверного использования:
    from elicense_client import ElicenseClient
    with ElicenseClient() as c:
        result = c.search("250640027866")     # 5 попыток по умолчанию
    result["notifications"]  # уведомления о начале СМР, по дате

Особенности:
  * страница всегда открывается в русской версии (?lang=ru)
  * на каждый неудачный ввод капчи — новая попытка со свежей картинкой
  * Chromium по умолчанию headless, переключается флагом или переменной
  * после успешного входа догружаются все страницы выдачи
  * браузер живёт в выделенном потоке: sync_api Playwright не потокобезопасен
  * есть фильтр «Дата выдачи» — удобно для среза свежих уведомлений

Схема скачивания PDF (капча для неё НЕ нужна):
    /Licenses/GetFileListByDocument?guid=<guid>   → список языковых версий
    /licenses/DownloadFileById/<guid>?documentId=<id>  → сам файл
Русскую версию выбираем по полю Lang == "ru", а не по тексту кнопки:
без cookie lang сайт отдаёт казахские названия.

Playwright и ddddocr импортируются лениво: без них модуль
остаётся импортируемым, а ошибка возникает только при реальном поиске.
"""

import os
import queue
import re
import threading
import time
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

from elicense_html_parser import (
    NOTIFICATION_MARKER,
    is_empty_result,
    parse_elicense_results,
    sort_notifications,
    summarize,
    total_pages,
)

# ============================================================
# КОНФИГ
# ============================================================
SEARCH_URL = "https://elicense.kz/LicensingContent/SimpleSearchLicense?isExtended=true&lang=ru"

DEFAULT_RETRIES = 5          # попыток ввода капчи на один БИН
RETRY_DELAY = 3.0            # пауза между попытками, сек
MAX_PAGES = 20               # предохранитель по числу страниц выдачи

BIN_INPUT = "#BinStr"
CAPTCHA_INPUT = "#Captcha"
CAPTCHA_IMAGE = "#ExampleCaptcha_CaptchaImage"
SUBMIT = 'input[type="submit"]'
RESULT_TABLE = "table#table__content__body"

# Поиск по лицензиату — наименованию заказчика из реестра qportal.
# Это позволяет обойтись без определения БИН: название берётся прямо
# из проекта, а БИН застройщика потом вытаскивается из PDF уведомления.
LICENSIAT_INPUT = "#LicensiatStr"
LICENSIAT_SELECT = "#LicensiatEquality"

# Операторы «Лицензиат» — те же битовые маски, что и у «Дата выдачи».
# По умолчанию «Равно»: в реестре qportal и в выдаче elicense хранится
# одно и то же полное наименование.
LICENSIAT_EQUALITY = {
    "eq": "1",     # Равно
    "gt": "4",     # Больше
    "lt": "8",     # Меньше
    "lte": "16",   # Не больше
    "gte": "32",   # Не меньше
}

# Фильтр «Дата выдачи». На сайте это пара: select с оператором
# (битовая маска) + input[type=date] со значением в формате ГГГГ-ММ-ДД.
DATE_INPUT = "#DateIssueStr"
DATE_SELECT = "#DateIssueEquality"
DATE_EQUALITY = {
    "eq": "1",     # Равно
    "gt": "4",     # Больше
    "lt": "8",     # Меньше
    "lte": "16",   # Не больше
    "gte": "32",   # Не меньше
}


def iso_date(value):
    """Принимает date/datetime/строку и возвращает 'ГГГГ-ММ-ДД'."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10]
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(value))
    if m:
        return m.group(0)
    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", str(value))
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


def days_ago(n, today=None):
    """Дата n дней назад — для фильтра «за последние N дней»."""
    base = today or datetime.now()
    return (base - timedelta(days=int(n))).isoformat()[:10]


def months_ago(n, today=None):
    """
    Дата n месяцев назад — для фильтра «за последние N месяцев».

    Считаем по календарю, а не как «30 дней за месяц»: в три месяца
    бывает 90–92 дня, и на коротком окне это уже одна лишняя неделя.
    """
    base = today or datetime.now()
    months = int(n)

    year, month = base.year, base.month - months
    while month <= 0:
        month += 12
        year -= 1

    day = base.day
    while True:
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            # 31 марта минус месяц → 31 февраля не бывает
            day -= 1

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_ocr_lock = threading.Lock()
_ocr_instance = None


BASE_URL = "https://elicense.kz"

# Страница карточки документа. Русская версия — параметром lang=ru.
DETAIL_URL = BASE_URL + "/Licenses/Details/{guid}?lang=ru"
FILE_LIST_API = BASE_URL + "/Licenses/GetFileListByDocument?guid={guid}"
DOWNLOAD_API = BASE_URL + "/licenses/DownloadFileById/{guid}?documentId={doc}"

# Из ссылки на карточку достаём идентификатор документа
GUID_IN_URL = re.compile(r"/Details/([0-9a-fA-F-]{36})")


def guid_from_url(url):
    """Достаёт GUID документа из ссылки на карточку."""
    if not url:
        return None
    m = GUID_IN_URL.search(url)
    return m.group(1) if m else None


# ============================================================
# ВСПОМОГАТЕЛЬНОЕ
# ============================================================
def _get_ocr():
    """ddddocr грузится тяжело — держим один экземпляр на процесс."""
    global _ocr_instance
    with _ocr_lock:
        if _ocr_instance is None:
            try:
                import ddddocr
            except ImportError as e:
                raise RuntimeError(
                    "Требуется ddddocr: pip install -r requirements.txt"
                ) from e

            _ensure_utf8_stdout()
            _ocr_instance = ddddocr.DdddOcr(beta=True)
        return _ocr_instance


def _ensure_utf8_stdout():
    """
    Переводит вывод в UTF-8, иначе с errors=replace.

    ddddocr при инициализации печатает приветствие, а консоль Windows
    по умолчанию в cp1251 — процесс падает с UnicodeEncodeError ещё до
    начала поиска. Меняем кодировку потока, а не глушим вывод: иначе
    строка лога из соседнего потока ушла бы в пустоту.
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass       # поток уже перенаправлен или недоступен


def _headless_default():
    val = os.environ.get("ELICENSE_HEADLESS", "1").strip().lower()
    return val not in ("0", "false", "no", "off")


def _container_chromium_args():
    """Дополнительные флаги Chromium, если мы внутри контейнера.

    В Docker образ Chromium обычно запускается от root, а песочница
    под root не работает: Chromium падает с «Running as root without
    --no-sandbox is not supported». На обычной машине флаг не нужен
    и без него безопаснее, поэтому добавляем его только когда
    реально работаем от root.

    Отдельная переменная ELICENSE_NO_SANDBOX=1 включает флаг
    принудительно. Это нужно на платформах без возможности настроить
    seccomp-профиль и --ipc=host: Cloud Run применяет свой seccomp и
    песочница Chromium там не поднимается. Ошибиться в имени
    переменной нельзя, поэтому это отдельный флаг, а не строчка в
    ELICENSE_CHROMIUM_ARGS.
    """
    args = []

    force_no_sandbox = os.environ.get("ELICENSE_NO_SANDBOX", "").strip().lower()
    if force_no_sandbox in ("1", "true", "yes", "on"):
        args.append("--no-sandbox")
    else:
        try:
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                args.append("--no-sandbox")
        except (AttributeError, OSError):
            pass

    if os.environ.get("ELICENSE_CHROMIUM_ARGS"):
        args += [a for a in os.environ["ELICENSE_CHROMIUM_ARGS"].split()
                 if a.strip()]

    return args


def _is_captcha_failed(page):
    """Сообщение об ошибке капчи на elicense.kz."""
    try:
        el = page.locator('.tab div[style*="color: red"], .tab div[style*="color:red"]')
        if el.count() > 0:
            text = el.first.inner_text().strip()
            if "капч" in text.lower():
                return True, text
    except Exception:
        pass
    return False, ""


def _page_url(url, page_no):
    """Собирает URL страницы выдачи, заменяя параметр page."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k != "page"]
    query.append(("page", str(page_no)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(query), parts.fragment))


# ============================================================
# ПОТОК-ВЛАДЕЛЕЦ БРАУЗЕРА
# ============================================================
class _BrowserOwner:
    """
    Выполняет задачи в одном выделенном потоке.

    Зачем: объекты playwright.sync_api можно использовать только в том
    потоке, где они были созданы. Веб-сервер отдаёт каждый запрос из
    своего потока, а SSE — из отдельного, поэтому нужен посредник:
    он создаёт браузер у себя и никогда его не отдаёт наружу.
    """

    def __init__(self):
        self._jobs = queue.Queue()
        self._thread = None
        self._start_lock = threading.Lock()

    def _ensure_thread(self):
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._loop, name="elicense-browser", daemon=True)
                self._thread.start()

    def _loop(self):
        while True:
            job = self._jobs.get()
            if job is None:
                self._jobs.task_done()
                break
            try:
                job()
            except Exception:
                pass            # ошибка уже доехала до вызывающего
            finally:
                self._jobs.task_done()

    def run(self, fn, *args, **kwargs):
        """
        Выполняет fn(*args, **kwargs) в потоке-владельце и возвращает
        результат. Ошибки пробрасываются вызывающему.
        """
        self._ensure_thread()
        done = threading.Event()
        box = {}

        def job():
            try:
                box["value"] = fn(*args, **kwargs)
            except BaseException as e:      # noqa: BLE001 — пробрасываем как есть
                box["error"] = e
            finally:
                done.set()

        self._jobs.put(job)
        done.wait()

        if "error" in box:
            raise box["error"]
        return box.get("value")

    def shutdown(self):
        """Останавливает поток. Браузер закрывает сам владелец."""
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                self._jobs.put(None)
                self._thread.join(timeout=10)
            self._thread = None


# ============================================================
# КЛИЕНТ
# ============================================================
class ElicenseClient:
    """
    Браузер живёт между вызовами search(), чтобы не поднимать Chromium
    на каждый запрос. Закрывается через close() или context manager.
    """

    def __init__(self, headless=None, retries=DEFAULT_RETRIES,
                 retry_delay=RETRY_DELAY, max_pages=MAX_PAGES, timeout=30000):
        self.headless = _headless_default() if headless is None else headless
        self.retries = max(1, int(retries))
        self.retry_delay = retry_delay
        self.max_pages = max_pages
        self.timeout = timeout

        self._owner = _BrowserOwner()
        self._playwright = None
        self._browser = None
        self._doc_ctx = None
        self._closed = False

    # ----------------------------------------------------------
    # Жизненный цикл браузера (только в потоке-владельце!)
    # ----------------------------------------------------------
    def _ensure_browser(self):
        if self._browser is not None:
            return self._browser

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                "Требуется playwright: pip install -r requirements.txt "
                "и playwright install chromium"
            ) from e

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"]
            + _container_chromium_args(),
        )
        return self._browser

    def close(self):
        self._owner.run(self._close_browser)
        self._owner.shutdown()
        self._closed = True

    def _close_browser(self):
        if self._doc_ctx is not None:
            try:
                self._doc_ctx.close()
            except Exception:
                pass
            self._doc_ctx = None
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ----------------------------------------------------------
    # Внутреннее
    # ----------------------------------------------------------
    def _fetch_captcha_bytes(self, page):
        """Байты картинки капчи: из src, иначе — скриншот элемента."""
        try:
            src = page.locator(CAPTCHA_IMAGE).get_attribute("src")
            if src:
                resp = page.request.get(urljoin(page.url, src))
                return resp.body()
        except Exception:
            pass
        return page.locator(CAPTCHA_IMAGE).screenshot()

    def _doc_context(self):
        """
        Контекст для страниц, где капча не нужна (карточка, API файлов).
        Живёт вместе с браузером и закрывается вместе с ним.
        """
        if self._doc_ctx is None:
            browser = self._ensure_browser()
            self._doc_ctx = browser.new_context(
                user_agent=USER_AGENT,
                locale="ru-RU",
                viewport={"width": 1440, "height": 900},
            )
        return self._doc_ctx

    def _request_text(self, url):
        return self._doc_context().request.get(url).text()

    def _request_bytes(self, url):
        return self._doc_context().request.get(url).body()

    def warm_document_api(self):
        """
        Прогревает cookie страницы деталей. Само по себе не обязательно —
        эндпоинты файлов отвечают и без неё, — но делает поведение
        ближе к браузеру пользователя.
        """
        ctx = self._doc_context()
        page = ctx.new_page()
        try:
            page.goto(BASE_URL + "/LicensingContent/SimpleSearchLicense?lang=ru",
                      wait_until="domcontentloaded", timeout=self.timeout)
        finally:
            page.close()

    def _submit_and_check(self, page, captcha_text):
        """
        Заполняет капчу и отправляет форму.
        Возвращает статус: ok | captcha_failed | empty | no_results
        """
        page.fill(CAPTCHA_INPUT, captcha_text)
        page.click(SUBMIT)
        page.wait_for_load_state("domcontentloaded", timeout=self.timeout)
        page.wait_for_timeout(1200)   # дать JS отрисовать результат

        failed, msg = _is_captcha_failed(page)
        if failed:
            return "captcha_failed", msg

        if page.locator(RESULT_TABLE).count() > 0:
            return "ok", ""

        if is_empty_result(page.content()):
            return "empty", ""

        return "no_results", ""

    def _collect_pages(self, page):
        """
        Читает все страницы выдачи текущей сессии.

        Возвращает (records, pages_read, truncated). truncated=True, если
        сайт обещал больше страниц, чем отдаёт.

        Для поиска по названию заказчика это норма: пагинация рисуется на
        все страницы, но запрос ?page=2 возвращает пустую страницу —
        фильтр «Лицензиат» применяется уже после выборки первой страницы.
        Без этого сигнала усечение выглядело бы как «найдено немного».
        """
        records = []
        pages_read = 0

        pages_advertised = total_pages(page.content())
        pages_total = min(pages_advertised, self.max_pages)

        for page_no in range(1, pages_total + 1):
            if page_no > 1:
                page.goto(_page_url(page.url, page_no),
                          wait_until="domcontentloaded", timeout=self.timeout)
                page.wait_for_timeout(800)
                if page.locator(RESULT_TABLE).count() == 0:
                    break

            records.extend(parse_elicense_results(page.content(),
                                                  only_notifications=False))
            pages_read += 1

        truncated = pages_read < pages_total

        # Убираем возможные дубли при повторной выдаче страниц
        seen, unique = set(), []
        for r in records:
            key = r.get("unique_number") or r.get("nikad") or id(r)
            if key in seen:
                continue
            seen.add(key)
            unique.append(r)

        # Страницы склеиваются в порядке выдачи, а он по дате не отсортирован
        # (внутри страницы сортировка уже есть). Сортируем итог целиком,
        # иначе хронология разъезжается на многостраничных выдачах.
        return sort_notifications(unique), pages_read, truncated

    @staticmethod
    def _split_notifications(records):
        """Отделяет уведомления о начале СМР от прочих записей."""
        marker = ("уведомление о начале производства "
                  "строительно-монтажных работ")
        return [r for r in records if marker in (r.get("activity") or "").lower()]

    # ----------------------------------------------------------
    # Работа с PDF-уведомлениями
    #
    # Проверено: страница карточки, список файлов и само скачивание
    # работают БЕЗ капчи. Капча нужна только для поиска по БИН, поэтому
    # разбор накопленных уведомлений не требует её прогонять заново.
    # ----------------------------------------------------------
    def list_document_files(self, guid):
        """Доступные языковые версии документа. Без капчи."""
        import json as _json

        url = FILE_LIST_API.format(guid=guid)
        data = _json.loads(self._request_text(url))
        return data.get("result") or []

    def download_document(self, guid, document_guid):
        """Байты файла документа."""
        return self._request_bytes(DOWNLOAD_API.format(guid=guid,
                                                       doc=document_guid))

    def download_ru_document(self, guid):
        """
        Русская версия документа. Возвращает байты PDF или None,
        если русской версии нет. Без капчи.
        """
        files = self.list_document_files(guid)
        ru = next((f for f in files if (f.get("Lang") or "").lower() == "ru"), None)
        if not ru:
            return None
        return self.download_document(guid, ru["Guid"])

    def enrich_notifications(self, notifications, on_item=None, only_smr=True):
        """
        Дополняет уведомления данными из PDF: адрес объекта, дата приёма,
        входящий номер, БИН заявителя, наименование объекта.

        Работает без капчи. Возвращает новый список; исходный не меняется.
        on_item(index, total, notification) — прогресс для SSE.

        only_smr=True — качать PDF только для уведомлений о начале СМР.
        Это в разы быстрее: в выдаче застройщика обычно десятки записей
        других видов деятельности, а для даты начала стройки нужны
        только уведомления о начале строительно-монтажных работ.

        Публичный метод: выполняется в потоке-владельце, поэтому его
        можно звать из любого потока сервера.
        """
        return self._owner.run(self._enrich_impl, notifications,
                               on_item, only_smr)

    def _enrich_impl(self, notifications, on_item=None, only_smr=True):
        try:
            from elicense_pdf import parse_pdf
        except ImportError as e:
            raise RuntimeError("Требуется elicense_pdf.py рядом с клиентом") from e

        enriched = []

        # Считаем только те, что реально будем качать — прогресс честный
        targets = [
            n for n in notifications
            if not only_smr or NOTIFICATION_MARKER
            in (n.get("activity") or "").lower()
        ]
        total = len(targets)
        done = 0

        for notif in notifications:
            item = dict(notif)

            if only_smr and NOTIFICATION_MARKER not in (
                    (item.get("activity") or "").lower()):
                enriched.append(item)      # не наше — оставляем как есть
                continue

            done += 1
            guid = guid_from_url(item.get("detail_url"))

            if guid:
                try:
                    data = self.download_ru_document(guid)
                except Exception as e:
                    data = None
                    item["pdf_error"] = f"{type(e).__name__}: {e}"

                if data:
                    try:
                        item["pdf"] = parse_pdf(data)
                    except Exception as e:
                        item["pdf_error"] = f"разбор PDF: {type(e).__name__}: {e}"
                else:
                    item["pdf_error"] = "русская версия документа недоступна"

            # Плоские поля — их удобнее отдавать во фронтенд
            pdf = item.get("pdf") or {}
            item["address"] = pdf.get("address")
            item["cadastral"] = pdf.get("cadastral")
            item["received_date"] = pdf.get("received_date")
            item["received_at"] = pdf.get("received_at")
            item["incoming_number"] = pdf.get("incoming_number")
            item["applicant_bin"] = pdf.get("applicant_bin")
            item["object_name"] = pdf.get("object_name")

            enriched.append(item)
            if on_item:
                try:
                    on_item(done, total, item)
                except Exception:
                    pass

        return enriched

    # ----------------------------------------------------------
    # Публичное
    # ----------------------------------------------------------
    def search(self, bin_number=None, on_attempt=None, date_from=None,
                   date_equality="gte", licensiat=None,
                   licensiat_equality="eq"):
        """
        Ищет уведомления на elicense.kz.

        bin_number        — поиск по БИН заказчика;
        licensiat         — поиск по наименованию заказчика (поле «Лицензиат»).
                            Берётся прямо из реестра проектов qportal, поэтому
                            знать БИН заранее не требуется: он потом
                            извлекается из PDF уведомления.
                            Если заданы оба — работают оба условия.
        licensiat_equality— оператор «Лицензиат»: eq (по умолчанию), gt,
                            gte, lt, lte.
        date_from         — фильтр «Дата выдачи»;
        date_equality     — оператор фильтра: gte (по умолчанию), gt, lte,
                            lt, eq.
        on_attempt        — колбэк прогресса по попыткам капчи.

        Вся работа с браузером происходит в потоке-владельце, поэтому
        метод безопасно звать из любого потока сервера.
        """
        if self._closed:
            raise RuntimeError("Клиент уже закрыт")

        bin_number = str(bin_number or "").strip() or None
        licensiat = str(licensiat or "").strip() or None

        if not bin_number and not licensiat:
            raise ValueError("Нужно указать bin= или licensiat=")

        if licensiat_equality not in LICENSIAT_EQUALITY:
            raise ValueError(
                f"Неизвестный оператор «Лицензиат»: {licensiat_equality!r}, "
                f"допустимо {sorted(LICENSIAT_EQUALITY)}")

        return self._owner.run(self._search_impl, bin_number, on_attempt,
                               date_from, date_equality, licensiat,
                               licensiat_equality)

    def _search_impl(self, bin_number=None, on_attempt=None, date_from=None,
                     date_equality="gte", licensiat=None,
                     licensiat_equality="eq"):
        def _notify(**info):
            if on_attempt:
                try:
                    on_attempt(info)
                except Exception:
                    pass    # колбэк прогресса не должен ронять поиск

        bin_number = str(bin_number or "").strip()
        licensiat = str(licensiat or "").strip()
        result = {
            "success": False,
            "bin": bin_number,
            "licensiat": licensiat,
            "attempts": 0,
            "notifications": [],
            "records": [],
            "company": None,
            "pages": 0,
            "stats": None,
            "error": None,
            "history": [],
            "date_from": iso_date(date_from),
            "date_equality": date_equality if iso_date(date_from) else None,
        }

        if bin_number and not bin_number.isdigit():
            result["error"] = f"БИН должен состоять из цифр: {bin_number!r}"
            return result

        if not bin_number and not licensiat:
            result["error"] = "Нужно указать БИН или наименование лицензиата"
            return result

        ocr = _get_ocr()
        browser = self._ensure_browser()

        for attempt in range(1, self.retries + 1):
            result["attempts"] = attempt
            context = None
            try:
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    locale="ru-RU",
                    viewport={"width": 1440, "height": 900},
                )
                page = context.new_page()

                # Русская версия сайта
                page.goto(SEARCH_URL, wait_until="domcontentloaded",
                          timeout=self.timeout)

                # Условия поиска: БИН и/или наименование лицензиата
                if bin_number:
                    page.fill(BIN_INPUT, bin_number)
                if licensiat:
                    page.select_option(
                        LICENSIAT_SELECT,
                        LICENSIAT_EQUALITY[licensiat_equality])
                    page.fill(LICENSIAT_INPUT, licensiat[:300])
                    _notify(attempt=attempt, total=self.retries,
                            status="licensiat", name=licensiat,
                            operator=licensiat_equality)

                # Фильтр «Дата выдачи» — заполняем до отправки формы
                date_value = iso_date(date_from)
                if date_value:
                    op = DATE_EQUALITY.get(date_equality, DATE_EQUALITY["gte"])
                    page.select_option(DATE_SELECT, op)
                    page.fill(DATE_INPUT, date_value)
                    _notify(attempt=attempt, total=self.retries,
                            status="filtering", date_issue=date_value,
                            operator=date_equality)

                page.locator(CAPTCHA_IMAGE).wait_for(
                    state="visible", timeout=self.timeout)
                page.wait_for_timeout(500)

                img = self._fetch_captcha_bytes(page)
                captcha_text = ocr.classification(img) or ""
                result["history"].append(
                    {"attempt": attempt, "captcha": captcha_text})
                _notify(attempt=attempt, total=self.retries,
                        status="solving", captcha=captcha_text)

                status, msg = self._submit_and_check(page, captcha_text)

                _notify(attempt=attempt, total=self.retries,
                        status=status, captcha=captcha_text, note=msg)

                if status == "ok":
                    records, pages_read, truncated = self._collect_pages(page)
                    notifs = self._split_notifications(records)
                    # Как заказчик записан в самой выдаче: при расхождении
                    # с запросом видно, что запись в реестре неточна.
                    found_name = next((r.get("licensiat") for r in records
                                       if r.get("licensiat")), None)
                    result.update({
                        "success": True,
                        "notifications": notifs,
                        "records": records,
                        "company": found_name,
                        "licensiat_operator": licensiat_equality,
                        "truncated": truncated,
                        "pages": pages_read,
                        "stats": summarize(notifs),
                    })
                    return result

                if status == "empty":
                    # Капча пройдена, уведомлений нет — это успех
                    result.update({
                        "success": True,
                        "records": [],
                        "notifications": [],
                        "stats": summarize([]),
                    })
                    return result

                result["history"][-1]["note"] = msg or status

            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                result["history"].append({"attempt": attempt, "error": err})
                _notify(attempt=attempt, total=self.retries,
                        status="error", note=err)
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass

            if attempt < self.retries:
                time.sleep(self.retry_delay)

        result["error"] = f"Капча не решена за {self.retries} попыток"
        return result


# ============================================================
# УДОБНЫЕ ОБЁРТКИ
# ============================================================
def search_elicense(bin_number, retries=DEFAULT_RETRIES, headless=None,
                    on_attempt=None):
    """Разовая проверка: поднимает браузер, ищет, закрывает."""
    with ElicenseClient(headless=headless, retries=retries) as client:
        return client.search(bin_number, on_attempt=on_attempt)


def search_elicense_many(bins, retries=DEFAULT_RETRIES, headless=None,
                         pause=3.0, on_progress=None, on_attempt=None):
    """
    Обход нескольких БИН одним браузером.
    on_progress(bin_number, index, total, result) — после каждого БИН.
    on_attempt(info) — прогресс по попыткам капчи внутри текущего БИН.
    """
    bins = [str(b).strip() for b in bins if str(b).strip()]
    out = {}

    with ElicenseClient(headless=headless, retries=retries) as client:
        for i, b in enumerate(bins, 1):
            if on_progress:
                on_progress(b, i, len(bins), None)
            res = client.search(b, on_attempt=on_attempt)
            out[b] = res
            if on_progress:
                on_progress(b, i, len(bins), res)
            if i < len(bins):
                time.sleep(pause)

    return out