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

Playwright и ddddocr импортируются лениво: без них модуль
остаётся импортируемым, а ошибка возникает только при реальном поиске.
"""

import os
import queue
import threading
import time
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

from elicense_html_parser import (
    is_empty_result,
    parse_elicense_results,
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

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_ocr_lock = threading.Lock()
_ocr_instance = None


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
            _ocr_instance = ddddocr.DdddOcr(beta=True)
        return _ocr_instance


def _headless_default():
    val = os.environ.get("ELICENSE_HEADLESS", "1").strip().lower()
    return val not in ("0", "false", "no", "off")


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
            args=["--disable-blink-features=AutomationControlled"],
        )
        return self._browser

    def close(self):
        self._owner.run(self._close_browser)
        self._owner.shutdown()
        self._closed = True

    def _close_browser(self):
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
        """Читает все страницы выдачи текущей сессии."""
        records = []
        pages_read = 0

        pages_total = min(total_pages(page.content()), self.max_pages)
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

        # Убираем возможные дубли при повторной выдаче страниц
        seen, unique = set(), []
        for r in records:
            key = r.get("unique_number") or r.get("nikad") or id(r)
            if key in seen:
                continue
            seen.add(key)
            unique.append(r)

        return unique, pages_read

    @staticmethod
    def _split_notifications(records):
        """Отделяет уведомления о начале СМР от прочих записей."""
        marker = ("уведомление о начале производства "
                  "строительно-монтажных работ")
        return [r for r in records if marker in (r.get("activity") or "").lower()]

    # ----------------------------------------------------------
    # Публичное
    # ----------------------------------------------------------
    def search(self, bin_number, on_attempt=None):
        """
        Ищет уведомления по БИН. Возвращает словарь:
            success, bin, attempts, notifications, records,
            company, pages, stats, error, history

        on_attempt(info) — необязательный колбэк, вызывается после каждой
        попытки капчи, пока запрос ещё не завершён. Позволяет отдавать
        прогресс в SSE в реальном времени.

        Вся работа с браузером происходит в потоке-владельце, поэтому
        метод безопасно звать из любого потока.
        """
        if self._closed:
            raise RuntimeError("Клиент уже закрыт")
        return self._owner.run(self._search_impl, bin_number, on_attempt)

    def _search_impl(self, bin_number, on_attempt):
        def _notify(**info):
            if on_attempt:
                try:
                    on_attempt(info)
                except Exception:
                    pass    # колбэк прогресса не должен ронять поиск

        bin_number = str(bin_number or "").strip()
        result = {
            "success": False,
            "bin": bin_number,
            "attempts": 0,
            "notifications": [],
            "records": [],
            "company": None,
            "pages": 0,
            "stats": None,
            "error": None,
            "history": [],
        }

        if not bin_number:
            result["error"] = "Пустой БИН"
            return result

        if not bin_number.isdigit():
            result["error"] = f"БИН должен состоять из цифр: {bin_number!r}"
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

                page.fill(BIN_INPUT, bin_number)

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
                    records, pages_read = self._collect_pages(page)
                    notifs = self._split_notifications(records)
                    licensiat = next((r.get("licensiat") for r in records
                                      if r.get("licensiat")), None)
                    result.update({
                        "success": True,
                        "notifications": notifs,
                        "records": records,
                        "company": licensiat,
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