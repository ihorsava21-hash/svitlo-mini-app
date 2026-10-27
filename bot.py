import os
import json
import time
import logging
import threading

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import telebot
from telebot import types


# =========================================================
# НАЛАШТУВАННЯ
# =========================================================

# На Render додай Environment Variable: BOT_TOKEN
BOT_TOKEN = os.environ["BOT_TOKEN"]

ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "367762402")

DEFAULT_GROUP = os.environ.get("DEFAULT_GROUP", "35.1")

YASNO_API = (
    "https://app.yasno.ua/api/blackout-service/public/"
    "shutdowns/regions/25/dsos/902/planned-outages"
)

KYIV_TZ = ZoneInfo("Europe/Kyiv")

API_HOST = "0.0.0.0"
API_PORT = int(os.environ.get("PORT", "8080"))

CACHE_SECONDS = 60
REQUEST_TIMEOUT = 15
ALERT_CHECK_SECONDS = 300
ALERT_LEAD_MINUTES = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

bot = telebot.TeleBot(BOT_TOKEN)

# Вибрані черги зберігаються в оперативній пам'яті.
# Після перезапуску сервісу вони скидаються.
user_groups = {}

# Підписки та попередні версії графіків скидаються після перезапуску Render.
alert_subscribers = {}
schedule_fingerprints = {}
sent_schedule_reminders = set()
alert_lock = threading.Lock()

# Кеш даних YASNO
yasno_cache = {
    "timestamp": 0,
    "data": None,
    "last_error": None,
}

cache_lock = threading.Lock()


# =========================================================
# ДОПОМІЖНІ ФУНКЦІЇ
# =========================================================

def kyiv_now():
    return datetime.now(KYIV_TZ)


def normalize_yasno_response(data):
    """
    За потреби розгортає типові обгортки відповіді API.
    Основні дані очікуються як словник черг:
    {
        "1.1": {
            "today": {"slots": [...]},
            "tomorrow": {"slots": [...]}
        }
    }
    """
    if not isinstance(data, dict):
        return None

    # Не розгортаємо data, якщо верхній рівень уже містить черги.
    if any(
        isinstance(value, dict)
        and ("today" in value or "tomorrow" in value)
        for value in data.values()
    ):
        return data

    nested = data.get("data")

    if isinstance(nested, dict):
        return nested

    return data


def get_yasno_cache_metadata():
    with cache_lock:
        timestamp = yasno_cache["timestamp"]
        has_data = yasno_cache["data"] is not None
        last_error = yasno_cache["last_error"]

    return {
        "dataFetchedAt": (
            datetime.fromtimestamp(timestamp, KYIV_TZ).isoformat(timespec="seconds")
            if timestamp
            else None
        ),
        "dataIsStale": bool(has_data and last_error),
        "refreshError": bool(last_error),
    }


def fetch_yasno_data(force_refresh=False):
    """Завантажує графік YASNO з кешуванням."""
    now = time.time()

    with cache_lock:
        cached_data = yasno_cache["data"]
        cached_at = yasno_cache["timestamp"]

        if (
            not force_refresh
            and cached_data is not None
            and now - cached_at < CACHE_SECONDS
        ):
            return cached_data

    request = Request(
        YASNO_API,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw_data = json.loads(
                response.read().decode("utf-8")
            )

        data = normalize_yasno_response(raw_data)
        has_schedule = isinstance(data, dict) and any(
            isinstance(value, dict)
            and (
                isinstance(value.get("today"), dict)
                or isinstance(value.get("tomorrow"), dict)
            )
            for value in data.values()
        )
        if not has_schedule:
            raise ValueError("YASNO повернув відповідь без даних графіка")

        with cache_lock:
            yasno_cache["data"] = data
            yasno_cache["timestamp"] = time.time()
            yasno_cache["last_error"] = None

        return data

    except Exception as error:
        logging.warning("Помилка отримання даних YASNO: %s", error)

        with cache_lock:
            yasno_cache["last_error"] = str(error)
            # Кеш повертається, але API позначить його як застарілий.
            return yasno_cache["data"]

def get_groups(data=None):
    """Повертає черги, які реально є у відповіді YASNO."""
    if data is None:
        data = fetch_yasno_data()

    if not isinstance(data, dict):
        return []

    groups = []

    for key, value in data.items():
        if not isinstance(value, dict):
            continue

        if (
            isinstance(value.get("today"), dict)
            or isinstance(value.get("tomorrow"), dict)
        ):
            groups.append(str(key))

    def sort_key(group):
        return tuple(
            int(part) if part.isdigit() else part
            for part in group.split(".")
        )

    return sorted(groups, key=sort_key)


def get_group_day_data(group, day, data=None):
    """Повертає графік конкретної черги на today або tomorrow."""
    if day not in ("today", "tomorrow"):
        return None

    if data is None:
        data = fetch_yasno_data()

    if not isinstance(data, dict):
        return None

    group_data = data.get(group)

    if not isinstance(group_data, dict):
        return None

    day_data = group_data.get(day)

    if not isinstance(day_data, dict):
        return None

    return day_data


def get_schedule_payload(group, day, force_refresh=False):
    """
    Формує формат відповіді для HTML:
    {
        "group": "1.1",
        "day": "today",
        "date": "...",
        "status": "...",
        "updatedOn": "...",
        "slots": [...]
    }
    """
    data = fetch_yasno_data(force_refresh=force_refresh)
    cache_metadata = get_yasno_cache_metadata()

    if data is None:
        return {
            "error": "Не вдалося отримати дані YASNO",
            **cache_metadata,
        }, 502

    group_data = data.get(group)

    if not isinstance(group_data, dict):
        return {
            "error": f"Чергу {group} не знайдено у відповіді YASNO",
        }, 404

    day_data = group_data.get(day)

    if not isinstance(day_data, dict):
        return {
            "error": f"Графік на {day} поки недоступний",
        }, 503

    slots = day_data.get("slots")

    if not isinstance(slots, list):
        return {
            "error": "YASNO повернув графік у невідомому форматі",
        }, 502

    # Перевіряємо, що кожен інтервал має коректні межі.
    valid_slots = []

    for slot in slots:
        if not isinstance(slot, dict):
            continue

        start = slot.get("start")
        end = slot.get("end")

        if (
            isinstance(start, (int, float))
            and not isinstance(start, bool)
            and isinstance(end, (int, float))
            and not isinstance(end, bool)
            and 0 <= start < end <= 1440
        ):
            valid_slots.append(slot)

    return {
        "group": group,
        "day": day,
        "date": day_data.get("date"),
        "status": day_data.get("status"),
        "updatedOn": group_data.get("updatedOn"),
        **cache_metadata,
        "slots": valid_slots,
    }, 200


def format_minutes(minutes):
    hours = int(minutes) // 60
    mins = int(minutes) % 60
    return f"{hours:02d}:{mins:02d}"



def format_schedule_for_telegram(group, day):
    payload, status_code = get_schedule_payload(group, day)

    if status_code != 200:
        return payload.get("error", "Не вдалося отримати графік.")

    slots = payload["slots"]

    status_map = {
        "ScheduleApplies": "Графік відключень діє",
        "WaitingForSchedule": "Графік очікується",
        "NoShutdowns": "Планових відключень немає",
        "PlannedShutdowns": "Є планові відключення",
        "EmergencyShutdowns": "Аварійні відключення",
    }

    raw_status = payload.get("status")
    status = status_map.get(raw_status, "Статус уточнюється")

    date = payload.get("date") or "Дата не вказана"

    if isinstance(date, str) and date != "Дата не вказана":
        try:
            parsed_date = datetime.fromisoformat(date.replace("Z", "+00:00"))
            if parsed_date.tzinfo is not None:
                parsed_date = parsed_date.astimezone(KYIV_TZ)
            date = parsed_date.strftime("%d.%m.%Y")
        except ValueError:
            pass

    day_label = "Сьогодні" if day == "today" else "Завтра"

    lines = [
        f"📅 {day_label}: {date}",
        f"🔌 Черга: {group}",
        f"📊 Статус: {status}",
        "",
    ]

    outages = [
        slot
        for slot in slots
        if str(slot.get("type", "")).lower().replace("_", "")
        in ("definite", "definiteoutage")
    ]

    outages.sort(key=lambda slot: slot["start"])

    if outages:
        lines.append("🔴 Періоди відключень:")
        for slot in outages:
            lines.append(
                f"• {format_minutes(slot['start'])}"
                f"–{format_minutes(slot['end'])}"
            )
    else:
        lines.append(
            "У даних немає визначених періодів відключення."
        )

    lines.extend([
        "",
        "ℹ️ Це плановий графік, а не підтвердження "
        "фактичної наявності електрики.",
    ])

    return "\n".join(lines)
# =========================================================
# ECOFLOW — ПОКИ ЗАГЛУШКА
# =========================================================

def format_kyiv_timestamp(value):
    if not value:
        return "час невідомий"

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=KYIV_TZ)
        return parsed.astimezone(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return str(value)


def schedule_fingerprint(group_data, day):
    day_data = group_data.get(day) if isinstance(group_data, dict) else None
    if not isinstance(day_data, dict):
        return None

    slots = day_data.get("slots")
    if not isinstance(slots, list):
        return None

    normalized_slots = []
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        start = slot.get("start")
        end = slot.get("end")
        if (
            isinstance(start, (int, float))
            and not isinstance(start, bool)
            and isinstance(end, (int, float))
            and not isinstance(end, bool)
            and 0 <= start < end <= 1440
        ):
            slot_type = str(slot.get("type", "")).lower()
            slot_type = slot_type.replace("_", "").replace("-", "").replace(" ", "")
            normalized_slots.append((start, end, slot_type))

    normalized_slots.sort()
    signature = {
        "date": day_data.get("date"),
        "status": day_data.get("status"),
        "slots": normalized_slots,
    }
    return json.dumps(signature, ensure_ascii=False, sort_keys=True)


def set_schedule_baseline(group, data):
    with alert_lock:
        group_data = data.get(group) if isinstance(data, dict) else None
        for day in ("today", "tomorrow"):
            key = (group, day)
            fingerprint = schedule_fingerprint(group_data, day)
            if fingerprint is None:
                schedule_fingerprints.pop(key, None)
            else:
                schedule_fingerprints[key] = fingerprint


def clear_schedule_baseline(group):
    with alert_lock:
        for day in ("today", "tomorrow"):
            schedule_fingerprints.pop((group, day), None)


def get_upcoming_schedule_reminders(group_data, now):
    """Повертає планові переходи в межах вікна попередження."""
    events = []
    for day in ("today", "tomorrow"):
        day_data = group_data.get(day) if isinstance(group_data, dict) else None
        if not isinstance(day_data, dict) or day_data.get("status") not in ("ScheduleApplies", "PlannedShutdowns"):
            continue
        try:
            event_date = date.fromisoformat(str(day_data.get("date"))[:10])
        except (TypeError, ValueError):
            continue
        slots = day_data.get("slots")
        if not isinstance(slots, list):
            continue
        outages = []
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            kind = str(slot.get("type", "")).lower().replace("_", "").replace("-", "")
            start, end = slot.get("start"), slot.get("end")
            if kind in ("definite", "definiteoutage") and isinstance(start, (int, float)) and not isinstance(start, bool) and isinstance(end, (int, float)) and not isinstance(end, bool) and 0 <= start < end <= 1440:
                outages.append((int(start), int(end)))
        merged = []
        for start, end in sorted(outages):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        for start, end in merged:
            for minute, event_kind, label in (
                (start, "off", "планове вимкнення"),
                (end, "on", "планове відновлення електропостачання"),
            ):
                event_at = datetime.combine(event_date, datetime.min.time(), KYIV_TZ) + timedelta(minutes=minute)
                minutes_left = (event_at - now).total_seconds() / 60
                if 0 < minutes_left <= ALERT_LEAD_MINUTES:
                    events.append((event_date.isoformat(), event_kind, minute, label, event_at))
    return events

def check_schedule_changes():
    data = fetch_yasno_data(force_refresh=True)
    if data is None or get_yasno_cache_metadata()["dataIsStale"]:
        return

    changes = []
    reminders = []
    now = kyiv_now()
    with alert_lock:
        for group in get_groups(data):
            group_data = data.get(group)
            for day in ("today", "tomorrow"):
                fingerprint = schedule_fingerprint(group_data, day)
                if fingerprint is None:
                    continue
                key = (group, day)
                previous = schedule_fingerprints.get(key)
                if previous is not None and previous != fingerprint:
                    changes.append((group, day))
                schedule_fingerprints[key] = fingerprint
        subscribers = list(alert_subscribers.items())

        for group in get_groups(data):
            for event_date, kind, minute, label, event_at in get_upcoming_schedule_reminders(data.get(group), now):
                key = (group, event_date, kind, minute)
                if key not in sent_schedule_reminders:
                    reminders.append((key, group, label, event_at))

    for group, day in changes:
        message_text = "🔔 Графік YASNO змінився.\n\n" + format_schedule_for_telegram(group, day)
        target_chats = {
            chat_id
            for user_id, chat_id in subscribers
            if get_user_group(user_id) == group
        }
        for chat_id in target_chats:
            try:
                bot.send_message(chat_id, message_text)
            except Exception:
                logging.exception("Не вдалося надіслати сповіщення про зміну графіка")


    for reminder_key, group, label, event_at in reminders:
        message_text = (
            f"⏰ За графіком YASNO через {ALERT_LEAD_MINUTES} хв — {label}.\n"
            f"Черга: {group}\n"
            f"Очікуваний час: {event_at.strftime('%H:%M')} за Києвом.\n\n"
            "Це нагадування за планом, а не підтвердження фактичного стану мережі."
        )
        target_chats = {chat_id for user_id, chat_id in subscribers if get_user_group(user_id) == group}
        if not target_chats:
            continue
        sent_any = False
        for chat_id in target_chats:
            try:
                bot.send_message(chat_id, message_text)
                sent_any = True
            except Exception:
                logging.exception("Не вдалося надіслати нагадування про подію графіка")
        if sent_any:
            with alert_lock:
                sent_schedule_reminders.add(reminder_key)

def run_schedule_alert_monitor():
    while True:
        try:
            check_schedule_changes()
        except Exception:
            logging.exception("Помилка перевірки змін графіка")
        time.sleep(ALERT_CHECK_SECONDS)


def get_ecoflow_status():
    return {
        "connected": False,
        "message": "Інтеграцію EcoFlow ще не налаштовано.",
    }


# =========================================================
# TELEGRAM-БОТ
# =========================================================

def get_user_group(user_id):
    return user_groups.get(user_id, DEFAULT_GROUP)


def main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )

    keyboard.add(
        types.KeyboardButton("📅 Сьогодні"),
        types.KeyboardButton("📅 Завтра"),
    )
    keyboard.add(
        types.KeyboardButton("🔌 Моя черга"),
        types.KeyboardButton("✏️ Змінити чергу"),
    )
    keyboard.add(types.KeyboardButton("🔋 EcoFlow"))

    return keyboard


def is_valid_group(group):
    return group in get_groups()


@bot.message_handler(commands=["start", "help"])
def start_command(message):
    group = get_user_group(message.from_user.id)

    bot.send_message(
        message.chat.id,
        (
            "Привіт! 👋\n\n"
            "Я допомагаю переглядати графік відключень YASNO.\n\n"
            f"Твоя черга: {group}\n\n"
            "Обери дію в меню або введи номер черги, "
            "наприклад 1.1.\n\n"
            "Сповіщення про зміни графіка: /subscribe — увімкнути, "
            "/unsubscribe — вимкнути, /alerts — перевірити стан."
        ),
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["group"])
def group_command(message):
    bot.send_message(
        message.chat.id,
        "Введи номер своєї черги, наприклад 1.1.",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["refresh"])
def refresh_command(message):
    data = fetch_yasno_data(force_refresh=True)
    cache_metadata = get_yasno_cache_metadata()

    if data is None:
        response = "Не вдалося оновити дані YASNO: актуального кешу немає."
    elif cache_metadata["dataIsStale"]:
        fetched_at = format_kyiv_timestamp(cache_metadata["dataFetchedAt"])
        response = (
            "YASNO зараз недоступний. Показую застарілий кеш, "
            f"отриманий {fetched_at} за київським часом."
        )
    else:
        fetched_at = format_kyiv_timestamp(cache_metadata["dataFetchedAt"])
        response = f"Дані YASNO оновлено {fetched_at} за київським часом."

    bot.send_message(
        message.chat.id,
        response,
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["subscribe"])
def subscribe_command(message):
    user_id = message.from_user.id
    group = get_user_group(user_id)
    data = fetch_yasno_data(force_refresh=True)
    cache_metadata = get_yasno_cache_metadata()

    if data is not None and not cache_metadata["dataIsStale"]:
        set_schedule_baseline(group, data)
    else:
        clear_schedule_baseline(group)

    with alert_lock:
        alert_subscribers[user_id] = message.chat.id

    bot.send_message(
        message.chat.id,
        (
            f"🔔 Сповіщення увімкнено для черги {group}. "
            "Напишу про зміни графіка та за 15 хвилин до планового вимкнення чи відновлення. "
            "Після перезапуску бота підписку потрібно буде ввімкнути знову."
        ),
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["unsubscribe"])
def unsubscribe_command(message):
    with alert_lock:
        alert_subscribers.pop(message.from_user.id, None)

    bot.send_message(
        message.chat.id,
        "🔕 Сповіщення вимкнено.",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["alerts"])
def alerts_command(message):
    user_id = message.from_user.id
    with alert_lock:
        is_subscribed = user_id in alert_subscribers

    if is_subscribed:
        text = (
            f"🔔 Сповіщення увімкнено для черги {get_user_group(user_id)}. "
            "Для вимкнення: /unsubscribe."
        )
    else:
        text = "🔕 Сповіщення вимкнено. Для підписки: /subscribe."

    bot.send_message(message.chat.id, text, reply_markup=main_keyboard())


@bot.message_handler(
    func=lambda message: message.text == "📅 Сьогодні"
)
def today_handler(message):
    group = get_user_group(message.from_user.id)

    bot.send_message(
        message.chat.id,
        format_schedule_for_telegram(group, "today"),
        reply_markup=main_keyboard(),
    )


@bot.message_handler(
    func=lambda message: message.text == "📅 Завтра"
)
def tomorrow_handler(message):
    group = get_user_group(message.from_user.id)

    bot.send_message(
        message.chat.id,
        format_schedule_for_telegram(group, "tomorrow"),
        reply_markup=main_keyboard(),
    )


@bot.message_handler(
    func=lambda message: message.text == "🔌 Моя черга"
)
def my_group_handler(message):
    group = get_user_group(message.from_user.id)

    bot.send_message(
        message.chat.id,
        f"Твоя поточна черга: {group}",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(
    func=lambda message: message.text == "✏️ Змінити чергу"
)
def change_group_handler(message):
    bot.send_message(
        message.chat.id,
        "Введи номер черги, наприклад 1.1."
    )


@bot.message_handler(
    func=lambda message: message.text == "🔋 EcoFlow"
)
def ecoflow_handler(message):
    status = get_ecoflow_status()

    bot.send_message(
        message.chat.id,
        "🔋 EcoFlow\n\n" + status["message"],
        reply_markup=main_keyboard(),
    )


@bot.message_handler(
    content_types=["text"],
    func=lambda message: bool(message.text)
)
def text_handler(message):
    text = message.text.strip()
    user_id = message.from_user.id

    if text.startswith("/"):
        return

    # Не дозволяємо випадковому тексту змінити чергу.
    if not is_valid_group(text):
        bot.send_message(
            message.chat.id,
            (
                "Не знайшов такої черги у поточних даних YASNO.\n"
                "Перевір номер або спробуй пізніше."
            ),
            reply_markup=main_keyboard(),
        )
        return

    user_groups[user_id] = text

    bot.send_message(
        message.chat.id,
        f"✅ Чергу змінено на {text}.",
        reply_markup=main_keyboard(),
    )


# =========================================================
# HTTP API ДЛЯ HTML POWER / KYIV
# =========================================================

class APIHandler(BaseHTTPRequestHandler):

    def send_json(self, status_code, payload):
        body = json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8")

        self.send_response(status_code)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/health":
            self.send_json(200, {
                "status": "ok",
                "time": kyiv_now().isoformat(),
            })
            return

        if parsed.path == "/groups":
            data = fetch_yasno_data()

            if data is None:
                self.send_json(502, {
                    "error": "Не вдалося отримати дані YASNO",
                    "groups": [],
                })
                return

            self.send_json(200, {
                "groups": get_groups(data),
            })
            return

        if parsed.path == "/schedule":
            group = params.get("group", [DEFAULT_GROUP])[0]
            day = params.get("day", ["today"])[0]
            force_refresh = params.get("refresh", ["0"])[0].lower() in (
                "1", "true", "yes"
            )

            if day not in ("today", "tomorrow"):
                self.send_json(400, {
                    "error": "Параметр day має бути today або tomorrow",
                })
                return

            payload, status_code = get_schedule_payload(
                group,
                day,
                force_refresh=force_refresh,
            )
            self.send_json(status_code, payload)
            return

        if parsed.path == "/ecoflow":
            self.send_json(200, get_ecoflow_status())
            return

        self.send_json(404, {
            "error": "Маршрут не знайдено",
        })

    def log_message(self, format_string, *args):
        logging.info(
            "HTTP %s - %s",
            self.address_string(),
            format_string % args,
        )


def run_http_server():
    server = ThreadingHTTPServer(
        (API_HOST, API_PORT),
        APIHandler,
    )

    logging.info(
        "HTTP API запущено на порту %s",
        API_PORT,
    )

    server.serve_forever()


# =========================================================
# ЗАПУСК
# =========================================================

if __name__ == "__main__":
    server_thread = threading.Thread(
        target=run_http_server,
        daemon=True,
    )
    server_thread.start()

    alert_thread = threading.Thread(
        target=run_schedule_alert_monitor,
        daemon=True,
    )
    alert_thread.start()

    logging.info("Telegram-бот запускається")

    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=True,
            )
        except Exception:
            logging.exception("Помилка Telegram polling")
            time.sleep(5)