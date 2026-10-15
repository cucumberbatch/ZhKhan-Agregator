"""
Хранилище результатов elicense.kz поверх SQLite.

Уведомления сгруппированы по БИН, внутри — в хронологическом порядке.

Ключевое отличие от прежней реализации на JSON: уникальный индекс
(bin, dedupe_key) делает повторную загрузку идемпотентной, а семантика
«полный поиск заменяет, срез по дате дописывает» выражена одной
транзакцией, а не слиянием словарей в памяти.

Слой намеренно тонкий: прикладной код работает с теми же словарями,
что и раньше, поэтому переезд на PostgreSQL затронет только storage.py.
"""

from storage import (
    NOTIFICATION_FIELDS,
    db,
    dedupe_key,
)

_COMPANY_COLUMNS = ("bin", "company", "fetched_at", "enriched_at",
                    "attempts", "pages")


# ============================================================
# ВСПОМОГАТЕЛЬНОЕ
# ============================================================
def _now():
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def _clean(notification):
    """Отбрасывает вложенный блок 'pdf' — он дублирует плоские поля."""
    return {k: v for k, v in notification.items() if k != "pdf"}


def _notification_row(notification):
    cleaned = _clean(notification)
    return (cleaned.get("bin"), dedupe_key(cleaned)) + tuple(
        cleaned.get(field) for field in NOTIFICATION_FIELDS)


def _upsert_sql():
    """
    UPSERT уведомления.

    Ключевой момент: поля обновляются через COALESCE(excluded.x, table.x).
    Без этого повторный поиск по БИН затирал бы всё, что набрано из PDF —
    в результате парсера таблицы этих полей просто нет, они приходят как
    NULL, и наивный SET address=excluded.address обнулял бы адрес.

    Не-null значения при этом обновляются как обычно: новый адрес из нового
    PDF перезапишет старый.
    """
    cols = ["bin", "dedupe_key"] + list(NOTIFICATION_FIELDS)
    placeholders = ",".join("?" * len(cols))
    updates = ", ".join(
        f"{c}=COALESCE(excluded.{c}, elicense_notifications.{c})"
        for c in NOTIFICATION_FIELDS)
    return (f"INSERT INTO elicense_notifications ({','.join(cols)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(bin, dedupe_key) DO UPDATE SET {updates}")


UPSERT_NOTIFICATION = _upsert_sql()


def _sorted(notifications):
    return sorted(notifications,
                  key=lambda n: (n.get("issue_date_iso") or "9999",
                                 n.get("nikad") or ""))


def _stats(notifications):
    dated = [n["issue_date_iso"] for n in notifications
             if n.get("issue_date_iso")]
    return {
        "total": len(notifications),
        "with_date": len(dated),
        "first_date": min(dated) if dated else None,
        "last_date": max(dated) if dated else None,
    }


def _strip_internal(notification):
    """Убирает технические поля перед выдачей наружу."""
    return {k: v for k, v in notification.items()
            if k not in ("bin", "dedupe_key")}


# ============================================================
# ЧТЕНИЕ
# ============================================================
def get_entry(bin_number):
    """
    Запись одного БИН в виде словаря с отсортированными уведомлениями.
    None, если такого БИН нет.
    """
    bin_number = str(bin_number or "").strip()
    if not bin_number:
        return None

    company = db.query_one(
        "SELECT bin, company, fetched_at, enriched_at, attempts, pages"
        " FROM elicense_companies WHERE bin = ?", (bin_number,))
    if company is None:
        return None

    rows = db.query_all(
        "SELECT * FROM elicense_notifications WHERE bin = ?"
        " ORDER BY (issue_date_iso IS NULL), issue_date_iso, nikad",
        (bin_number,))

    notifications = [_strip_internal(dict(r)) for r in rows]
    entry = dict(company)
    entry["notifications"] = notifications
    entry["stats"] = _stats(notifications)
    return entry


def list_bins():
    """Все БИН, которые уже есть в хранилище."""
    rows = db.query_all("SELECT bin FROM elicense_companies ORDER BY bin")
    return [r["bin"] for r in rows]


def timeline(bin_number):
    """Хронология уведомлений по БИН — по возрастанию даты."""
    entry = get_entry(bin_number)
    return entry.get("notifications") if entry else []


def summary():
    """Сводка по хранилищу — для /api/elicense/all."""
    companies = db.scalar("SELECT COUNT(*) FROM elicense_companies", default=0)
    notifications = db.scalar(
        "SELECT COUNT(*) FROM elicense_notifications", default=0)
    latest = db.scalar("SELECT MAX(fetched_at) FROM elicense_companies")

    return {
        "file": db.path,
        "exists": True,
        "updated_at": latest,
        "companies": companies,
        "notifications": notifications,
        "bins": list_bins(),
        "size_bytes": db.stats().get("size_bytes", 0),
    }


def all_notifications():
    """
    Все уведомления со всех БИН — для сопоставления с картой.
    Возвращает список словарей, каждый со своим 'bin'.
    """
    rows = db.query_all(
        "SELECT * FROM elicense_notifications"
        " ORDER BY bin, (issue_date_iso IS NULL), issue_date_iso, nikad")
    return [dict(r) for r in rows]


# ============================================================
# ЗАПИСЬ
# ============================================================
def save_result(result, merge=False):
    """
    Сохраняет результат search() по его БИН.

    Полный поиск по БИН (date_from пуст) считается авторитетным:
    уведомления записи предварительно удаляются, иначе исчезнувшие с
    сайта документы остались бы висеть вечно.

    Поиск с фильтром даты только дописывает — иначе срез «за 30 дней»
    стёр бы накопленную историю.

    merge=True отключает удаление вовсе. Он нужен для поиска по названию
    заказчика: выдача там приходит ДО разбора PDF и уже подрезана
    фильтром «только начало СМР», то есть это заведомо не вся история.
    Авторитетным её считать нельзя — иначе такой поиск стирал бы данные,
    собранные ранее полным поиском по БИН.
    """
    bin_number = str(result.get("bin") or "").strip()
    if not bin_number:
        raise ValueError("В результате отсутствует БИН")

    filtered = bool(result.get("date_from"))
    authoritative = not filtered and not merge
    incoming = [_clean(n) for n in (result.get("notifications") or [])]

    with db.transaction() as conn:
        if authoritative:
            # Полный поиск авторитетен: документы, которых больше нет
            # в выдаче, удаляем. Но удалять ВСЁ нельзя — тогда следом идущий
            # UPSERT не сможет сохранить адреса, ранее разобранные из PDF
            # (в результате парсера таблицы этих полей попросту нет).
            # Поэтому чистим только те, чьих ключей нет в новой выдаче.
            keys = [dedupe_key(n) for n in incoming]
            if keys:
                placeholders = ",".join("?" * len(keys))
                conn.execute(
                    f"DELETE FROM elicense_notifications WHERE bin = ?"
                    f" AND dedupe_key NOT IN ({placeholders})",
                    [bin_number] + keys)
            else:
                conn.execute(
                    "DELETE FROM elicense_notifications WHERE bin = ?",
                    (bin_number,))
                print(f"[store] выдача пуста, уведомления {bin_number} удалены")

        conn.execute(
            "INSERT INTO elicense_companies"
            " (bin, company, fetched_at, attempts, pages)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(bin) DO UPDATE SET"
            " company=COALESCE(excluded.company, elicense_companies.company),"
            " fetched_at=excluded.fetched_at,"
            " attempts=excluded.attempts, pages=excluded.pages",
            (bin_number, result.get("company"), _now(),
             result.get("attempts"), result.get("pages")))

        for notification in incoming:
            notification["bin"] = bin_number
            conn.execute(UPSERT_NOTIFICATION, _notification_row(notification))

    return get_entry(bin_number)


def update_notifications(bin_number, notifications):
    """
    Заменяет уведомления существующей записи — используется после
    дозагрузки данных из PDF, без повторного поиска по БИН.
    Запись создаётся, если её ещё не было.
    """
    bin_number = str(bin_number or "").strip()
    if not bin_number:
        raise ValueError("Пустой БИН")

    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO elicense_companies (bin, company, fetched_at)"
            " VALUES (?, NULL, ?)"
            " ON CONFLICT(bin) DO UPDATE SET enriched_at=excluded.fetched_at",
            (bin_number, _now()))

        for notification in notifications or []:
            cleaned = _clean(notification)
            cleaned["bin"] = bin_number
            conn.execute(UPSERT_NOTIFICATION, _notification_row(cleaned))

    return get_entry(bin_number)


def upsert_company(bin_number, company=None):
    """Регистрирует БИН без уведомлений — чтобы он попал в перечень."""
    bin_number = str(bin_number or "").strip()
    if not bin_number:
        return None
    db.execute(
        "INSERT INTO elicense_companies (bin, company, fetched_at)"
        " VALUES (?,?,?)"
        " ON CONFLICT(bin) DO UPDATE SET"
        " company=COALESCE(excluded.company, elicense_companies.company)",
        (bin_number, company, _now()))
    return get_entry(bin_number)