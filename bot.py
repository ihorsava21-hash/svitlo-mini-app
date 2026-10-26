import os
import json
import random
import threading
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import telebot
from telebot import types


# =========================
# НАЛАШТУВАННЯ
# =========================

BOT_TOKEN = os.environ['BOT_TOKEN']
ADMIN_CHAT_ID = os.environ.get('ADMIN_CHAT_ID', '367762402')

DEFAULT_GROUP = '35.1'

YASNO_API = (
    "https://app.yasno.ua/api/blackout-service/public/"
    "shutdowns/regions/25/dsos/902/planned-outages"
)

KYIV_TZ = ZoneInfo("Europe/Kyiv")

API_HOST = "127.0.0.1"
API_PORT = 8080

CACHE_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

bot = telebot.TeleBot(BOT_TOKEN)

# Черги користувачів зберігаються в пам'яті.
# Після перезапуску бота вони скинуться.
user_groups = {}

# Кеш відповіді YASNO
yasno_cache = {
    "timestamp": 0,
    "data": None
}

cache_lock = threading.Lock()


# =========================
# ЧАС
# =========================

def kyiv_now():
    return datetime.now(KYIV_TZ)


def format_date(date_obj):
    return date_obj.strftime("%d.%m.%Y")


def get_target_date(day_offset=0):
    return (kyiv_now() + timedelta(days=day_offset)).date()


# =========================
# ЗАПИТ ДО YASNO
# =========================

def fetch_yasno_data():
    """Отримує графік YASNO з кешуванням."""
    now_timestamp = datetime.now().timestamp()

    with cache_lock:
        cached_data = yasno_cache["data"]
        cache_timestamp = yasno_cache["timestamp"]

        if (
            cached_data is not None
            and now_timestamp - cache_timestamp < CACHE_SECONDS
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
        with urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))

        with cache_lock:
            yasno_cache["data"] = data
            yasno_cache["timestamp"] = now_timestamp

        return data

    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        logging.exception("Помилка запиту до YASNO: %s", error)

        # Якщо запит не вдався, повертаємо кешовані дані, якщо вони є.
        with cache_lock:
            if yasno_cache["data"] is not None:
                return yasno_cache["data"]

        return None

    except Exception:
        logging.exception("Неочікувана помилка під час запиту до YASNO")
        return None


def extract_schedule(data, group, target_date):
    """
    Витягує графік для черги та дати.

    Структура відповіді YASNO може змінюватися, тому тут
    передбачена обробка кількох типових варіантів.
    """
    if data is None:
        return None

    date_string = target_date.isoformat()

    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        records = (
            data.get("outages")
            or data.get("data")
            or data.get("plannedOutages")
            or []
        )

        if isinstance(records, dict):
            records = records.get("outages", [])
    else:
        return None

    if not isinstance(records, list):
        return None

    matching_records = []

    for record in records:
        if not isinstance(record, dict):
            continue

        record_group = (
            record.get("group")
            or record.get("queue")
            or record.get("shutdownGroup")
            or record.get("groupName")
        )

        record_date = (
            record.get("date")
            or record.get("shutdownDate")
            or record.get("day")
        )

        if record_group is not None and str(record_group) != str(group):
            continue

        if record_date is not None and not str(record_date).startswith(date_string):
            continue

        matching_records.append(record)

    if matching_records:
        return matching_records

    # Якщо API повертає графік у вкладеній структурі,
    # повертаємо дані для окремої подальшої перевірки.
    return {
        "message": (
            "Не вдалося визначити структуру графіка у відповіді YASNO. "
            "Можливо, формат API змінився."
        ),
        "raw": data,
    }


def format_schedule(schedule, group, target_date):
    if schedule is None:
        return (
            "Не вдалося отримати графік YASNO. "
            "Спробуйте ще раз трохи пізніше."
        )

    if isinstance(schedule, dict) and "message" in schedule:
        logging.warning("Неочікуваний формат відповіді YASNO")
        return (
            f"📅 Дата: {format_date(target_date)}\n"
            f"🔌 Черга: {group}\n\n"
            "Не вдалося розпізнати формат графіка YASNO. "
            "Потрібно перевірити актуальну структуру відповіді API."
        )

    lines = [
        f"📅 Дата: {format_date(target_date)}",
        f"🔌 Черга: {group}",
        "",
    ]

    found_period = False

    for record in schedule:
        start = (
            record.get("start")
            or record.get("startTime")
            or record.get("from")
        )
        end = (
            record.get("end")
            or record.get("endTime")
            or record.get("to")
        )

        if start or end:
            lines.append(f"⏰ {start or '?'} — {end or '?'}")
            found_period = True

        status = record.get("status") or record.get("type")
        if status:
            lines.append(f"Статус: {status}")

    if not found_period:
        lines.append(
            "Графік отримано, але формат інтервалів не розпізнано."
        )

    return "\n".join(lines)


def get_schedule_text(group, day_offset=0):
    target_date = get_target_date(day_offset)
    data = fetch_yasno_data()

    schedule = extract_schedule(data, group, target_date)

    return format_schedule(schedule, group, target_date)


# =========================
# ECOFLOW — ПОКИ ЗАГЛУШКА
# =========================

def get_ecoflow_status():
    """
    Тимчасова заглушка.
    Пізніше тут можна підключити реальні дані EcoFlow.
    """
    return {
        "connected": False,
        "message": "Інтеграцію EcoFlow ще не налаштовано."
    }


# =========================
# TELEGRAM-БОТ
# =========================

def main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2
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


def get_user_group(user_id):
    return user_groups.get(user_id, DEFAULT_GROUP)


def save_user_group(user_id, group):
    user_groups[user_id] = group


@bot.message_handler(commands=["start", "help"])
def start_command(message):
    user_id = message.from_user.id
    group = get_user_group(user_id)

    bot.send_message(
        message.chat.id,
        (
            "Привіт! 👋\n\n"
            "Я допомагаю переглядати графік відключень YASNO.\n\n"
            f"Твоя поточна черга: {group}\n\n"
            "Обери дію в меню нижче."
        ),
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["group"])
def group_command(message):
    bot.send_message(
        message.chat.id,
        "Введи свою чергу, наприклад: 3.1 або 5.2"
    )


@bot.message_handler(func=lambda message: message.text == "📅 Сьогодні")
def today_handler(message):
    group = get_user_group(message.from_user.id)

    bot.send_message(
        message.chat.id,
        get_schedule_text(group, 0),
        reply_markup=main_keyboard(),
    )


@bot.message_handler(func=lambda message: message.text == "📅 Завтра")
def tomorrow_handler(message):
    group = get_user_group(message.from_user.id)

    bot.send_message(
        message.chat.id,
        get_schedule_text(group, 1),
        reply_markup=main_keyboard(),
    )


@bot.message_handler(func=lambda message: message.text == "🔌 Моя черга")
def my_group_handler(message):
    group = get_user_group(message.from_user.id)

    bot.send_message(
        message.chat.id,
        f"Твоя поточна черга: {group}",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(func=lambda message: message.text == "✏️ Змінити чергу")
def change_group_handler(message):
    bot.send_message(
        message.chat.id,
        "Введи номер черги, наприклад 3.1 або 5.2."
    )


@bot.message_handler(func=lambda message: message.text == "🔋 EcoFlow")
def ecoflow_handler(message):
    status = get_ecoflow_status()

    bot.send_message(
        message.chat.id,
        "🔋 EcoFlow\n\n" + status["message"],
        reply_markup=main_keyboard(),
    )


@bot.message_handler(func=lambda message: message.text is not None)
def text_handler(message):
    text = message.text.strip()
    user_id = message.from_user.id

    # Не трактуємо довільний текст як номер черги.
    valid_groups = {
        f"{major}.{minor}"
        for major in range(1, 7)
        for minor in range(1, 3)
    }

    if text in valid_groups:
        save_user_group(user_id, text)

        bot.send_message(
            message.chat.id,
            f"✅ Чергу змінено на {text}.",
            reply_markup=main_keyboard(),
        )
        return

    bot.send_message(
        message.chat.id,
        "Не розпізнав команду. Скористайся кнопками меню "
        "або введи номер черги, наприклад 3.1.",
        reply_markup=main_keyboard(),
    )


# =========================
# HTTP API
# =========================

class APIHandler(BaseHTTPRequestHandler):
    def send_json(self, status_code, payload):
        body = json.dumps(
            payload,
            ensure_ascii=False
        ).encode("utf-8")

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            self.send_json(200, {
                "status": "ok",
                "time": kyiv_now().isoformat(),
            })
            return

        if parsed.path == "/ecoflow":
            self.send_json(200, get_ecoflow_status())
            return

        if parsed.path == "/schedule":
            from urllib.parse import parse_qs

            params = parse_qs(parsed.query)
            group = params.get("group", [DEFAULT_GROUP])[0]
            day = params.get("day", ["0"])[0]

            valid_groups = {
                f"{major}.{minor}"
                for major in range(1, 7)
                for minor in range(1, 3)
            }

            if group not in valid_groups:
                self.send_json(400, {
                    "error": "Invalid group",
                    "allowed_example": "3.1",
                })
                return

            if day not in {"0", "1"}:
                self.send_json(400, {
                    "error": "Parameter day must be 0 or 1"
                })
                return

            target_date = get_target_date(int(day))
            data = fetch_yasno_data()
            schedule = extract_schedule(data, group, target_date)

            self.send_json(200, {
                "group": group,
                "date": target_date.isoformat(),
                "schedule": schedule,
            })
            return

        self.send_json(404, {"error": "Not found"})

    def log_message(self, format_string, *args):
        logging.info("HTTP API: " + format_string, *args)


def run_http_server():
    server = HTTPServer((API_HOST, API_PORT), APIHandler)

    logging.info(
        "HTTP API запущено: http://%s:%s",
        API_HOST,
        API_PORT,
    )

    server.serve_forever()


# =========================
# ЗАПУСК
# =========================

if __name__ == "__main__":
    server_thread = threading.Thread(
        target=run_http_server,
        daemon=True,
    )
    server_thread.start()

    logging.info("Telegram-бот запускається")

    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
            )
        except Exception:
            logging.exception("Помилка Telegram-бота")