import asyncio
import base64
import io
import json
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Asia/Almaty")
DB_PATH = os.getenv("DB_PATH", "bot.db")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
try:  # Telegram ID администратора (узнать: написать боту /id), сюда приходят сообщения из /help
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
except ValueError:
    ADMIN_ID = 0
DAYS =["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# ---------- база данных ----------
db = sqlite3.connect(DB_PATH, isolation_level=None)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY, tz TEXT, goal INTEGER DEFAULT 2000,
    height REAL, weight REAL, remind INTEGER DEFAULT 5, hw_hour INTEGER DEFAULT 16, grade INTEGER);
CREATE TABLE IF NOT EXISTS water(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, day TEXT, ml INTEGER);
CREATE TABLE IF NOT EXISTS bells(
    user_id INTEGER, num INTEGER, start TEXT, end TEXT, PRIMARY KEY(user_id, num));
CREATE TABLE IF NOT EXISTS lessons(
    user_id INTEGER, weekday INTEGER, num INTEGER, title TEXT, PRIMARY KEY(user_id, weekday, num));
CREATE TABLE IF NOT EXISTS homework(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, subject TEXT, text TEXT,
    lesson_date TEXT, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS holidays(
    user_id INTEGER, date TEXT, name TEXT, source TEXT, PRIMARY KEY(user_id, date));
CREATE TABLE IF NOT EXISTS holiday_sync(user_id INTEGER PRIMARY KEY, synced_at TEXT, until TEXT);
CREATE TABLE IF NOT EXISTS habits(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, title TEXT);
CREATE TABLE IF NOT EXISTS habit_log(user_id INTEGER, habit_id INTEGER, day TEXT, PRIMARY KEY(user_id, habit_id, day));
CREATE TABLE IF NOT EXISTS support(admin_msg_id INTEGER PRIMARY KEY, user_id INTEGER);
"""
)
for _col in ("hw_hour INTEGER DEFAULT 16", "grade INTEGER", "ask_hw INTEGER DEFAULT 1"):  # для баз, созданных старой версией бота
    try:
        db.execute(f"ALTER TABLE users ADD COLUMN {_col}")
    except sqlite3.OperationalError:
        pass


def user(uid):
    db.execute("INSERT OR IGNORE INTO users(user_id, tz) VALUES(?, ?)", (uid, DEFAULT_TZ))
    return db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()


def now_of(u):
    return datetime.now(ZoneInfo(u["tz"]))


# ---------- клавиатуры ----------
def kb(*rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


MENU = ("🏠 Меню", "menu")
MAIN_KB = kb(
    [("💧 Вода", "water"), ("⚖️ ИМТ", "bmi")],
    [("📅 Расписание", "sched"), ("📚 Домашка", "hw")],
    [("✅ Чек-лист", "check"), ("⚙️ Настройки", "set")],
    [("❓ Помощь", "help")],
)


async def edit(cb: CallbackQuery, text, markup):
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass
    await cb.answer()


def to_num(text):
    try:
        return float(text.replace(",", ".").strip())
    except ValueError:
        return None


def norm_time(t):
    h, m = re.split(r"[:.]", t)
    h, m = int(h), int(m)
    if h > 23 or m > 59:
        raise ValueError
    return f"{h:02d}:{m:02d}"


router = Router()


class S(StatesGroup):
    water = State()
    height = State()
    weight = State()
    bells = State()
    lessons = State()
    tz = State()
    hw_task = State()
    hol = State()
    ck = State()
    support = State()


# ---------- старт и меню ----------
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    user(m.from_user.id)
    await m.answer(
        "Привет! Выбирай, что нужно 👇\n\nСначала укажи свой город: ⚙️ Настройки → 🌍 Часовой пояс.\n\nЧтобы записать домашку, просто напиши мне после урока, "
        "например: Математика №12–15",
        reply_markup=MAIN_KB,
    )


@router.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cb, "Главное меню", MAIN_KB)


# ---------- помощь, ID и связь с администратором ----------
HELP_TEXT = (
    "❓ Что умеет бот\n\n"
    "💧 Вода: считает, сколько ты выпил за день.\n"
    "📅 Расписание: звонки и уроки по дням, напоминания перед уроком.\n"
    "📚 Домашка: просто напиши «Математика №12–15», бот найдёт следующий урок.\n"
    "📷 Фото задания: пришли фото и выбери «Только решение» или «С объяснением по шагам». "
    "ИИ ответит на том же языке, что и задание.\n"
    "✅ Чек-лист: свои ежедневные дела.\n"
    "⚙️ Настройки: город, класс, выходные.\n\n"
    "Команды: /start (меню), /help (помощь), /id (твой Telegram ID)."
)
HELP_KB = kb([("✉️ Написать администратору", "support")], [MENU])


@router.message(Command("help"))
async def help_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(HELP_TEXT, reply_markup=HELP_KB)


@router.callback_query(F.data == "help")
async def help_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cb, HELP_TEXT, HELP_KB)


@router.message(Command("id"))
async def my_id(m: Message):
    await m.answer(f"Твой Telegram ID: {m.from_user.id}")


@router.callback_query(F.data == "support")
async def support_start(cb: CallbackQuery, state: FSMContext):
    if not ADMIN_ID:
        return await edit(cb, "Связь с администратором пока не настроена.", kb([MENU]))
    await state.set_state(S.support)
    await edit(
        cb,
        "Напиши сообщение для администратора одним сообщением (можно с фото). Я передам его, ответ придёт сюда.",
        kb([("❌ Отмена", "help")]),
    )


@router.message(S.support)
async def support_in(m: Message, state: FSMContext):
    await state.clear()
    u = m.from_user
    uname = f"@{u.username}" if u.username else "без username"
    head = f"✉️ Сообщение от {u.full_name} ({uname}), ID {u.id}.\nЧтобы ответить, ответь (reply) на это сообщение или на текст ниже."
    try:
        head_msg = await m.bot.send_message(ADMIN_ID, head)
        copy_msg = await m.bot.copy_message(ADMIN_ID, m.chat.id, m.message_id)
    except Exception:
        logging.exception("support send failed")
        return await m.answer("⚠️ Не получилось отправить, попробуй позже.", reply_markup=kb([MENU]))
    for mid in (head_msg.message_id, copy_msg.message_id):
        db.execute("INSERT OR REPLACE INTO support VALUES(?,?)", (mid, u.id))
    await m.answer("✅ Отправил администратору. Ответ придёт сюда.", reply_markup=kb([MENU]))


async def is_support_reply(m: Message) -> bool:
    """Ответ администратора (reply) на пересланное сообщение пользователя."""
    if not ADMIN_ID or not m.from_user or m.from_user.id != ADMIN_ID or not m.reply_to_message:
        return False
    row = db.execute("SELECT 1 FROM support WHERE admin_msg_id=?", (m.reply_to_message.message_id,)).fetchone()
    return row is not None


@router.message(is_support_reply)
async def admin_reply(m: Message):
    row = db.execute("SELECT user_id FROM support WHERE admin_msg_id=?", (m.reply_to_message.message_id,)).fetchone()
    try:
        await m.bot.send_message(row["user_id"], "✉️ Ответ администратора:")
        await m.bot.copy_message(row["user_id"], m.chat.id, m.message_id)
    except Exception:
        logging.exception("support reply failed")
        return await m.answer("⚠️ Не удалось доставить ответ (возможно, пользователь заблокировал бота).")
    await m.answer("✅ Ответ доставлен.")


# ---------- вода ----------
def water_total(uid):
    u = user(uid)
    day = now_of(u).date().isoformat()  # новый день = новая дата = счётчик с нуля
    return db.execute("SELECT COALESCE(SUM(ml),0) FROM water WHERE user_id=? AND day=?", (uid, day)).fetchone()[0]


def water_text(uid):
    u = user(uid)
    total, goal = water_total(uid), u["goal"]
    filled = min(10, total * 10 // goal)
    pct = min(100, total * 100 // goal)
    left = "🎉 Норма выполнена!" if total >= goal else f"Осталось: {goal - total} мл"
    return (
        f"💧 Вода сегодня\n\n{'▓' * filled}{'░' * (10 - filled)} {pct}%\n"
        f"{total / 1000:.2f} / {goal / 1000:.1f} л\n{left}".replace(".", ",")
    )


WATER_KB = kb(
    [("🥛 +250 мл", "w:250"), ("🍼 +500 мл", "w:500")],
    [("🍶 +750 мл", "w:750"), ("🍾 +1 л", "w:1000")],
    [("✏️ Своё количество", "w_custom"), ("↩️ Отменить последнее", "w_undo")],
    [MENU],
)


def add_water(uid, ml):
    u = user(uid)
    db.execute("INSERT INTO water(user_id, day, ml) VALUES(?,?,?)", (uid, now_of(u).date().isoformat(), ml))


@router.callback_query(F.data == "water")
async def water(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cb, water_text(cb.from_user.id), WATER_KB)


@router.callback_query(F.data.startswith("w:"))
async def water_add(cb: CallbackQuery):
    ml = int(cb.data.split(":")[1])
    add_water(cb.from_user.id, ml)
    await edit(cb, water_text(cb.from_user.id), WATER_KB)


@router.callback_query(F.data == "w_undo")
async def water_undo(cb: CallbackQuery):
    uid = cb.from_user.id
    day = now_of(user(uid)).date().isoformat()
    db.execute(
        "DELETE FROM water WHERE id=(SELECT MAX(id) FROM water WHERE user_id=? AND day=?)", (uid, day)
    )
    await edit(cb, water_text(uid), WATER_KB)


@router.callback_query(F.data == "w_custom")
async def water_custom(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.water)
    await edit(cb, "Сколько выпил? Напиши число в мл (например 300) или в литрах (0,3)", kb([("❌ Отмена", "water")]))


@router.message(S.water)
async def water_custom_in(m: Message, state: FSMContext):
    v = to_num(m.text or "")
    if v is None or v <= 0:
        return await m.answer("Нужно число, например 300 или 0,3")
    ml = int(v * 1000) if v < 10 else int(v)
    add_water(m.from_user.id, ml)
    await state.clear()
    await m.answer(water_text(m.from_user.id), reply_markup=WATER_KB)


# ---------- ИМТ ----------
def bmi_text(h, w):
    bmi = w / (h / 100) ** 2
    if bmi < 18.5:
        cat = "недостаточный вес"
    elif bmi < 25:
        cat = "норма"
    elif bmi < 30:
        cat = "избыточный вес"
    else:
        cat = "ожирение"
    return (
        f"⚖️ Рост {h:g} см, вес {w:g} кг\nИМТ: {bmi:.1f} ({cat})\n\n"
        "Для людей младше 18 лет ИМТ оценивают по возрастным таблицам, так что эти категории приблизительные."
    ).replace(".", ",")


@router.callback_query(F.data == "bmi")
async def bmi(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    u = user(cb.from_user.id)
    if u["height"] and u["weight"]:
        return await edit(cb, bmi_text(u["height"], u["weight"]), kb([("🔄 Пересчитать", "bmi_new")], [MENU]))
    await bmi_new(cb, state)


@router.callback_query(F.data == "bmi_new")
async def bmi_new(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.height)
    await edit(cb, "Введи рост в см (например 175)", kb([("❌ Отмена", "menu")]))


@router.message(S.height)
async def bmi_height(m: Message, state: FSMContext):
    v = to_num(m.text or "")
    if v is None or not 50 <= v <= 250:
        return await m.answer("Рост в см, например 175")
    db.execute("UPDATE users SET height=? WHERE user_id=?", (v, m.from_user.id))
    await state.set_state(S.weight)
    await m.answer("Теперь вес в кг (например 68,5)")


@router.message(S.weight)
async def bmi_weight(m: Message, state: FSMContext):
    v = to_num(m.text or "")
    if v is None or not 10 <= v <= 300:
        return await m.answer("Вес в кг, например 68,5")
    db.execute("UPDATE users SET weight=? WHERE user_id=?", (v, m.from_user.id))
    await state.clear()
    u = user(m.from_user.id)
    await m.answer(bmi_text(u["height"], u["weight"]), reply_markup=kb([("🔄 Пересчитать", "bmi_new")], [MENU]))


# ---------- расписание ----------
def day_text(uid, wd):
    rows = db.execute(
        "SELECT l.num, l.title, b.start, b.end FROM lessons l "
        "LEFT JOIN bells b ON b.user_id=l.user_id AND b.num=l.num "
        "WHERE l.user_id=? AND l.weekday=? ORDER BY l.num",
        (uid, wd),
    ).fetchall()
    if not rows:
        return "уроков нет"
    return "\n".join(
        f"{r['num']}. {r['start']}–{r['end']}  {r['title']}" if r["start"] else f"{r['num']}. {r['title']}"
        for r in rows
    )


SCHED_KB = kb(
    [("📆 Сегодня", "s_today"), ("➡️ Завтра", "s_tomorrow")],
    [("🗓 Вся неделя", "s_week")],
    [("🔔 Звонки", "s_bells"), ("✏️ Уроки по дням", "s_edit")],
    [MENU],
)
BACK_SCHED = kb([("⬅️ Назад", "sched")])


@router.callback_query(F.data == "sched")
async def sched(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cb, "📅 Расписание", SCHED_KB)


@router.callback_query(F.data.in_({"s_today", "s_tomorrow"}))
async def sched_day(cb: CallbackQuery):
    uid = cb.from_user.id
    now = now_of(user(uid))
    wd = (now.weekday() + (1 if cb.data == "s_tomorrow" else 0)) % 7
    await edit(cb, f"📅 {DAYS[wd]}\n\n{day_text(uid, wd)}", BACK_SCHED)


@router.callback_query(F.data == "s_week")
async def sched_week(cb: CallbackQuery):
    uid = cb.from_user.id
    text = "\n\n".join(f"📌 {DAYS[wd]}\n{day_text(uid, wd)}" for wd in range(7))
    await edit(cb, text, BACK_SCHED)


@router.callback_query(F.data == "s_bells")
async def sched_bells(cb: CallbackQuery, state: FSMContext):
    rows = db.execute("SELECT * FROM bells WHERE user_id=? ORDER BY num", (cb.from_user.id,)).fetchall()
    cur = "\n".join(f"{r['num']}. {r['start']}–{r['end']}" for r in rows) or "пока пусто"
    await state.set_state(S.bells)
    await edit(
        cb,
        f"🔔 Звонки сейчас:\n{cur}\n\nЧтобы заменить, пришли одним сообщением по строке на урок:\n"
        "08:30-09:15\n09:25-10:10\n10:20-11:05",
        BACK_SCHED,
    )


@router.message(S.bells)
async def bells_in(m: Message, state: FSMContext):
    pat = re.compile(r"^\s*(?:(\d+)[.)]\s*)?(\d{1,2}[:.]\d{2})\s*[-–—]\s*(\d{1,2}[:.]\d{2})\s*$")
    items = []
    for i, line in enumerate((m.text or "").splitlines(), 1):
        if not line.strip():
            continue
        g = pat.match(line)
        try:
            if not g:
                raise ValueError
            items.append((int(g.group(1) or len(items) + 1), norm_time(g.group(2)), norm_time(g.group(3))))
        except ValueError:
            return await m.answer(f"Не понял строку: «{line}». Формат: 08:30-09:15")
    if not items:
        return await m.answer("Пришли хотя бы одну строку вида 08:30-09:15")
    uid = m.from_user.id
    db.execute("DELETE FROM bells WHERE user_id=?", (uid,))
    db.executemany("INSERT INTO bells VALUES(?,?,?,?)", [(uid, n, s, e) for n, s, e in items])
    await state.clear()
    await m.answer(f"✅ Сохранил звонков: {len(items)}", reply_markup=BACK_SCHED)


@router.callback_query(F.data == "s_edit")
async def sched_edit(cb: CallbackQuery):
    rows = [[(d, f"sd:{i}") for i, d in enumerate(DAYS[:4])], [(d, f"sd:{i + 4}") for i, d in enumerate(DAYS[4:])]]
    await edit(cb, "Для какого дня вставить уроки?", kb(*rows, [("⬅️ Назад", "sched")]))


@router.callback_query(F.data.startswith("sd:"))
async def sched_day_pick(cb: CallbackQuery, state: FSMContext):
    wd = int(cb.data.split(":")[1])
    await state.set_state(S.lessons)
    await state.update_data(wd=wd)
    await edit(
        cb,
        f"📌 {DAYS[wd]}: пришли уроки по порядку, по строке на урок.\n"
        "Если окно, поставь «-».\n\nНапример:\nМатематика\nФизика\n-\nИстория",
        BACK_SCHED,
    )


@router.message(S.lessons)
async def lessons_in(m: Message, state: FSMContext):
    wd = (await state.get_data())["wd"]
    uid = m.from_user.id
    db.execute("DELETE FROM lessons WHERE user_id=? AND weekday=?", (uid, wd))
    count = 0
    for i, line in enumerate((m.text or "").splitlines(), 1):
        title = re.sub(r"^\s*\d+[.)]\s*", "", line).strip()
        if title and title not in {"-", "–", "—"}:
            db.execute("INSERT INTO lessons VALUES(?,?,?,?)", (uid, wd, i, title))
            count += 1
    await state.clear()
    await m.answer(f"✅ {DAYS[wd]}: сохранил уроков: {count}\n\n{day_text(uid, wd)}", reply_markup=BACK_SCHED)


# ---------- настройки ----------
@router.callback_query(F.data == "set")
async def settings(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    u = user(cb.from_user.id)
    text = (
        f"⚙️ Настройки\n\nНорма воды: {u['goal']} мл\nНапоминание об уроке: за {u['remind']} мин\n"
        f"Напоминание о домашке: в {u['hw_hour']}:00 и в 21:00\nКласс: {u['grade'] or 'не указан'}\n"
        f"Часовой пояс: {u['tz']}\n"
        f"Вопрос о домашке после урока: {'выключен' if u['ask_hw'] == 0 else 'включён'}"
    )
    await edit(
        cb,
        text,
        kb(
            [("💧 Норма воды", "set_goal")],
            [("⏰ Напоминание об уроке", "set_rem")],
            [("📚 Время для домашки", "set_hw")],
            [("📝 Вопрос после урока: вкл/выкл", "set_ask")],
            [("🎓 Класс", "set_grade"), ("🚫 Выходные", "set_hol")],
            [("🌍 Часовой пояс", "set_tz")],
            [MENU],
        ),
    )


@router.callback_query(F.data == "set_ask")
async def set_ask(cb: CallbackQuery, state: FSMContext):
    u = user(cb.from_user.id)
    db.execute("UPDATE users SET ask_hw=? WHERE user_id=?", (1 if u["ask_hw"] == 0 else 0, cb.from_user.id))
    await settings(cb, state)


@router.callback_query(F.data == "set_hw")
async def set_hw(cb: CallbackQuery):
    await edit(
        cb,
        "Во сколько напоминать о домашке на завтра? (второе напоминание придёт в 21:00, если не отметишь)",
        kb([(f"{h}:00", f"h:{h}") for h in (14, 16, 18, 19)], [("⬅️ Назад", "set")]),
    )


@router.callback_query(F.data == "set_goal")
async def set_goal(cb: CallbackQuery):
    await edit(cb, "Сколько воды в день?", kb([(f"{g / 1000:g} л".replace(".", ","), f"g:{g}") for g in (1500, 2000, 2500, 3000)], [("⬅️ Назад", "set")]))


@router.callback_query(F.data == "set_rem")
async def set_rem(cb: CallbackQuery):
    await edit(cb, "За сколько минут до урока писать?", kb([(f"{r} мин", f"r:{r}") for r in (5, 10, 15)], [("⬅️ Назад", "set")]))


@router.callback_query(F.data.startswith(("g:", "r:", "h:")))
async def set_value(cb: CallbackQuery, state: FSMContext):
    key, val = cb.data.split(":")
    col = {"g": "goal", "r": "remind", "h": "hw_hour"}[key]
    db.execute(f"UPDATE users SET {col}=? WHERE user_id=?", (int(val), cb.from_user.id))
    await settings(cb, state)


TZS = ["Asia/Almaty", "Europe/Moscow", "Europe/Kyiv", "Europe/Minsk", "Asia/Tashkent", "Asia/Yekaterinburg"]

CITY_TZ = {
    **dict.fromkeys(
        ["алматы", "астана", "нур-султан", "шымкент", "караганда", "павлодар", "семей", "усть-каменогорск",
         "оскемен", "петропавловск", "кокшетау", "талдыкорган", "тараз", "туркестан", "экибастуз"],
        "Asia/Almaty",
    ),
    "актобе": "Asia/Aqtobe", "актау": "Asia/Aqtau", "атырау": "Asia/Atyrau", "уральск": "Asia/Oral",
    "кызылорда": "Asia/Qyzylorda", "костанай": "Asia/Qostanay",
    **dict.fromkeys(["москва", "санкт-петербург", "питер", "казань", "нижний новгород", "воронеж"], "Europe/Moscow"),
    "самара": "Europe/Samara", "екатеринбург": "Asia/Yekaterinburg", "омск": "Asia/Omsk",
    "новосибирск": "Asia/Novosibirsk", "красноярск": "Asia/Krasnoyarsk", "иркутск": "Asia/Irkutsk",
    "владивосток": "Asia/Vladivostok", "киев": "Europe/Kyiv", "минск": "Europe/Minsk",
    **dict.fromkeys(["ташкент", "самарканд"], "Asia/Tashkent"),
    "бишкек": "Asia/Bishkek", "ош": "Asia/Bishkek", "баку": "Asia/Baku", "тбилиси": "Asia/Tbilisi",
    "ереван": "Asia/Yerevan", "душанбе": "Asia/Dushanbe", "ашхабад": "Asia/Ashgabat",
    "стамбул": "Europe/Istanbul", "берлин": "Europe/Berlin", "лондон": "Europe/London", "париж": "Europe/Paris",
    "варшава": "Europe/Warsaw", "дубай": "Asia/Dubai", "нью-йорк": "America/New_York", "пекин": "Asia/Shanghai",
}


def zone_by_name(text):
    q = text.strip()
    try:
        ZoneInfo(q)
        return q  # человек сразу написал название зоны вроде Asia/Almaty
    except Exception:
        pass
    key = q.lower().replace("ё", "е")
    if key in CITY_TZ:
        return CITY_TZ[key]
    for z in available_timezones():  # английское название: Almaty, Bishkek, New York
        if z.split("/")[-1].lower().replace("_", " ") == key:
            return z
    return None


async def zone_for_city(text):
    z = zone_by_name(text)
    if z or not GEMINI_KEY:
        return z
    try:  # незнакомый город спрашиваем у Gemini
        ans = await gemini(
            [{"text": f"Определи IANA-часовой пояс города «{text.strip()[:60]}» (если их несколько, основной). "
                      "Ответь только названием зоны, например Asia/Almaty, или UNKNOWN."}]
        )
        ans = ans.split()[0].strip("`'\".,")
        ZoneInfo(ans)
        return ans
    except Exception:
        return None


@router.callback_query(F.data == "set_tz")
async def set_tz(cb: CallbackQuery):
    rows = [[(z.split("/")[1], f"tz:{z}")] for z in TZS]
    await edit(
        cb, "Выбери из списка или введи свой город:", kb(*rows, [("✏️ Ввести город", "tz_other")], [("⬅️ Назад", "set")])
    )


@router.callback_query(F.data.startswith("tz:"))
async def tz_pick(cb: CallbackQuery, state: FSMContext):
    db.execute("UPDATE users SET tz=? WHERE user_id=?", (cb.data[3:], cb.from_user.id))
    await settings(cb, state)


@router.callback_query(F.data == "tz_other")
async def tz_other(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.tz)
    await edit(cb, "Напиши название своего города, например: Алматы, Актобе или Bishkek", kb([("❌ Отмена", "set")]))


@router.message(S.tz)
async def tz_in(m: Message, state: FSMContext):
    z = await zone_for_city(m.text or "")
    if not z:
        return await m.answer("Не нашёл такой город. Попробуй написать по-английски (Almaty) или ближайший крупный город.")
    db.execute("UPDATE users SET tz=? WHERE user_id=?", (z, m.from_user.id))
    await state.clear()
    local = datetime.now(ZoneInfo(z)).strftime("%H:%M")
    await m.answer(f"✅ Часовой пояс: {z}\nСейчас там {local}. Если время не совпадает, введи другой город.", reply_markup=MAIN_KB)


# ---------- домашка ----------
HW_EVENING = 21
SEP = " :,.-–—\t"


def fmt(d):
    return f"{DAYS[d.weekday()]} {d:%d.%m}"


def deadline_text(lesson_d, today):
    if lesson_d <= today:
        return "урок уже сегодня или прошёл"
    do_day = lesson_d - timedelta(days=1)
    label = "сегодня" if do_day == today else fmt(do_day)
    return f"сделать {label} до полуночи (до 00:00 на {DAYS[lesson_d.weekday()]})"


def subjects(uid):
    rows = db.execute("SELECT DISTINCT title FROM lessons WHERE user_id=?", (uid,)).fetchall()
    return sorted((r["title"] for r in rows), key=len, reverse=True)


def find_subject(line, titles):
    line = line.strip()
    low = line.lower()
    for t in titles:
        tl = t.lower()
        if low.startswith(tl) and (len(low) == len(tl) or not low[len(tl)].isalnum()):
            return t, line[len(t):].lstrip(SEP)
    first = re.split(r"[\s:,.\-–—]+", line, maxsplit=1)[0].lower()
    if len(first) >= 3:
        cand = {t for t in titles if t.lower().startswith(first)}
        if len(cand) == 1:
            return cand.pop(), line[len(first):].lstrip(SEP)
    return None


def holiday(uid, d):
    r = db.execute("SELECT name, source FROM holidays WHERE user_id=? AND date=?", (uid, d.isoformat())).fetchone()
    return r["name"] if r and r["source"] in ("ai", "manual") else None


def next_lesson(uid, title):
    """Ближайший будущий урок предмета с учётом выходных: (дата, время начала, пропущенные дни)."""
    now = now_of(user(uid))
    skipped = []
    for off in range(60):
        d = now.date() + timedelta(days=off)
        row = db.execute(
            "SELECT b.start FROM lessons l LEFT JOIN bells b ON b.user_id=l.user_id AND b.num=l.num "
            "WHERE l.user_id=? AND l.weekday=? AND l.title=? ORDER BY l.num LIMIT 1",
            (uid, d.weekday(), title),
        ).fetchone()
        if not row:
            continue
        name = holiday(uid, d)
        if name:
            skipped.append((d, name))
            continue
        if off == 0:  # сегодняшний урок считаем следующим, только если он ещё не начался
            if not row["start"]:
                continue
            h, mi = map(int, row["start"].split(":"))
            if now.replace(hour=h, minute=mi, second=0, microsecond=0) <= now:
                continue
        return d, row["start"], skipped
    return None


def reschedule_hw(uid):
    """Если дата урока оказалась выходной, переносим задание на следующий настоящий урок."""
    n = 0
    for r in db.execute("SELECT * FROM homework WHERE user_id=? AND done=0", (uid,)).fetchall():
        if holiday(uid, date.fromisoformat(r["lesson_date"])):
            nxt = next_lesson(uid, r["subject"])
            if nxt:
                db.execute("UPDATE homework SET lesson_date=? WHERE id=?", (nxt[0].isoformat(), r["id"]))
                n += 1
    return n


last_try = {}


async def ensure_holidays(uid, force=False):
    """Раз в неделю спрашиваем у Gemini, какие в Казахстане выходные в ближайшие 60 дней."""
    if not GEMINI_KEY:
        return False
    today = now_of(user(uid)).date()
    row = db.execute("SELECT * FROM holiday_sync WHERE user_id=?", (uid,)).fetchone()
    if (
        not force
        and row
        and datetime.fromisoformat(row["synced_at"]) > datetime.now() - timedelta(days=7)
        and date.fromisoformat(row["until"]) >= today + timedelta(days=30)
    ):
        return True
    if not force and time.time() - last_try.get(uid, 0) < 600:
        return False
    last_try[uid] = time.time()
    end = today + timedelta(days=60)
    prompt = (
        f"Перечисли нерабочие праздничные дни и официально перенесённые выходные дни в Казахстане "
        f"с {today} по {end} включительно (по производственному календарю). Не включай обычные субботы и "
        "воскресенья, если они не связаны с праздником или переносом. Если не уверен в дате, не включай её. "
        'Ответь только JSON-массивом вида [{"date": "YYYY-MM-DD", "name": "Название"}]. Если таких дней нет, верни [].'
    )
    try:
        items = json.loads(await gemini([{"text": prompt}], json_mode=True))
        if isinstance(items, dict):
            items = items.get("holidays") or next(iter(items.values()), [])
    except Exception:
        logging.exception("holiday sync failed")
        return False
    rows = []
    for it in items:
        try:
            d = date.fromisoformat(str(it["date"]))
            if today <= d <= end:
                rows.append((uid, d.isoformat(), str(it.get("name") or "Выходной")[:60]))
        except Exception:
            continue
    db.execute(
        "DELETE FROM holidays WHERE user_id=? AND source='ai' AND date BETWEEN ? AND ?",
        (uid, today.isoformat(), end.isoformat()),
    )
    db.executemany("INSERT OR IGNORE INTO holidays VALUES(?,?,?,'ai')", rows)
    db.execute("INSERT OR REPLACE INTO holiday_sync VALUES(?,?,?)", (uid, datetime.now().isoformat(), end.isoformat()))
    reschedule_hw(uid)
    return True


async def add_hw(uid, title, text):
    await ensure_holidays(uid)
    nxt = next_lesson(uid, title)
    if not nxt:
        return f"⚠️ Не нашёл следующий урок «{title}» в расписании на ближайшие два месяца, задание не сохранил."
    d, start, skipped = nxt
    db.execute(
        "INSERT INTO homework(user_id, subject, text, lesson_date) VALUES(?,?,?,?)", (uid, title, text, d.isoformat())
    )
    today = now_of(user(uid)).date()
    when = fmt(d) + (f", {start}" if start else "")
    dl = deadline_text(d, today)
    msg = f"✅ {title}: {text}\n📅 Следующий урок: {when}\n⏰ {dl[0].upper() + dl[1:]}"
    if skipped:
        msg += "\n🚫 Пропустил выходные: " + ", ".join(f"{fmt(x)} ({n})" for x, n in skipped)
        msg += "\nЕсли это не так, поправь в Настройки → Выходные."
    return msg


def hw_screen(uid):
    today = now_of(user(uid)).date()
    rows = db.execute(
        "SELECT * FROM homework WHERE user_id=? AND done=0 ORDER BY lesson_date, id", (uid,)
    ).fetchall()
    if not rows:
        return (
            "📚 Невыполненной домашки нет 🎉\n\nЧтобы записать задание, просто напиши мне: Математика №12–15",
            kb([MENU]),
        )
    parts, buttons, cur = ["📚 Домашка"], [], None
    for r in rows:
        d = date.fromisoformat(r["lesson_date"])
        if d != cur:
            cur = d
            parts.append(f"\n📌 Урок {fmt(d)}: {deadline_text(d, today)}")
        parts.append(f"• {r['subject']}: {r['text']}")
        buttons.append([(f"✅ {r['subject']}: {r['text'][:25]}", f"hd:{r['id']}")])
    return "\n".join(parts), kb(*buttons[:20], [MENU])


@router.callback_query(F.data == "hw")
async def hw(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = hw_screen(cb.from_user.id)
    await edit(cb, text, markup)


@router.callback_query(F.data.startswith("hd:"))
async def hw_done(cb: CallbackQuery):
    db.execute("UPDATE homework SET done=1 WHERE id=? AND user_id=?", (int(cb.data[3:]), cb.from_user.id))
    text, markup = hw_screen(cb.from_user.id)
    await edit(cb, text, markup)


HW_AFTER = kb([("📚 Вся домашка", "hw")], [MENU])


@router.callback_query(F.data.startswith("hq:"))
async def hw_ask_yes(cb: CallbackQuery, state: FSMContext):
    """Кнопка «Записать задание» под вопросом после урока: дальше работает обычная запись домашки."""
    _, wd, num = cb.data.split(":")
    row = db.execute(
        "SELECT title FROM lessons WHERE user_id=? AND weekday=? AND num=?", (cb.from_user.id, int(wd), int(num))
    ).fetchone()
    if not row:
        return await cb.answer("Не нашёл этот урок в расписании", show_alert=True)
    await state.set_state(S.hw_task)
    await state.update_data(subj=row["title"])
    await edit(cb, f"✍️ Что задали по предмету «{row['title']}»? Напиши одним сообщением.", kb([("❌ Отмена", "menu")]))


@router.callback_query(F.data == "hqn")
async def hw_ask_no(cb: CallbackQuery):
    await edit(cb, "Хорошо, ничего не записываю 👍", kb([MENU]))


@router.message(S.hw_task)
async def hw_task_in(m: Message, state: FSMContext):
    title = (await state.get_data())["subj"]
    await state.clear()
    await m.answer(await add_hw(m.from_user.id, title, (m.text or "").strip()), reply_markup=HW_AFTER)


@router.message(StateFilter(None), F.text)
async def free_text(m: Message, state: FSMContext):
    """Любой текст вида «Математика №12–15» = запись домашки."""
    uid = m.from_user.id
    titles = subjects(uid)
    if not titles:
        return await m.answer("Сначала добавь расписание: 📅 Расписание → Уроки по дням", reply_markup=MAIN_KB)
    items = []
    for line in m.text.splitlines():
        if not line.strip():
            continue
        found = find_subject(line, titles)
        if found:
            items.append([found[0], found[1].strip()])
        elif items:
            items[-1][1] += "\n" + line.strip()
        else:
            return await m.answer(
                "Не понял, по какому предмету. Начни сообщение с названия предмета из расписания "
                f"({', '.join(sorted(titles))}), например: Математика №12–15",
                reply_markup=MAIN_KB,
            )
    if len(items) == 1 and not items[0][1]:
        await state.set_state(S.hw_task)
        await state.update_data(subj=items[0][0])
        return await m.answer(f"Что задали по предмету «{items[0][0]}»?", reply_markup=kb([("❌ Отмена", "menu")]))
    replies = [await add_hw(uid, t, txt) for t, txt in items if txt]
    if not replies:
        return await m.answer("Напиши и предмет, и задание, например: Математика №12–15")
    await m.answer("\n\n".join(replies), reply_markup=HW_AFTER)


async def hw_remind(bot: Bot, u, now):
    """Напоминаем накануне урока: задание нужно сделать до полуночи."""
    uid = u["user_id"]
    tomorrow = (now.date() + timedelta(days=1)).isoformat()
    for tag, hour in (("m", u["hw_hour"]), ("e", HW_EVENING)):
        if tag == "e" and u["hw_hour"] >= HW_EVENING:
            continue
        if now.hour != hour:
            continue
        key = (uid, now.date().isoformat(), "hw" + tag)
        if key in sent:
            continue
        tasks = db.execute(
            "SELECT * FROM homework WHERE user_id=? AND done=0 AND lesson_date=? ORDER BY id", (uid, tomorrow)
        ).fetchall()
        if not tasks:
            continue
        sent.add(key)
        head = "📚 Домашка на завтра. Сделай до 00:00:" if tag == "m" else "⏰ Скоро полночь, а домашка не отмечена:"
        text = head + "\n" + "\n".join(f"• {t['subject']}: {t['text']}" for t in tasks)
        markup = kb(*[[(f"✅ {t['subject']}", f"hd:{t['id']}")] for t in tasks], [("📚 Вся домашка", "hw")])
        try:
            await bot.send_message(uid, text, reply_markup=markup)
        except Exception:
            logging.exception("hw send failed")


# ---------- Gemini: помощь с заданиями и праздники ----------
async def gemini(parts, system=None, json_mode=False):
    if not GEMINI_KEY:
        raise RuntimeError("GEMINI_API_KEY не задан")
    body = {"contents": [{"role": "user", "parts": parts}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if json_mode:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
        async with s.post(url, json=body, headers={"x-goog-api-key": GEMINI_KEY}) as r:
            data = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"{r.status}: {data.get('error', {}).get('message', data)}")
    try:
        text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError):
        raise RuntimeError("пустой ответ от Gemini")
    return text.strip()


def tutor_prompt(grade, mode):
    who = f"ученику {grade} класса школы в Казахстане" if grade else "школьнику"
    base = (
        f"Ты школьный репетитор. Помоги {who} с заданием на фото. "
        "Отвечай строго на том же языке, на котором написано задание на фото (русский, казахский, английский и т. д.), "
        "включая пояснения и итоговый ответ. Если язык определить нельзя, отвечай на русском. "
        "Пиши простым текстом без Markdown и без LaTeX, формулы записывай обычными символами (x^2, 3/4, √). "
    )
    if mode == "solve":
        return base + "Дай полное решение и чётко выдели итоговый ответ. Без лишних рассуждений."
    return base + (
        "Объясни решение по шагам на уровне этого класса: что делаем, почему так, какое правило применяем. "
        "В конце напиши ответ и подскажи, как себя проверить, чтобы ученик понял и смог повторить сам."
    )


pend = {}


@router.message(F.photo)
async def photo(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    pend[uid] = {"file_id": m.photo[-1].file_id, "caption": m.caption or ""}
    g = user(uid)["grade"]
    note = f"Класс: {g}" if g else "Класс не указан (⚙️ Настройки → 🎓 Класс), так ответ будет точнее"
    await m.answer(
        f"📷 Задание получил. {note}.\nКак помочь?",
        reply_markup=kb([("✅ Только решение", "ai:solve")], [("📖 С объяснением по шагам", "ai:explain")], [("❌ Отмена", "menu")]),
    )


@router.callback_query(F.data.in_({"ai:solve", "ai:explain"}))
async def ai_run(cb: CallbackQuery):
    uid = cb.from_user.id
    p = pend.get(uid)
    if not p:
        return await cb.answer("Пришли фото задания заново", show_alert=True)
    if not GEMINI_KEY:
        return await edit(cb, "Не задан ключ GEMINI_API_KEY на хостинге, см. README.", kb([MENU]))
    mode = cb.data.split(":")[1]
    try:
        await cb.message.edit_text("⏳ Решаю, это может занять до минуты…")
    except TelegramBadRequest:
        pass
    await cb.answer()
    try:
        buf = io.BytesIO()
        await cb.bot.download(p["file_id"], destination=buf)
        parts = [
            {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}},
            {"text": "Реши задание на фото." + (f" Комментарий ученика: {p['caption']}" if p["caption"] else "")},
        ]
        answer = await gemini(parts, system=tutor_prompt(user(uid)["grade"], mode))
    except Exception as e:
        logging.exception("gemini")
        msg = "Лимит бесплатных запросов исчерпан, попробуй позже." if "429" in str(e) else "Не получилось получить ответ от ИИ, попробуй ещё раз позже."
        return await cb.message.answer("⚠️ " + msg, reply_markup=kb([MENU]))
    other = ("📖 Объяснить по шагам", "ai:explain") if mode == "solve" else ("✅ Только решение", "ai:solve")
    chunks = [answer[i : i + 4000] for i in range(0, len(answer), 4000)] or ["ИИ ничего не ответил."]
    for i, c in enumerate(chunks):
        await cb.message.answer(c, reply_markup=kb([other], [MENU]) if i == len(chunks) - 1 else None)


@router.callback_query(F.data == "set_grade")
async def set_grade(cb: CallbackQuery):
    rows = [[(str(g), f"gr:{g}") for g in range(s, min(s + 4, 12))] for s in (1, 5, 9)]
    await edit(cb, "В каком ты классе?", kb(*rows, [("⬅️ Назад", "set")]))


@router.callback_query(F.data.startswith("gr:"))
async def grade_pick(cb: CallbackQuery, state: FSMContext):
    db.execute("UPDATE users SET grade=? WHERE user_id=?", (int(cb.data[3:]), cb.from_user.id))
    await settings(cb, state)


# ---------- выходные и каникулы ----------
def hol_screen(uid):
    today = now_of(user(uid)).date().isoformat()
    rows = db.execute(
        "SELECT * FROM holidays WHERE user_id=? AND date>=? AND source IN ('ai','manual') ORDER BY date LIMIT 12",
        (uid, today),
    ).fetchall()
    lines = [
        f"{'🤖' if r['source'] == 'ai' else '✋'} {fmt(date.fromisoformat(r['date']))}: {r['name']}" for r in rows
    ] or ["пока пусто"]
    text = "🚫 Ближайшие выходные и каникулы\n\n" + "\n".join(lines)
    text += "\n\n🤖 нашёл ИИ, ✋ добавлено вручную. Домашка не ставится на эти дни."
    if not GEMINI_KEY:
        text += "\nЧтобы бот сам искал праздники, задай GEMINI_API_KEY."
    dels = [(f"❌ {date.fromisoformat(r['date']):%d.%m}", f"hdel:{r['date']}") for r in rows]
    del_rows = [dels[i : i + 3] for i in range(0, len(dels), 3)]
    return text, kb([("➕ Добавить", "hol_add"), ("🔄 Обновить через ИИ", "hol_sync")], *del_rows, [("⬅️ Назад", "set")])


@router.callback_query(F.data == "set_hol")
async def set_hol(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_holidays(cb.from_user.id)
    await edit(cb, *hol_screen(cb.from_user.id))


@router.callback_query(F.data == "hol_sync")
async def hol_sync(cb: CallbackQuery):
    await ensure_holidays(cb.from_user.id, force=True)
    await edit(cb, *hol_screen(cb.from_user.id))


@router.callback_query(F.data.startswith("hdel:"))
async def hol_del(cb: CallbackQuery):
    uid, d = cb.from_user.id, cb.data[5:]
    r = db.execute("SELECT source FROM holidays WHERE user_id=? AND date=?", (uid, d)).fetchone()
    if r and r["source"] == "ai":  # помечаем как «рабочий день», чтобы ИИ не вернул его при обновлении
        db.execute("UPDATE holidays SET source='off' WHERE user_id=? AND date=?", (uid, d))
    elif r:
        db.execute("DELETE FROM holidays WHERE user_id=? AND date=?", (uid, d))
    await edit(cb, *hol_screen(uid))


@router.callback_query(F.data == "hol_add")
async def hol_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.hol)
    await edit(
        cb,
        "Напиши дату или период, можно с названием:\n27.10\n27.10-02.11 Каникулы",
        kb([("❌ Отмена", "set_hol")]),
    )


DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?")


def parse_hol(text, today):
    ms = list(DATE_RE.finditer(text))
    if not ms:
        return None

    def mk(m):
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        year = (int(y) + (2000 if int(y) < 100 else 0)) if y else today.year
        x = date(year, mo, d)
        if not y and x < today - timedelta(days=30):
            x = date(year + 1, mo, d)
        return x

    a = mk(ms[0])
    b = mk(ms[1]) if len(ms) > 1 else a
    if b < a or (b - a).days > 120:
        return None
    name = text[ms[-1].end() :].strip(" -–—,:") or ("Каникулы" if b != a else "Выходной")
    return a, b, name[:60]


@router.message(S.hol)
async def hol_in(m: Message, state: FSMContext):
    uid = m.from_user.id
    try:
        parsed = parse_hol(m.text or "", now_of(user(uid)).date())
    except ValueError:
        parsed = None
    if not parsed:
        return await m.answer("Не понял дату. Пример: 27.10 или 27.10-02.11 Каникулы")
    a, b, name = parsed
    for i in range((b - a).days + 1):
        db.execute(
            "INSERT OR REPLACE INTO holidays VALUES(?,?,?,'manual')", (uid, (a + timedelta(days=i)).isoformat(), name)
        )
    moved = reschedule_hw(uid)
    await state.clear()
    text, markup = hol_screen(uid)
    await m.answer((f"✅ Сохранил. Перенёс заданий: {moved}\n\n" if moved else "✅ Сохранил\n\n") + text, reply_markup=markup)


# ---------- чек-лист дня ----------
def checklist_screen(uid):
    u = user(uid)
    today = now_of(u).date()
    day, tomorrow = today.isoformat(), (today + timedelta(days=1)).isoformat()
    total, goal = water_total(uid), u["goal"]
    hw_left = db.execute(
        "SELECT COUNT(*) FROM homework WHERE user_id=? AND done=0 AND lesson_date<=?", (uid, tomorrow)
    ).fetchone()[0]
    habits = db.execute(
        "SELECT h.id, h.title, (SELECT 1 FROM habit_log g WHERE g.habit_id=h.id AND g.user_id=h.user_id AND g.day=?) AS done "
        "FROM habits h WHERE h.user_id=? ORDER BY h.id",
        (day, uid),
    ).fetchall()
    water_ok, hw_ok = total >= goal, hw_left == 0
    done = int(water_ok) + int(hw_ok) + sum(1 for h in habits if h["done"])
    count = 2 + len(habits)
    lines = [
        f"{'✅' if water_ok else '⬜'} Вода: {total / 1000:.1f} / {goal / 1000:.1f} л".replace(".", ","),
        f"{'✅' if hw_ok else '⬜'} Домашка на завтра и долги: " + ("всё сделано" if hw_ok else f"осталось {hw_left}"),
    ] + [f"{'✅' if h['done'] else '⬜'} {h['title']}" for h in habits]
    text = f"✅ Чек-лист на {fmt(today)}\n\nВыполнено {done} из {count}\n\n" + "\n".join(lines)
    if not habits:
        text += "\n\nДобавь свои пункты, например: зарядка, чтение 20 минут, собрать рюкзак."
    rows = [[(f"{'✅' if h['done'] else '⬜'} {h['title'][:30]}", f"ck:{h['id']}")] for h in habits]
    return text, kb(
        *rows,
        [("💧 Вода", "water"), ("📚 Домашка", "hw")],
        [("➕ Пункт", "ck_add"), ("🗑 Убрать", "ck_del")],
        [MENU],
    )


@router.callback_query(F.data == "check")
async def check(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cb, *checklist_screen(cb.from_user.id))


@router.callback_query(F.data.startswith("ck:"))
async def check_toggle(cb: CallbackQuery):
    uid, hid = cb.from_user.id, int(cb.data[3:])
    day = now_of(user(uid)).date().isoformat()
    if db.execute("SELECT 1 FROM habit_log WHERE user_id=? AND habit_id=? AND day=?", (uid, hid, day)).fetchone():
        db.execute("DELETE FROM habit_log WHERE user_id=? AND habit_id=? AND day=?", (uid, hid, day))
    elif db.execute("SELECT 1 FROM habits WHERE id=? AND user_id=?", (hid, uid)).fetchone():
        db.execute("INSERT INTO habit_log VALUES(?,?,?)", (uid, hid, day))
    await edit(cb, *checklist_screen(uid))


@router.callback_query(F.data == "ck_add")
async def check_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.ck)
    await edit(
        cb,
        "Напиши пункт, который нужно отмечать каждый день. Можно несколько, по строке на пункт.",
        kb([("❌ Отмена", "check")]),
    )


@router.message(S.ck)
async def check_add_in(m: Message, state: FSMContext):
    uid = m.from_user.id
    titles = [t.strip()[:60] for t in (m.text or "").splitlines() if t.strip()]
    if not titles:
        return await m.answer("Напиши текст пункта, например: Зарядка")
    for t in titles[:10]:
        db.execute("INSERT INTO habits(user_id, title) VALUES(?,?)", (uid, t))
    await state.clear()
    text, markup = checklist_screen(uid)
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data == "ck_del")
async def check_del(cb: CallbackQuery):
    habits = db.execute("SELECT id, title FROM habits WHERE user_id=? ORDER BY id", (cb.from_user.id,)).fetchall()
    if not habits:
        return await edit(cb, "Своих пунктов пока нет.", kb([("⬅️ Назад", "check")]))
    rows = [[(f"❌ {h['title'][:30]}", f"ckd:{h['id']}")] for h in habits]
    await edit(cb, "Какой пункт убрать?", kb(*rows, [("⬅️ Назад", "check")]))


@router.callback_query(F.data.startswith("ckd:"))
async def check_del_one(cb: CallbackQuery):
    uid, hid = cb.from_user.id, int(cb.data[4:])
    db.execute("DELETE FROM habits WHERE id=? AND user_id=?", (hid, uid))
    db.execute("DELETE FROM habit_log WHERE habit_id=? AND user_id=?", (hid, uid))
    await edit(cb, *checklist_screen(uid))


# ---------- напоминания об уроках ----------
sent = set()


async def check_reminders(bot: Bot):
    for u in db.execute("SELECT * FROM users").fetchall():
        now = now_of(u)
        rows = db.execute(
            "SELECT l.num, l.title, b.start, b.end FROM lessons l "
            "JOIN bells b ON b.user_id=l.user_id AND b.num=l.num "
            "WHERE l.user_id=? AND l.weekday=?",
            (u["user_id"], now.weekday()),
        ).fetchall()
        await ensure_holidays(u["user_id"])
        if holiday(u["user_id"], now.date()):
            rows = []  # в выходной об уроках не напоминаем
        for r in rows:
            h, mi = map(int, r["start"].split(":"))
            start = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            if start - timedelta(minutes=u["remind"]) <= now < start:
                key = (u["user_id"], now.date().isoformat(), r["num"])
                if key in sent:
                    continue
                sent.add(key)
                mins = max(1, -(-int((start - now).total_seconds()) // 60))
                try:
                    await bot.send_message(
                        u["user_id"], f"🔔 Через {mins} мин урок №{r['num']}: {r['title']}\n🕐 {r['start']}–{r['end']}"
                    )
                except Exception:
                    logging.exception("send failed")
        if u["ask_hw"] != 0:  # после последнего урока предмета за день спрашиваем, что задали
            for r in rows:
                if any(x["title"] == r["title"] and x["num"] > r["num"] for x in rows):
                    continue
                eh, em = map(int, r["end"].split(":"))
                end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
                if not end <= now < end + timedelta(minutes=20):
                    continue
                key = (u["user_id"], now.date().isoformat(), r["title"], "after")
                if key in sent:
                    continue
                sent.add(key)
                try:
                    await bot.send_message(
                        u["user_id"],
                        f"📝 Урок «{r['title']}» закончился. Что задали?\n"
                        "Нажми кнопку или просто напиши задание, например: " + f"{r['title']} №12–15",
                        reply_markup=kb(
                            [("✍️ Записать задание", f"hq:{now.weekday()}:{r['num']}")],
                            [("🙅 Ничего не задали", "hqn")],
                        ),
                    )
                except Exception:
                    logging.exception("after-lesson send failed")
        await hw_remind(bot, u, now)
    if len(sent) > 2000:
        sent.clear()


async def reminder_loop(bot: Bot):
    while True:
        try:
            await check_reminders(bot)
        except Exception:
            logging.exception("reminder loop")
        await asyncio.sleep(20)


async def main():
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise SystemExit("Задай переменную окружения BOT_TOKEN")
    bot = Bot(TOKEN)
    try:  # список команд в меню Telegram
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Главное меню"),
                BotCommand(command="help", description="Помощь и связь с администратором"),
                BotCommand(command="id", description="Мой Telegram ID"),
            ]
        )
    except Exception:
        logging.exception("set_my_commands failed")
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    task = asyncio.create_task(reminder_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
