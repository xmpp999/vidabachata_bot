"""
Telegram-бот для продажи билетов на вечеринки Vida Bachata.
Хранилище: Supabase (PostgreSQL).
"""

import asyncio
import csv
import io
import os
import random
import re
import uuid
from datetime import datetime, timedelta
from email.message import EmailMessage

import aiosmtplib
import dateparser
import qrcode
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
from supabase import create_client, Client

# ================== КОНФИГ ==================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "your_bot")

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

# Resend API (для отправки email)
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")

# Старые SMTP-переменные — для отчёта на email
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.mail.ru")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")

# ================== БОТ ==================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ================== SUPABASE ==================
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ================== ХРАНИЛИЩЕ (в памяти, синхронизируется с Supabase) ==================
users = {}
codes = {}
username_index = {}
pending_invites = {}
tickets = {}
orders = []
events = []

settings = {
    "support": os.getenv("SUPPORT", "@your_support"),
    "yoomoney_link": os.getenv("YOOMONEY_LINK", "https://yoomoney.ru/to/0000000000000"),
    "sales_stop_hours": 1,
}


def save_data():
    """Синхронизирует всё из памяти в Supabase."""
    try:
        # ---- USERS ----
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

        # ---- EVENTS ----
        if events:
            supabase.table("events").upsert(events).execute()

        # ---- TICKETS ----
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
                    "issued_at": t.get("issued_at", ""),
                })
            supabase.table("tickets").upsert(tickets_rows).execute()

        # ---- ORDERS ----
        if orders:
            supabase.table("orders").upsert(orders).execute()

    except Exception as e:
        print(f"[SUPABASE] Ошибка сохранения: {e}")


def load_data():
    """Загружает всё из Supabase в память."""
    global events, orders, tickets, users, username_index
    try:
        # ---- USERS ----
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

        # ---- EVENTS ----
        resp = supabase.table("events").select("*").execute()
        events = resp.data or []

        # ---- TICKETS ----
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
                "issued_at": row.get("issued_at", ""),
            }

        # ---- ORDERS ----
        resp = supabase.table("orders").select("*").execute()
        orders = resp.data or []

        print(
            f"✅ Загружено из Supabase: {len(events)} событий, "
            f"{len(orders)} заказов, {len(users)} юзеров, {len(tickets)} билетов"
        )
    except Exception as e:
        print(f"[SUPABASE] Ошибка загрузки: {e}")


# ================== УТИЛИТЫ ==================
def is_valid_email(email: str) -> bool:
    return re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) is not None


def parse_event_date(text: str):
    """Парсит дату в свободной форме. Возвращает (datetime, date_text) или (None, None)."""
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
        t.get("issued_at", ""),
    ] for code, t in tickets.items()]
    tickets_csv = make_csv(
        ["ticket_code", "holder_telegram_id", "holder_name",
         "type", "status", "issued_at"], tickets_rows
    )

    total_revenue = sum(o["price"] for o in orders)

    body = (
        f"📊 ИТОГИ:\n"
        f"- Пользователей: {len(users)}\n"
        f"- Заказов: {len(orders)}\n"
        f"- Выручка: {total_revenue} ₽\n"
        f"- Выдано билетов: {len(tickets)}\n\n"
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
    delete_select = State()
    find_ticket = State()
    broadcast = State()
    settings_edit = State()
    search_user = State()


# ================== /start ==================
@dp.message(CommandStart(deep_link=True))
async def start_with_invite(message: Message, state: FSMContext, command: CommandObject):
    await state.clear()

    # Админ не регистрируется
    if message.from_user.id == ADMIN_ID:
        return await start(message, state)

    # Уже зарегистрирован — не надо заново
    if message.from_user.id in users:
        return await start(message, state)

    payload = command.args or ""
    if payload.startswith("invite_"):
        try:
            inviter_id = int(payload.replace("invite_", ""))
        except ValueError:
            inviter_id = None
        if inviter_id and inviter_id != message.from_user.id:
            pending_invites[message.from_user.id] = inviter_id
            await message.answer(
                "👋 Привет! Вас пригласили купить парный билет 💃\n\n"
                "Пройдите быструю регистрацию — и мы свяжем вас "
                "с пригласившим.",
            )

    # Сразу начинаем с телефона
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться контактом", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await message.answer(
        "📱 Нажмите «Поделиться контактом», чтобы подтвердить ваш номер телефона:",
        reply_markup=kb,
    )
    await state.set_state(Reg.phone)


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    user_name = message.from_user.first_name or "друг"

    # ============ АДМИН ============
    if user_id == ADMIN_ID:
        greeting = (
            f"Привет, {user_name} 👋\n"
            "Вы вошли как <b>администратор</b>.\n\n"
            "Используйте панель ниже для управления ботом."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin:back")],
            [InlineKeyboardButton(text="🎉 Мероприятия", callback_data="show_events")],
        ])
        await message.answer(greeting, reply_markup=kb, parse_mode="HTML")
        return

    # ============ ЗАРЕГИСТРИРОВАННЫЙ ПОЛЬЗОВАТЕЛЬ ============
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
            [InlineKeyboardButton(text="ВК", url="https://vk.com/vidabachata"),
             InlineKeyboardButton(text="Telegram", url="https://t.me/+eTMoEG6V1883Nzli")],
        ])
        await message.answer(greeting, reply_markup=kb, parse_mode="HTML")
        return

    # ============ НОВЫЙ ПОЛЬЗОВАТЕЛЬ ============
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
    # Админ не регистрируется
    if call.from_user.id == ADMIN_ID:
        await call.answer("Вы администратор, регистрация не нужна 👍", show_alert=True)
        return

    # Уже зарегистрирован
    if call.from_user.id in users:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎉 Мероприятия", callback_data="show_events")],
        ])
        await call.message.answer(
            "✅ Вы уже зарегистрированы!\n\nНажмите «Мероприятия».",
            reply_markup=kb,
        )
        return


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
    await state.update_data(phone=message.contact.phone_number)
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

    # Пропустить email
    if email_input == "-":
        await state.update_data(email="")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="👨 Мужской", callback_data="gender:M"),
            InlineKeyboardButton(text="👩 Женский", callback_data="gender:F"),
        ]])
        await message.answer("📭 Email пропущен.\n\n⚧ Выберите ваш пол:", reply_markup=kb)
        await state.set_state(Reg.gender)
        return

    # Проверка формата email (без отправки кода)
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


# ================== МЕРОПРИЯТИЯ ==================
@dp.callback_query(F.data == "show_events")
async def show_events(call: CallbackQuery):
    now = datetime.now()
    upcoming = [
        e for e in events
        if e.get("active", True) and datetime.fromisoformat(e["start_dt"]) > now
    ]

    if not upcoming:
        await call.message.answer("😔 Пока нет доступных событий.")
        return

    buttons = []
    for e in upcoming:
        buttons.append([InlineKeyboardButton(
            text=f"💃 {e['title']} — {e['date_text']}",
            callback_data=f"event:{e['id']}"
        )])

    await call.message.answer(
        "🎉 <b>Выберите мероприятие:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("event:"))
async def event_selected(call: CallbackQuery, state: FSMContext):
    eid = call.data.split(":")[1]
    event = next((e for e in events if e["id"] == eid), None)
    if not event:
        await call.answer("Событие не найдено")
        return

    if call.from_user.id not in users:
        await call.answer("Сначала пройдите регистрацию 📝", show_alert=True)
        return

    if sales_closed(event):
        await call.answer("❌ Продажа билетов закрыта", show_alert=True)
        return

    await state.update_data(event_id=eid, event=event)

    buttons = [[InlineKeyboardButton(
        text=f"🎫 Обычный — {event['price_single']} ₽",
        callback_data="ticket:single"
    )]]
    if event.get("price_pair", 0) > 0:
        buttons.append([InlineKeyboardButton(
            text=f"💑 Парный — {event['price_pair']} ₽",
            callback_data="ticket:pair"
        )])

    caption = (
        f"🎉 <b>{event['title']}</b>\n\n"
        f"📅 {event['date_text']}\n"
        f"📍 {event['place']}\n\n"
        f"{event['description']}\n\n"
        "Выберите тип билета:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if event.get("photo_id"):
        await call.message.answer_photo(
            event["photo_id"], caption=caption,
            reply_markup=kb, parse_mode="HTML"
        )
    else:
        await call.message.answer(caption, reply_markup=kb, parse_mode="HTML")

    await state.set_state(Reg.ticket_type)


# ================== ОПЛАТА ==================
async def go_to_payment(message: Message, state: FSMContext):
    data = await state.get_data()
    price = data.get("price", 0)
    ticket_label = "Парный" if data.get("ticket_type") == "pair" else "Обычный"
    yoomoney = settings.get("yoomoney_link", "")
    support = settings.get("support", "")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить — {price} ₽", url=yoomoney)],
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data="paid")],
        [InlineKeyboardButton(text="🆘 Техподдержка", url=f"https://t.me/{support.lstrip('@')}")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_order")],
    ])
    await message.answer(
        f"💳 <b>Билет: {ticket_label} — {price} ₽</b>\n\n"
        "1. Нажмите «Оплатить».\n"
        f"2. В комментарии укажите: <code>{message.chat.id}</code>\n"
        "3. Нажмите «Я оплатил».\n\n"
        "⏱ 60 минут на оплату.",
        reply_markup=kb, parse_mode="HTML",
    )
    await state.set_state(Reg.payment)


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
        "💑 Введите <b>@username</b> второго участника:",
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


async def issue_ticket(user_id: int, code: str, ticket_type: str, holder_name: str, event: dict):
    tickets[code] = {
        "user_id": user_id,
        "type": ticket_type,
        "holder": holder_name,
        "used": False,
        "event_id": event.get("id"),
        "event_title": event.get("title", ""),
        "issued_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    save_data()

    qr_file = make_qr_image(code)
    type_label = "Парный" if ticket_type == "pair" else "Обычный"
    caption = (
        f"🎫 <b>Ваш билет на {event['title']}</b>\n\n"
        f"👤 {holder_name}\n"
        f"🎟 {type_label}\n"
        f"📅 {event['date_text']}\n"
        f"📍 {event['place']}\n\n"
        f"Код: <code>{code}</code>\n\n"
        "Покажите QR на входе."
    )
    await bot.send_photo(user_id, qr_file, caption=caption, parse_mode="HTML")


@dp.callback_query(F.data == "paid", Reg.payment)
async def paid(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    buyer_id = call.from_user.id
    buyer_name = f"{data['name']} {data['surname']}"
    ticket_type = data.get("ticket_type", "single")
    event = data.get("event", {})

    order = {
        "order_id": next_order_id(),
        "user_id": buyer_id,
        "event_id": data.get("event_id"),
        "event_title": event.get("title", ""),
        "email": data.get("email", ""),
        "name": buyer_name,
        "ticket_type": ticket_type,
        "price": data.get("price", 0),
        "paid_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    orders.append(order)
    save_data()

    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔔 <b>Заказ!</b>\n"
            f"{event.get('title')}\n"
            f"{buyer_name} | {data.get('phone')}\n"
            f"{ticket_type} | {data.get('price')} ₽",
            parse_mode="HTML",
        )
    except Exception:
        pass

    buyer_code = generate_ticket_code()
    await issue_ticket(buyer_id, buyer_code, ticket_type, buyer_name, event)

    if ticket_type == "pair":
        partner_id = data.get("partner_id")
        if partner_id and partner_id in users:
            partner = users[partner_id]
            partner_name = f"{partner['name']} {partner['surname']}"
            await issue_ticket(partner_id, generate_ticket_code(), ticket_type, partner_name, event)

    await call.message.edit_text("✅ Спасибо! Билеты отправлены 🎫")
    await state.clear()


@dp.callback_query(F.data == "cancel_order")
async def cancel_order(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Отменено. /start — заново.")


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
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users:0")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🎫 Найти билет", callback_data="admin:find_ticket")],
        [InlineKeyboardButton(text="📤 Отправить отчёт на email", callback_data="admin:report")],
    ])
    await message.answer(
        f"🛠 <b>Админ-панель</b>\n\n"
        f"Событий: {len(events)} | Заказов: {len(orders)} | Юзеров: {len(users)}",
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
        f"🎫 Билетов активно: <b>{tickets_active}</b>\n"
        f"✅ Билетов использовано: <b>{tickets_used}</b>\n"
    )

    if by_event:
        text += "\n<b>Продажи по событиям:</b>\n"
        for title, info in by_event.items():
            text += f"• {title}: {info['count']} шт, {info['sum']} ₽\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:back")],
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


# ================== ПОЛЬЗОВАТЕЛИ (АДМИН) ==================
USERS_PER_PAGE = 5


def build_users_page(page: int):
    user_ids = list(users.keys())
    total = len(user_ids)

    if total == 0:
        return "📭 Пока нет зарегистрированных пользователей.", None

    total_pages = (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE
    page = max(0, min(page, total_pages - 1))

    start = page * USERS_PER_PAGE
    end = start + USERS_PER_PAGE
    page_ids = user_ids[start:end]

    text = f"👥 <b>Пользователи</b> ({total} всего)\n"
    text += f"Страница {page + 1} из {total_pages}\n\n"

    for uid in page_ids:
        u = users[uid]
        text += format_user_info(uid, u)
        text += "➖➖➖➖➖➖➖➖➖➖\n"

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(
            text="⬅️ Назад", callback_data=f"admin:users:{page - 1}"
        ))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(
            text="Вперёд ➡️", callback_data=f"admin:users:{page + 1}"
        ))

    kb_rows = []
    if nav_buttons:
        kb_rows.append(nav_buttons)

    kb_rows.append([InlineKeyboardButton(
        text="📤 Выгрузить всех в CSV", callback_data="admin:users:export"
    )])
    kb_rows.append([InlineKeyboardButton(
        text="🔍 Найти по email или ID", callback_data="admin:users:search"
    )])
    kb_rows.append([InlineKeyboardButton(
        text="⬅️ В админ-панель", callback_data="admin:back"
    )])

    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


@dp.callback_query(F.data.startswith("admin:users:"))
async def admin_users(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Только для админа", show_alert=True)
        return

    action = call.data.split(":")[2]

    if action == "export":
        if not users:
            await call.answer("Нет пользователей", show_alert=True)
            return
        rows = []
        for uid, u in users.items():
            rows.append([
                uid, u.get("username", ""), u.get("email", ""),
                u.get("name", ""), u.get("surname", ""),
                u.get("gender", ""), u.get("phone", ""),
                u.get("registered_at", ""),
            ])
        csv_bytes = make_csv(
            ["telegram_id", "username", "email", "name", "surname",
             "gender", "phone", "registered_at"], rows,
        )
        file = BufferedInputFile(csv_bytes, filename="users.csv")
        await call.message.answer_document(
            file,
            caption=f"👥 Пользователи: {len(users)}\n"
                    f"Сформировано: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        )
        await call.answer("Отправляю файл...")
        return

    if action == "search":
        await call.message.answer(
            "🔍 Отправьте <b>email</b>, <b>@username</b> или <b>Telegram ID</b>:",
            parse_mode="HTML",
        )
        await state.set_state(Admin.search_user)
        return

    try:
        page = int(action)
    except ValueError:
        page = 0

    text, kb = build_users_page(page)
    if kb is None:
        await call.message.edit_text(text)
    else:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.message(Admin.search_user)
async def admin_users_search_input(message: Message, state: FSMContext):
    query = message.text.strip().lower()
    await state.set_state(Admin.menu)

    found = []
    for uid, u in users.items():
        if query == str(uid):
            found.append((uid, u))
            continue
        email = (u.get("email") or "").lower()
        username = (u.get("username") or "").lower()
        query_clean = query.lstrip("@")
        if query_clean and (query_clean in email or query_clean in username):
            found.append((uid, u))

    if not found:
        await message.answer(
            f"❌ Никого не найдено по запросу: <code>{query}</code>",
            parse_mode="HTML",
        )
        return

    text = f"🔍 Найдено: <b>{len(found)}</b>\n\n"
    for uid, u in found[:10]:
        text += format_user_info(uid, u)
        text += "➖➖➖➖➖➖➖➖➖➖\n"

    await message.answer(text, parse_mode="HTML")


# ================== РАССЫЛКА ==================
@dp.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    if not users:
        await call.answer("Нет пользователей", show_alert=True)
        return
    await call.message.edit_text(
        f"📢 Отправьте сообщение для рассылки.\n\n"
        f"Получателей: <b>{len(users)}</b>\n\n"
        f"Поддерживается HTML: &lt;b&gt;жирный&lt;/b&gt;, &lt;i&gt;курсив&lt;/i&gt;\n\n"
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

    text = message.html_text if message.text else None
    if not text:
        await message.answer("❌ Отправьте текстовое сообщение.")
        return

    sent = 0
    failed = 0
    await message.answer(f"📢 Начинаю рассылку на {len(users)} чел...")

    for uid in list(users.keys()):
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await message.answer(
        f"✅ Рассылка завершена.\n\n"
        f"Отправлено: {sent}\n"
        f"Ошибок: {failed}"
    )
    await state.clear()
    await show_admin_menu(message, state)


# ================== ПОИСК БИЛЕТА ==================
@dp.callback_query(F.data == "admin:find_ticket")
async def admin_find_ticket_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text(
        "🎫 Отправьте код билета (например: <code>VIDA-XXXX-XXXX</code>)\n\n"
        "Для отмены — /admin",
        parse_mode="HTML",
    )
    await state.set_state(Admin.find_ticket)


@dp.message(Admin.find_ticket)
async def admin_find_ticket(message: Message, state: FSMContext):
    if message.text == "/admin":
        await state.clear()
        await show_admin_menu(message, state)
        return

    code = message.text.strip().upper()
    ticket = tickets.get(code)

    if not ticket:
        await message.answer("❌ Билет с таким кодом не найден.")
        return

    status = "✅ использован" if ticket.get("used") else "🟢 активен"
    text = (
        f"🎫 <b>Билет найден</b>\n\n"
        f"Код: <code>{code}</code>\n"
        f"Статус: {status}\n"
        f"Владелец: {ticket['holder']}\n"
        f"ID: <code>{ticket['user_id']}</code>\n"
        f"Тип: {'Парный' if ticket['type'] == 'pair' else 'Обычный'}\n"
        f"Событие: {ticket.get('event_title', '—')}\n"
        f"Выдан: {ticket.get('issued_at', '—')}\n"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    if not ticket.get("used"):
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Отметить как использованный",
                callback_data=f"admin:use_ticket:{code}"
            )
        ]])

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("admin:use_ticket:"))
async def admin_use_ticket(call: CallbackQuery):
    code = call.data.split(":")[2]
    ticket = tickets.get(code)
    if not ticket:
        await call.answer("Билет не найден")
        return
    ticket["used"] = True
    save_data()
    await call.message.edit_text(
        f"✅ Билет <code>{code}</code> отмечен как использованный.",
        parse_mode="HTML",
    )


# ================== ЗАПУСК ==================
async def main():
    load_data()
    print("🚀 Бот запускается...")
    me = await bot.get_me()
    print(f"✅ Бот @{me.username} работает")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
