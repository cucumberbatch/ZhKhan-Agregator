"""
Хранилище на SQLite: схема, подключения и разовое переселение из JSON.

Почему SQLite:
  * файл — переносится на любой хостинг без настройки сервера БД;
  * атомарность и целостность из коробки, в отличие от переписываемого JSON;
  * уникальные индексы дают идемпотентные вставки, поэтому повторная
    загрузка одних и тех же уведомлений не плодит дубли;
  * при необходимости переезд на PostgreSQL — вопрос драйвера и правок
    типов в схеме, а не переписывания прикладного кода.

Потокобезопасность:
  сервер основан на ThreadingMixIn, а объекты sqlite3.Connection нельзя
  делить между потоками. Поэтому на поток — своё подключение
  (threading.local). Режим WAL позволяет читать и писать одновременно,
  busy_timeout снимает конфликты при одновременной записи.
"""

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

DEFAULT_DB_FILE = "maptool.db"
SCHEMA_VERSION = 1


# ============================================================
# ПОДКЛЮЧЕНИЯ
# ============================================================
class Database:
    """Потокобезопасная обёртка над одним файлом SQLite."""

    def __init__(self, path=DEFAULT_DB_FILE):
        self.path = path
        self._local = threading.local()
        self._write_lock = threading.Lock()

    # ----------------------------------------------------------
    def connect(self):
        """Подключение текущего потока (создаётся при первом обращении)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn

        conn = sqlite3.connect(self.path, timeout=30.0,
                               isolation_level=None)   # autocommit, транзакции вручную
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None

    # ----------------------------------------------------------
    def execute(self, sql, params=()):
        return self.connect().execute(sql, params)

    def query_all(self, sql, params=()):
        return self.connect().execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        return self.connect().execute(sql, params).fetchone()

    def scalar(self, sql, params=(), default=None):
        row = self.query_one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    @contextmanager
    def transaction(self):
        """
        Явная транзакция. BEGIN IMMEDIATE сразу берёт блокировку на запись,
        поэтому два потока не упрутся в SQLITE_BUSY на середине.
        """
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # ----------------------------------------------------------
    def init_schema(self):
        """Создаёт таблицы и индексы, если их ещё нет. Идемпотентно."""
        conn = self.connect()
        conn.executescript(SCHEMA_SQL)
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return conn

    def stats(self):
        """Сводка по таблицам — для /api/elicense/all и отладки."""
        out = {"file": self.path, "exists": os.path.exists(self.path),
               "size_bytes": 0, "tables": {}}
        if os.path.exists(self.path):
            out["size_bytes"] = os.path.getsize(self.path)

        try:
            for table in ("geocode_cache", "bin_cache",
                          "elicense_companies", "elicense_notifications",
                          "telegram_users", "sent_notifications",
                          "refresh_log"):
                try:
                    n = self.scalar(f"SELECT COUNT(*) FROM {table}", default=0)
                    out["tables"][table] = n
                except sqlite3.Error:
                    out["tables"][table] = None
        except sqlite3.Error:
            pass
        return out


# ============================================================
# СХЕМА
# ============================================================
SCHEMA_SQL = """
-- Кэш геокодирования: адрес -> координаты
CREATE TABLE IF NOT EXISTS geocode_cache (
    address    TEXT PRIMARY KEY,
    lat        REAL,
    lon        REAL,
    display    TEXT,
    updated_at TEXT NOT NULL
);

-- Кэш БИН: название заказчика -> ИИН
CREATE TABLE IF NOT EXISTS bin_cache (
    customer   TEXT PRIMARY KEY,
    bin        TEXT,
    updated_at TEXT NOT NULL
);

-- Застройщики, найденные на elicense.kz
CREATE TABLE IF NOT EXISTS elicense_companies (
    bin         TEXT PRIMARY KEY,
    company     TEXT,
    fetched_at  TEXT,
    enriched_at TEXT,
    attempts    INTEGER,
    pages       INTEGER
);

-- Уведомления о начале СМР.
-- dedupe_key делает повторную загрузку идемпотентной: тот же документ
-- обновляет строку, а не плодит дубли.
CREATE TABLE IF NOT EXISTS elicense_notifications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    bin               TEXT NOT NULL
                      REFERENCES elicense_companies(bin) ON DELETE CASCADE,
    dedupe_key        TEXT NOT NULL,
    nikad             TEXT,
    unique_number     TEXT,
    application_nikad TEXT,
    issue_date        TEXT,
    issue_date_iso    TEXT,
    licensiar         TEXT,
    licensiat         TEXT,
    activity          TEXT,
    status            TEXT,
    detail_url        TEXT,
    address           TEXT,
    cadastral         TEXT,
    received_date     TEXT,
    received_at       TEXT,
    incoming_number   TEXT,
    applicant_bin     TEXT,
    object_name       TEXT,
    pdf_error         TEXT,
    UNIQUE (bin, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_notif_bin
    ON elicense_notifications (bin);
CREATE INDEX IF NOT EXISTS idx_notif_date
    ON elicense_notifications (issue_date_iso);
CREATE INDEX IF NOT EXISTS idx_notif_applicant
    ON elicense_notifications (applicant_bin);
CREATE INDEX IF NOT EXISTS idx_geo_updated
    ON geocode_cache (updated_at);

-- Подписчики телеграм-бота: те, кто подключил бота и хочет уведомления
CREATE TABLE IF NOT EXISTS telegram_users (
    chat_id    TEXT PRIMARY KEY,
    username   TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

-- Что уже отправлено подписчикам. Ключ — устойчивый идентификатор
-- объекта (id ЖК или номер документа), вид — что за событие.
-- Без этой таблики после перезапуска уведомления приходили бы заново.
CREATE TABLE IF NOT EXISTS sent_notifications (
    kind        TEXT NOT NULL,
    item_key    TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    subscribers INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, item_key)
);

-- Журнал фоновых обновлений: когда проходил запуск и что изменилось
CREATE TABLE IF NOT EXISTS refresh_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT,
    new_zhk     INTEGER NOT NULL DEFAULT 0,
    new_smr     INTEGER NOT NULL DEFAULT 0,
    notified    INTEGER NOT NULL DEFAULT 0,
    message     TEXT
);
CREATE INDEX IF NOT EXISTS idx_refresh_started
    ON refresh_log (started_at);
"""


# ============================================================
# СВОДКА ПО УВЕДОМЛЕНИЯМ
# ============================================================
NOTIFICATION_FIELDS = (
    "nikad", "unique_number", "application_nikad", "issue_date",
    "issue_date_iso", "licensiar", "licensiat", "activity", "status",
    "detail_url", "address", "cadastral", "received_date", "received_at",
    "incoming_number", "applicant_bin", "object_name", "pdf_error",
)


def dedupe_key(notification):
    """
    Устойчивый ключ документа. Уникальный номер документа — самый
    надёжный идентификатор; НИКАД и дата с видом деятельности — запасные.
    """
    for field in ("unique_number", "nikad"):
        value = notification.get(field)
        if value:
            return str(value)
    return "{}|{}".format(notification.get("issue_date_iso") or "",
                          (notification.get("activity") or "")[:80])


def row_to_dict(row):
    return dict(row) if row is not None else None


# ============================================================
# РАЗОВОЕ ПЕРЕСЕЛЕНИЕ ИЗ JSON
# ============================================================
def _load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[storage] не удалось прочитать {path}: {e}")
        return None


def migrate_from_json(db, geocode_file="geocode_cache.json",
                      bin_file="bin_cache.json", elicense_file="elicense_data.json",
                      force=False):
    """
    Переносит накопленные JSON-файлы в базу. Работает только при пустой
    базе (или force=True) — иначе старый файл, случайно вернувшийся в
    рабочую папку, перезаписал бы более свежие данные.

    JSON-файлы не удаляются — остаются как резервная копия, и на этом
    их роль заканчивается.
    Возвращает словарь с количеством перенесённого (пустой, если база
    уже не пустая).
    """
    if not force:
        existing = sum(db.scalar(f"SELECT COUNT(*) FROM {t}", default=0)
                       for t in ("geocode_cache", "bin_cache",
                                 "elicense_notifications"))
        if existing:
            print("[storage] база не пуста, перенос из JSON пропущен "
                  f"(в базе {existing} записей)")
            return {"geocode": 0, "bin": 0, "companies": 0, "notifications": 0,
                    "skipped": True}

    moved = {"geocode": 0, "bin": 0, "companies": 0, "notifications": 0}

    # --- кэш геокодирования ---
    data = _load_json(geocode_file)
    if isinstance(data, dict):
        now = _now()
        with db.transaction() as conn:
            for address, value in data.items():
                lat = lon = display = None
                if isinstance(value, (list, tuple)) and len(value) == 3:
                    lat, lon, display = value
                conn.execute(
                    "INSERT INTO geocode_cache"
                    " (address, lat, lon, display, updated_at)"
                    " VALUES (?,?,?,?,?)"
                    " ON CONFLICT(address) DO UPDATE SET"
                    " lat=excluded.lat, lon=excluded.lon,"
                    " display=excluded.display, updated_at=excluded.updated_at",
                    (address, lat, lon, display, now))
                moved["geocode"] += 1

    # --- кэш БИН ---
    data = _load_json(bin_file)
    if isinstance(data, dict):
        now = _now()
        with db.transaction() as conn:
            for customer, bin_val in data.items():
                conn.execute(
                    "INSERT INTO bin_cache (customer, bin, updated_at)"
                    " VALUES (?,?,?)"
                    " ON CONFLICT(customer) DO UPDATE SET"
                    " bin=excluded.bin, updated_at=excluded.updated_at",
                    (customer, bin_val, now))
                moved["bin"] += 1

    # --- хранилище eLicense ---
    data = _load_json(elicense_file)
    if isinstance(data, dict) and isinstance(data.get("companies"), dict):
        now = _now()
        with db.transaction() as conn:
            for bin_val, entry in data["companies"].items():
                conn.execute(
                    "INSERT INTO elicense_companies"
                    " (bin, company, fetched_at, enriched_at, attempts, pages)"
                    " VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(bin) DO UPDATE SET"
                    " company=excluded.company, fetched_at=excluded.fetched_at,"
                    " enriched_at=excluded.enriched_at",
                    (bin_val, entry.get("company"), entry.get("fetched_at"),
                     entry.get("enriched_at"), entry.get("attempts"),
                     entry.get("pages")))

                for notif in entry.get("notifications") or []:
                    conn.execute(_UPSERT_NOTIFICATION_SQL,
                                 (bin_val, dedupe_key(notif))
                                 + tuple(notif.get(f) for f in NOTIFICATION_FIELDS))
                    moved["notifications"] += 1
                moved["companies"] += 1

    return moved


def _upsert_sql(table="elicense_notifications"):
    """INSERT ... ON CONFLICT(bin, dedupe_key) DO UPDATE по всем полям."""
    cols = ["bin", "dedupe_key"] + list(NOTIFICATION_FIELDS)
    placeholders = ",".join("?" * len(cols))
    updates = ", ".join(f"{c}=excluded.{c}" for c in NOTIFICATION_FIELDS)
    return (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(bin, dedupe_key) DO UPDATE SET {updates}")


_UPSERT_NOTIFICATION_SQL = _upsert_sql()


def _now():
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def wait_for_db(path=DEFAULT_DB_FILE, timeout=10.0):
    """Ждёт появления файла базы — для стартовых проверок."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.1)
    return False


# Единственный экземпляр на процесс
db = Database(os.environ.get("MAPTOOL_DB", DEFAULT_DB_FILE))