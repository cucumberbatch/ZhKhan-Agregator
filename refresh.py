"""
Фоновое обновление данных: раз в сутки, в 3:00 по времени Астаны.

Задание не «просто опрашивает сайты», а ищет, что изменилось:

  * новые объекты в реестре проектов qportal — сообщаем с именем
    застройщика и адресом;
  * новые уведомления о начале СМР — сообщаем с датой выдачи и
    объектом; если уведомление удалось сопоставить с ЖК, присылаем
    и его название.

Состояние «что уже показывали пользователям» лежит в БД
(sent_notifications), поэтому перезапуск сервера не приводит к
повторной рассылке, а потеря одного уведомления приводит — в этом
и смысл журнала.

Почему в потоке сервера, а не отдельной задачей: обновление должно
идти в том же процессе, где уже лежит SQLite-база и браузер
Playwright. Отдельный процесс означал бы вторую копию состояния.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

# Астана и Алматы живут по UTC+5 (Казахстан перешёл на единый
# пояс 1 марта 2024). zoneinfo точнее константы: не придётся менять
# код, если страна снова переведёт стрелки.
ASTANA_TZ = "Asia/Almaty"

# Расписание по умолчанию. Меняется переменными окружения.
REFRESH_HOUR = 3
REFRESH_MINUTE = 0

# Не опрашивать чаще этого интервала даже при ручном запуске.
MIN_INTERVAL_S = 600


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(ASTANA_TZ)
    except Exception:
        return timezone(timedelta(hours=5))


def _now():
    return datetime.now(_tz())


def _iso(dt=None):
    return (dt or _now()).isoformat(timespec="seconds")


def seconds_until_next_run(hour=REFRESH_HOUR, minute=REFRESH_MINUTE, now=None):
    """Сколько секунд до следующего запуска в hour:minute по Астане."""
    now = now or _now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


# ============================================================
# ОПРЕДЕЛЕНИЕ ИЗМЕНЕНИЙ
# ============================================================
def _was_sent(db, kind, item_key):
    row = db.query_one(
        "SELECT 1 FROM sent_notifications WHERE kind=? AND item_key=?",
        (kind, item_key))
    return row is not None


def _mark_sent(db, kind, item_key, subscribers):
    db.execute(
        "INSERT INTO sent_notifications (kind, item_key, sent_at, subscribers)"
        " VALUES (?,?,?,?) ON CONFLICT(kind, item_key) DO UPDATE SET"
        " sent_at=excluded.sent_at",
        (kind, item_key, _iso(), subscribers))


def find_new_zhk(db, zhk_list, is_first_run):
    """
    Объекты, которых не было при предыдущем запуске.

    При первом запуске ничего не выдаём: 135 объектов разом — это
    не новость, а шум, и база с этого момента просто считается
    заполненной.
    """
    out = []
    for z in zhk_list:
        key = z.get("id") or f"{z.get('code')}|{z.get('address')}"
        if not _was_sent(db, "zhk", key):
            out.append((key, z))
    return [] if is_first_run else out


def find_new_notifications(db, notifications, is_first_run):
    """
    Уведомления о начале СМР, которых ещё не показывали.

    Ключ — НИКАД документа, он же dedupe_key в хранилище и он же
    устойчив к правке даты выдачи.
    """
    out = []
    for n in notifications:
        key = n.get("nikad") or n.get("unique_number")
        if not key:
            continue
        if not _was_sent(db, "notification", str(key)):
            out.append((str(key), n))
    return [] if is_first_run else out


def _log_refresh(db, started, status, new_zhk, new_smr, notified, message):
    db.execute(
        "INSERT INTO refresh_log (started_at, finished_at, status, new_zhk,"
        " new_smr, notified, message) VALUES (?,?,?,?,?,?,?)",
        (started, _iso(), status, new_zhk, new_smr, notified, message))


# ============================================================
# ТЕКСТЫ СООБЩЕНИЙ
# ============================================================
def fmt_zhk_message(zhk, base_url=""):
    name = (zhk.get("name") or "").strip()
    address = (zhk.get("address") or "").strip()
    customer = (zhk.get("customer") or "").strip()
    lines = ["<b>Новый объект в реестре проектов</b>"]
    if name:
        lines.append(escape_html(name))
    if customer:
        lines.append("Застройщик: " + escape_html(customer))
    if address:
        lines.append("Адрес: " + escape_html(address))
    if zhk.get("code"):
        lines.append("Код: " + escape_html(zhk["code"]))
    if base_url and zhk.get("id"):
        lines.append(f"<a href=\"{base_url}/?zhk={zhk['id']}\">Открыть на карте</a>")
    return "\n".join(lines)


def fmt_notification_message(notif, zhk=None, base_url=""):
    date = notif.get("issue_date_iso") or notif.get("issue_date") or "—"
    company = (notif.get("licensiat") or notif.get("applicant") or "").strip()
    object_name = (notif.get("object_name") or "").strip()

    lines = ["<b>Уведомление о начале СМР</b>",
             "Дата выдачи: " + escape_html(str(date))]
    if zhk:
        lines.append("Объект: " + escape_html((zhk.get("name") or "").strip()))
    elif object_name:
        lines.append("Объект: " + escape_html(object_name))
    if company:
        lines.append("Заказчик: " + escape_html(company))
    if not zhk and object_name:
        lines.append("<i>Объект не найден в реестре проектов — "
                     "на карте его нет.</i>")
    addr = (notif.get("address") or "").strip()
    if addr:
        lines.append("Адрес: " + escape_html(addr))

    # Ссылка на документ в elicense, если она есть; иначе — на карту.
    # Раньше было наоборот, и уведомления со ссылкой уходили в никуда.
    if notif.get("detail_url"):
        lines.append('<a href="' + escape_html(notif["detail_url"]) +
                     '">Документ на elicense</a>')
    elif base_url:
        lines.append(f'<a href="{escape_html(base_url)}/">Открыть карту</a>')
    return "\n".join(lines)


def escape_html(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


# ============================================================
# ПЛАНИРОВЩИК
# ============================================================
class RefreshScheduler:
    """
    Поток с одним заданием в сутки.

    first_run=True подавляет рассылку при самом старте сервера:
    иначе при каждом деплое пользователям прилетала бы пачка
    «новых» объектов, которых на самом деле давно нет.
    """

    def __init__(self, refresh_fn, bot=None, db=None, hour=REFRESH_HOUR,
                 minute=REFRESH_MINUTE, log=print):
        self.refresh_fn = refresh_fn
        self.bot = bot
        self.db = db
        self.hour = hour
        self.minute = minute
        self.log = log
        self.first_run = True
        self._stop = threading.Event()
        self._thread = None
        self._last_run = 0.0

    def start(self):
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="refresh",
                                        daemon=True)
        self._thread.start()
        when = _now().replace(hour=self.hour, minute=self.minute,
                              second=0, microsecond=0)
        if when <= _now():
            when += timedelta(days=1)
        self.log(f"[refresh] ежедневно в {self.hour:02d}:{self.minute:02d}"
                 f" по Астане, ближайший запуск {when:%Y-%m-%d %H:%M}")
        return True

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            wait = seconds_until_next_run(self.hour, self.minute)
            # Просыпаемся кусками, чтобы stop() не ждал до суток
            while wait > 0 and not self._stop.is_set():
                nap = min(wait, 60)
                if self._stop.wait(nap):
                    return
                wait -= nap

            if self._stop.is_set():
                return
            try:
                self.run_now()
            except Exception as e:
                self.log(f"[refresh] сбой задания: {type(e).__name__}: {e}")

    def run_now(self, force=False):
        """Прогоняет обновление. force игнорирует защиту от частых запусков."""
        now = time.time()
        if not force and (now - self._last_run) < MIN_INTERVAL_S:
            self.log("[refresh] пропуск: прошло меньше 10 минут с прошлого")
            return None
        self._last_run = now

        started = _iso()
        self.log("[refresh] старт")
        try:
            result = self.refresh_fn()
        except Exception as e:
            self.log(f"[refresh] ошибка обновления: {type(e).__name__}: {e}")
            if self.db:
                _log_refresh(self.db, started, "error", 0, 0, 0, str(e)[:300])
            return None

        self._apply_result(result, started)
        self.first_run = False
        return result

    def _apply_result(self, result, started):
        """Рассылает найденное и записывает, что уже показано."""
        if not result or self.db is None:
            return

        zhk_list = result.get("zhk") or []
        notifications = result.get("notifications") or []
        base_url = (result.get("base_url") or "").rstrip("/")
        by_zhk_id = {z.get("id"): z for z in zhk_list}

        new_zhk = find_new_zhk(self.db, zhk_list, self.first_run)
        new_notifs = find_new_notifications(self.db, notifications,
                                            self.first_run)

        notified = 0
        if self.bot and self.bot.token:
            for key, zhk in new_zhk:
                text = fmt_zhk_message(zhk, base_url)
                notified += self.bot.broadcast(text)
                _mark_sent(self.db, "zhk", key, notified)

            for key, notif in new_notifs:
                zhk = None
                for slot in (result.get("by_zhk") or {}).values():
                    if any(n.get("nikad") == notif.get("nikad")
                           for n in slot.get("notifications") or []):
                        zhk = {"id": slot.get("zhk_id"),
                               "name": slot.get("zhk_name")}
                        break
                text = fmt_notification_message(notif, zhk, base_url)
                notified += self.bot.broadcast(text)
                _mark_sent(self.db, "notification", key, notified)
        else:
            # Бота нет — всё равно помечаем, иначе при первом же
            # запуске с ботом прилетели бы все накопившиеся объекты
            for key, _ in new_zhk:
                _mark_sent(self.db, "zhk", key, 0)
            for key, _ in new_notifs:
                _mark_sent(self.db, "notification", key, 0)

        _log_refresh(self.db, started, "ok", len(new_zhk), len(new_notifs),
                     notified, None)
        self.log(f"[refresh] готово: новых ЖК {len(new_zhk)}, "
                 f"новых СМР {len(new_notifs)}, отправлено {notified}")