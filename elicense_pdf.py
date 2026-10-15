"""
Разбор PDF-уведомления elicense.kz (русская версия).

Документ — «Талон о приеме уведомления о начале или прекращении
осуществления деятельности». В нём есть всё, чего не хватает
в таблице выдачи:

  * дата и время приёма уведомления  — фактическая дата появления
  * входящий регистрационный номер   — идентификатор заявления
  * адрес объекта                    — для привязки к ЖК на карте
  * БИН заявителя                    — без обращения к DaData
  * наименование объекта/деятельности

Извлечение идёт из текстового слоя PDF, без OCR: отчёт генерится
сервером (Stimulsoft/iText), поэтому текст в нём настоящий.
"""

import io
import re
from datetime import date, datetime

# ============================================================
# ПОЛЯ ДОКУМЕНТА
# ============================================================
RE_RECEIVED = re.compile(
    r"Дата\s+и\s+время\s+приема\s+уведомления\s*:?\s*([\d./\-]+)"
    r"(?:\s+(\d{1,2}:\d{2}))?", re.IGNORECASE)

RE_INCOMING = re.compile(
    r"Входящий\s+регистрационный\s+номер\s+уведомления\s*:?\s*([A-Z0-9\-]+)",
    re.IGNORECASE)

# Заявитель идёт в предложении «Настоящим, <название>, <БИН> (полное …)»
RE_APPLICANT = re.compile(
    r"Настоящим\s*,\s*(.+?)\s*,\s*(\d{12})\s*\(\s*полное\s+наименование",
    re.IGNORECASE | re.DOTALL)

# Начало блока «начале осуществления определенного действия по …»
RE_ACTIVITY_HEAD = re.compile(
    r"определенного\s+действия\s+по\s+(.+)$", re.IGNORECASE)

# Разбор блока «определенного действия по …». У eLicense встречаются
# как минимум три варианта того, как заканчивается наименование объекта
# и начинается адрес:
#
#   новый   …, «ЖК со встроенными помещениями, паркингом и ТП»,
#           расположенный по адресу: г.Астана, … 4 очередь» (без сметной
#           документации)», 010000,РЕСПУБЛИКА КАЗАХСТАН, …
#   старый  …, «ЖК со встроенными помещениями», расположенный в
#           г. Нур-Султан, район «Есиль», ул. Букар Жырау, уч. №26. …» (без …)
#   третий  …, «ЖК со встроенными помещениями и паркингом по адресу:
#           город Нур-Султан, район Есиль, улица Бухар Жырау, участок №26»
#           1-ая очередь строительства. Наружные инженерные сети», ,Республика…
#
# В третьем варианте слова «расположенный» нет вовсе, поэтому искать
# адрес только по нему нельзя: наименование уходит в остаток документа.
RE_NAME_STOP = re.compile(
    r"расположенн\w*|\sпо\s+адресу\s*:?|\(\s*указывается\b|"
    r",\s*(?:\d{6}\s*,\s*)?РЕСПУБЛИКА\b",
    re.IGNORECASE)

# Адрес начинается после «по адресу:» либо после «расположенный в …».
# Ищем только внутри блока «определенного действия по …»: по всему
# документу «в» встречается слишком часто и поймал бы чужое место.
RE_ADDR_START = re.compile(r"по\s+адресу\s*:|расположенн\w*\s+в\s+",
                           re.IGNORECASE)

# Адрес либо заканчивается вставкой «(без …)», либо кадастровой строкой,
# либо служебным текстом — в зависимости от шаблона.
RE_ADDR_STOP = re.compile(
    r"\(\s*без\b|\(\s*указывается\b|Наименование\s+принимающей\b|"
    r"Входящий\s+регистрационный\b|,\s*(?:\d{6}\s*,\s*)?РЕСПУБЛИКА\b",
    re.IGNORECASE)

# Запасной адрес по всему документу, если блок «определенного действия»
# не распознался.
RE_ADDR = re.compile(
    r"расположенн\w*\s+(?:по\s+адресу\s*:?|в)\s*(.+?)\s*\(\s*без",
    re.IGNORECASE | re.DOTALL)

# Кадастровая строка идёт после «(без …)». В новом шаблоне перед ней стоит
# код региона (010000,), в старшем — нет, поэтому код не обязателен.
RE_CADASTRAL = re.compile(
    r"\(\s*без[^)]*\)\s*[«»\"']?[\s,.;]*"
    r"((?:\d{6}[\s,.;]*)?(?:РЕСПУБЛИКА|район).{0,320}?)"
    r"(?:\(\s*проектный\s+адрес|\(\s*указывается|Наименование\s+принимающей)",
    re.IGNORECASE | re.DOTALL)

RE_BIN = re.compile(r"\b(\d{12})\b")

# Принимающая организация
RE_RECEIVER = re.compile(
    r"Наименование\s+принимающей\s+организации\s*:?\s*(.+?)\s*(?=Входящий|$)",
    re.IGNORECASE | re.DOTALL)


# ============================================================
# ИЗВЛЕЧЕНИЕ
# ============================================================
def extract_text(pdf_bytes):
    """Текст всех страниц PDF. Бросает RuntimeError, если нужен модуль."""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError(
            "Требуется pypdf: pip install pypdf"
        ) from e

    reader = PdfReader(io.BytesIO(pdf_bytes))
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts)


def _clean(text):
    """
    Схлопывает пробелы и переносы строк.

    Перенос внутри составного слова восстанавливаем с дефисом:
    в русском тексте перенос строки без дефиса — это разрыв слова,
    а «строительно-\nмонтажных» — это перенос настоящего дефиса.
    """
    text = re.sub(r"([а-яёa-z])\s*-\s*\n\s*([а-яёa-z])", r"\1-\2", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"\s*\n\s*", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _tidy(text, strip_quotes=True):
    """Убирает хвостовые/ведущие кавычки, запятые и лишние пробелы."""
    if not text:
        return None
    if strip_quotes:
        # Хвост вида «…строительства». — закрывающая кавычка с точкой
        text = re.sub(r"^[\s«»\"'`,.;]+", "", text)
        text = re.sub(r"[\s«»\"'`,.;]+$", "", text)
    else:
        text = text.strip().strip(",").strip()
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip() or None


def parse_date(value, time_value=None):
    """«22/04/2026» + «13:07» → ISO-дата и ISO-дата со временем."""
    if not value:
        return None, None

    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", value)
    if not m:
        return None, None
    d, mo, y = (int(g) for g in m.groups())
    try:
        parsed = date(y, mo, d)
    except ValueError:
        return None, None

    stamp = None
    if time_value:
        tm = re.match(r"(\d{1,2}):(\d{2})", time_value.strip())
        if tm:
            hh, mm = int(tm.group(1)), int(tm.group(2))
            try:
                stamp = datetime(y, mo, d, hh, mm).isoformat()
            except ValueError:
                stamp = None

    return parsed.isoformat(), stamp


def parse_pdf(pdf_bytes):
    """
    Возвращает словарь с данными уведомления:
        received_date, received_at, incoming_number,
        address, cadastral, applicant, applicant_bin,
        activity_type, object_name, receiver
    """
    text = extract_text(pdf_bytes)
    flat = _clean(text)

    # --- дата и время приёма уведомления ---
    m = RE_RECEIVED.search(flat)
    received_date, received_at = parse_date(
        m.group(1) if m else None, m.group(2) if m else None)

    # --- входящий регистрационный номер ---
    m = RE_INCOMING.search(flat)
    incoming = m.group(1) if m else None

    # --- заявитель и БИН ---
    applicant = applicant_bin = None
    m = RE_APPLICANT.search(flat)
    if m:
        # Название компании заключено в кавычки — их не срезаем
        applicant = _tidy(m.group(1), strip_quotes=False)
        applicant_bin = m.group(2)
    if not applicant_bin:
        m = RE_BIN.search(flat)
        applicant_bin = m.group(1) if m else None

    # --- вид деятельности, наименование объекта и адрес ---
    activity_type = object_name = address = None
    m = RE_ACTIVITY_HEAD.search(flat)
    if m:
        segment = m.group(1)
        head, _, tail = segment.partition(",")
        activity_type = _tidy(head)

        # Наименование объекта — от начала блока до адреса. Отрезать
        # обязательно: иначе в третем шаблоне (без слова «расположенный»)
        # сюда уедет весь остаток документа вместе с реквизитами.
        stop = RE_NAME_STOP.search(tail)
        object_name = _tidy(tail[:stop.start()] if stop else tail) \
            or activity_type

        am = RE_ADDR_START.search(tail)
        if am:
            rest = tail[am.end():]
            stop_a = RE_ADDR_STOP.search(rest)
            address = _tidy(rest[:stop_a.start()] if stop_a else rest)

    # --- адрес объекта (запасной путь по всему документу) ---
    if not address:
        m = RE_ADDR.search(flat)
        address = _tidy(m.group(1)) if m else None

    # --- кадастровая строка (запасной источник адреса) ---
    m = RE_CADASTRAL.search(flat)
    cadastral = _tidy(m.group(1)) if m else None

    # --- принимающая организация ---
    m = RE_RECEIVER.search(flat)
    receiver = _tidy(m.group(1)) if m else None

    return {
        "received_date": received_date,
        "received_at": received_at,
        "incoming_number": incoming,
        "address": address,
        "cadastral": cadastral,
        "applicant": applicant,
        "applicant_bin": applicant_bin,
        "activity_type": activity_type,
        "object_name": object_name,
        "receiver": receiver,
    }


def summarize(parsed):
    """Короткое представление — для логов и отладки."""
    return {k: v for k, v in parsed.items() if v}