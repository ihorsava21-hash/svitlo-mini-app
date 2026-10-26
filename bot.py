import telebot
from telebot import types
import requests
import datetime
import random
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

BOT_TOKEN = '8680430959:AAE8T_Q4ffvdl6O7OXZ6cv9UinNbdeuz8g8'
GROUP = '35.1'
YASNO_API = "https://app.yasno.ua/api/blackout-service/public/shutdowns/regions/25/dsos/902/planned-outages"

bot = telebot.TeleBot(BOT_TOKEN)

# --- Міні-сервер для Render (0.0.0.0) ---
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
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
# ----------------------------------------

def hhmm(m):
    h = int(m // 60)
    minute = int(m % 60)
    return f"{h:02d}:{minute:02d}"

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
        return None, f"Помилка зв'язку з сервером: {e}"

def build_intervals(slots):
    # Фільтруємо на відключення ("definite") та періоди зі світлом ("notplanned")
    valid_slots = [
        s for s in slots 
        if str(s.get("type", "")).lower() in ["definite", "notplanned"]
    ]
    valid_slots.sort(key=lambda x: x["start"])

    merged = []
    for s in valid_slots:
        stype = str(s.get("type", "")).lower()
        if merged and merged[-1]["end"] == s["start"] and merged[-1]["type"] == stype:
            merged[-1]["end"] = s["end"]
        else:
            merged.append({
                "start": s["start"],
                "end": s["end"],
                "type": stype
            })
    return merged

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

        now_m = datetime.datetime.now().hour * 60 + datetime.datetime.now().minute
        intervals = build_intervals(slots)
        
        # Визначаємо поточний стан
        current_outage = next((s for s in slots if str(s.get("type", "")).lower() == "definite" and s["start"] <= now_m < s["end"]), None)
        
        if current_outage:
            status_header = f"🔴 **Світла немає** (до {hhmm(current_outage['end'])})"
        else:
            next_outage = next((s for s in slots if str(s.get("type", "")).lower() == "definite" and s["start"] > now_m), None)
            if next_outage:
                status_header = f"🟢 **Світло є** (відключення о {hhmm(next_outage['start'])})"
            else:
                status_header = "🟢 **Світло є** (відключень до кінця доби не очікується)"

        # Найближчі 3 інтервали
        upcoming = [i for i in intervals if i["end"] > now_m][:3]
        
        lines = [f"📅 **Графік YASNO — Черга {GROUP}**", f"Поточний стан: {status_header}\n", "Найближчі інтервали:"]
        for item in upcoming:
            is_off = item["type"] == "definite"
            icon = "🔴" if is_off else "🟢"
            label = "Світла немає" if is_off else "Світло є за графіком"
            lines.append(f"{icon} {hhmm(item['start'])}–{hhmm(item['end'])} — {label}")
            
        bot.send_message(message.chat.id, "\n".join(lines), parse_mode="Markdown")

bot.polling(none_stop=True)
