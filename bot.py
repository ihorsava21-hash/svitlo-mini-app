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

BOT_TOKEN = '8680430959:AAE8T_Q4ffvdl6O7OXZ6cv9UinNbdeuz8g8'
ADMIN_CHAT_ID = '367762402'
GROUP = '35.1'
YASNO_API = "https://app.yasno.ua/api/blackout-service/public/shutdowns/regions/25/dsos/902/planned-outages"

bot = telebot.TeleBot(BOT_TOKEN)
last_known_schedule = None

def get_kyiv_now():
    # Точний київський час незалежно від серверної зони
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

def get_yasno_data():
    try:
        headers = {"Accept": "application/json"}
        res = requests.get(YASNO_API, headers=headers, timeout=15).json()
        group_data = res.get(GROUP)
        
        if not group_data or not group_data.get("today"):
            return None, "Графік для черги не знайдено"

        today = group_data["today"]
        status = today.get("status")
        slots = today.get("slots", [])
        
        if status != "ScheduleApplies":
            return None, "Графік очікується (YASNO ще не підтвердив розклад)"
            
        return slots, None
    except Exception as e:
        return None, f"Помилка: {e}"

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

def format_schedule_message(slots):
    intervals = build_intervals(slots)
    outages = [i for i in intervals if i["is_outage"]]
    
    if not outages:
        return "🎉 Згідно з новим графіком, відключень не планується!"
    
    lines = ["🔔 **УВАГА: Графік відключень змінився!**\n", f"Актуальні періоди без світла (Черга {GROUP}):"]
    for item in outages:
        lines.append(f"🔴 {hhmm(item['start'])} – {hhmm(item['end'])}")
    return "\n".join(lines)

def monitor_schedule_changes():
    global last_known_schedule
    slots, err = get_yasno_data()
    if slots:
        last_known_schedule = json.dumps(slots, sort_keys=True)
    
    while True:
        try:
            time.sleep(300)
            current_slots, err = get_yasno_data()
            if current_slots:
                current_dump = json.dumps(current_slots, sort_keys=True)
                if last_known_schedule is not None and current_dump != last_known_schedule:
                    last_known_schedule = current_dump
                    msg = format_schedule_message(current_slots)
                    bot.send_message(ADMIN_CHAT_ID, msg, parse_mode="Markdown")
                else:
                    last_known_schedule = current_dump
        except Exception as e:
            print(f"Помилка моніторингу: {e}")

threading.Thread(target=monitor_schedule_changes, daemon=True).start()

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/schedule':
            slots, err = get_yasno_data()
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            response = {"slots": slots, "error": err}
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

@bot.message_handler(commands=['start'])
def send_welcome(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton('🔋 Статус EcoFlow'), types.KeyboardButton('📅 Графік черги'))
    bot.send_message(
        message.chat.id, 
        f"Бот моніторингу світла (Черга {GROUP}). Обери пункт:", 
        reply_markup=markup
    )

@bot.message_handler(content_types=['text'])
def handle_menu(message):
    if message.text == '🔋 Статус EcoFlow':
        has_power = random.choice([True, False])
        battery = random.randint(60, 100)
        state_icon = "🟢" if has_power else "🔴"
        state_text = "Мережа активна" if has_power else "Працюємо від батареї"
        bot.send_message(
            message.chat.id, 
            f"{state_icon} **{state_text}**\nЗаряд EcoFlow: {battery}%", 
            parse_mode="Markdown"
        )

    elif message.text == '📅 Графік черги':
        slots, err = get_yasno_data()
        if err:
            bot.send_message(message.chat.id, f"⚠️ {err}")
            return

        now = get_kyiv_now()
        now_m = now.hour * 60 + now.minute
        intervals = build_intervals(slots)
        
        current_outage = next((s for s in slots if is_outage(s) and s["start"] <= now_m < s["end"]), None)
        
        if current_outage:
            status_header = f"🔴 **Світла немає** (до {hhmm(current_outage['end'])})"
        else:
            next_outage = next((s for s in slots if is_outage(s) and s["start"] > now_m), None)
            if next_outage:
                status_header = f"🟢 **Світло є** (відключення о {hhmm(next_outage['start'])})"
            else:
                status_header = "🟢 **Світло є** (відключень далі немає)"

        upcoming = [i for i in intervals if i["end"] > now_m][:3]
        
        lines = [
            f"📅 **Графік YASNO — Черга {GROUP}**",
            f"Поточний стан: {status_header}\n",
            "Найближчі інтервали:"
        ]
        
        if not upcoming:
            lines.append("Більше немає запланованих інтервалів на сьогодні")
        else:
            for item in upcoming:
                icon = "🔴" if item["is_outage"] else "🟢"
                label = "Світла немає" if item["is_outage"] else "Світло є за графіком"
                lines.append(f"{icon} {hhmm(item['start'])}–{hhmm(item['end'])} — {label}")
            
        bot.send_message(message.chat.id, "\n".join(lines), parse_mode="Markdown")

bot.polling(none_stop=True)
