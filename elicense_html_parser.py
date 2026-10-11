"""
Парсер HTML страницы результатов поиска elicense.kz (русская версия).

Извлекает из таблицы `#table__content__body` уведомления
о начале производства строительно-монтажных работ и возвращает
их как список словарей, готовых к сортировке по дате.

Структура строки результата (4 ячейки):
    td[0] — порядковый номер
    td[1] — НИКАД / Уникальный номер документа / Дата выдачи / НИКАД заявления
    td[2] — Лицензиар / Лицензиат / Вид деятельности
    td[3] — статус и ссылка на карточку
"""

import re
from datetime import date, datetime

BASE_URL = "https://elicense.kz"

# Маркер уведомления о начале СМР. Сравнение регистронезависимое
# и по подстроке — на сайте встречаются варианты:
#   «Уведомление о начале производства строительно-монтажных работ»
#   «... по строительству нового объекта»
NOTIFICATION_MARKER = "уведомление о начале производства строительно-монтажных работ"

# Признаки того, что запрос отработал нормально, но результатов нет
EMPTY_MARKERS = ("не найдено", "табылған жоқ", "по вашему запросу ничего")

# Признак неверно введённой капчи (без браузера, по тексту страницы)
CAPTCHA_ERROR_MARKERS = ("неверно введен", "неверный код", "капч", " captcha")


# ============================================================
# ВСПОМОГАТЕЛЬНОЕ
# ============================================================
def _norm_title(text):
    """Нормализует заголовок поля: режет хвостовое двоеточие и внешние пробелы."""
    return (text or "").strip().rstrip(":").strip().lower()


def parse_date(value):
    """«16.07.2026» → date(2026, 7, 16). Возвращает None при неудаче."""
    if not value:
        return None
    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", str(value))
    if not m:
        return None
    d, mth, y = (int(g) for g in m.groups())
    try:
        return date(y, mth, d)
    except ValueError:
        return None


def _extract_field(cell, title):
    """
    Внутри ячейки ищет <div class="row_result_field">, где
    <div class="row__content__title"> = title, и возвращает текст
    следующего <div>. Сравнение заголовков нормализованное,
    поэтому «Дата выдачи:» найдётся по запросу «Дата выдачи».
    """
    if cell is None:
        return None

    wanted = _norm_title(title)

    for field_div in cell.find_all("div", class_="row_result_field"):
        title_div = field_div.find("div", class_="row__content__title")
        if not title_div:
            continue
        if _norm_title(title_div.get_text(strip=True)) != wanted:
            continue
        value_div = title_div.find_next_sibling("div")
        if value_div:
            text = value_div.get_text(" ", strip=True)
            return text or None
    return None


def _absolute(href):
    if not href:
        return None
    if href.startswith("http"):
        return href
    return BASE_URL + ("" if href.startswith("/") else "/") + href


# ============================================================
# СОСТОЯНИЕ СТРАНИЦЫ
# ============================================================
def has_captcha_error(html):
    """Проверяет текст страницы на сообщение об ошибке капчи."""
    if not html:
        return False
    low = html.lower()
    return any(m in low for m in CAPTCHA_ERROR_MARKERS)


def is_empty_result(html):
    """Результатов нет, но запрос обработан (капча пройдена)."""
    if not html:
        return False
    low = html.lower()
    return any(m in low for m in EMPTY_MARKERS)


def total_pages(html):
    """
    Число страниц выдачи. Берётся из <input type="number" max="N">
    блока пагинации. 1, если пагинации нет.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return 1

    soup = BeautifulSoup(html, "lxml")
    box = soup.select_one("div.paging, div.pagination")
    if box is None:
        return 1

    inp = box.find("input", attrs={"max": True})
    if inp is not None:
        try:
            return max(1, int(inp["max"]))
        except (TypeError, ValueError):
            pass

    pages = [a.get_text(strip=True) for a in box.select("div.pages a")]
    nums = [int(p) for p in pages if p.isdigit()]
    return max(nums) if nums else 1


# ============================================================
# ОСНОВНОЙ ПАРСИНГ
# ============================================================
def parse_elicense_results(html, only_notifications=True):
    """
    Принимает HTML страницы с результатами поиска elicense.kz.
    Возвращает список словарей с уведомлениями, отсортированный
    по дате выдачи (по возрастанию, внутри дня — по убыванию НИКАД).

    Если only_notifications=False — вернёт все строки выдачи.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError as e:
        raise RuntimeError("Требуется beautifulsoup4: pip install -r requirements.txt") from e

    soup = BeautifulSoup(html, "lxml")

    table = soup.find("table", id="table__content__body")
    if not table:
        return []

    results = []

    for row in table.find_all("tr"):
        # Пропускаем заголовочную строку — в ней <th>
        if row.find("th"):
            continue

        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        activity = _extract_field(cells[2], "Вид деятельности")
        if not activity:
            continue

        if only_notifications and NOTIFICATION_MARKER not in activity.lower():
            continue

        # Столбец 2: номера и даты
        nikad = _extract_field(cells[1], "НИКАД")
        uniq_num = _extract_field(cells[1], "Уникальный номер документа")
        issue_dt = _extract_field(cells[1], "Дата выдачи")
        app_nikad = _extract_field(cells[1], "НИКАД заявления")

        # Столбец 3: лицензиар и лицензиат
        licensiar = _extract_field(cells[2], "Лицензиар")
        licensiat = _extract_field(cells[2], "Лицензиат")

        # Столбец 4: статус и ссылка на карточку
        status = None
        detail_url = None
        link = cells[3].find("a")
        if link:
            detail_url = _absolute(link.get("href"))
            status_span = link.find("span")
            if status_span:
                status = status_span.get_text(" ", strip=True)

        parsed_date = parse_date(issue_dt)

        results.append({
            "nikad": nikad,
            "unique_number": uniq_num,
            "application_nikad": app_nikad,
            "issue_date": issue_dt,
            "issue_date_iso": parsed_date.isoformat() if parsed_date else None,
            "licensiar": licensiar,
            "licensiat": licensiat,
            "activity": activity,
            "status": status,
            "detail_url": detail_url,
        })

    return sort_notifications(results)


def sort_notifications(items):
    """
    Хронологический порядок: сначала самые ранние уведомления.
    Строки без даты уходят в конец, между собой — по номеру НИКАД.
    """
    def key(item):
        iso = item.get("issue_date_iso")
        if iso:
            return (0, iso, item.get("nikad") or "")
        return (1, "", item.get("nikad") or "")

    return sorted(items, key=key)


def summarize(items):
    """Короткая сводка по набору уведомлений — для API и логов."""
    dated = [i for i in items if i.get("issue_date_iso")]
    return {
        "total": len(items),
        "with_date": len(dated),
        "first_date": min(i["issue_date_iso"] for i in dated) if dated else None,
        "last_date": max(i["issue_date_iso"] for i in dated) if dated else None,
    }