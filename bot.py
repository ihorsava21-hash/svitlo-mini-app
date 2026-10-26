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

bot = telebot.TeleBot(BOT_TOKEN)

# --- Додаємо міні-сервер для Render ---
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server_address = ('', port)
    httpd = HTTPServer(server_address, DummyHandler)
    httpd.serve_forever()

# Запускаємо сервер у фоновому потоці
threading.Thread(target=run_dummy_server, daemon=True).start()
# ----------------------------------------

def get_outages():
    try:
        url = "https://yasno.energal.info/api/v1/schedules"
        res = requests.get(url, timeout=10).json()
        outages = []
        if 'kiev' in res and GROUP in res['kiev']:
            for p in res['kiev'][GROUP]:
                if p['type'] in ['DEFINITE_OUTAGE', 'definite']:
                    for h in range(int(p['start']), int(p['end'])):
                        if h not in outages:
                            outages.append(h)
        return outages
    except Exception as e:
        print(f"Помилка API: {e}")
        return []

def get_periods(outages, current_hour):
    is_currently_off = current_hour in outages
    p1_end = current_hour
    while p1_end < 24 and (p1_end in outages) == is_currently_off:
        p1_end += 1
    
    is_next_off = not is_currently_off
    p2_end = p1_end
    while p2_end < 24 and (p2_end in outages) == is_next_off:
        p2_end += 1
        
    return {
        "current": {"start": current_hour, "end": p1_end, "hasPower": not is_currently_off},
        "next": {"start": p1_end, "end": p2_end, "hasPower": not is_next_off}
    }

def format_time(h):
    if h >= 24: return "00:00"
    return f"{h:02d}:00"

@bot.message_handler(commands=['start'])
def send_welcome(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton('🔋 Статус EcoFlow'), types.KeyboardButton('📅 Графік черги'))
    bot.send_message(
        message.chat.id, 
        f"Привіт! Я слідкую за світлом для черги {GROUP}. Обери дію:", 
        reply_markup=markup
    )

@bot.message_handler(content_types=['text'])
def handle_menu(message):
    if message.text == '🔋 Статус EcoFlow':
        has_power = random.choice([True, False])
        battery = random.randint(60, 100)
        text = "🟢 **Світло є!** Мережа активна.\n" if has_power else "🔴 **Світла немає!** Працюємо від батареї.\n"
        text += f"Заряд River 3 Plus: {battery}%"
        bot.send_message(message.chat.id, text, parse_mode="Markdown")

    elif message.text == '📅 Графік черги':
        outages = get_outages()
        current_hour = datetime.datetime.now().hour
        periods = get_periods(outages, current_hour)
        
        c = periods['current']
        n = periods['next']
        
        c_status = "🟢 Є" if c['hasPower'] else "🔴 Немає"
        n_status = "🟢 Є" if n['hasPower'] else "🔴 Можливе відключення"
        
        text = f"Графік для черги {GROUP}:\n\nЗараз: {format_time(c['start'])} - {format_time(c['end'])} ➔ {c_status}\n"
        if n['start'] < 24:
            text += f"Далі: {format_time(n['start'])} - {format_time(n['end'])} ➔ {n_status}"
        bot.send_message(message.chat.id, text)

print("Бот запущено на Render! Чекаю на повідомлення...")
bot.polling(none_stop=True)
