"""
Telegram-бот для продажи билетов на вечеринки Vida Bachata.
Хранилище: Supabase (PostgreSQL).
"""

import asyncio
import csv
import http.server
import io
import json
import os
import re
import socketserver
import threading
import urllib.request
import uuid
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Optional

import aiosmtplib
import dateparser
import qrcode
import requests
import resend
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv
from PIL import Image
from supabase import create_client, Client
from yookassa import Configuration, Payment

# ================== КОНФИГ ==================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "your_bot")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.mail.ru")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")

# ЮKassa
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")

Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY

# ================== БОТ ==================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ================== SUPABASE ==================
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ================== ХРАНИЛИЩЕ ==================
users = {}
username_index = {}
pending_invites = {}
tickets = {}
orders = []
events = []
blacklist = {}
reg_attempts = []
pending_payments = {}
processed_payments = set()
checkers = {}
ticket_logs = []

settings = {
    "support": os.getenv("SUPPORT", "@your_support"),
    "yoomoney_link": os.getenv("YOOMONEY_LINK", "https://yoomoney.ru/to/0000000000000"),
    "sales_stop_hours": 1,
}


def save_data():
    """Синхронизирует всё из памяти в Supabase."""
    try:
        if users:
            users_rows = []
            for uid, u in users.items():
                users_rows.append({
                    "user_id": uid,
                    "username": u.get("username", ""),
                    "email": u.get("email", ""),
                    "name": u.get("name", ""),
                    "surname": u.get("surname", ""),
                    "gender": u.get("gender", ""),
                    "phone": u.get("phone", ""),
                    "registered_at": u.get("registered_at", ""),
                })
            supabase.table("users").upsert(users_rows).execute()

        if events:
            supabase.table("events").upsert(events).execute()

        if tickets:
            tickets_rows = []
            for code, t in tickets.items():
                tickets_rows.append({
                    "code": code,
                    "user_id": t.get("user_id"),
                    "type": t.get("type", ""),
                    "holder": t.get("holder", ""),
                    "used": t.get("used", False),
                    "event_id": t.get("event_id", ""),
                    "event_title": t.get("event_title", ""),
                    "event_date": t.get("event_date", ""),
                    "issued_at": t.get("issued_at", ""),
                })
            supabase.table("tickets").upsert(tickets_rows).execute()

        if orders:
            supabase.table("orders").upsert(orders).execute()

        if checkers:
            checkers_rows = []
            for chk_id, c in checkers.items():
                checkers_rows.append({
                    "user_id": c.get("user_id"),
                    "username": c.get("username", ""),
                    "phone": c.get("phone", ""),
                    "name": c.get("name", ""),
                    "event_id": c.get("event_id"),
                    "added_by": c.get("added_by"),
                    "added_at": c.get("added_at", ""),
                    "active": c.get("active", True),
                })
            supabase.table("checkers").delete().neq("id", 0).execute()
            if checkers_rows:
                supabase.table("checkers").insert(checkers_rows).execute()

        if ticket_logs:
            for log in ticket_logs:
                try:
                    supabase.table("ticket_logs").insert(log).execute()
                except Exception:
                    pass
            ticket_logs.clear()

    except Exception as e:
        print(f"[SUPABASE] Ошибка сохранения: {e}")


def load_data():
    """Загружает всё из Supabase в память."""
    global events, orders, tickets, users, username_index, blacklist, reg_attempts, checkers
    try:
        resp = supabase.table("users").select("*").execute()
        users = {}
        username_index = {}
        for row in resp.data:
            uid = int(row["user_id"])
            users[uid] = {
                "username": row.get("username", ""),
                "email": row.get("email", ""),
                "name": row.get("name", ""),
                "surname": row.get("surname", ""),
                "gender": row.get("gender", ""),
                "phone": row.get("phone", ""),
                "registered_at": row.get("registered_at", ""),
            }
            uname = (row.get("username") or "").lower()
            if uname:
                username_index[uname] = uid

        resp = supabase.table("events").select("*").execute()
        events = resp.data or []

        resp = supabase.table("tickets").select("*").execute()
        tickets = {}
        for row in resp.data:
            tickets[row["code"]] = {
                "user_id": row.get("user_id"),
                "type": row.get("type", ""),
                "holder": row.get("holder", ""),
                "used": row.get("used", False),
                "event_id": row.get("event_id", ""),
                "event_title": row.get("event_title", ""),
                "event_date": row.get("event_date", ""),
                "issued_at": row.get("issued_at", ""),
            }

        resp = supabase.table("orders").select("*").execute()
        orders = resp.data or []

        resp = supabase.table("blacklist").select("*").execute()
        blacklist = {}
        for row in resp.data:
            v = (row.get("value") or "").lower().lstrip("@")
            v = v.replace(" ", "").replace("-", "").replace("+", "")
            key = f"{row.get('type')}:{v}"
            blacklist[key] = {
                "type": row.get("type", ""),
                "value": row.get("value", ""),
                "reason": row.get("reason", ""),
                "added_at": row.get("added_at", ""),
            }

        resp = supabase.table("reg_attempts").select("*").order("id", desc=False).limit(500).execute()
        reg_attempts = resp.data or []

        resp = supabase.table("checkers").select("*").execute()
        checkers = {}
        for row in resp.data:
            chk_id = f"chk_{row['id']}"
            checkers[chk_id] = {
                "id": chk_id,
                "db_id": row["id"],
                "user_id": row.get("user_id"),
                "username": (row.get("username") or "").lstrip("@").lower(),
                "phone": row.get("phone") or "",
                "name": row.get("name") or "",
                "event_id": row.get("event_id"),
                "added_by": row.get("added_by"),
                "added_at": row.get("added_at", ""),
                "active": row.get("active", True),
            }

        print(
            f"✅ Загружено из Supabase: {len(events)} событий, "
            f"{len(orders)} заказов, {len(users)} юзеров, "
            f"{len(tickets)} билетов, {len(blacklist)} в ЧС, "
            f"{len(reg_attempts)} попыток"
        )
        print(f"   └ Checkers: {len(checkers)}")
    except Exception as e:
        print(f"[SUPABASE] Ошибка загрузки: {e}")


# ================== ЮKASSA ==================
def create_yookassa_payment(amount: int, description: str, user_id: int, metadata: dict) -> dict:
    """Создаёт платёж в ЮKassa."""
    idempotence_key = str(uuid.uuid4())

    payment_data = {
        "amount": {
            "value": f"{amount}.00",
            "currency": "RUB"
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": "https://t.me/" + BOT_USERNAME
        },
        "description": description,
        "metadata": {
            "user_id": user_id,
            **metadata
        }
    }

    try:
        payment = Payment.create(payment_data, idempotence_key)
        return {
            "payment_id": payment.id,
            "confirmation_url": payment.confirmation.confirmation_url,
            "status": payment.status
        }
    except Exception as e:
        print(f"[YOOKASSA] Ошибка создания платежа: {e}")
        return {}


def verify_payment_with_yookassa(payment_id: str) -> bool:
    """Проверяет платёж через API ЮKassa."""
    try:
        payment = Payment.find_one(payment_id)
        if not payment:
            print(f"[YOOKASSA] Платёж {payment_id} не найден в API")
            return False

        if payment.status != "succeeded":
            print(f"[YOOKASSA] Платёж {payment_id} имеет статус {payment.status}, не succeeded")
            return False

        try:
            if str(payment.amount.value) and payment.amount.currency != "RUB":
                print(f"[YOOKASSA] Платёж {payment_id} не в рублях")
                return False
        except Exception:
            pass

        return True
    except Exception as e:
        print(f"[YOOKASSA] Ошибка верификации платежа {payment_id}: {e}")
        return False


# ================== ЧЁРНЫЙ СПИСОК ==================
def is_blacklisted(username: str = "", phone: str = "") -> bool:
    """Проверяет, есть ли username или phone в чёрном списке."""
    if username:
        u = username.lower().lstrip("@")
        for key, item in blacklist.items():
            if item.get("type") == "username":
                v = item.get("value", "").lower().lstrip("@")
                if v == u:
                    return True
    if phone:
        p = phone.replace(" ", "").replace("-", "").replace("+", "")
        for key, item in blacklist.items():
            if item.get("type") == "phone":
                v = item.get("value", "").replace(" ", "").replace("-", "").replace("+", "")
                if v == p:
                    return True
    return False


def add_to_blacklist(entry_type: str, value: str, reason: str = "") -> bool:
    """Добавляет запись в чёрный список."""
    value = value.strip()
    if not value:
        return False

    value_clean = value.lower().lstrip("@").replace(" ", "").replace("-", "").replace("+", "")
    for key, item in blacklist.items():
        existing = item.get("value", "").lower().lstrip("@").replace(" ", "").replace("-", "").replace("+", "")
        if item.get("type") == entry_type and existing == value_clean:
            return False

    key = f"{entry_type}:{value_clean}"
    blacklist[key] = {
        "type": entry_type,
        "value": value,
        "reason": reason,
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    try:
        supabase.table("blacklist").upsert({
            "type": entry_type,
            "value": value,
            "reason": reason,
            "added_at": blacklist[key]["added_at"],
        }).execute()
    except Exception as e:
        print(f"[SUPABASE] Ошибка добавления в ЧС: {e}")

    return True


def remove_from_blacklist(value: str) -> bool:
    """Удаляет запись из чёрного списка по value."""
    value_clean = value.lower().lstrip("@").replace(" ", "").replace("-", "").replace("+", "")
    found_key = None
    for key, item in blacklist.items():
        existing = item.get("value", "").lower().lstrip("@").replace(" ", "").replace("-", "").replace("+", "")
        if existing == value_clean:
            found_key = key
            break

    if not found_key:
        return False

    item = blacklist.pop(found_key)
    try:
        supabase.table("blacklist").delete().eq("value", item["value"]).execute()
    except Exception as e:
        print(f"[SUPABASE] Ошибка удаления из ЧС: {e}")
    return True


def get_blacklist_text() -> str:
    """Возвращает текстовое представление чёрного списка."""
    if not blacklist:
        return "📭 Чёрный список пуст."

    text = f"🚫 <b>Чёрный список</b> ({len(blacklist)} записей)\n\n"
    for key, item in blacklist.items():
        icon = "📱" if item.get("type") == "phone" else "🔗"
        reason = f" — {item.get('reason')}" if item.get("reason") else ""
        text += f"{icon} <code>{item.get('value')}</code>{reason}\n"
    return text


# ================== ЛОГ ПОПЫТОК РЕГИСТРАЦИИ ==================
def log_reg_attempt(
    user_id: int,
    username: str = "",
    first_name: str = "",
    phone: str = "",
    status: str = "started",
    step: str = "start",
    reason: str = "",
):
    """Логирует попытку регистрации."""
    attempt = {
        "user_id": user_id,
        "username": username or "",
        "first_name": first_name or "",
        "phone": phone or "",
        "status": status,
        "step": step,
        "reason": reason,
        "attempted_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    reg_attempts.append(attempt)

    try:
        supabase.table("reg_attempts").insert(attempt).execute()
    except Exception as e:
        print(f"[SUPABASE] Ошибка логирования попытки: {e}")


def get_reg_attempts_text(limit: int = 30) -> str:
    """Возвращает текст со свежими попытками регистрации."""
    if not reg_attempts:
        return "📭 Пока нет попыток регистрации."

    total = len(reg_attempts)
    blocked_user = sum(1 for a in reg_attempts if a.get("status") == "blocked_username")
    blocked_phone = sum(1 for a in reg_attempts if a.get("status") == "blocked_phone")
    success = sum(1 for a in reg_attempts if a.get("status") == "success")

    text = (
        f"📊 <b>Попытки регистрации</b>\n\n"
        f"Всего: <b>{total}</b>\n"
        f"✅ Успешных: <b>{success}</b>\n"
        f"🚫 Заблокирован username: <b>{blocked_user}</b>\n"
        f"🚫 Заблокирован телефон: <b>{blocked_phone}</b>\n\n"
        f"<b>Последние {min(limit, total)}:</b>\n\n"
    )

    for a in reg_attempts[-limit:][::-1]:
        status_icon = {
            "started": "🟡",
            "success": "✅",
            "blocked_username": "🚫",
            "blocked_phone": "🚫",
        }.get(a.get("status"), "❓")

        text += (
            f"{status_icon} <code>{a.get('user_id')}</code>"
            f" | @{a.get('username') or '—'}"
            f" | {a.get('first_name') or '—'}\n"
            f"   └ {a.get('status')} / шаг: {a.get('step')}"
            f" / {a.get('attempted_at')}\n"
        )
        if a.get("reason"):
            text += f"   └ причина: {a.get('reason')}\n"

    return text


# ================== УТИЛИТЫ ==================
def is_valid_email(email: str) -> bool:
    return re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) is not None


def parse_event_date(text: str):
    """Парсит дату в свободной форме."""
    settings_parser = {
        "DATE_ORDER": "DMY",
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": False,
    }
    dt = dateparser.parse(text, languages=["ru", "en"], settings=settings_parser)
    if dt is None:
        return None, None

    months_ru = {
        1: "января", 2: "февраля", 3: "марта", 4: "апреля",
        5: "мая", 6: "июня", 7: "июля", 8: "августа",
        9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
    }
    date_text = f"{dt.day} {months_ru[dt.month]} {dt.year}, {dt.strftime('%H:%M')}"
    return dt, date_text


def sales_closed(event: dict) -> bool:
    start_dt = datetime.fromisoformat(event["start_dt"])
    stop_before = timedelta(hours=settings.get("sales_stop_hours", 1))
    return datetime.now() >= start_dt - stop_before


def generate_ticket_code() -> str:
    return f"VIDA-{uuid.uuid4().hex[:4].upper()}-{uuid.uuid4().hex[:4].upper()}"


def next_order_id() -> str:
    import time
    return f"ORD-{int(time.time() * 1000) % 10**10:010d}"


def make_qr_image(data: str) -> BufferedInputFile:
    """Генерирует PNG с QR-кодом для отправки гостю."""
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return BufferedInputFile(buf.getvalue(), filename=f"{data}.png")


def make_csv(headers, rows) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def format_user_info(user_id: int, u: dict) -> str:
    return (
        f"👤 <b>{u.get('name', '—')} {u.get('surname', '—')}</b>\n"
        f"🆔 <code>{user_id}</code>\n"
        f"📧 {u.get('email', '—') or '—'}\n"
        f"📱 {u.get('phone', '—')}\n"
        f"⚧ {u.get('gender', '—')}\n"
        f"🔗 @{u.get('username', '—') or '—'}\n"
        f"📅 {u.get('registered_at', '—')}\n"
    )


def get_short_event_label(event_date: str) -> str:
    """
    Возвращает короткую метку даты вида '15.11' из строки '15 ноября 2026, 22:00'.
    Если не удалось — возвращает пустую строку.
    """
    if not event_date:
        return ""
    try:
        m = re.match(r"^(\d{1,2})\s+([а-яё]+)", event_date, re.IGNORECASE)
        if not m:
            return ""
        day = m.group(1).zfill(2)
        month_ru = m.group(2).lower()
        months_map = {
            "января": "01", "февраля": "02", "марта": "03",
            "апреля": "04", "мая": "05", "июня": "06",
            "июля": "07", "августа": "08", "сентября": "09",
            "октября": "10", "ноября": "11", "декабря": "12",
        }
        month_num = months_map.get(month_ru)
        if month_num:
            return f"{day}.{month_num}"
    except Exception:
        pass
    return ""


def get_ticket_price_from_orders(uid, event_id) -> int:
    """Ищет цену билета в orders по user_id и event_id."""
    for o in orders:
        if (str(o.get("user_id")) == str(uid)
                and str(o.get("event_id")) == str(event_id)):
            try:
                p = int(o.get("price", 0) or 0)
                if p > 0:
                    return p
            except (TypeError, ValueError):
                pass
            break
    return 0


def get_button_suffix(ticket: dict) -> str:
    """
    Возвращает суффикс для кнопки вида ' (15.11, 500 ₽)' или '' если не удалось.
    """
    date_part = get_short_event_label(ticket.get("event_date") or "")
    uid = ticket.get("user_id")
    ev_id = ticket.get("event_id")
    price = get_ticket_price_from_orders(uid, ev_id)
    price_part = f"{price} ₽" if price > 0 else ""

    parts = [p for p in [date_part, price_part] if p]
    if not parts:
        return ""
    return f" ({', '.join(parts)})"


def _ticket_type_label(ticket: dict) -> str:
    """Возвращает 'Парный' или 'Обычный'."""
    return "Парный" if ticket.get("type") == "pair" else "Обычный"


# ================== УТИЛИТЫ ДЛЯ CHECKER'ОВ ==================
def find_checker_for_user(user_id: int, username: str = "", phone: str = "") -> Optional[dict]:
    """Ищет checker'а по user_id, username или телефону."""
    for chk_id, c in checkers.items():
        if not c.get("active", True):
            continue
        if c.get("user_id") and int(c["user_id"]) == int(user_id):
            return c

    if username:
        u = username.lower().lstrip("@")
        for chk_id, c in checkers.items():
            if not c.get("active", True):
                continue
            if c.get("username") and c["username"] == u:
                return c

    if phone:
        p = phone.replace(" ", "").replace("-", "").replace("+", "")
        for chk_id, c in checkers.items():
            if not c.get("active", True):
                continue
            c_phone = (c.get("phone") or "").replace(" ", "").replace("-", "").replace("+", "")
            if c_phone and c_phone == p:
                return c

    return None


def activate_checker(chk_id: str, user_id: int):
    """Привязывает user_id к checker'у после активации."""
    if chk_id in checkers:
        checkers[chk_id]["user_id"] = user_id
        save_data()


def checker_can_access_event(checker: dict, event_id: str) -> bool:
    """Проверяет, может ли checker погасить билет этого события."""
    if not checker:
        return False
    if checker.get("event_id") is None:
        return True
    return str(checker.get("event_id")) == str(event_id)


def get_checker_events(checker: dict) -> list:
    """Возвращает список событий, доступных checker'у."""
    if not checker:
        return []
    if checker.get("event_id") is None:
        return list(events)
    return [e for e in events if str(e["id"]) == str(checker.get("event_id"))]


def log_ticket_action(ticket_code: str, checker: dict, action: str):
    """Пишет в лог: кто и что сделал с билетом."""
    log_entry = {
        "ticket_code": ticket_code,
        "checker_user_id": checker.get("user_id"),
        "checker_name": checker.get("name", "—"),
        "event_id": checker.get("event_id"),
        "action": action,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    ticket_logs.append(log_entry)
    try:
        supabase.table("ticket_logs").insert(log_entry).execute()
    except Exception as e:
        print(f"[SUPABASE] Ошибка лога билета: {e}")


# ================== УТИЛИТЫ ДЛЯ QR ==================
def compress_image_for_qr(image_bytes: bytes, max_kb: int = 950) -> bytes:
    """Сжимает изображение до размера < max_kb."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("RGB")

        quality = 90
        while quality >= 20:
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=quality, optimize=True)
            if buffer.tell() / 1024 <= max_kb:
                return buffer.getvalue()
            quality -= 10

        max_dim = 1200
        while max_dim >= 400:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=80, optimize=True)
            if buffer.tell() / 1024 <= max_kb:
                return buffer.getvalue()
            max_dim -= 200

        return buffer.getvalue()
    except Exception as e:
        print(f"[QR] Ошибка сжатия: {e}")
        return image_bytes


def decode_qr_via_qrserver(image_bytes: bytes) -> Optional[str]:
    """Распознаёт QR-код через api.qrserver.com."""
    url = "https://api.qrserver.com/v1/read-qr-code/?outputformat=json"
    files = {"file": ("qr.jpg", image_bytes, "image/jpeg")}

    try:
        resp = requests.post(url, files=files, timeout=15)
        data = resp.json()

        if not data or not isinstance(data, list):
            print(f"[QR] Неожиданный ответ: {data}")
            return None

        symbol = data[0].get("symbol", [])
        if not symbol:
            print("[QR] Символы не найдены")
            return None

        item = symbol[0]
        if item.get("error"):
            print(f"[QR] Ошибка декодирования: {item['error']}")
            return None

        decoded = (item.get("data") or "").strip()
        if decoded:
            print(f"[QR] Распознано: {decoded}")
            return decoded

        return None
    except Exception as e:
        print(f"[QR] Ошибка API: {e}")
        return None


async def send_email_code(email: str, code: str) -> bool:
    """Отправляет код подтверждения через Resend API."""
    if not RESEND_API_KEY:
        print(f"[EMAIL] RESEND_API_KEY не задан, код для {email}: {code}")
        return False

    resend.api_key = RESEND_API_KEY

    try:
        result = await asyncio.to_thread(
            resend.Emails.send,
            {
                "from": "onboarding@resend.dev",
                "to": email,
                "subject": "Код подтверждения регистрации",
                "html": (
                    f"<p>Здравствуйте!</p>"
                    f"<p>Ваш код подтверждения: "
                    f"<b style='font-size:20px;letter-spacing:3px'>{code}</b></p>"
                    f"<p>Введите его в боте.</p>"
                    f"<p>— Vida Bachata 💃</p>"
                ),
            },
        )
        print(f"[EMAIL] Код отправлен на {email}: {result}")
        return True
    except Exception as e:
        print(f"[EMAIL] Ошибка отправки на {email}: {e}")
        return False


async def send_report_to_admin():
    """Отправляет отчёт админу на email."""
    users_rows = [[
        uid, u.get("username", ""), u.get("email", ""),
        u.get("name", ""), u.get("surname", ""), u.get("gender", ""),
        u.get("phone", ""), u.get("registered_at", ""),
    ] for uid, u in users.items()]
    users_csv = make_csv(
        ["telegram_id", "username", "email", "name", "surname",
         "gender", "phone", "registered_at"], users_rows
    )

    orders_rows = [[
        o["order_id"], o["user_id"], o.get("event_title", ""),
        o["email"], o["name"], o["ticket_type"], o["price"], o["paid_at"],
    ] for o in orders]
    orders_csv = make_csv(
        ["order_id", "telegram_id", "event", "email", "name",
         "ticket_type", "price", "paid_at"], orders_rows
    )

    tickets_rows = [[
        code, t["user_id"], t["holder"], t["type"],
        "использован" if t.get("used") else "активен",
        t.get("event_title", ""), t.get("event_date", ""),
        t.get("issued_at", ""),
    ] for code, t in tickets.items()]
    tickets_csv = make_csv(
        ["ticket_code", "holder_telegram_id", "holder_name",
         "type", "status", "event_title", "event_date", "issued_at"],
        tickets_rows
    )

    attempts_rows = [[
        a.get("user_id", ""), a.get("username", ""), a.get("first_name", ""),
        a.get("phone", ""), a.get("status", ""), a.get("step", ""),
        a.get("reason", ""), a.get("attempted_at", ""),
    ] for a in reg_attempts]
    attempts_csv = make_csv(
        ["user_id", "username", "first_name", "phone",
         "status", "step", "reason", "attempted_at"], attempts_rows
    )

    total_revenue = sum(o["price"] for o in orders)
    success_count = sum(1 for a in reg_attempts if a.get("status") == "success")
    blocked_user = sum(1 for a in reg_attempts if a.get("status") == "blocked_username")
    blocked_phone = sum(1 for a in reg_attempts if a.get("status") == "blocked_phone")

    body = (
        f"📊 ИТОГИ:\n"
        f"- Пользователей: {len(users)}\n"
        f"- Заказов: {len(orders)}\n"
        f"- Выручка: {total_revenue} ₽\n"
        f"- Выдано билетов: {len(tickets)}\n"
        f"- Попыток регистрации: {len(reg_attempts)}\n"
        f"  • успешных: {success_count}\n"
        f"  • заблокировано username: {blocked_user}\n"
        f"  • заблокировано телефон: {blocked_phone}\n\n"
        f"Сформировано: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    )

    if not (SMTP_USER and SMTP_PASSWORD and ADMIN_EMAIL):
        print("[EMAIL] SMTP не настроен, отчёт не отправлен.")
        print(body)
        return

    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = ADMIN_EMAIL
    msg["Subject"] = "Отчёт по боту Vida Bachata"
    msg.set_content(body)

    msg.add_attachment(users_csv, maintype="text", subtype="csv", filename="users.csv")
    msg.add_attachment(orders_csv, maintype="text", subtype="csv", filename="orders.csv")
    msg.add_attachment(tickets_csv, maintype="text", subtype="csv", filename="tickets.csv")
    msg.add_attachment(attempts_csv, maintype="text", subtype="csv", filename="reg_attempts.csv")

    try:
        await aiosmtplib.send(
            msg, hostname=SMTP_HOST, port=SMTP_PORT,
            username=SMTP_USER, password=SMTP_PASSWORD, use_tls=True,
        )
        print(f"[EMAIL] Отчёт отправлен на {ADMIN_EMAIL}")
    except Exception as e:
        print(f"[EMAIL] Ошибка: {e}")


# ================== СОСТОЯНИЯ ==================
class Reg(StatesGroup):
    phone = State()
    name = State()
    surname = State()
    email = State()
    gender = State()
    confirm = State()
    ticket_type = State()
    partner_input = State()
    payment = State()


class Admin(StatesGroup):
    menu = State()
    add_title = State()
    add_date_text = State()
    add_place = State()
    add_description = State()
    add_price_single = State()
    add_price_pair = State()
    add_photo = State()
    checker_add_username = State()
    checker_add_phone = State()
    checker_add_name = State()
    checker_choose_event = State()
    blacklist_add_username = State()
    blacklist_add_phone = State()
    blacklist_reason = State()
    broadcast = State()
    find_ticket = State()


class CheckerStates(StatesGroup):
    menu = State()
    waiting_qr = State()
    find_code = State()
    find_name = State()


# ================== /start (deep_link = invite) ==================
@dp.message(CommandStart(deep_link=True))
async def start_with_invite(message: Message, state: FSMContext, command: CommandObject):
    """Обработка /start с payload (invite_XXX)."""
    payload = command.args or ""
    if payload.startswith("invite_"):
        try:
            inviter_id = int(payload.replace("invite_", ""))
            if inviter_id != message.from_user.id:
                pending_invites[message.from_user.id] = inviter_id
                print(f"[INVITE] user={message.from_user.id} приглашён от {inviter_id}")
        except ValueError:
            pass

    # Передаём управление обычному start
    await start(message, state)


# ================== /start ==================
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    user_name = message.from_user.first_name or "друг"

       # ============ ГЛАВНЫЙ АДМИН ============
    if user_id == ADMIN_ID:
        greeting = (
            f"Привет, {user_name} 👋\n"
            "Вы вошли как <b>главный администратор</b>.\n\n"
            "Используйте панель ниже для управления ботом.\n\n"
            "Подпишитесь на соцсети:\n"
            "ВК (https://vk.com/vidabachata) • "
            "Telegram (https://t.me/+eTMoEG6V1883Nzli)"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin:back")],
            [InlineKeyboardButton(text="🎉 Мероприятия", callback_data="show_events")],
            [InlineKeyboardButton(text="🎟 Мои билеты", callback_data="my_tickets")],
            [InlineKeyboardButton(text="ВК", url="https://vk.com/vidabachata"),
             InlineKeyboardButton(text="Telegram", url="https://t.me/+eTMoEG6V1883Nzli")],
        ])
        await message.answer(greeting, reply_markup=kb, parse_mode="HTML")
        return

    # ============ CHECKER ============
    checker = find_checker_for_user(
        user_id=user_id,
        username=message.from_user.username or "",
    )

    if checker and not checker.get("user_id") and checker.get("phone"):
        await message.answer(
            f"👋 {user_name}, вы — админ на входе.\n\n"
            "Для активации доступа поделитесь контактом:",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="📱 Поделиться контактом", request_contact=True)]],
                resize_keyboard=True, one_time_keyboard=True,
            ),
        )
        await state.update_data(checker_id=checker["id"])
        await state.set_state(CheckerStates.menu)
        return

    if checker:
        if not checker.get("user_id"):
            activate_checker(checker["id"], user_id)
        return await checker_start(message, state, checker)

    # ============ ЧС ============
    if is_blacklisted(username=message.from_user.username or ""):
        log_reg_attempt(
            user_id=user_id,
            username=message.from_user.username or "",
            first_name=user_name,
            status="blocked_username",
            step="start",
            reason="username в ЧС (/start)",
        )
        await message.answer("⚠️ Бот на техническом обслуживании. Попробуйте позже.")
        return

    # ============ ЗАРЕГИСТРИРОВАННЫЙ ============
    if user_id in users:
        greeting = (
            f"Привет, {user_name} 👋\n"
            "Здесь можно купить билет на вечеринки <b>Vida Bachata</b> 💃🕺\n\n"
            "Выберите мероприятие:\n\n"
            "Подпишитесь на соцсети:\n"
            "ВК (https://vk.com/vidabachata) • "
            "Telegram (https://t.me/+eTMoEG6V1883Nzli)"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎉 Мероприятия", callback_data="show_events")],
            [InlineKeyboardButton(text="🎟 Мои билеты", callback_data="my_tickets")],
            [InlineKeyboardButton(text="ВК", url="https://vk.com/vidabachata"),
             InlineKeyboardButton(text="Telegram", url="https://t.me/+eTMoEG6V1883Nzli")],
        ])
        await message.answer(greeting, reply_markup=kb, parse_mode="HTML")
        return

    # ============ НОВЫЙ ============
    greeting = (
        f"Привет, {user_name} 👋\n"
        "Здесь можно купить билет на вечеринки <b>Vida Bachata</b> 💃🕺\n\n"
        "Зарегистрируйтесь или посмотрите мероприятия.\n\n"
        "Подпишитесь на соцсети:\n"
        "ВК (https://vk.com/vidabachata) • "
        "Telegram (https://t.me/+eTMoEG6V1883Nzli)"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Зарегистрироваться", callback_data="start_registration")],
        [InlineKeyboardButton(text="🎉 Мероприятия", callback_data="show_events")],
        [InlineKeyboardButton(text="ВК", url="https://vk.com/vidabachata"),
         InlineKeyboardButton(text="Telegram", url="https://t.me/+eTMoEG6V1883Nzli")],
    ])
    await message.answer(greeting, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "start_registration")
async def start_registration(call: CallbackQuery, state: FSMContext):
    if call.from_user.id == ADMIN_ID:
        await call.answer("Вы администратор, регистрация не нужна 👍", show_alert=True)
        return

    checker = find_checker_for_user(
        user_id=call.from_user.id,
        username=call.from_user.username or "",
    )
    if checker:
        await call.answer("Вы админ на входе 👍", show_alert=True)
        return

    if is_blacklisted(username=call.from_user.username or ""):
        log_reg_attempt(
            user_id=call.from_user.id,
            username=call.from_user.username or "",
            first_name=call.from_user.first_name or "",
            status="blocked_username",
            step="start_registration",
            reason="username в ЧС",
        )
        await call.answer("Доступ ограничен", show_alert=True)
        await call.message.answer("⚠️ Бот на техническом обслуживании. Попробуйте позже.")
        return

    if call.from_user.id in users:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎉 Мероприятия", callback_data="show_events")],
            [InlineKeyboardButton(text="🎟 Мои билеты", callback_data="my_tickets")],
        ])
        await call.message.answer(
            "✅ Вы уже зарегистрированы!\n\nНажмите «Мероприятия».",
            reply_markup=kb,
        )
        return

    log_reg_attempt(
        user_id=call.from_user.id,
        username=call.from_user.username or "",
        first_name=call.from_user.first_name or "",
        status="started",
        step="start",
    )

    print(f"[REG START] user={call.from_user.id} — запускаем регистрацию с телефона")

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться контактом", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await call.message.answer(
        "📱 Нажмите «Поделиться контактом», чтобы подтвердить ваш номер телефона:",
        reply_markup=kb,
    )
    await state.set_state(Reg.phone)


# ================== РЕГИСТРАЦИЯ ==================
@dp.message(Reg.phone, F.contact)
async def get_phone(message: Message, state: FSMContext):
    phone = message.contact.phone_number

    checker = find_checker_for_user(
        user_id=message.from_user.id,
        username=message.from_user.username or "",
        phone=phone,
    )
    if checker:
        activate_checker(checker["id"], message.from_user.id)
        await message.answer(
            "✅ Ваш номер подтверждён. Добро пожаловать!",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.clear()
        return await checker_start(message, state, checker)

    if is_blacklisted(phone=phone):
        log_reg_attempt(
            user_id=message.from_user.id,
            username=message.from_user.username or "",
            first_name=message.from_user.first_name or "",
            phone=phone,
            status="blocked_phone",
            step="phone",
            reason="телефон в ЧС",
        )
        await message.answer(
            "⚠️ Бот на техническом обслуживании. Попробуйте позже.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.clear()
        return

    await state.update_data(phone=phone)
    await message.answer(
        "✅ Телефон получен!\n\n👤 Отправьте ваше Имя:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(Reg.name)


@dp.message(Reg.phone)
async def wrong_phone(message: Message):
    await message.answer("Пожалуйста, нажмите кнопку «Поделиться контактом» 👇")


@dp.message(Reg.name)
async def get_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("Слишком короткое имя:")
        return
    await state.update_data(name=name)
    await message.answer("👤 Отправьте вашу Фамилию.")
    await state.set_state(Reg.surname)


@dp.message(Reg.surname)
async def get_surname(message: Message, state: FSMContext):
    surname = message.text.strip()
    if len(surname) < 2:
        await message.answer("Слишком короткая фамилия:")
        return
    await state.update_data(surname=surname)
    await message.answer(
        "📧 Отправьте ваш Email (для рассылки).\n"
        "Можно пропустить — отправьте <code>-</code>",
        parse_mode="HTML",
    )
    await state.set_state(Reg.email)


@dp.message(Reg.email)
async def get_email(message: Message, state: FSMContext):
    email_input = message.text.strip()

    if email_input == "-":
        await state.update_data(email="")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="👨 Мужской", callback_data="gender:M"),
            InlineKeyboardButton(text="👩 Женский", callback_data="gender:F"),
        ]])
        await message.answer("📭 Email пропущен.\n\n⚧ Выберите ваш пол:", reply_markup=kb)
        await state.set_state(Reg.gender)
        return

    if not is_valid_email(email_input):
        await message.answer(
            "❌ Похоже, email введён неверно. Попробуйте ещё раз\n"
            "или отправьте <code>-</code>, чтобы пропустить:",
            parse_mode="HTML",
        )
        return

    await state.update_data(email=email_input)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👨 Мужской", callback_data="gender:M"),
        InlineKeyboardButton(text="👩 Женский", callback_data="gender:F"),
    ]])
    await message.answer(
        "✅ Email сохранён для рассылки.\n\n⚧ Выберите ваш пол:",
        reply_markup=kb,
    )
    await state.set_state(Reg.gender)


@dp.callback_query(F.data.startswith("gender:"), Reg.gender)
async def get_gender(call: CallbackQuery, state: FSMContext):
    gender = "Мужской" if call.data == "gender:M" else "Женский"
    await state.update_data(gender=gender)
    data = await state.get_data()

    email_line = data.get("email") or "—"
    text = (
        "📋 <b>Проверьте данные:</b>\n\n"
        f"📱 {data.get('phone', '—')}\n"
        f"👤 {data.get('name', '—')} {data.get('surname', '—')}\n"
        f"📧 {email_line}\n"
        f"⚧ {gender}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Согласен", callback_data="confirm_yes")],
        [InlineKeyboardButton(text="🔄 Перезаполнить", callback_data="confirm_restart")],
    ])
    await call.message.edit_text(f"Пол: {gender}")
    await call.message.answer(text, parse_mode="HTML")
    await call.message.answer("Всё верно?", reply_markup=kb)
    await state.set_state(Reg.confirm)


@dp.callback_query(F.data == "confirm_yes", Reg.confirm)
async def confirm_yes(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    data["registered_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    data["username"] = call.from_user.username or ""
    users[call.from_user.id] = data
    if call.from_user.username:
        username_index[call.from_user.username.lower()] = call.from_user.id

    log_reg_attempt(
        user_id=call.from_user.id,
        username=call.from_user.username or "",
        first_name=data.get("name", ""),
        phone=data.get("phone", ""),
        status="success",
        step="confirm",
    )

    save_data()

    inviter_id = pending_invites.pop(call.from_user.id, None)
    if inviter_id and inviter_id in users:
        try:
            await bot.send_message(
                inviter_id,
                f"🎉 Второй участник зарегистрировался!\n"
                f"👤 {data['name']} {data['surname']}"
            )
        except Exception:
            pass

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎉 Мероприятия", callback_data="show_events")],
        [InlineKeyboardButton(text="🎟 Мои билеты", callback_data="my_tickets")],
    ])
    await call.message.edit_text(
        "✅ Регистрация завершена!\n\nНажмите «Мероприятия».",
        reply_markup=kb,
    )


@dp.callback_query(F.data == "confirm_restart", Reg.confirm)
async def confirm_restart(call: CallbackQuery, state: FSMContext):
    users.pop(call.from_user.id, None)
    if call.from_user.username:
        username_index.pop(call.from_user.username.lower(), None)
    await state.clear()
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться контактом", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await call.message.edit_text("🔄 Начинаем заново.")
    await call.message.answer(
        "📱 Нажмите «Поделиться контактом»:",
        reply_markup=kb,
    )
    await state.set_state(Reg.phone)


# ================== ПАНЕЛЬ CHECKER'А ==================
async def checker_start(message: Message, state: FSMContext, checker: dict):
    """Панель администратора на входе."""
    await state.clear()
    await state.set_state(CheckerStates.menu)

    name = checker.get("name") or message.from_user.first_name or "коллега"
    event_id = checker.get("event_id")

    if event_id is None:
        access_text = "🌐 <b>Все вечеринки</b>"
    else:
        ev = next((e for e in events if str(e["id"]) == str(event_id)), None)
        if ev:
            access_text = f"🎯 <b>{ev['title']}</b>\n📅 {ev['date_text']}"
        else:
            access_text = f"🎯 Событие (id={event_id})"

    text = (
        f"👋 Привет, <b>{name}</b>!\n\n"
        f"🎫 Вы — <b>администратор на входе</b>.\n\n"
        f"Область доступа:\n{access_text}\n\n"
        "Выберите действие:"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 Сканировать QR-код", callback_data="checker:scan_qr")],
        [InlineKeyboardButton(text="🔍 Найти билет по коду", callback_data="checker:find_code")],
        [InlineKeyboardButton(text="👤 Найти по имени/телефону", callback_data="checker:find_name")],
        [InlineKeyboardButton(text="📊 Статистика входа", callback_data="checker:stats")],
        [InlineKeyboardButton(text="🏠 На главную", callback_data="main_menu")],
    ])

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "checker:menu")
async def checker_menu_callback(call: CallbackQuery, state: FSMContext):
    checker = find_checker_for_user(
        user_id=call.from_user.id,
        username=call.from_user.username or "",
    )
    if not checker:
        await call.answer("Вы не админ на входе", show_alert=True)
        return
    await state.clear()
    await checker_start(call.message, state, checker)
    await call.answer()


@dp.message(F.contact, CheckerStates.menu)
async def checker_phone_confirm(message: Message, state: FSMContext):
    """Обработка подтверждения телефона для checker'а."""
    phone = message.contact.phone_number

    checker = find_checker_for_user(
        user_id=message.from_user.id,
        username=message.from_user.username or "",
        phone=phone,
    )
    if not checker:
        await message.answer(
            "❌ Ваш номер не найден в списке админов на входе.\n"
            "Обратитесь к главному администратору.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.clear()
        return

    activate_checker(checker["id"], message.from_user.id)
    await message.answer("✅ Номер подтверждён!", reply_markup=ReplyKeyboardRemove())
    await checker_start(message, state, checker)


@dp.message(CheckerStates.menu)
async def checker_any_message(message: Message, state: FSMContext):
    """Если checker пишет что-то вручную."""
    await message.answer(
        "Используйте кнопки ниже 👇\n"
        "Или отправьте /start для возврата в меню.",
    )


# ================== СКАНИРОВАНИЕ QR (CHECKER) ==================
@dp.callback_query(F.data == "checker:scan_qr")
async def checker_scan_qr(call: CallbackQuery, state: FSMContext):
    checker = find_checker_for_user(
        user_id=call.from_user.id,
        username=call.from_user.username or "",
    )
    if not checker:
        await call.answer("Вы не админ на входе", show_alert=True)
        return

    await call.message.answer(
        "📷 <b>Сканирование QR</b>\n\n"
        "Отправьте <b>фото</b> с QR-кодом билета.\n\n"
        "Совет:\n"
        "• Наведите камеру так, чтобы QR был целиком в кадре\n"
        "• Хорошее освещение\n"
        "• Держите камеру ровно\n\n"
        "Для отмены — /start",
        parse_mode="HTML",
    )
    await state.set_state(CheckerStates.waiting_qr)


# ================== СКАНИРОВАНИЕ QR (ГЛАВНЫЙ АДМИН) ==================
@dp.callback_query(F.data == "admin:scan_qr")
async def admin_scan_qr(call: CallbackQuery, state: FSMContext):
    """Сканирование QR главным админом."""
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для главного админа", show_alert=True)
        return

    await call.message.answer(
        "📷 <b>Сканирование QR</b>\n\n"
        "Отправьте <b>фото</b> с QR-кодом билета.\n\n"
        "Вы можете погасить любой билет "
        "(без ограничений по событию).\n\n"
        "Для отмены — /admin",
        parse_mode="HTML",
    )
    await state.set_state(CheckerStates.waiting_qr)


# ================== ОБРАБОТКА ФОТО С QR ==================
@dp.message(CheckerStates.waiting_qr, F.photo)
async def process_qr_photo(message: Message, state: FSMContext):
    """Обработка фото с QR-кодом — для главного админа и checker'ов."""
    is_super_admin = (message.from_user.id == ADMIN_ID)
    checker = None

    if not is_super_admin:
        checker = find_checker_for_user(
            user_id=message.from_user.id,
            username=message.from_user.username or "",
        )
        if not checker:
            await message.answer("⛔ Вы не админ на входе.")
            await state.clear()
            return

    msg = await message.answer("🔍 Распознаю QR-код...")

    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file.file_path)
        image_bytes = file_bytes.read()

        compressed = await asyncio.to_thread(compress_image_for_qr, image_bytes)
        code = await asyncio.to_thread(decode_qr_via_qrserver, compressed)

        if not code:
            kb_err = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🏠 На главную", callback_data="main_menu")
            ]])
            await msg.edit_text(
                "❌ Не удалось распознать QR-код.\n\n"
                "Попробуйте:\n"
                "• Сделать фото крупнее\n"
                "• Улучшить освещение\n"
                "• Или введите код вручную",
                reply_markup=kb_err,
            )
            if is_super_admin:
                await state.set_state(Admin.menu)
            else:
                await state.set_state(CheckerStates.menu)
            return

        ticket = tickets.get(code.upper().strip())
        if not ticket:
            kb_err = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🏠 На главную", callback_data="main_menu")
            ]])
            await msg.edit_text(
                f"⚠️ QR распознан: <code>{code}</code>\n\n"
                "Но билет с таким кодом не найден в базе.",
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            if is_super_admin:
                await state.set_state(Admin.menu)
            else:
                await state.set_state(CheckerStates.menu)
            return

        if not is_super_admin and not checker_can_access_event(checker, ticket.get("event_id")):
            ev = next(
                (e for e in events if str(e["id"]) == str(ticket.get("event_id"))),
                None
            )
            ev_title = ev["title"] if ev else "другое событие"
            kb_err = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🏠 На главную", callback_data="main_menu")
            ]])
            await msg.edit_text(
                f"⛔ У вас нет доступа к этому событию.\n\n"
                f"Билет относится к: <b>{ev_title}</b>",
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            await state.set_state(CheckerStates.menu)
            return

        await msg.delete()
        if is_super_admin:
            await show_super_admin_ticket_card(message, code.upper().strip(), ticket)
            await state.set_state(Admin.menu)
        else:
            await show_checker_ticket_card(message, code.upper().strip(), ticket)

    except Exception as e:
        print(f"[QR] Ошибка обработки: {e}")
        kb_err = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏠 На главную", callback_data="main_menu")
        ]])
        await msg.edit_text(
            "❌ Ошибка при обработке фото. Попробуйте ещё раз.",
            reply_markup=kb_err,
        )
        if is_super_admin:
            await state.set_state(Admin.menu)
        else:
            await state.set_state(CheckerStates.menu)


@dp.message(CheckerStates.waiting_qr)
async def wrong_qr_input(message: Message, state: FSMContext):
    await message.answer(
        "📷 Отправьте <b>фото</b> с QR-кодом.\n\n"
        "Для отмены — /start",
        parse_mode="HTML",
    )


# ================== ПОИСК БИЛЕТА ПО КОДУ ==================
@dp.callback_query(F.data == "checker:find_code")
async def checker_find_code(call: CallbackQuery, state: FSMContext):
    checker = find_checker_for_user(
        user_id=call.from_user.id,
        username=call.from_user.username or "",
    )
    if not checker:
        await call.answer("Вы не админ на входе", show_alert=True)
        return

    await call.message.answer(
        "🔍 <b>Поиск билета</b>\n\n"
        "Отправьте <b>код билета</b> (например, <code>VIDA-XXXX-XXXX</code>).\n\n"
        "Код написан под QR-кодом в билете гостя.\n\n"
        "Для отмены — /start",
        parse_mode="HTML",
    )
    await state.set_state(CheckerStates.find_code)


@dp.message(CheckerStates.find_code)
async def checker_process_code(message: Message, state: FSMContext):
    checker = find_checker_for_user(
        user_id=message.from_user.id,
        username=message.from_user.username or "",
    )
    if not checker:
        await message.answer("⛔ Вы не админ на входе.")
        await state.clear()
        return

    code = message.text.strip().upper()

    ticket = tickets.get(code)
    if not ticket:
        await message.answer(
            f"❌ Билет с кодом <code>{code}</code> не найден.",
            parse_mode="HTML",
        )
        return

    if not checker_can_access_event(checker, ticket.get("event_id")):
        ev = next(
            (e for e in events if str(e["id"]) == str(ticket.get("event_id"))),
            None
        )
        ev_title = ev["title"] if ev else "другое событие"
        await message.answer(
            f"⛔ У вас нет доступа к этому событию.\n\n"
            f"Билет относится к: <b>{ev_title}</b>",
            parse_mode="HTML",
        )
        return

    await show_checker_ticket_card(message, code, ticket)


# ================== ПОИСК ПО ИМЕНИ / ТЕЛЕФОНУ ==================
@dp.callback_query(F.data == "checker:find_name")
async def checker_find_name(call: CallbackQuery, state: FSMContext):
    checker = find_checker_for_user(
        user_id=call.from_user.id,
        username=call.from_user.username or "",
    )
    if not checker:
        await call.answer("Вы не админ на входе", show_alert=True)
        return

    await call.message.answer(
        "👤 <b>Поиск по имени или телефону</b>\n\n"
        "Отправьте имя или номер телефона гостя.\n\n"
        "Используется, если гость забыл код билета.\n\n"
        "Для отмены — /start",
        parse_mode="HTML",
    )
    await state.set_state(CheckerStates.find_name)


@dp.message(CheckerStates.find_name)
async def checker_process_name(message: Message, state: FSMContext):
    checker = find_checker_for_user(
        user_id=message.from_user.id,
        username=message.from_user.username or "",
    )
    if not checker:
        await message.answer("⛔ Вы не админ на входе.")
        await state.clear()
        return

    query = message.text.strip().lower()
    if len(query) < 2:
        await message.answer("Слишком короткий запрос. Введите минимум 2 символа.")
        return

    found = []
    for code, t in tickets.items():
        if not checker_can_access_event(checker, t.get("event_id")):
            continue

        holder = (t.get("holder") or "").lower()
        uid = t.get("user_id")

        match = query in holder

        if not match and uid:
            try:
                u = users.get(int(uid), {})
                full = f"{u.get('name', '')} {u.get('surname', '')}".lower()
                phone = (u.get("phone") or "").replace(" ", "").replace("-", "").replace("+", "")
                query_clean = query.replace(" ", "").replace("-", "").replace("+", "")
                if query in full or (query_clean and query_clean in phone):
                    match = True
            except (TypeError, ValueError):
                pass

        if match:
            found.append((code, t))

    if not found:
        await message.answer(
            f"❌ По запросу «{query}» ничего не найдено.\n\n"
            "Попробуйте другое имя или телефон.",
        )
        return

    text = f"🔍 Найдено: <b>{len(found)}</b>\n\n"
    buttons = []
    for code, t in found[:10]:
        status = "🔴" if t.get("used") else "🟢"
        type_label = "Парный" if t.get("type") == "pair" else "Обычный"
        text += (
            f"{status} <code>{code}</code>\n"
            f"   👤 {t.get('holder', '—')}\n"
            f"   🎫 {type_label}\n"
            f"   🎉 {t.get('event_title', '—')}\n\n"
        )
        buttons.append([InlineKeyboardButton(
            text=f"Показать {code[-8:]}",
            callback_data=f"checker:show:{code}"
        )])

    buttons.append([InlineKeyboardButton(
        text="⬅️ В меню", callback_data="checker:menu"
    )])

    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("checker:show:"))
async def checker_show_ticket(call: CallbackQuery):
    code = call.data.split(":", 2)[2]

    checker = find_checker_for_user(
        user_id=call.from_user.id,
        username=call.from_user.username or "",
    )
    if not checker:
        await call.answer("Вы не админ на входе", show_alert=True)
        return

    ticket = tickets.get(code)
    if not ticket:
        await call.answer("Билет не найден", show_alert=True)
        return

    if not checker_can_access_event(checker, ticket.get("event_id")):
        await call.answer("Нет доступа к этому событию", show_alert=True)
        return

    await call.answer()
    await show_checker_ticket_card(call.message, code, ticket)


# ================== КАРТОЧКА БИЛЕТА ДЛЯ CHECKER'А ==================
async def show_checker_ticket_card(message: Message, code: str, ticket: dict):
    """Показывает карточку билета с кнопкой погашения (для checker'а)."""
    status = "🔴 использован" if ticket.get("used") else "🟢 активен"

    holder_name = ticket.get("holder", "—")
    holder_username = ""
    holder_phone = "—"

    uid = ticket.get("user_id")
    try:
        if uid:
            user = users.get(int(uid), {})
            holder_username = user.get("username") or ""
            holder_phone = user.get("phone") or "—"
    except (TypeError, ValueError):
        pass

    text = (
        f"🎫 <b>Билет найден</b>\n\n"
        f"Код: <code>{code}</code>\n"
        f"Статус: {status}\n\n"
        f"👤 {holder_name}\n"
    )
    if holder_username:
        text += f"🔗 @{holder_username}\n"
    text += f"📱 {holder_phone}\n\n"
    text += (
        f"🎫 {_ticket_type_label(ticket)}\n"
        f"🎉 {ticket.get('event_title', '—')}\n"
        f"📅 {ticket.get('event_date', '—')}\n"
        f"🕐 Выдан: {ticket.get('issued_at', '—')}\n"
    )

    kb_rows = []
    if not ticket.get("used"):
        kb_rows.append([InlineKeyboardButton(
            text="✅ Погасить билет",
            callback_data=f"checker:use:{code}"
        )])
    kb_rows.append([InlineKeyboardButton(
        text="🔄 Обновить",
        callback_data=f"checker:show:{code}"
    )])
    kb_rows.append([InlineKeyboardButton(
        text="📷 Сканировать ещё",
        callback_data="checker:scan_qr"
    )])
    kb_rows.append([InlineKeyboardButton(
        text="⬅️ В меню", callback_data="checker:menu"
    )])
    kb_rows.append([InlineKeyboardButton(
        text="🏠 На главную", callback_data="main_menu"
    )])

    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="HTML",
    )


# ================== КАРТОЧКА БИЛЕТА ДЛЯ ГЛАВНОГО АДМИНА ==================
async def show_super_admin_ticket_card(message: Message, code: str, ticket: dict):
    """Карточка билета для главного админа."""
    status = "🔴 использован" if ticket.get("used") else "🟢 активен"

    holder_name = ticket.get("holder", "—")
    holder_username = ""
    holder_phone = "—"

    uid = ticket.get("user_id")
    try:
        if uid:
            user = users.get(int(uid), {})
            holder_username = user.get("username") or ""
            holder_phone = user.get("phone") or "—"
    except (TypeError, ValueError):
        pass

    text = (
        f"🎫 <b>Билет найден</b> <i>(главный админ)</i>\n\n"
        f"Код: <code>{code}</code>\n"
        f"Статус: {status}\n\n"
        f"👤 {holder_name}\n"
    )
    if holder_username:
        text += f"🔗 @{holder_username}\n"
    text += f"📱 {holder_phone}\n\n"
    text += (
        f"🎫 {_ticket_type_label(ticket)}\n"
        f"🎉 {ticket.get('event_title', '—')}\n"
        f"📅 {ticket.get('event_date', '—')}\n"
        f"🕐 Выдан: {ticket.get('issued_at', '—')}\n"
    )

    kb_rows = []
    if not ticket.get("used"):
        kb_rows.append([InlineKeyboardButton(
            text="✅ Погасить билет",
            callback_data=f"admin:use_ticket_qr:{code}"
        )])
    kb_rows.append([InlineKeyboardButton(
        text="📷 Сканировать ещё",
        callback_data="admin:scan_qr"
    )])
    kb_rows.append([InlineKeyboardButton(
        text="⬅️ В админ-панель", callback_data="admin:back"
    )])
    kb_rows.append([InlineKeyboardButton(
        text="🏠 На главную", callback_data="main_menu"
    )])

    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="HTML",
    )


# ================== ПОГАШЕНИЕ БИЛЕТА CHECKER'ОМ ==================
@dp.callback_query(F.data.startswith("checker:use:"))
async def checker_use_ticket(call: CallbackQuery):
    code = call.data.split(":", 2)[2]

    checker = find_checker_for_user(
        user_id=call.from_user.id,
        username=call.from_user.username or "",
    )
    if not checker:
        await call.answer("Вы не админ на входе", show_alert=True)
        return

    ticket = tickets.get(code)
    if not ticket:
        await call.answer("Билет не найден", show_alert=True)
        return

    if not checker_can_access_event(checker, ticket.get("event_id")):
        await call.answer("Нет доступа к этому событию", show_alert=True)
        return

    if ticket.get("used"):
        await call.answer("Билет уже погашен", show_alert=True)
        return

    ticket["used"] = True
    ticket["used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    ticket["used_by"] = checker.get("name") or call.from_user.full_name
    save_data()

    log_ticket_action(code, checker, action="used")

    # Уведомление главному админу
    try:
        checker_name = checker.get("name") or call.from_user.full_name
        await bot.send_message(
            ADMIN_ID,
            f"🎫 <b>Билет погашен</b>\n\n"
            f"Код: <code>{code}</code>\n"
            f"🎉 {ticket.get('event_title', '—')}\n"
            f"👤 {ticket.get('holder', '—')}\n\n"
            f"Погасил: <b>{checker_name}</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    # Уведомление гостю (владельцу билета)
    try:
        owner_id = ticket.get("user_id")
        if owner_id:
            await bot.send_message(
                owner_id,
                f"✅ <b>Билет погашен!</b>\n\n"
                f"🎫 Код: <code>{code}</code>\n"
                f"🎉 {ticket.get('event_title', '—')}\n"
                f"📅 {ticket.get('event_date', '—')}\n"
                f"👤 {ticket.get('holder', '—')}\n\n"
                f"Хорошего вечера! 💃🕺",
                parse_mode="HTML",
            )
    except Exception as e:
        print(f"[NOTIFY] Ошибка уведомления гостя: {e}")

    # Checker видит подтверждение
    checker_name = checker.get("name") or call.from_user.full_name
    kb_ok = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 Сканировать ещё", callback_data="checker:scan_qr")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="checker:menu")],
        [InlineKeyboardButton(text="🏠 На главную", callback_data="main_menu")],
    ])
    await call.message.edit_text(
        f"✅ <b>Билет погашен</b>\n\n"
        f"Код: <code>{code}</code>\n"
        f"👤 {ticket.get('holder', '—')}\n"
        f"🎉 {ticket.get('event_title', '—')}\n\n"
        f"Погасил: {checker_name}",
        reply_markup=kb_ok,
        parse_mode="HTML",
    )


# ================== ПОГАШЕНИЕ ГЛАВНЫМ АДМИНОМ (через QR) ==================
@dp.callback_query(F.data.startswith("admin:use_ticket_qr:"))
async def admin_use_ticket_qr(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для главного админа", show_alert=True)
        return

    code = call.data.split(":", 2)[2]
    ticket = tickets.get(code)
    if not ticket:
        await call.answer("Билет не найден", show_alert=True)
        return
    if ticket.get("used"):
        await call.answer("Уже погашен", show_alert=True)
        return

    ticket["used"] = True
    ticket["used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    ticket["used_by"] = "Главный админ"
    save_data()

    # Уведомление гостю
    try:
        owner_id = ticket.get("user_id")
        if owner_id:
            await bot.send_message(
                int(owner_id),
                f"✅ <b>Ваш билет погашен</b>\n\n"
                f"Код: <code>{code}</code>\n"
                f"Администратор: главный админ\n"
                f"Время: {ticket['used_at']}",
                parse_mode="HTML",
            )
    except Exception:
        pass

    await call.answer("✅ Погашен")
    await show_super_admin_ticket_card(call.message, code, ticket)


# ================== ОТКЛОНЕНИЕ БИЛЕТА CHECKER'ОМ ==================
@dp.callback_query(F.data.startswith("checker:reject:"))
async def checker_reject_ticket(call: CallbackQuery):
    """Checker отклоняет присланный билет."""
    code = call.data.split(":", 2)[2]

    checker = find_checker_for_user(
        user_id=call.from_user.id,
        username=call.from_user.username or "",
    )
    if not checker:
        await call.answer("Вы не админ на входе", show_alert=True)
        return

    ticket = tickets.get(code)
    if not ticket:
        await call.answer("Билет не найден", show_alert=True)
        return

    log_ticket_action(code, checker, action="rejected")

    try:
        owner_id = ticket.get("user_id")
        if owner_id:
            await bot.send_message(
                owner_id,
                f"❌ <b>Проверяющий отклонил билет</b>\n\n"
                f"Код: <code>{code}</code>\n\n"
                f"Обратитесь к организатору.",
                parse_mode="HTML",
            )
    except Exception:
        pass

    checker_name = checker.get("name") or call.from_user.full_name
    kb_rej = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="checker:menu")],
        [InlineKeyboardButton(text="🏠 На главную", callback_data="main_menu")],
    ])
    await call.message.edit_text(
        f"❌ <b>Билет отклонён</b>\n\n"
        f"Код: <code>{code}</code>\n"
        f"👤 {ticket.get('holder', '—')}\n\n"
        f"Отклонил: {checker_name}",
        reply_markup=kb_rej,
        parse_mode="HTML",
    )


# ================== СТАТИСТИКА CHECKER'А ==================
@dp.callback_query(F.data == "checker:stats")
async def checker_stats(call: CallbackQuery):
    checker = find_checker_for_user(
        user_id=call.from_user.id,
        username=call.from_user.username or "",
    )
    if not checker:
        await call.answer("Вы не админ на входе", show_alert=True)
        return

    accessible_events = get_checker_events(checker)
    event_ids = {str(e["id"]) for e in accessible_events}

    total = 0
    used = 0
    for code, t in tickets.items():
        if str(t.get("event_id")) not in event_ids:
            continue
        total += 1
        if t.get("used"):
            used += 1

    if checker.get("event_id") is None:
        header = "🌐 <b>Все вечеринки</b>"
    else:
        ev = next((e for e in events if str(e["id"]) == str(checker.get("event_id"))), None)
        header = f"🎯 <b>{ev['title'] if ev else 'Событие'}</b>"

    text = (
        f"📊 <b>Статистика входа</b>\n\n"
        f"{header}\n\n"
        f"🎫 Всего билетов: <b>{total}</b>\n"
        f"🔴 Погашено: <b>{used}</b>\n"
        f"🟢 Осталось: <b>{total - used}</b>\n"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="checker:stats")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="checker:menu")],
        [InlineKeyboardButton(text="🏠 На главную", callback_data="main_menu")],
    ])

    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


# ================== АДМИН ==================
@dp.message(Command("admin"))
async def admin_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только для админа.")
        return
    await state.clear()
    await show_admin_menu(message, state)


async def show_admin_menu(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать событие", callback_data="admin:add")],
        [InlineKeyboardButton(text="🗑 Удалить событие", callback_data="admin:delete")],
        [InlineKeyboardButton(text="📋 Список событий", callback_data="admin:list")],
        [InlineKeyboardButton(text="🎉 Анонсы (просмотр)", callback_data="show_events")],
        [InlineKeyboardButton(text="📷 Сканировать QR", callback_data="admin:scan_qr")],
        [InlineKeyboardButton(text="🎫 Админы на входе", callback_data="admin:checkers")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users:0")],
        [InlineKeyboardButton(text="🚫 Чёрный список", callback_data="admin:blacklist")],
        [InlineKeyboardButton(text="📊 Попытки регистрации", callback_data="admin:reg_attempts")],
        [InlineKeyboardButton(text="📈 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🎟 Все билеты", callback_data="admin:tickets")],
        [InlineKeyboardButton(text="🎫 Найти билет", callback_data="admin:find_ticket")],
        [InlineKeyboardButton(text="📤 Отправить отчёт на email", callback_data="admin:report")],
    ])
    await message.answer(
        f"🛠 <b>Админ-панель</b>\n\n"
        f"Событий: {len(events)} | Заказов: {len(orders)} | Юзеров: {len(users)}\n"
        f"ЧС: {len(blacklist)} | Попыток: {len(reg_attempts)} | Checkers: {len(checkers)}",
        reply_markup=kb, parse_mode="HTML",
    )
    await state.set_state(Admin.menu)


@dp.callback_query(F.data == "admin:back")
async def admin_back(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_admin_menu(call.message, state)


@dp.callback_query(F.data == "admin:report")
async def admin_report(call: CallbackQuery):
    await call.answer("Отправляю...")
    await send_report_to_admin()
    await call.message.answer("📤 Отчёт отправлен на email.")


# ================== РАЗДЕЛ «АДМИНЫ НА ВХОДЕ» ==================
@dp.callback_query(F.data == "admin:checkers")
async def admin_checkers_menu(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для главного админа", show_alert=True)
        return

    total = len(checkers)
    all_events_cnt = sum(1 for c in checkers.values() if c.get("event_id") is None)
    specific_cnt = total - all_events_cnt
    active_cnt = sum(1 for c in checkers.values() if c.get("user_id"))

    text = (
        f"🎫 <b>Админы на входе</b>\n\n"
        f"Всего: <b>{total}</b>\n"
        f"🌐 На все вечеринки: <b>{all_events_cnt}</b>\n"
        f"🎯 На конкретные: <b>{specific_cnt}</b>\n"
        f"🟢 Активировано: <b>{active_cnt}</b>\n\n"
        f"Что делаем?"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить по @username", callback_data="admin:chk:add_username")],
        [InlineKeyboardButton(text="📱 Добавить по телефону", callback_data="admin:chk:add_phone")],
        [InlineKeyboardButton(text="📋 Список всех", callback_data="admin:chk:list")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="admin:chk:remove")],
        [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:back")],
    ])

    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "admin:chk:add_username")
async def admin_chk_add_username(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для главного админа", show_alert=True)
        return

    await state.update_data(chk_type="username")
    await call.message.edit_text(
        "➕ <b>Добавление админа на входе</b>\n\n"
        "Шаг 1/3. Отправьте <b>@username</b> (например, @kolya_vhod):\n\n"
        "Для отмены — /admin",
        parse_mode="HTML",
    )
    await state.set_state(Admin.checker_add_username)


@dp.message(Admin.checker_add_username)
async def admin_chk_save_username(message: Message, state: FSMContext):
    if message.text == "/admin":
        await state.clear()
        await show_admin_menu(message, state)
        return

    value = message.text.strip()
    if not value.startswith("@"):
        value = "@" + value

    if len(value) < 3:
        await message.answer("❌ Слишком короткий username. Попробуйте ещё раз:")
        return

    value_clean = value.lstrip("@").lower()
    for chk_id, c in checkers.items():
        if c.get("username") == value_clean:
            await message.answer(
                f"⚠️ Checker с username {value} уже существует.",
            )
            await state.clear()
            await show_admin_menu(message, state)
            return

    await state.update_data(chk_value=value)
    await state.set_state(Admin.checker_add_name)
    await message.answer(
        "Шаг 2/3. Отправьте <b>имя</b> checker'а (для отображения):\n\n"
        "Например: <code>Коля (вход)</code>\n\n"
        "Или отправьте <code>-</code>, чтобы оставить пустым:",
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "admin:chk:add_phone")
async def admin_chk_add_phone(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для главного админа", show_alert=True)
        return

    await state.update_data(chk_type="phone")
    await call.message.edit_text(
        "➕ <b>Добавление админа на входе</b>\n\n"
        "Шаг 1/3. Отправьте <b>номер телефона</b> (например, +79991234567):\n\n"
        "Для отмены — /admin",
        parse_mode="HTML",
    )
    await state.set_state(Admin.checker_add_phone)


@dp.message(Admin.checker_add_phone)
async def admin_chk_save_phone(message: Message, state: FSMContext):
    if message.text == "/admin":
        await state.clear()
        await show_admin_menu(message, state)
        return

    value = message.text.strip()
    value_clean = value.replace(" ", "").replace("-", "").replace("+", "")

    if len(value_clean) < 10:
        await message.answer("❌ Слишком короткий номер. Попробуйте ещё раз:")
        return

    for chk_id, c in checkers.items():
        existing = (c.get("phone") or "").replace(" ", "").replace("-", "").replace("+", "")
        if existing == value_clean:
            await message.answer(
                f"⚠️ Checker с телефоном {value} уже существует.",
            )
            await state.clear()
            await show_admin_menu(message, state)
            return

    await state.update_data(chk_value=value)
    await state.set_state(Admin.checker_add_name)
    await message.answer(
        "Шаг 2/3. Отправьте <b>имя</b> checker'а (для отображения):\n\n"
        "Например: <code>Коля (вход)</code>\n\n"
        "Или отправьте <code>-</code>, чтобы оставить пустым:",
        parse_mode="HTML",
    )


@dp.message(Admin.checker_add_name)
async def admin_chk_save_name(message: Message, state: FSMContext):
    if message.text == "/admin":
        await state.clear()
        await show_admin_menu(message, state)
        return

    name = message.text.strip()
    if name == "-":
        name = ""

    await state.update_data(chk_name=name)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🌐 Все вечеринки",
            callback_data="admin:chk:access:all"
        )],
        [InlineKeyboardButton(
            text="🎯 Конкретная вечеринка",
            callback_data="admin:chk:access:specific"
        )],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:checkers")],
    ])
    await message.answer(
        "Шаг 3/3. Выберите <b>область доступа</b>:",
        reply_markup=kb, parse_mode="HTML",
    )
    await state.set_state(Admin.checker_choose_event)


@dp.callback_query(F.data == "admin:chk:access:all")
async def admin_chk_access_all(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для главного админа", show_alert=True)
        return

    data = await state.get_data()
    await finish_add_checker(call, state, event_id=None, data=data)


@dp.callback_query(F.data == "admin:chk:access:specific")
async def admin_chk_access_specific(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для главного админа", show_alert=True)
        return

    if not events:
        await call.answer("Нет событий. Сначала создайте событие.", show_alert=True)
        return

    buttons = []
    for e in events:
        buttons.append([InlineKeyboardButton(
            text=f"🎉 {e['title']} — {e['date_text']}",
            callback_data=f"admin:chk:ev:{e['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin:checkers")])

    await call.message.edit_text(
        "🎯 Выберите <b>событие</b>, к которому даёте доступ:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("admin:chk:ev:"))
async def admin_chk_choose_event(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для главного админа", show_alert=True)
        return

    event_id = call.data.split(":", 3)[3]
    data = await state.get_data()
    await finish_add_checker(call, state, event_id=event_id, data=data)


async def finish_add_checker(call: CallbackQuery, state: FSMContext, event_id: Optional[str], data: dict):
    """Завершает добавление checker'а."""
    chk_type = data.get("chk_type", "username")
    chk_value = data.get("chk_value", "")
    chk_name = data.get("chk_name", "")

    if not chk_value:
        await call.answer("Ошибка: нет значения", show_alert=True)
        await state.clear()
        return

    chk_id = f"chk_{uuid.uuid4().hex[:8]}"

    new_checker = {
        "id": chk_id,
        "user_id": None,
        "username": chk_value.lstrip("@").lower() if chk_type == "username" else "",
        "phone": chk_value if chk_type == "phone" else "",
        "name": chk_name,
        "event_id": event_id,
        "added_by": ADMIN_ID,
        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "active": True,
    }

    checkers[chk_id] = new_checker
    save_data()

    if event_id is None:
        access_text = "🌐 Все вечеринки"
    else:
        ev = next((e for e in events if str(e["id"]) == str(event_id)), None)
        access_text = f"🎯 {ev['title']} — {ev['date_text']}" if ev else f"🎯 Событие {event_id}"

    if chk_type == "username":
        value_text = f"🔗 @{new_checker['username']}"
    else:
        value_text = f"📱 {new_checker['phone']}"

    name_text = new_checker['name'] or "—"

    await state.clear()

    await call.message.edit_text(
        f"✅ <b>Админ на входе добавлен!</b>\n\n"
        f"👤 {name_text}\n"
        f"{value_text}\n"
        f"Область: {access_text}\n\n"
        f"<i>Он активируется, когда напишет /start боту "
        f"или поделится контактом (если добавлен по телефону).</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ К списку админов", callback_data="admin:checkers")
        ]]),
    )


@dp.callback_query(F.data == "admin:chk:list")
async def admin_chk_list(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для главного админа", show_alert=True)
        return

    if not checkers:
        await call.message.edit_text(
            "📭 Нет админов на входе.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:checkers")
            ]]),
        )
        return

    text = f"📋 <b>Админы на входе</b> ({len(checkers)})\n\n"
    for chk_id, c in checkers.items():
        if c.get("user_id"):
            status_icon = "🟢"
        else:
            status_icon = "🟡"

        name = c.get("name") or "—"
        if c.get("username"):
            value = f"🔗 @{c['username']}"
        elif c.get("phone"):
            value = f"📱 {c['phone']}"
        else:
            value = "—"

        if c.get("event_id") is None:
            access = "🌐 Все вечеринки"
        else:
            ev = next((e for e in events if str(e["id"]) == str(c["event_id"])), None)
            access = f"🎯 {ev['title']}" if ev else f"🎯 {c['event_id']}"

        text += (
            f"{status_icon} <b>{name}</b>\n"
            f"   {value}\n"
            f"   {access}\n\n"
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:chk:list")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:checkers")],
    ])
    await call.message.edit_text(text[:4000], reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "admin:chk:remove")
async def admin_chk_remove_start(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для главного админа", show_alert=True)
        return

    if not checkers:
        await call.answer("Список пуст", show_alert=True)
        return

    buttons = []
    for chk_id, c in checkers.items():
        name = c.get("name") or "—"
        if c.get("username"):
            value = f"@{c['username']}"
        elif c.get("phone"):
            value = c["phone"]
        else:
            value = "—"
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {name} — {value}",
            callback_data=f"admin:chk:del:{chk_id}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:checkers")])

    await call.message.edit_text(
        "🗑 Выберите админа для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.callback_query(F.data.startswith("admin:chk:del:"))
async def admin_chk_remove_confirm(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для главного админа", show_alert=True)
        return

    chk_id = call.data.split(":", 3)[3]
    if chk_id not in checkers:
        await call.answer("Не найден", show_alert=True)
        return

    removed = checkers.pop(chk_id)
    save_data()

    name = removed.get("name") or "—"
    await call.answer(f"Удалён: {name}")
    await admin_checkers_menu(call, None)


# ================== СОЗДАНИЕ СОБЫТИЙ ==================
@dp.callback_query(F.data == "admin:add")
async def admin_add_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(Admin.add_title)
    await call.message.edit_text(
        "➕ <b>Новое событие</b>\n\n"
        "Шаг 1/7. Введите <b>название</b> события:",
        parse_mode="HTML",
    )


@dp.message(Admin.add_title)
async def add_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(Admin.add_date_text)
    await message.answer(
        "Шаг 2/7. Введите <b>дату и время</b> события в любой удобной форме:\n\n"
        "Например:\n"
        "• <code>15.11.2026 22:00</code>\n"
        "• <code>15/11/2026 22:00</code>\n"
        "• <code>15 ноября 2026, 22:00</code>\n"
        "• <code>15 ноября 22:00</code>",
        parse_mode="HTML",
    )


@dp.message(Admin.add_date_text)
async def add_date_text(message: Message, state: FSMContext):
    text = message.text.strip()
    dt, date_text = parse_event_date(text)

    if dt is None:
        await message.answer(
            "❌ Не могу распознать дату.\n\n"
            "Попробуй один из форматов:\n"
            "• <code>15.11.2026 22:00</code>\n"
            "• <code>15/11/2026 22:00</code>\n"
            "• <code>15 ноября 2026, 22:00</code>\n"
            "• <code>15 ноября 22:00</code>",
            parse_mode="HTML",
        )
        return

    if dt < datetime.now():
        await message.answer("❌ Дата уже в прошлом. Введи будущую дату.")
        return

    await state.update_data(start_dt=dt.isoformat(), date_text=date_text)
    await state.set_state(Admin.add_place)
    await message.answer(
        f"✅ Распознано: <b>{date_text}</b>\n\n"
        f"Шаг 3/7. Место проведения:",
        parse_mode="HTML",
    )


@dp.message(Admin.add_place)
async def add_place(message: Message, state: FSMContext):
    await state.update_data(place=message.text.strip())
    await state.set_state(Admin.add_description)
    await message.answer("Шаг 4/7. Описание:")


@dp.message(Admin.add_description)
async def add_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await state.set_state(Admin.add_price_single)
    await message.answer("Шаг 5/7. Цена обычного (число):")


@dp.message(Admin.add_price_single)
async def add_price_single(message: Message, state: FSMContext):
    try:
        price = int(message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Число > 0")
        return
    await state.update_data(price_single=price)
    await state.set_state(Admin.add_price_pair)
    await message.answer("Шаг 6/7. Цена парного (0 если нет):")


@dp.message(Admin.add_price_pair)
async def add_price_pair(message: Message, state: FSMContext):
    try:
        price = int(message.text.strip())
        if price < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Число ≥ 0")
        return
    await state.update_data(price_pair=price)
    await state.set_state(Admin.add_photo)
    await message.answer("Шаг 7/7. Фото-обложка или <code>-</code>:", parse_mode="HTML")


@dp.message(Admin.add_photo, F.photo)
async def add_photo(message: Message, state: FSMContext):
    await state.update_data(photo_id=message.photo[-1].file_id)
    await finish_add_event(message, state)


@dp.message(Admin.add_photo, F.text == "-")
async def add_photo_skip(message: Message, state: FSMContext):
    await state.update_data(photo_id=None)
    await finish_add_event(message, state)


async def finish_add_event(message: Message, state: FSMContext):
    data = await state.get_data()
    event_id = f"evt_{uuid.uuid4().hex[:8]}"
    event = {
        "id": event_id,
        "title": data["title"],
        "date_text": data["date_text"],
        "start_dt": data["start_dt"],
        "place": data["place"],
        "description": data["description"],
        "price_single": data["price_single"],
        "price_pair": data["price_pair"],
        "photo_id": data.get("photo_id"),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "active": True,
    }
    events.append(event)
    save_data()
    await state.clear()
    await message.answer(f"✅ Событие «{data['title']}» добавлено!")


@dp.callback_query(F.data == "admin:list")
async def admin_list(call: CallbackQuery):
    if not events:
        await call.message.edit_text("📭 Нет событий.")
        return
    text = "📋 <b>События:</b>\n\n"
    for e in events:
        status = "🟢" if e.get("active") else "🔴"
        text += f"{status} <b>{e['title']}</b>\n   📅 {e['date_text']}\n   🆔 <code>{e['id']}</code>\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:list")],
        [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:back")],
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "admin:delete")
async def admin_delete(call: CallbackQuery, state: FSMContext):
    if not events:
        await call.message.edit_text("📭 Нет событий.")
        return
    buttons = []
    for e in events:
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {e['title']} — {e['date_text']}",
            callback_data=f"admin:del:{e['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:back")])
    await call.message.edit_text(
        "🗑 Выберите событие для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.callback_query(F.data.startswith("admin:del:"))
async def admin_delete_confirm(call: CallbackQuery, state: FSMContext):
    eid = call.data.split(":")[2]
    events[:] = [e for e in events if e["id"] != eid]
    supabase.table("events").delete().eq("id", eid).execute()
    await call.message.edit_text("✅ Удалено.")
    await show_admin_menu(call.message, state)


@dp.callback_query(F.data == "admin:stats")
async def admin_stats(call: CallbackQuery):
    total_revenue = sum(o["price"] for o in orders)
    paid_orders = len(orders)
    tickets_active = sum(1 for t in tickets.values() if not t.get("used"))
    tickets_used = sum(1 for t in tickets.values() if t.get("used"))

    by_event = {}
    for o in orders:
        title = o.get("event_title", "—")
        by_event.setdefault(title, {"count": 0, "sum": 0})
        by_event[title]["count"] += 1
        by_event[title]["sum"] += o["price"]

    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{len(users)}</b>\n"
        f"🎉 Событий: <b>{len(events)}</b>\n"
        f"🛒 Заказов: <b>{paid_orders}</b>\n"
        f"💰 Выручка: <b>{total_revenue} ₽</b>\n\n"
        f"🟢 Билетов активно: <b>{tickets_active}</b>\n"
        f"🔴 Билетов использовано: <b>{tickets_used}</b>\n"
        f"🚫 В ЧС: <b>{len(blacklist)}</b>\n"
        f"📊 Попыток рег.: <b>{len(reg_attempts)}</b>\n"
        f"🎫 Checkers: <b>{len(checkers)}</b>\n"
    )

    if by_event:
        text += "\n<b>Продажи по событиям:</b>\n"
        for title, info in by_event.items():
            text += f"• {title}: {info['count']} шт, {info['sum']} ₽\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:stats")],
        [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:back")],
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


# ================== ЧЁРНЫЙ СПИСОК (АДМИН) ==================
@dp.callback_query(F.data == "admin:blacklist")
async def admin_blacklist_menu(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для админа", show_alert=True)
        return

    text = get_blacklist_text()[:4000]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить username", callback_data="admin:bl:add_username")],
        [InlineKeyboardButton(text="📱 Добавить телефон", callback_data="admin:bl:add_phone")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="admin:bl:remove")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:blacklist")],
        [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:back")],
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "admin:bl:add_username")
async def admin_bl_add_username(call: CallbackQuery, state: FSMContext):
    await state.update_data(bl_type="username")
    await call.message.edit_text(
        "🚫 <b>Добавление в ЧС</b>\n\n"
        "Шаг 1/2. Отправьте <b>@username</b>:\n\n"
        "Для отмены — /admin",
        parse_mode="HTML",
    )
    await state.set_state(Admin.blacklist_add_username)


@dp.message(Admin.blacklist_add_username)
async def admin_bl_save_username(message: Message, state: FSMContext):
    if message.text == "/admin":
        await state.clear()
        await show_admin_menu(message, state)
        return

    value = message.text.strip()
    if not value.startswith("@"):
        value = "@" + value

    await state.update_data(bl_value=value)
    await state.set_state(Admin.blacklist_reason)
    await message.answer("Шаг 2/2. Причина (или <code>-</code>):", parse_mode="HTML")


@dp.callback_query(F.data == "admin:bl:add_phone")
async def admin_bl_add_phone(call: CallbackQuery, state: FSMContext):
    await state.update_data(bl_type="phone")
    await call.message.edit_text(
        "🚫 <b>Добавление в ЧС</b>\n\n"
        "Шаг 1/2. Отправьте <b>номер телефона</b>:\n\n"
        "Для отмены — /admin",
        parse_mode="HTML",
    )
    await state.set_state(Admin.blacklist_add_phone)


@dp.message(Admin.blacklist_add_phone)
async def admin_bl_save_phone(message: Message, state: FSMContext):
    if message.text == "/admin":
        await state.clear()
        await show_admin_menu(message, state)
        return

    value = message.text.strip()
    await state.update_data(bl_value=value)
    await state.set_state(Admin.blacklist_reason)
    await message.answer("Шаг 2/2. Причина (или <code>-</code>):", parse_mode="HTML")


@dp.message(Admin.blacklist_reason)
async def admin_bl_save_reason(message: Message, state: FSMContext):
    reason = message.text.strip()
    if reason == "-":
        reason = ""

    data = await state.get_data()
    bl_type = data.get("bl_type", "username")
    bl_value = data.get("bl_value", "")

    ok = add_to_blacklist(bl_type, bl_value, reason)
    await state.clear()

    if ok:
        await message.answer(f"✅ Добавлено в ЧС: <code>{bl_value}</code>", parse_mode="HTML")
    else:
        await message.answer("⚠️ Уже в ЧС.")

    await show_admin_menu(message, state)


@dp.callback_query(F.data == "admin:bl:remove")
async def admin_bl_remove_start(call: CallbackQuery):
    if not blacklist:
        await call.answer("ЧС пуст", show_alert=True)
        return

    buttons = []
    for key, item in list(blacklist.items())[:30]:
        icon = "📱" if item.get("type") == "phone" else "👤"
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {icon} {item.get('value')}",
            callback_data=f"admin:bl:del:{item.get('value')}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:blacklist")])

    await call.message.edit_text(
        "🗑 Выберите запись для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.callback_query(F.data.startswith("admin:bl:del:"))
async def admin_bl_remove_confirm(call: CallbackQuery):
    value = call.data.split(":", 3)[3]
    ok = remove_from_blacklist(value)
    if ok:
        await call.answer("Удалено")
    else:
        await call.answer("Не найдено", show_alert=True)
    await admin_blacklist_menu(call)


# ================== ПОПЫТКИ РЕГИСТРАЦИИ (АДМИН) ==================
@dp.callback_query(F.data == "admin:reg_attempts")
async def admin_reg_attempts(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для админа", show_alert=True)
        return

    text = get_reg_attempts_text(limit=30)[:4000]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:reg_attempts")],
        [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:back")],
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


# ================== ПОЛЬЗОВАТЕЛИ (АДМИН) ==================
PAGE_SIZE = 10


@dp.callback_query(F.data.startswith("admin:users:"))
async def admin_users(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для админа", show_alert=True)
        return

    page = int(call.data.split(":")[2])
    all_ids = list(users.keys())
    total = len(all_ids)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    if page < 0:
        page = 0
    if page >= total_pages:
        page = total_pages - 1

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_ids = all_ids[start:end]

    text = f"👥 <b>Пользователи</b> ({total})\n"
    text += f"Страница {page + 1}/{total_pages}\n\n"

    for uid in page_ids:
        u = users[uid]
        text += (
            f"👤 <b>{u.get('name','')} {u.get('surname','')}</b>\n"
            f"🆔 <code>{uid}</code>\n"
            f"💬 @{u.get('username','—') or '—'}\n"
            f"📱 {u.get('phone','—')}\n\n"
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:users:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="admin:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"admin:users:{page+1}"))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        nav,
        [InlineKeyboardButton(text="📥 Скачать CSV", callback_data="admin:users_csv")],
        [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:back")],
    ])
    await call.message.edit_text(text[:4000], reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "admin:noop")
async def admin_noop(call: CallbackQuery):
    await call.answer()


@dp.callback_query(F.data == "admin:users_csv")
async def admin_users_csv(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для админа", show_alert=True)
        return

    rows = [[
        uid, u.get("username", ""), u.get("email", ""),
        u.get("name", ""), u.get("surname", ""), u.get("gender", ""),
        u.get("phone", ""), u.get("registered_at", ""),
    ] for uid, u in users.items()]

    csv_bytes = make_csv(
        ["telegram_id", "username", "email", "name", "surname",
         "gender", "phone", "registered_at"],
        rows
    )
    await call.message.answer_document(
        BufferedInputFile(csv_bytes, filename="users.csv"),
        caption=f"👥 Пользователи ({len(rows)})",
    )
    await call.answer()


# ================== РАССЫЛКА ==================
@dp.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для админа", show_alert=True)
        return

    await call.message.edit_text(
        f"📢 <b>Рассылка</b>\n\n"
        f"Получателей: <b>{len(users)}</b>\n\n"
        f"Отправьте текст сообщения (можно с фото):\n\n"
        f"Для отмены — /admin",
        parse_mode="HTML",
    )
    await state.set_state(Admin.broadcast)


@dp.message(Admin.broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext):
    if message.text == "/admin":
        await state.clear()
        await show_admin_menu(message, state)
        return

    await state.clear()
    await message.answer("📢 Начинаю рассылку...")

    sent = 0
    failed = 0
    for uid in list(users.keys()):
        try:
            if message.photo:
                await bot.send_photo(
                    uid,
                    message.photo[-1].file_id,
                    caption=message.caption or "",
                )
            else:
                await bot.send_message(uid, message.text or "")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await message.answer(
        f"✅ Рассылка завершена\n\n"
        f"📤 Отправлено: <b>{sent}</b>\n"
        f"❌ Ошибок: <b>{failed}</b>",
        parse_mode="HTML",
    )
    await show_admin_menu(message, state)


# ================== НАЙТИ БИЛЕТ (АДМИН) ==================
@dp.callback_query(F.data == "admin:find_ticket")
async def admin_find_ticket_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для админа", show_alert=True)
        return

    await call.message.edit_text(
        "🔍 <b>Поиск билета</b>\n\n"
        "Введите <b>код</b>, <b>имя</b>, <b>телефон</b> или <b>email</b>:",
        parse_mode="HTML",
    )
    await state.set_state(Admin.find_ticket)


@dp.message(Admin.find_ticket)
async def admin_find_ticket_query(message: Message, state: FSMContext):
    query = message.text.strip().lower()
    if len(query) < 2:
        await message.answer("❌ Слишком короткий запрос.")
        return

    found = []
    for code, t in tickets.items():
        holder = (t.get("holder") or "").lower()
        owner = users.get(t.get("user_id"), {})
        owner_name = f"{owner.get('name','')} {owner.get('surname','')}".strip().lower()
        owner_phone = (owner.get("phone") or "").replace(" ", "").replace("-", "").replace("+", "")
        owner_email = (owner.get("email") or "").lower()

        if (query in code.lower()
                or query in holder
                or query in owner_name
                or query in owner_phone
                or query in owner_email):
            found.append((code, t))

    if not found:
        await message.answer(
            "❌ Ничего не найдено.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:back")],
            ]),
        )
        await state.clear()
        return

    if len(found) == 1:
        code, t = found[0]
        await show_super_admin_ticket_card(message, code, t)
        await state.clear()
        return

        buttons = []
    for code, t in found[:30]:
        holder = t.get("holder", "—")
        status = "🔴" if t.get("used") else "🟢"
        buttons.append([InlineKeyboardButton(
            text=f"{status} {code} — {holder[:20]}",
            callback_data=f"admin:ticket:show:{code}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:back")])

    await message.answer(
        f"🔍 Найдено: <b>{len(found)}</b>\n\nВыберите:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("admin:ticket:show:"))
async def admin_ticket_show(call: CallbackQuery):
    code = call.data.split(":", 3)[3]
    ticket = tickets.get(code)
    if not ticket:
        await call.answer("Не найден", show_alert=True)
        return
    await call.answer()
    await show_super_admin_ticket_card(call.message, code, ticket)


@dp.callback_query(F.data.startswith("admin:use_ticket:"))
async def admin_use_ticket(call: CallbackQuery):
    """Погашение билета главным админом (по коду, не по QR)."""
    code = call.data.split(":", 2)[2]
    ticket = tickets.get(code)
    if not ticket:
        await call.answer("Билет не найден", show_alert=True)
        return
    if ticket.get("used"):
        await call.answer("Уже погашен", show_alert=True)
        return

    ticket["used"] = True
    ticket["used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    ticket["used_by"] = "Главный админ"
    save_data()

    # Уведомление гостю
    owner_id = ticket.get("user_id")
    try:
        if owner_id:
            await bot.send_message(
                int(owner_id),
                f"✅ <b>Ваш билет погашен</b>\n\n"
                f"Код: <code>{code}</code>\n"
                f"Администратор: главный админ\n"
                f"Время: {ticket['used_at']}",
                parse_mode="HTML",
            )
    except Exception:
        pass

    await call.answer("✅ Погашен")
    await show_super_admin_ticket_card(call.message, code, ticket)


# ================== ВСЕ БИЛЕТЫ (АДМИН) ==================
TICKETS_PAGE_SIZE = 15


@dp.callback_query(F.data.startswith("admin:tickets"))
async def admin_tickets(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для админа", show_alert=True)
        return

    parts = call.data.split(":")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    all_items = list(tickets.items())
    all_items.sort(key=lambda kv: kv[1].get("issued_at", ""), reverse=True)

    total = len(all_items)
    total_pages = max(1, (total + TICKETS_PAGE_SIZE - 1) // TICKETS_PAGE_SIZE)

    if page < 0:
        page = 0
    if page >= total_pages:
        page = total_pages - 1

    start = page * TICKETS_PAGE_SIZE
    end = start + TICKETS_PAGE_SIZE
    page_items = all_items[start:end]

    active = sum(1 for _, t in tickets.items() if not t.get("used"))
    used = total - active

    text = (
        f"🎫 <b>Все билеты</b>\n\n"
        f"Всего: <b>{total}</b>\n"
        f"🟢 Активных: <b>{active}</b>\n"
        f"✅ Погашено: <b>{used}</b>\n\n"
        f"Страница {page + 1}/{total_pages}\n\n"
    )

   for code, t in page_items:
        status = "🔴" if t.get("used") else "🟢"
        holder = t.get("holder", "—")
        title = t.get("event_title", "—")
        date_text = t.get("event_date", "—")
        text += (
            f"{status} <code>{code}</code>\n"
            f"   👤 {holder}\n"
            f"   🎉 {title} — {date_text}\n\n"
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin:tickets:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="admin:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin:tickets:{page+1}"))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        nav,
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admin:tickets:{page}")],
        [InlineKeyboardButton(text="📥 Скачать CSV", callback_data="admin:tickets_csv")],
        [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:back")],
    ])
    await call.message.edit_text(text[:4000], reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "admin:tickets_csv")
async def admin_tickets_csv(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для админа", show_alert=True)
        return

    rows = []
    for code, t in tickets.items():
        owner = users.get(t.get("user_id"), {})
        rows.append([
            code,
            t.get("user_id", ""),
            t.get("holder", ""),
            owner.get("phone", ""),
            owner.get("username", ""),
            t.get("type", ""),
            "использован" if t.get("used") else "активен",
            t.get("event_title", ""),
            t.get("event_date", ""),
            t.get("issued_at", ""),
            t.get("used_at", ""),
            t.get("used_by", ""),
        ])

    csv_bytes = make_csv(
        ["ticket_code", "holder_id", "holder_name", "phone", "username",
         "type", "status", "event_title", "event_date", "issued_at",
         "used_at", "used_by"],
        rows
    )
    await call.message.answer_document(
        BufferedInputFile(csv_bytes, filename="tickets.csv"),
        caption=f"🎫 Все билеты ({len(rows)})",
    )
    await call.answer()


# ================== МЕРОПРИЯТИЯ (КЛИЕНТ) ==================
@dp.callback_query(F.data == "show_events")
async def show_events(call: CallbackQuery):
    now = datetime.now()
    upcoming = [
        e for e in events
        if e.get("active", True) and datetime.fromisoformat(e["start_dt"]) > now
    ]

    if not upcoming:
        await call.message.answer("📭 Пока нет доступных событий.")
        return

    buttons = []
    for e in upcoming:
        buttons.append([InlineKeyboardButton(
            text=f"🎉 {e['title']} — {e['date_text']}",
            callback_data=f"event:{e['id']}"
        )])

    buttons.append([InlineKeyboardButton(
        text="🔄 Обновить", callback_data="show_events"
    )])
    buttons.append([InlineKeyboardButton(
        text="🏠 На главную", callback_data="main_menu"
    )])

    await call.message.answer(
        "🎉 <b>Выберите мероприятие:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


# ================== МОИ БИЛЕТЫ (КЛИЕНТ) ==================
@dp.callback_query(F.data == "my_tickets")
async def my_tickets(call: CallbackQuery):
    """Показывает все билеты текущего пользователя."""
    user_id = call.from_user.id

    user_tickets = [
        (code, t) for code, t in tickets.items()
        if str(t.get("user_id")) == str(user_id)
    ]

    if not user_tickets:
        await call.message.answer(
            "🎫 <b>Мои билеты</b>\n\n"
            "📭 У вас пока нет билетов.\n\n"
            "Нажмите «🎉 Мероприятия» чтобы купить.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎉 Мероприятия", callback_data="show_events")],
                [InlineKeyboardButton(text="🏠 На главную", callback_data="main_menu")],
            ]),
        )
        return

    user_tickets.sort(
        key=lambda kv: (kv[1].get("used", False), kv[1].get("event_date", "")),
    )

    active_cnt = sum(1 for _, t in user_tickets if not t.get("used"))
    used_cnt = len(user_tickets) - active_cnt

    text = (
        f"🎫 <b>Мои билеты</b>\n\n"
        f"Всего: <b>{len(user_tickets)}</b>\n"
        f"🟢 Активных: <b>{active_cnt}</b>\n"
        f"✅ Использовано: <b>{used_cnt}</b>\n\n"
    )

    buttons = []
    for code, t in user_tickets:
        status = "🔴" if t.get("used") else "🟢"
        type_label = "Парный" if t.get("type") == "pair" else "Обычный"
        event_title = t.get("event_title", "—")
        event_date = t.get("event_date", "—")

        text += (
            f"{status} <code>{code}</code>\n"
            f"   🎉 {event_title}\n"
            f"   📅 {event_date}\n"
            f"   🎟 {type_label}\n\n"
        )

        if not t.get("used"):
            suffix = get_button_suffix(t)
            buttons.append([InlineKeyboardButton(
                text=f"📱 Показать QR — {code[-8:]}{suffix}",
                callback_data=f"my_ticket:{code}"
            )])
            buttons.append([InlineKeyboardButton(
                text=f"📤 Отправить на вход — {code[-8:]}{suffix}",
                callback_data=f"my_ticket:send:{code}"
            )])

    buttons.append([InlineKeyboardButton(
        text="🔄 Обновить", callback_data="my_tickets"
    )])
    buttons.append([InlineKeyboardButton(
        text="🎉 Мероприятия", callback_data="show_events"
    )])
    buttons.append([InlineKeyboardButton(
        text="🏠 На главную", callback_data="main_menu"
    )])

    await call.message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


# ================== ОТПРАВКА БИЛЕТА НА ВХОД (ПЕРВЫМ — правильный порядок!) ==================
@dp.callback_query(F.data.startswith("my_ticket:send:"))
async def my_ticket_send_start(call: CallbackQuery):
    """Начало отправки билета checker'у."""
    code = call.data.split(":", 2)[2]
    user_id = call.from_user.id

    ticket = tickets.get(code)
    if not ticket:
        await call.answer("Билет не найден", show_alert=True)
        return

    if str(ticket.get("user_id")) != str(user_id):
        await call.answer("Это не ваш билет", show_alert=True)
        return

    if ticket.get("used"):
        await call.answer("Этот билет уже использован", show_alert=True)
        return

    event_id = ticket.get("event_id")

    available = []
    for chk_id, c in checkers.items():
        if not c.get("active", True):
            continue
        if not c.get("user_id"):
            continue
        if not checker_can_access_event(c, event_id):
            continue
        available.append((chk_id, c))

    if not available:
        await call.answer()
        await call.message.answer(
            "⚠️ <b>Нет доступных проверяющих</b>\n\n"
            "Обратитесь к организатору.",
            parse_mode="HTML",
        )
        return

    if len(available) == 1:
        chk_id, c = available[0]
        await _do_send_ticket_to_checker(call, code, chk_id, c)
        return

    buttons = []
    for chk_id, c in available:
        name = c.get("name") or "проверяющий"
        buttons.append([InlineKeyboardButton(
            text=f"👤 {name}",
            callback_data=f"my_ticket:sent:{code}:{chk_id}"
        )])
    buttons.append([InlineKeyboardButton(
        text="⬅️ Назад", callback_data="my_tickets"
    )])

    await call.message.answer(
        f"📤 <b>Отправка билета на вход</b>\n\n"
        f"Код: <code>{code}</code>\n\n"
        f"Выберите, кому отправить:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )
    await call.answer()


# ================== ОТПРАВКА КОНКРЕТНОМУ CHECKER'У (ВТОРЫМ) ==================
@dp.callback_query(F.data.startswith("my_ticket:sent:"))
async def my_ticket_send_to_checker(call: CallbackQuery):
    """Отправка билета конкретному checker'у."""
    parts = call.data.split(":")
    try:
        code = parts[2]
        chk_id = parts[3]
    except IndexError:
        await call.answer("Ошибка данных", show_alert=True)
        return

    user_id = call.from_user.id

    ticket = tickets.get(code)
    if not ticket:
        await call.answer("Билет не найден", show_alert=True)
        return

    if str(ticket.get("user_id")) != str(user_id):
        await call.answer("Это не ваш билет", show_alert=True)
        return

    if ticket.get("used"):
        await call.answer("Билет уже использован", show_alert=True)
        return

    checker = checkers.get(chk_id)
    if not checker:
        await call.answer("Проверяющий недоступен", show_alert=True)
        return

    if not checker_can_access_event(checker, ticket.get("event_id")):
        await call.answer("Этот проверяющий не работает с вашим событием", show_alert=True)
        return

    await _do_send_ticket_to_checker(call, code, chk_id, checker)


# ================== ОБЩАЯ ФУНКЦИЯ ОТПРАВКИ ==================
async def _do_send_ticket_to_checker(call: CallbackQuery, code: str, chk_id: str, checker: dict):
    """Общая функция: отправляет карточку билета checker'у."""
    ticket = tickets.get(code)
    if not ticket:
        await call.answer("Билет не найден", show_alert=True)
        return

    owner_id = ticket.get("user_id")
    holder_name = ticket.get("holder", "—")

    holder_user = {}
    try:
        if owner_id:
            holder_user = users.get(int(owner_id), {})
    except (TypeError, ValueError):
        pass

    holder_username = holder_user.get("username") or ""
    holder_phone = holder_user.get("phone") or "—"

    text = (
        f"🎫 <b>Билет на проверку</b>\n\n"
        f"От: <b>{holder_name}</b>"
    )
    if holder_username:
        text += f" (@{holder_username})"
    text += (
        f"\nКод: <code>{code}</code>\n\n"
        f"👤 {holder_name}\n"
    )
    if holder_username:
        text += f"💬 @{holder_username}\n"
    text += f"📱 {holder_phone}\n\n"
    text += (
        f"🎟 {_ticket_type_label(ticket)}\n"
        f"🎉 {ticket.get('event_title', '—')}\n"
        f"📅 {ticket.get('event_date', '—')}\n"
        f"📌 Выдан: {ticket.get('issued_at', '—')}\n"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Погасить билет",
            callback_data=f"checker:use:{code}"
        )],
        [InlineKeyboardButton(
            text="❌ Отклонить",
            callback_data=f"checker:reject:{code}"
        )],
    ])

    checker_user_id = checker.get("user_id")

    try:
        await bot.send_message(
            checker_user_id,
            text,
            reply_markup=kb,
            parse_mode="HTML",
        )

        checker_name = checker.get("name") or "проверяющий"
        await call.message.answer(
            f"✅ <b>Билет отправлен на вход!</b>\n\n"
            f"Код: <code>{code}</code>\n"
            f"Проверяющий: <b>{checker_name}</b>\n\n"
            f"Покажите свой экран, если проверяющий попросит.",
            parse_mode="HTML",
        )
        await call.answer("Отправлено ✅")

    except Exception as e:
        print(f"[SEND] Ошибка отправки checker'у {chk_id}: {e}")
        await call.answer(
            "Не удалось отправить. Проверяющий не начинал чат с ботом?",
            show_alert=True,
        )


# ================== ПОКАЗ QR (ТРЕТЬИМ — общий) ==================
@dp.callback_query(F.data.startswith("my_ticket:"))
async def show_my_ticket_qr(call: CallbackQuery):
    """Показывает QR-код конкретного билета."""
    code = call.data.split(":", 1)[1]
    user_id = call.from_user.id

    ticket = tickets.get(code)
    if not ticket:
        await call.answer("Билет не найден", show_alert=True)
        return

    if str(ticket.get("user_id")) != str(user_id):
        await call.answer("Это не ваш билет", show_alert=True)
        return

    if ticket.get("used"):
        await call.answer("Этот билет уже использован", show_alert=True)
        return

    event = next(
        (e for e in events if str(e["id"]) == str(ticket.get("event_id"))),
        None
    )

    qr_file = make_qr_image(code)
    type_label = "Парный" if ticket.get("type") == "pair" else "Обычный"

    caption = (
        f"🎫 <b>Ваш билет</b>\n\n"
        f"👤 {ticket.get('holder', '—')}\n"
        f"🎟 {type_label}\n"
        f"🎉 {ticket.get('event_title', '—')}\n"
        f"📅 {ticket.get('event_date', '—')}\n"
    )
    if event and event.get("place"):
        caption += f"📍 {event.get('place')}\n"
    caption += (
        f"\nКод: <code>{code}</code>\n\n"
        "Покажите QR на входе."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🏠 На главную", callback_data="main_menu")
    ]])

    await call.message.answer_photo(
        qr_file,
        caption=caption,
        reply_markup=kb,
        parse_mode="HTML",
    )
    await call.answer()


# ================== ВЫБОР СОБЫТИЯ КЛИЕНТОМ ==================
@dp.callback_query(F.data.startswith("event:"))
async def event_selected(call: CallbackQuery, state: FSMContext):
    eid = call.data.split(":")[1]
    event = next((e for e in events if e["id"] == eid), None)
    if not event:
        await call.answer("Событие не найдено")
        return

    is_admin = (call.from_user.id == ADMIN_ID)

    if not is_admin and call.from_user.id not in users:
        await call.answer("Сначала пройдите регистрацию 👤", show_alert=True)
        return

    if sales_closed(event):
        await call.answer("🛑 Продажа билетов закрыта", show_alert=True)
        return

    await state.update_data(event_id=eid, event=event)

    if is_admin:
        buttons = [[InlineKeyboardButton(
            text="⬅️ Закрыть",
            callback_data="admin:back"
        )]]
    else:
        buttons = [[InlineKeyboardButton(
            text=f"🎟 Обычный — {event['price_single']} ₽",
            callback_data="ticket:single"
        )]]
        if event.get("price_pair", 0) > 0:
            buttons.append([InlineKeyboardButton(
                text=f"🎟 Парный — {event['price_pair']} ₽",
                callback_data="ticket:pair"
            )])
        buttons.append([InlineKeyboardButton(
            text="🏠 На главную", callback_data="main_menu"
        )])

    caption = (
        f"🎉 <b>{event['title']}</b>\n\n"
        f"📅 {event['date_text']}\n"
        f"📍 {event['place']}\n\n"
        f"{event['description']}\n\n"
    )
    if is_admin:
        caption += "👁 <i>Режим просмотра (админ)</i>"
    else:
        caption += "Выберите тип билета:"

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if event.get("photo_id"):
        await call.message.answer_photo(
            event["photo_id"], caption=caption,
            reply_markup=kb, parse_mode="HTML"
        )
    else:
        await call.message.answer(caption, reply_markup=kb, parse_mode="HTML")

    await state.set_state(Reg.ticket_type)


# ================== ОПЛАТА ЧЕРЕЗ ЮKASSA ==================
@dp.callback_query(F.data == "ticket:single", Reg.ticket_type)
async def ticket_single(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    event = data.get("event")
    if not event:
        await call.answer("Ошибка")
        return
    await state.update_data(ticket_type="single", price=event["price_single"])
    await go_to_payment(call.message, state)


@dp.callback_query(F.data == "ticket:pair", Reg.ticket_type)
async def ticket_pair(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    event = data.get("event")
    if not event or event.get("price_pair", 0) <= 0:
        await call.answer("Парные недоступны")
        return
    await state.update_data(ticket_type="pair", price=event["price_pair"])
    await call.message.edit_text(
        "🎟 Введите <b>@username</b> второго участника:",
        parse_mode="HTML",
    )
    await state.set_state(Reg.partner_input)


@dp.message(Reg.partner_input)
async def partner_input(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.startswith("@") or len(text) < 3:
        await message.answer("❌ Формат: <b>@username</b>", parse_mode="HTML")
        return

    partner_id = username_index.get(text.lower())
    if partner_id == message.from_user.id:
        await message.answer("❌ Нельзя себя.")
        return

    if partner_id and partner_id in users:
        await state.update_data(partner_id=partner_id)
        partner = users[partner_id]
        await message.answer(
            f"✅ Найден: {partner['name']} {partner['surname']}",
        )
        await go_to_payment(message, state)
    else:
        invite_link = f"https://t.me/{BOT_USERNAME}?start=invite_{message.from_user.id}"
        await message.answer(
            f"❌ Не найден. Отправьте ссылку:\n<code>{invite_link}</code>\n\n"
            "После регистрации напишите /start заново.",
            parse_mode="HTML",
        )


async def go_to_payment(message: Message, state: FSMContext):
    """Создаёт платёж в ЮKassa."""
    data = await state.get_data()
    price = data.get("price", 0)
    ticket_type = data.get("ticket_type", "single")
    event = data.get("event", {})
    user_id = message.chat.id

    ticket_label = "Парный" if ticket_type == "pair" else "Обычный"
    description = f"{event.get('title', 'Билет')} — {ticket_label} билет"

    payment = create_yookassa_payment(
        amount=price,
        description=description,
        user_id=user_id,
        metadata={
            "event_id": data.get("event_id", ""),
            "ticket_type": ticket_type,
            "price": str(price),
        },
    )

    if not payment:
        await message.answer(
            "⚠️ Ошибка при создании платежа. Попробуйте позже или напишите в поддержку."
        )
        return

    payment_id = payment["payment_id"]
    confirmation_url = payment["confirmation_url"]

    pending_payments[payment_id] = {
        "user_id": user_id,
        "event_id": data.get("event_id", ""),
        "event_title": event.get("title", ""),
        "ticket_type": ticket_type,
        "price": price,
        "partner_id": data.get("partner_id"),
        "name": f"{data.get('name', '')} {data.get('surname', '')}".strip(),
        "phone": data.get("phone", ""),
        "email": data.get("email", ""),
    }

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {price} ₽", url=confirmation_url)],
        [InlineKeyboardButton(text="🆘 Техподдержка", url=f"https://t.me/{settings.get('support', '@your_support').lstrip('@')}")],
        [InlineKeyboardButton(text="🏠 На главную", callback_data="main_menu")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_order")],
    ])

    await message.answer(
        f"💳 <b>Билет: {ticket_label} — {price} ₽</b>\n\n"
        f"🎉 {event.get('title', '')}\n"
        f"📅 {event.get('date_text', '')}\n\n"
        "1. Нажмите «Оплатить» — откроется страница ЮKassa.\n"
        "2. Оплатите картой, СБП или другим способом.\n"
        "3. После оплаты билет придёт автоматически в течение 1 минуты.\n\n"
        "⏱ Страница оплаты активна 60 минут.",
        reply_markup=kb, parse_mode="HTML",
    )
    await state.set_state(Reg.payment)


@dp.callback_query(F.data == "cancel_order")
async def cancel_order(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Отменено. /start — заново.")


# ================== ВЫДАЧА БИЛЕТА ==================
async def issue_ticket(user_id: int, code: str, ticket_type: str, holder_name: str, event: dict):
    tickets[code] = {
        "user_id": user_id,
        "type": ticket_type,
        "holder": holder_name,
        "used": False,
        "event_id": event.get("id", ""),
        "event_title": event.get("title", ""),
        "event_date": event.get("date_text", ""),
        "issued_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    save_data()

    qr_file = make_qr_image(code)
    type_label = "Парный" if ticket_type == "pair" else "Обычный"

    holder_user = users.get(user_id, {})
    holder_username = holder_user.get("username") or ""

    caption = (
        f"🎫 <b>Ваш билет на {event.get('title', 'Vida Bachata')}</b>\n\n"
        f"👤 {holder_name}\n"
    )
    if holder_username:
        caption += f"💬 @{holder_username}\n"
    caption += (
        f"🎟 {type_label}\n"
        f"📅 {event.get('date_text', '')}\n"
        f"📍 {event.get('place', '')}\n\n"
        f"Код: <code>{code}</code>\n\n"
        "Покажите QR на входе."
    )

    try:
        await bot.send_photo(user_id, qr_file, caption=caption, parse_mode="HTML")
    except Exception as e:
        print(f"[TICKET] Ошибка отправки билета user={user_id}: {e}")


async def process_successful_payment(payment_id: str):
    """Обрабатывает успешную оплату с защитой от двойной обработки."""
    if payment_id in processed_payments:
        print(f"[YOOKASSA] ⚠️ Платёж {payment_id} уже был обработан — пропускаем")
        return

    pending = pending_payments.pop(payment_id, None)
    if not pending:
        print(f"[YOOKASSA] payment_id={payment_id} не найден в pending")
        processed_payments.add(payment_id)
        return

    processed_payments.add(payment_id)
    print(f"[YOOKASSA] ✅ Платёж {payment_id} принят в обработку")

    user_id = pending["user_id"]
    event = next((e for e in events if e["id"] == pending["event_id"]), None)
    if not event:
        print(f"[YOOKASSA] Событие {pending['event_id']} не найдено")
        return

    ticket_type = pending["ticket_type"]
    buyer_name = pending["name"] or "Клиент"

    order = {
        "order_id": next_order_id(),
        "user_id": user_id,
        "event_id": pending["event_id"],
        "event_title": event.get("title", ""),
        "email": pending.get("email", ""),
        "name": buyer_name,
        "ticket_type": ticket_type,
        "price": pending["price"],
        "paid_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    orders.append(order)
    save_data()

    buyer_code = generate_ticket_code()
    await issue_ticket(user_id, buyer_code, ticket_type, buyer_name, event)

    if ticket_type == "pair" and pending.get("partner_id"):
        partner_id = pending["partner_id"]
        if partner_id in users:
            partner = users[partner_id]
            partner_name = f"{partner['name']} {partner['surname']}"
            await issue_ticket(
                partner_id, generate_ticket_code(),
                ticket_type, partner_name, event
            )

    buyer_user = users.get(user_id, {})
    buyer_full_name = " ".join(filter(None, [
        (buyer_user.get("name") or "").strip(),
        (buyer_user.get("surname") or "").strip(),
    ])).strip()
    if not buyer_full_name:
        buyer_full_name = buyer_name

    buyer_username = buyer_user.get("username") or ""
    buyer_phone = buyer_user.get("phone") or pending.get("phone", "") or "—"
    ticket_type_label = "Парный" if ticket_type == "pair" else "Обычный"

    # Уведомление админу
    admin_lines = [
        f"💰 <b>Оплата получена!</b>\n\n",
        f"🆔 Платёж: <code>{payment_id}</code>\n",
        f"🎉 {event.get('title', '')}\n",
        f"📅 {event.get('date_text', '')}\n",
        "\n",
        f"👤 <b>{buyer_full_name}</b>\n",
        f"🆔 <code>{user_id}</code>\n",
    ]
    if buyer_username:
        admin_lines.append(f"💬 @{buyer_username}\n")
    admin_lines.append(f"📱 {buyer_phone}\n")
    admin_lines.append("\n")
    admin_lines.append(f"🎟 {ticket_type_label} | {pending['price']} ₽\n")

    try:
        await bot.send_message(ADMIN_ID, "".join(admin_lines), parse_mode="HTML")
    except Exception:
        pass

    # Уведомление клиенту
    client_lines = [
        "✅ <b>Оплата прошла!</b>\n\n",
        f"👤 {buyer_full_name}\n",
        f"🎉 {event.get('title', '')}\n",
        f"📅 {event.get('date_text', '')}\n",
    ]
    if event.get("place"):
        client_lines.append(f"📍 {event.get('place')}\n")
    client_lines.append(f"🎟 {ticket_type_label} | {pending['price']} ₽\n\n")
    client_lines.append("🎫 Билет отправлен выше ⬆️\n")
    client_lines.append("Покажите QR-код на входе.")

    try:
        await bot.send_message(user_id, "".join(client_lines), parse_mode="HTML")
    except Exception:
        pass

    if len(processed_payments) > 5000:
        to_remove = list(processed_payments)[:1000]
        for pid in to_remove:
            processed_payments.discard(pid)


# ================== ГЛАВНОЕ МЕНЮ ==================
async def show_main_menu(call: CallbackQuery, state: FSMContext):
    """Возвращает пользователя в главное меню (редактирует текущее сообщение)."""
    await state.clear()
    user_id = call.from_user.id

        if user_id == ADMIN_ID:
        text = (
            f"👋 Привет!\n"
            f"Вы вошли как <b>главный администратор</b>.\n\n"
            f"Выберите действие:"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin:back")],
            [InlineKeyboardButton(text="🎉 Мероприятия", callback_data="show_events")],
            [InlineKeyboardButton(text="🎫 Мои билеты", callback_data="my_tickets")],
            [InlineKeyboardButton(text="ВК", url="https://vk.com/vidabachata"),
             InlineKeyboardButton(text="Telegram", url="https://t.me/+eTMoEG6V1883Nzli")],
        ])
    else:
        checker = find_checker_for_user(
            user_id=user_id,
            username=call.from_user.username or "",
        )
        if checker:
            return await checker_start(call.message, state, checker)
        else:
            name = ""
            if user_id in users:
                name = users[user_id].get("name", "")
            greeting = f"👋 Привет, <b>{name}</b>!\n\n" if name else "👋 Привет!\n\n"
            text = (
                f"{greeting}"
                f"Здесь можно купить билет на вечеринки <b>Vida Bachata</b> 💃🕺\n\n"
                f"Выберите действие:"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎉 Мероприятия", callback_data="show_events")],
                [InlineKeyboardButton(text="🎫 Мои билеты", callback_data="my_tickets")],
                [InlineKeyboardButton(text="ВК", url="https://vk.com/vidabachata"),
                 InlineKeyboardButton(text="Telegram", url="https://t.me/+eTMoEG6V1883Nzli")],
            ])

    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data == "main_menu")
async def main_menu_callback(call: CallbackQuery, state: FSMContext):
    await show_main_menu(call, state)


# ================== КОСТЫЛЬ ДЛЯ RENDER ==================
_main_loop = None


def start_keepalive_server():
    """HTTP-сервер на порту + webhook ЮKassa."""
    PORT = int(os.getenv("PORT", "10000"))

    class MainHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        def do_POST(self):
            if self.path == "/yookassa/webhook":
                try:
                    content_length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(content_length).decode("utf-8")
                    data = json.loads(body)

                    event = data.get("event", "")
                    obj = data.get("object", {})
                    payment_id = obj.get("id", "")
                    status = obj.get("status", "")

                    client_ip = self.client_address[0]
                    forwarded = self.headers.get("X-Forwarded-For", "")
                    print(
                        f"[YOOKASSA WEBHOOK] event={event}, payment_id={payment_id}, "
                        f"status={status}, ip={client_ip}, xff={forwarded}"
                    )

                    if event == "payment.succeeded" and status == "succeeded":
                        if _main_loop:
                            asyncio.run_coroutine_threadsafe(
                                verify_and_process(payment_id),
                                _main_loop
                            )
                        else:
                            print(
                                "[YOOKASSA WEBHOOK] ⚠️ _main_loop не установлен — "
                                "платёж проигнорирован"
                            )

                    self.send_response(200)
                    self.send_header("Content-type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"OK")

                except Exception as e:
                    print(f"[YOOKASSA WEBHOOK] Ошибка: {e}")
                    self.send_response(500)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    class ReusableTCPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    with ReusableTCPServer(("", PORT), MainHandler) as httpd:
        print(f"[KEEPALIVE] HTTP-сервер запущен на порту {PORT}")
        httpd.serve_forever()


async def verify_and_process(payment_id: str):
    """Проверяет платёж через API ЮKassa и обрабатывает если реальный."""
    if payment_id in processed_payments:
        print(f"[YOOKASSA] ⚠️ Платёж {payment_id} уже обработан (до верификации)")
        return

    is_real = await asyncio.to_thread(verify_payment_with_yookassa, payment_id)

    if not is_real:
        print(f"[YOOKASSA] ❌ Поддельный или неизвестный платёж {payment_id} — игнорируем")
        return

    print(f"[YOOKASSA] ✅ Платёж {payment_id} подтверждён через API — обрабатываем")
    await process_successful_payment(payment_id)


async def _self_ping_loop():
    """Пингует свой URL каждые 10 минут."""
    render_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if not render_url:
        print("[KEEPALIVE] RENDER_EXTERNAL_URL не задан — самопинг отключён")
        return

    print(f"[KEEPALIVE] Самопинг активирован для {render_url}")
    while True:
        await asyncio.sleep(600)
        try:
            await asyncio.to_thread(urllib.request.urlopen, render_url, timeout=10)
            print("[KEEPALIVE] Самопинг успешен")
        except Exception as e:
            print(f"[KEEPALIVE] Самопинг ошибка: {e}")


def start_keepalive():
    """Запускает HTTP-сервер в фоновом потоке."""
    server_thread = threading.Thread(target=start_keepalive_server, daemon=True)
    server_thread.start()


def start_self_ping():
    """Запускает самопинг в фоне (async)."""
    asyncio.create_task(_self_ping_loop())


# ================== ЗАПУСК ==================
async def main():
    global _main_loop
    _main_loop = asyncio.get_running_loop()

    load_data()
    print("🤖 Бот запускается...")

    start_keepalive()
    start_self_ping()

    me = await bot.get_me()
    print(f"✅ Бот @{me.username} работает")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
