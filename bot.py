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

# Безпечне отримання токена з оточення (або резервний)
BOT_TOKEN = os.environ.get('BOT_TOKEN', '8680430959:AAE8T_Q4ffvdl6O7OXZ6cv9UinNbdeuz8g8')
ADMIN_CHAT_ID = os.environ.get('ADMIN_CHAT_ID', '367762402')
DEFAULT_GROUP = '35.1'
YASNO_API = "https://app.yasno.ua/api/blackout-service/public/shutdowns/regions/25/dsos/902/planned-outages"

bot = telebot.TeleBot(BOT_TOKEN)

# Збереження черг користувачів
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

def sort_key_group(g):
    """Сортує черги математично правильно (1.1, 1.2, 2.1 ... 10.1, 35.1)"""
    try:
        parts = str(g).split('.')
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except Exception:
        return (999, str(g))

def get_available_groups():
    data = fetch_full_yasno()
    if not data or not isinstance(data, dict):
        return [DEFAULT_GROUP]
    groups = [k for k in data.keys() if isinstance(data[k], dict) and ("today" in data[k] or "tomorrow" in data[k])]
    return sorted(groups, key=sort_key_group) if groups else [DEFAULT_GROUP]

def get_yasno_data(group=DEFAULT_GROUP, day="today"):
    res = fetch_full_yasno()
    if not res:
        return None, "Не вдалося отримати дані від сервера YASNO"

    target = str(group)
    group_data = res.get(target) or res.get(f"group_{target}") or res.get(target.replace("group_", ""))
    
    if not group_data:
        return None, f"Чергу «{group}» не знайдено в графіку YASNO"

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

# --- Фоновий моніторинг змін ---
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

# --- Веб-сервер для Mini App та пінгу cron-job ---
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
