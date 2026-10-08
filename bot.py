"""
Telegram-бот для продажи билетов на вечеринки Vida Bachata.
"""

import asyncio
import csv
import io
import json
import os
import random
import re
import uuid
from datetime import datetime, timedelta
from email.message import EmailMessage

import aiosmtplib
import qrcode
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

# ================== КОНФИГ ==================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# print(f"[DEBUG] BOT_TOKEN длина: {len(BOT_TOKEN)}, префикс: {BOT_TOKEN[:15]}")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "your_bot")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.mail.ru")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")

DATA_FILE = os.getenv("DATA_FILE", "bot_data.json")

# ================== БОТ ==================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ================== ХРАНИЛИЩЕ ==================
users = {}
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
    data = {
        "events": events,
        "orders": orders,
        "tickets": tickets,
        "settings": settings,
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_data():
    global events, orders, tickets, settings
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            events = data.get("events", [])
            orders = data.get("orders", [])
            tickets = data.get("tickets", {})
            settings.update(data.get("settings", {}))
            print(f"✅ Загружено: {len(events)} событий, {len(orders)} заказов")
    except FileNotFoundError:
        print("ℹ️ Файл данных не найден, начинаем с чистого листа")


# ================== УТИЛИТЫ ==================
def is_valid_email(email: str) -> bool:
    return re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) is not None


def sales_closed(event: dict) -> bool:
    start_dt = datetime.fromisoformat(event["start_dt"])
    stop_before = timedelta(hours=settings.get("sales_stop_hours", 1))
    return datetime.now() >= start_dt - stop_before


def generate_ticket_code() -> str:
    return f"VIDA-{uuid.uuid4().hex[:4].upper()}-{uuid.uuid4().hex[:4].upper()}"


def next_order_id() -> str:
    return f"ORD-{len(orders) + 1:04d}"


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


async def send_report_to_admin():
    """Отправляет отчёт админу на email (через SMTP — может не работать на Railway)."""
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
    gender = State()
    email = State()
    confirm = State()
    ticket_type = State()
    partner_input = State()
    payment = State()


class Admin(StatesGroup):
    menu = State()
    add_title = State()
    add_date_text = State()
    add_start_dt = State()
    add_place = State()
    add_description = State()
    add_price_single = State()
    add_price_pair = State()
    add_photo = State()
    delete_select = State()
    find_ticket = State()
    broadcast = State()
    settings_edit = State()


# ================== /start ==================
@dp.message(CommandStart(deep_link=True))
async def start_with_invite(message: Message, state: FSMContext, command: CommandObject):
    await state.clear()
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

    await ask_phone(message, state)


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    user_name = message.from_user.first_name or "друг"

    greeting = (
        f"Привет, {user_name} 👋\n"
        "Здесь можно купить билет на вечеринки <b>Vida Bachata</b> 💃🕺\n\n"
        "Зарегистрируйтесь или нажмите «Мероприятия».\n\n"
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
    if call.from_user.id in users:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎉 Мероприятия", callback_data="show_events")],
        ])
        await call.message.answer(
            "✅ Вы уже зарегистрированы!\n\nНажмите «Мероприятия».",
            reply_markup=kb,
        )
        return

    await ask_phone(call.message, state)


async def ask_phone(message: Message, state: FSMContext):
    """Запрашивает у пользователя контакт через кнопку «Поделиться»."""
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться контактом", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "Нажмите кнопку «Поделиться контактом» внизу, "
        "чтобы отправить свой номер телефона 👇",
        reply_markup=kb,
    )
    await state.set_state(Reg.phone)


# ================== РЕГИСТРАЦИЯ ==================
@dp.message(Reg.phone, F.contact)
async def get_phone(message: Message, state: FSMContext):
    contact = message.contact
    if contact.user_id and contact.user_id != message.from_user.id:
        await message.answer("Пожалуйста, отправьте свой собственный контакт.")
        return

    await state.update_data(phone=contact.phone_number)
    await message.answer(
        "✅ Телефон получен!\n\nОтправьте ответным сообщением своё Имя.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(Reg.name)


@dp.message(Reg.phone)
async def wrong_phone(message: Message):
    await message.answer("Нажмите кнопку «Поделиться контактом» 👇")


@dp.message(Reg.name)
async def get_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("Слишком короткое имя, попробуйте ещё раз:")
        return
    await state.update_data(name=name)
    await message.answer("Отправьте вашу Фамилию.")
    await state.set_state(Reg.surname)


@dp.message(Reg.surname)
async def get_surname(message: Message, state: FSMContext):
    surname = message.text.strip()
    if len(surname) < 2:
        await message.answer("Слишком короткая фамилия, попробуйте ещё раз:")
        return
    await state.update_data(surname=surname)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👨 Мужской", callback_data="gender:M"),
        InlineKeyboardButton(text="👩 Женский", callback_data="gender:F"),
    ]])
    await message.answer("Выберите ваш пол:", reply_markup=kb)
    await state.set_state(Reg.gender)


@dp.callback_query(F.data.startswith("gender:"), Reg.gender)
async def get_gender(call: CallbackQuery, state: FSMContext):
    gender = "Мужской" if call.data == "gender:M" else "Женский"
    await state.update_data(gender=gender)
    await call.message.edit_text(f"Пол: {gender}")
    await call.message.answer(
        "📧 Отправьте свой email — он понадобится для новостей и чеков.\n\n"
        "Или отправьте <code>-</code>, чтобы пропустить этот шаг.",
        parse_mode="HTML",
    )
    await state.set_state(Reg.email)


@dp.message(Reg.email)
async def get_email(message: Message, state: FSMContext):
    email = message.text.strip()

    if email == "-":
        await state.update_data(email="")
    elif not is_valid_email(email):
        await message.answer(
            "❌ Неверный email. Попробуйте ещё раз или отправьте <code>-</code>.",
            parse_mode="HTML",
        )
        return
    else:
        await state.update_data(email=email)

    await show_confirmation(message, state)


async def show_confirmation(message: Message, state: FSMContext):
    data = await state.get_data()
    email_line = f"📧 {data['email']}\n" if data.get("email") else ""

    text = (
        "📋 <b>Проверьте ваши данные:</b>\n\n"
        f"📱 {data['phone']}\n"
        f"👤 {data['name']} {data['surname']}\n"
        f"⚧ {data['gender']}\n"
        f"{email_line}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Согласен", callback_data="confirm_yes")],
        [InlineKeyboardButton(text="🔄 Перезаполнить", callback_data="confirm_restart")],
    ])
    await message.answer(text, parse_mode="HTML")
    await message.answer("Всё верно?", reply_markup=kb)
    await state.set_state(Reg.confirm)


@dp.callback_query(F.data == "confirm_yes", Reg.confirm)
async def confirm_yes(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    data["registered_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    data["username"] = call.from_user.username or ""
    users[call.from_user.id] = data
    if call.from_user.username:
        username_index[call.from_user.username.lower()] = call.from_user.id

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
    await call.message.edit_text("🔄 Начинаем заново.")
    await ask_phone(call.message, state)

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
    await call.message.edit_text("➕ Шаг 1/8. <b>Название события:</b>", parse_mode="HTML")


@dp.message(Admin.add_title)
async def add_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(Admin.add_date_text)
    await message.answer("Шаг 2/8. Дата текстом (например: 15 ноября 2026, 22:00):")


@dp.message(Admin.add_date_text)
async def add_date_text(message: Message, state: FSMContext):
    await state.update_data(date_text=message.text.strip())
    await state.set_state(Admin.add_start_dt)
    await message.answer("Шаг 3/8. Точная дата <code>ГГГГ-ММ-ДД ЧЧ:ММ</code>:", parse_mode="HTML")


@dp.message(Admin.add_start_dt)
async def add_start_dt(message: Message, state: FSMContext):
    try:
        dt = datetime.strptime(message.text.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        await message.answer("❌ Формат: 2026-11-15 22:00")
        return
    if dt < datetime.now():
        await message.answer("❌ Дата в прошлом.")
        return
    await state.update_data(start_dt=dt.isoformat())
    await state.set_state(Admin.add_place)
    await message.answer("Шаг 4/8. Место:")


@dp.message(Admin.add_place)
async def add_place(message: Message, state: FSMContext):
    await state.update_data(place=message.text.strip())
    await state.set_state(Admin.add_description)
    await message.answer("Шаг 5/8. Описание:")


@dp.message(Admin.add_description)
async def add_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await state.set_state(Admin.add_price_single)
    await message.answer("Шаг 6/8. Цена обычного (число):")


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
    await message.answer("Шаг 7/8. Цена парного (0 если нет):")


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
    await message.answer("Шаг 8/8. Фото-обложка или <code>-</code>:", parse_mode="HTML")


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
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:back")],
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "admin:delete")
async def admin_delete_list(call: CallbackQuery):
    if not events:
        await call.message.edit_text("📭 Нет событий.")
        return
    buttons = []
    for e in events:
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {e['title']}",
            callback_data=f"admin:del:{e['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:back")])
    await call.message.edit_text(
        "🗑 Выбери событие:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.callback_query(F.data.startswith("admin:del:"))
async def admin_delete_confirm(call: CallbackQuery, state: FSMContext):
    eid = call.data.split(":")[2]
    global events
    events = [e for e in events if e["id"] != eid]
    save_data()
    await call.message.edit_text("✅ Удалено.")
    await show_admin_menu(call.message, state)


@dp.callback_query(F.data == "admin:stats")
async def admin_stats(call: CallbackQuery):
    total = sum(o["price"] for o in orders)
    await call.message.edit_text(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Юзеров: {len(users)}\n"
        f"🎫 Заказов: {len(orders)}\n"
        f"💰 Выручка: {total} ₽\n"
        f"🎟 Билетов: {len(tickets)}",
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text(
        f"📢 Получателей: {len(users)}\n\nОтправь текст:"
    )
    await state.set_state(Admin.broadcast)


@dp.message(Admin.broadcast)
async def admin_broadcast_send(message: Message, state: FSMContext):
    sent = failed = 0
    for uid in users:
        try:
            await bot.send_message(uid, message.text, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
    await message.answer(f"✅ Отправлено: {sent}, ошибок: {failed}")
    await state.clear()


@dp.callback_query(F.data == "admin:find_ticket")
async def admin_find_ticket(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("🎫 Отправь код билета:")
    await state.set_state(Admin.find_ticket)


@dp.message(Admin.find_ticket)
async def admin_find_ticket_check(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    ticket = tickets.get(code)
    if not ticket:
        await message.answer(f"❌ Не найден: {code}")
        return
    status = "⚠️ Использован" if ticket.get("used") else "✅ Действителен"
    text = (
        f"🎫 <b>{code}</b>\n"
        f"{status}\n"
        f"Владелец: {ticket['holder']}\n"
        f"Тип: {ticket['type']}\n"
        f"ID: {ticket['user_id']}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Отметить использованным",
            callback_data=f"admin:used:{code}"
        )],
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")
    await state.clear()


@dp.callback_query(F.data.startswith("admin:used:"))
async def admin_mark_used(call: CallbackQuery):
    code = call.data.split(":")[2]
    if code in tickets:
        tickets[code]["used"] = True
        save_data()
        await call.message.edit_text(f"✅ {code} — использован.")


# ================== ЗАПУСК ==================
async def main():
    print("🚀 Бот запускается...")
    load_data()
    await bot.delete_webhook(drop_pending_updates=True)
    print(f"✅ Бот @{BOT_USERNAME} работает")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
