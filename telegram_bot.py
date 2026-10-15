"""
Телеграм-бот: рассылка об изменениях и подписка/отписка.

Бот работает на long polling в отдельном потоке — вебхук не нужен,
значит сервер может стоять за NAT, без белого IP и без домена.
Это важно: бесплатный хостинг почти всегда не даёт входящих
соединений, а исходящие наружу работают.

Что умеет:
  /start — подписаться на уведомления;
  /stop  — отписаться;
  /status — сколько сейчас ЖК, у скольких есть дата начала СМР;
  /help  — список команд.

Уведомления приходят из refresh: новый объект в реестре проектов либо
новое уведомление о начале СМР.

Токен берётся из переменной окружения TELEGRAM_BOT_TOKEN. Без неё
бот просто не стартует, а сервер работает как обычно.
"""

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError

TELEGRAM_API = "https://api.telegram.org/bot"

# Сколько секунд ждать ответа Telegram. Рассылка идёт по одному
# получателю, поэтому медленный ответ одного не должен стопорить всех.
REQUEST_TIMEOUT = 20

# Пауза между сообщениями: ограничение Telegram — около 30 сообщений
# в секунду на бота, но при большой базе подписчиков лучше не давить.
SEND_PAUSE = 0.05

# Polling: Telegram держит getUpdates открытым до 30 секунд.
POLL_TIMEOUT = 30


class TelegramError(Exception):
    pass


def _token():
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()


def _api_url(method, token):
    return f"{TELEGRAM_API}/{token}/{method}"


def _post(url, payload, timeout=REQUEST_TIMEOUT):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url, params, timeout=REQUEST_TIMEOUT):
    full = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(full, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_message(token, chat_id, text, disable_web_page_preview=False):
    """Отправляет одно сообщение. Возвращает True при успехе."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_web_page_preview,
    }
    try:
        data = _post(_api_url("sendMessage", token), payload)
        return bool(data.get("ok"))
    except HTTPError as e:
        raise TelegramError(f"HTTP {e.code}: {e.read()[:200]!r}") from e
    except (URLError, OSError, json.JSONDecodeError) as e:
        raise TelegramError(f"{type(e).__name__}: {e}") from e


class TelegramBot:
    """
    Подписчики в SQLite, приём команд и рассылка в одном потоке.

    Поток один: long polling и рассылка не должны мешать друг другу,
    а пользователей предположительно единицы.
    """

    def __init__(self, db, token=None, log=print):
        self.db = db
        self.token = token or _token()
        self.log = log
        self._stop = threading.Event()
        self._thread = None
        self._offset = None
        self._bot_username = None

    # ---------------------------------------------------------
    # Подписчики
    # ---------------------------------------------------------
    def subscribe(self, chat_id, username=None):
        now = _now()
        self.db.execute(
            "INSERT INTO telegram_users (chat_id, username, active,"
            " created_at, updated_at) VALUES (?,?,1,?,?)"
            " ON CONFLICT(chat_id) DO UPDATE SET active=1,"
            " username=excluded.username, updated_at=excluded.updated_at",
            (str(chat_id), username, now, now))
        return True

    def unsubscribe(self, chat_id):
        self.db.execute(
            "UPDATE telegram_users SET active=0, updated_at=?"
            " WHERE chat_id=?", (_now(), str(chat_id)))

    def subscribers(self):
        return [r["chat_id"] for r in
                self.db.query_all("SELECT chat_id FROM telegram_users"
                                  " WHERE active=1")]

    def stats(self):
        row = self.db.query_one(
            "SELECT COUNT(*) c FROM telegram_users WHERE active=1")
        return row["c"] if row else 0

    # ---------------------------------------------------------
    # Отправка
    # ---------------------------------------------------------
    def broadcast(self, text, preview=False):
        """
        Рассылает сообщение всем активным подписчикам.
        Возвращает число доставленных. Ошибка у одного не мешает
        остальным: у каждого подписчика свой try.
        """
        chats = self.subscribers()
        sent = 0
        for chat_id in chats:
            try:
                if send_message(self.token, chat_id, text,
                                 disable_web_page_preview=preview):
                    sent += 1
                time.sleep(SEND_PAUSE)
            except TelegramError as e:
                self.log(f"[telegram] {chat_id}: {e}")
                # Пользователь мог заблокировать бота — гасим подписку,
                # иначе будем биться в закрытую дверь каждый запуск.
                if "403" in str(e):
                    self.unsubscribe(chat_id)
        return sent

    # ---------------------------------------------------------
    # Команды
    # ---------------------------------------------------------
    def _help_text(self):
        return (
            "Отслеживаю новые объекты и уведомления о начале "
            "строительно-монтажных работ.\n\n"
            "/start — включить уведомления\n"
            "/stop — отключить\n"
            "/status — что сейчас известно\n"
            "/help — эта справка"
        )

    def _status_text(self, summary):
        return (
            "<b>Состояние данных</b>\n"
            f"Объектов в реестре: {summary.get('zhk', 0)}\n"
            f"С датой начала СМР: {summary.get('dated', 0)}\n"
            f"Застройщиков: {summary.get('developers', 0)}\n"
            f"Уведомлений о начале СМР: {summary.get('notifications', 0)}\n"
            f"Обновление данных: {summary.get('last_refresh') or 'ещё не было'}"
        )

    def handle_update(self, update):
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not chat_id:
            return

        text = (message.get("text") or "").strip()
        command = text.split()[0].lower() if text else ""
        command = command.split("@")[0]      # /start@MyBot

        username = chat.get("username")

        try:
            if command in ("/start",):
                self.subscribe(chat_id, username)
                body = ("Уведомления включены.\n\n" + self._help_text())
            elif command in ("/stop", "/unsubscribe"):
                self.unsubscribe(chat_id)
                body = "Уведомления отключены. Включить снова — /start"
            elif command == "/help":
                body = self._help_text()
            elif command == "/status":
                body = self._status_text(self.status_provider()
                                         if hasattr(self, "status_provider")
                                         else {})
            else:
                return    # не наша команда — молчим
            send_message(self.token, chat_id, body)
        except TelegramError as e:
            self.log(f"[telegram] команда не доставлена: {e}")

    # ---------------------------------------------------------
    # Цикл
    # ---------------------------------------------------------
    def _poll_once(self):
        params = {"timeout": POLL_TIMEOUT}
        if self._offset is not None:
            params["offset"] = self._offset

        try:
            # urllib не умеет таймаут длиннее, чем ждёт ответа сервера,
            # поэтому поток сокета выставляем в полтора раза больше
            url = _api_url("getUpdates", self.token)
            with urllib.request.urlopen(
                    url + "?" + urllib.parse.urlencode(params),
                    timeout=POLL_TIMEOUT + 10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, json.JSONDecodeError):
            return      # сеть моргнула — следующий цикл попробует снова

        if not data.get("ok"):
            return

        for update in data.get("result") or []:
            self._offset = update["update_id"] + 1
            try:
                self.handle_update(update)
            except Exception as e:        # команда не должна убивать цикл
                self.log(f"[telegram] сбой обработки: {type(e).__name__}: {e}")

    def _loop(self):
        self.log(f"[telegram] бот запущен, получателей: {self.stats()}")
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as e:
                self.log(f"[telegram] ошибка опроса: {type(e).__name__}: {e}")
                time.sleep(5)
        self.log("[telegram] бот остановлен")

    def start(self):
        if not self.token:
            self.log("[telegram] TELEGRAM_BOT_TOKEN не задан — бот не запущен")
            return False
        if self._thread and self._thread.is_alive():
            return True

        # Имя бота нужно для ссылки в приветствии
        try:
            me = _get(_api_url("getMe", self.token), {})
            self._bot_username = (me.get("result") or {}).get("username")
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as e:
            self.log(f"[telegram] getMe не сработал: {e}")

        self._stop.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name="telegram-bot", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=POLL_TIMEOUT + 10)


def _now():
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")