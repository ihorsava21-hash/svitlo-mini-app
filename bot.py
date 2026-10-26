import telebot
from telebot import types
import requests
import datetime
import random
import os
import json
import time
import threading
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

BOT_TOKEN = os.environ.get('BOT_TOKEN', '8680430959:AAE8T_Q4ffvdl6O7OXZ6cv9UinNbdeuz8g8')
ADMIN_CHAT_ID = os.environ.get('ADMIN_CHAT_ID', '367762402')
DEFAULT_GROUP = '35.1'
YASNO_API = "https://app.yasno.ua/api/blackout-service/public/shutdowns/regions/25/dsos/902/planned-outages"

bot = telebot.TeleBot(BOT_TOKEN)

USER_GROUPS_FILE = "user_groups.json"
user_groups = {}
if os.path.exists(USER_GROUPS_FILE):
    try:
        with open(USER_GROUPS_FILE, "r", encoding="utf-8") as f:
            user_groups = json.load(f)
    except Exception:
        user_groups = {}

def save_user_group(chat_id, group):
    user_groups[str(chat_id)] = str(group)
    try:
        with open(USER_GROUPS_FILE, "w", encoding="utf-8") as f:
            json.dump(user_groups, f, ensure_ascii=False)
    except Exception as e:
        print(f"Помилка запису черги: {e}")

def get_user_group(chat_id):
    return user_groups.get(str(chat_id), DEFAULT_GROUP)

def get_kyiv_now():
    return datetime.datetime.now(ZoneInfo("Europe/Kyiv"))

def hhmm(m):
    h = int(m // 60)
    minute = int(m % 60)
    return f"{h:02d}:{minute:02d}"

def type_of(slot):
    return str(slot.get("type", "")).lower().replace("_", "")

def is_outage(slot):
    t = type_of(slot)
    return t in ["definite", "definiteoutage"]

def is_not_planned(slot):
    return type_of(slot) == "notplanned"

def fetch_full_yasno():
    try:
        headers = {"Accept": "application/json"}
        return requests.get(YASNO_API, headers=headers, timeout=15).json()
    except Exception as e:
        print(f"Помилка YASNO API: {e}")
        return None

def get_available_groups():
    data = fetch_full_yasno()
    if not data or not isinstance(data, dict):
        return [DEFAULT_GROUP]
    # Фільтруємо технічні поля, залишаємо ключі черг
    groups = [k for k in data.keys() if isinstance(data[k], dict) and ("today" in data[k] or "tomorrow" in data[k])]
    return sorted(groups) if groups else [DEFAULT_GROUP]

def get_yasno_data(group=DEFAULT_GROUP, day="today"):
    res = fetch_full_yasno()
    if not res:
        return None, "Не вдалося отримати дані від сервера YASNO"

    # Спроба прямого збігу або альтернативного формату (group_X)
    target = str(group)
    group_data = res.get(target) or res.get(f"group_{target}") or res.get(target.replace("group_", ""))
    
    if not group_data:
        available = ", ".join(list(res.keys())[:8])
        return None, f"Чергу «{group}» не знайдено. Доступні черги: {available}..."

    day_data = group_data.get(day)
    if not day_data:
        day_str = "на завтра" if day == "tomorrow" else "на сьогодні"
        return None, f"Дані {day_str} для черги {group} наразі відсутні"

    status = day_data.get("status")
    slots = day_data.get("slots", [])
    
    if status != "ScheduleApplies":
        day_str = "на завтра" if day == "tomorrow" else "на сьогодні"
        return None, f"Графік {day_str} ще очікується (YASNO формує розклад)"
        
    return slots, None

def build_intervals(slots):
    valid = [s for s in slots if is_outage(s) or is_not_planned(s)]
    valid.sort(key=lambda x: x["start"])

    merged = []
    for s in valid:
        outage = is_outage(s)
        if merged and merged[-1]["end"] == s["start"] and merged[-1]["is_outage"] == outage:
            merged[-1]["end"] = s["end"]
        else:
            merged.append({
                "start": s["start"],
                "end": s["end"],
                "is_outage": outage
            })
    return merged

def format_schedule_text(slots, group, day="today"):
    now = get_kyiv_now()
    now_m = now.hour * 60 + now.minute if day == "today" else -1
    intervals = build_intervals(slots)
    
    day_title = "сьогодні" if day == "today" else "завтра"
    lines = [f"📅 **Графік YASNO ({day_title}) — Черга {group}**"]

    if day == "today":
        current_outage = next((s for s in slots if is_outage(s) and s["start"] <= now_m < s["end"]), None)
        if current_outage:
            status_header = f"🔴 **Світла немає** (до {hhmm(current_outage['end'])})"
        else:
            next_outage = next((s for s in slots if is_outage(s) and s["start"] > now_m), None)
            if next_outage:
                status_header = f"🟢 **Світло є** (відключення о {hhmm(next_outage['start'])})"
            else:
                status_header = "🟢 **Світло є** (відключень далі немає)"
        lines.append(f"Поточний стан: {status_header}\n")

    outages = [i for i in intervals if i["is_outage"]]
    if not outages:
        lines.append("🎉 Відключень за графіком не заплановано!")
    else:
        lines.append("Періоди без світла:")
        for item in outages:
            lines.append(f"🔴 {hhmm(item['start'])}–{hhmm(item['end'])}")

    return "\n".join(lines)

# Фоновий моніторинг
last_known_schedule = None

def monitor_schedule_changes():
    global last_known_schedule
    slots, err = get_yasno_data(DEFAULT_GROUP, "today")
    if slots:
        last_known_schedule = json.dumps(slots, sort_keys=True)
    
    while True:
        try:
            time.sleep(300)
            current_slots, err = get_yasno_data(DEFAULT_GROUP, "today")
            if current_slots:
                current_dump = json.dumps(current_slots, sort_keys=True)
                if last_known_schedule is not None and current_dump != last_known_schedule:
                    last_known_schedule = current_dump
                    msg = "🔔 **УВАГА: Графік оновлено!**\n\n" + format_schedule_text(current_slots, DEFAULT_GROUP, "today")
                    bot.send_message(ADMIN_CHAT_ID, msg, parse_mode="Markdown")
                else:
                    last_known_schedule = current_dump
        except Exception as e:
            print(f"Помилка моніторингу: {e}")

threading.Thread(target=monitor_schedule_changes, daemon=True).start()

# Сервер для Mini App
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/groups':
            groups = get_available_groups()
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"groups": groups}).encode('utf-8'))

        elif parsed.path == '/schedule':
            params = parse_qs(parsed.query)
            group = params.get('group', [DEFAULT_GROUP])[0]
            day = params.get('day', ['today'])[0]

            slots, err = get_yasno_data(group, day)
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            response = {"group": group, "day": day, "slots": slots, "error": err}
            self.wfile.write(json.dumps(response).encode('utf-8'))
        else:
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"Bot is active!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server_address = ('0.0.0.0', port)
    httpd = HTTPServer(server_address, DummyHandler)
    httpd.serve_forever()

threading.Thread(target=run_dummy_server, daemon=True).start()

def main_keyboard(chat_id):
    group = get_user_group(chat_id)
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(types.KeyboardButton('📅 Сьогодні'), types.KeyboardButton('🔮 Завтра'))
    markup.row(types.KeyboardButton('🔋 Статус EcoFlow'), types.KeyboardButton(f'⚙️ Черга ({group})'))
    return markup

@bot.message_handler(commands=['start'])
def send_welcome(message):
    group = get_user_group(message.chat.id)
    bot.send_message(
        message.chat.id, 
        f"⚡️ Бот моніторингу світла.\nПоточна черга: **{group}**.\nОберіть дію:", 
        reply_markup=main_keyboard(message.chat.id),
        parse_mode="Markdown"
    )

@bot.message_handler(content_types=['text'])
def handle_menu(message):
    chat_id = message.chat.id
    current_group = get_user_group(chat_id)

    if message.text == '🔋 Статус EcoFlow':
        has_power = random.choice([True, False])
        battery = random.randint(60, 100)
        state_icon = "🟢" if has_power else "🔴"
        state_text = "Мережа активна" if has_power else "Працюємо від батареї"
        bot.send_message(
            chat_id, 
            f"{state_icon} **{state_text}**\nЗаряд EcoFlow: {battery}%", 
            parse_mode="Markdown"
        )

    elif message.text == '📅 Сьогодні':
        slots, err = get_yasno_data(current_group, "today")
        if err:
            bot.send_message(chat_id, f"⚠️ {err}")
            return
        bot.send_message(chat_id, format_schedule_text(slots, current_group, "today"), parse_mode="Markdown")

    elif message.text == '🔮 Завтра':
        slots, err = get_yasno_data(current_group, "tomorrow")
        if err:
            bot.send_message(chat_id, f"⚠️ {err}")
            return
        bot.send_message(chat_id, format_schedule_text(slots, current_group, "tomorrow"), parse_mode="Markdown")

    elif message.text.startswith('⚙️ Черга'):
        groups = get_available_groups()
        inline_kb = types.InlineKeyboardMarkup(row_width=3)
        buttons = [types.InlineKeyboardButton(g, callback_data=f"set_group:{g}") for g in groups[:15]]
        inline_kb.add(*buttons)
        bot.send_message(
            chat_id, 
            f"Поточна черга: **{current_group}**.\nОберіть чергу зі списку (отримано з бази YASNO) або надішліть номер текстом:",
            reply_markup=inline_kb,
            parse_mode="Markdown"
        )

    else:
        # Якщо введено текст як чергу
        new_grp = message.text.strip()
        save_user_group(chat_id, new_grp)
        bot.send_message(
            chat_id, 
            f"✅ Чергу змінено на **{new_grp}**!",
            reply_markup=main_keyboard(chat_id),
            parse_mode="Markdown"
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith('set_group:'))
def callback_set_group(call):
    new_grp = call.data.split(":")[1]
    save_user_group(call.message.chat.id, new_grp)
    bot.answer_callback_query(call.id, f"Чергу змінено на {new_grp}")
    bot.edit_message_text(
        f"✅ Чергу збережено: **{new_grp}**",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        parse_mode="Markdown"
    )
    bot.send_message(
        call.message.chat.id, 
        "Оновлене меню:", 
        reply_markup=main_keyboard(call.message.chat.id)
    )

bot.polling(none_stop=True)
