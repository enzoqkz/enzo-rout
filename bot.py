import ast
import asyncio
import base64
import difflib
import html
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
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "Asia/Almaty")
DB_PATH = os.getenv("DB_PATH", "bot.db")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_FALLBACK = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-flash-lite-latest")  # запасная модель при перегрузке
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
def btn_style(text, data):
    """Цвет кнопки: danger = красная, success = зелёная, primary = синяя, None = обычная."""
    if data.startswith(("ckd:", "hdel:", "hqn")) or text.startswith(("❌", "🗑", "🙅", "🏁", "↩️")):
        return "danger"
    if text.startswith(("✅", "➕", "✍️")):
        return "success"
    if data == "menu" or text.startswith(("⬅️", "🏠")):
        return None
    if data.startswith(
        ("w:", "w_custom", "ai:", "nl:", "quiz", "notes", "hw_all", "bmi_new", "hol_", "ck_", "s_", "support", "tz_other")
    ):
        return "primary"
    return None


def kb(*rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d, style=btn_style(t, d)) for t, d in row] for row in rows
        ]
    )


MENU = ("🏠 Меню", "menu")
def _rk(*rows):
    return [[KeyboardButton(text=t) for t in row] for row in rows]


MAIN_REPLY = ReplyKeyboardMarkup(
    keyboard=_rk(
        ["💧 Вода", "⚖️ ИМТ", "📅 Расписание"],
        ["📚 Домашка", "📝 Конспект", "✅ Чек-лист"],
        ["⚙️ Настройки", "❓ Помощь"],
    ),
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Домашка: Математика №12–15",
)
MAIN_KB = kb(
    [("💧 Вода", "water"), ("⚖️ ИМТ", "bmi")],
    [("📅 Расписание", "sched"), ("📚 Домашка", "hw")],
    [("📝 Конспект", "notes"), ("✅ Чек-лист", "check")],
    [("⚙️ Настройки", "set"), ("❓ Помощь", "help")],
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
    notes = State()
    quiz = State()


# ---------- старт и меню ----------
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    user(m.from_user.id)
    await m.answer(
        "Привет! Кнопки меню теперь внизу экрана 👇\n\nСначала укажи свой город: ⚙️ Настройки → 🌍 Часовой пояс.\n\nЧтобы записать домашку, просто напиши мне после урока, "
        "например: Математика №12–15",
        reply_markup=MAIN_REPLY,
    )


@router.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cb, "🏠 Главное меню: кнопки внизу экрана 👇", None)


# ---------- кнопки внизу экрана ----------
@router.message(F.text == "💧 Вода")
async def bt_water(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(water_text(m.from_user.id), reply_markup=WATER_KB)


@router.message(F.text == "⚖️ ИМТ")
async def bt_bmi(m: Message, state: FSMContext):
    await state.clear()
    u = user(m.from_user.id)
    if u["height"] and u["weight"]:
        return await m.answer(bmi_text(u["height"], u["weight"]), reply_markup=kb([("🔄 Пересчитать", "bmi_new")], [MENU]))
    await state.set_state(S.height)
    await m.answer("Введи рост в см (например 175)", reply_markup=kb([("❌ Отмена", "menu")]))


@router.message(F.text == "📅 Расписание")
async def bt_sched(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("📅 Расписание", reply_markup=SCHED_KB)


@router.message(F.text == "📚 Домашка")
async def bt_hw(m: Message, state: FSMContext):
    await state.clear()
    text, markup = hw_screen(m.from_user.id)
    await m.answer(text, reply_markup=markup)


@router.message(F.text == "📝 Конспект")
async def bt_notes(m: Message, state: FSMContext):
    await state.clear()
    if not GEMINI_KEY:
        return await m.answer("Не задан ключ GEMINI_API_KEY на хостинге, см. README.")
    notes_ids[m.from_user.id] = []
    notes_group.pop(m.from_user.id, None)
    await state.set_state(S.notes)
    await m.answer(
        "📝 Конспект\n\nПришли фото параграфа: можно сразу несколько страниц (до 10). Когда всё пришлёшь, нажми «Готово».",
        reply_markup=kb([("❌ Отмена", "menu")]),
    )


@router.message(F.text == "✅ Чек-лист")
async def bt_check(m: Message, state: FSMContext):
    await state.clear()
    text, markup = checklist_screen(m.from_user.id)
    await m.answer(text, reply_markup=markup)


@router.message(F.text == "⚙️ Настройки")
async def bt_settings(m: Message, state: FSMContext):
    await state.clear()
    text, markup = settings_screen(m.from_user.id)
    await m.answer(text, reply_markup=markup)


@router.message(F.text == "❓ Помощь")
async def bt_help(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(HELP_TEXT, reply_markup=HELP_KB)


# ---------- помощь, ID и связь с администратором ----------
HELP_TEXT = (
    "❓ Что умеет бот\n\n"
    "💧 Вода: считает, сколько ты выпил за день.\n"
    "📅 Расписание: звонки и уроки по дням, напоминания перед уроком.\n"
    "📚 Домашка: просто напиши «Математика №12–15», бот найдёт следующий урок.\n"
    "📷 Фото задания: пришли фото и выбери «Только решение» или «С объяснением по шагам». "
    "ИИ ответит на том же языке, что и задание. Если в задании нужен график, бот его нарисует.\n"
    "📝 Конспект: пришли фото параграфа (можно несколько), выбери длину, потом ответь на 5 вопросов по теме.\n"
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
def settings_screen(uid):
    u = user(uid)
    text = (
        f"⚙️ Настройки\n\nНорма воды: {u['goal']} мл\nНапоминание об уроке: за {u['remind']} мин\n"
        f"Напоминание о домашке: в {u['hw_hour']}:00 и в 21:00\nКласс: {u['grade'] or 'не указан'}\n"
        f"Часовой пояс: {u['tz']}\n"
        f"Вопрос о домашке после урока: {'выключен' if u['ask_hw'] == 0 else 'включён'}"
    )
    return text, kb(
        [("💧 Норма воды", "set_goal")],
        [("⏰ Напоминание об уроке", "set_rem")],
        [("📚 Время для домашки", "set_hw")],
        [("📝 Вопрос после урока: вкл/выкл", "set_ask")],
        [("🎓 Класс", "set_grade"), ("🚫 Выходные", "set_hol")],
        [("🌍 Часовой пояс", "set_tz")],
        [MENU],
    )


@router.callback_query(F.data == "set")
async def settings(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cb, *settings_screen(cb.from_user.id))


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
    await m.answer(f"✅ Часовой пояс: {z}\nСейчас там {local}. Если время не совпадает, введи другой город.", reply_markup=MAIN_REPLY)


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


def _words(line):
    return list(re.finditer(r"[^\s:,.\-–—]+", line))


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
    # опечатки: «зимия» -> «Химия»
    ws = _words(line)
    scored = {}
    for t in titles:
        tl = t.lower().replace("ё", "е")
        for k, ref in ((len(tl.split()), tl), (1, tl.split()[0])):
            if k > len(ws):
                continue
            head = line[ws[0].start() : ws[k - 1].end()].lower().replace("ё", "е")
            if len(head) < 4:
                continue
            score = difflib.SequenceMatcher(None, head, ref).ratio()
            if score >= 0.75 and score > scored.get(t, (0, ""))[0]:
                scored[t] = (score, line[ws[k - 1].end() :].lstrip(SEP))
    if not scored:
        return None
    ranked = sorted(scored.items(), key=lambda kv: -kv[1][0])
    if len(ranked) > 1 and ranked[0][1][0] - ranked[1][1][0] < 0.05:
        return None  # неоднозначно: лучше переспросить, чем записать не туда
    return ranked[0][0], ranked[0][1][1]


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


def day_label(d, today):
    if d < today:
        return f"{fmt(d)}: урок уже был"
    if d == today:
        return f"{fmt(d)}: урок сегодня"
    if d == today + timedelta(days=1):
        return f"{fmt(d)}: на завтра"
    return fmt(d)


def hw_screen(uid, show_all=False):
    """По умолчанию только домашка на завтра. show_all=True: вся, разложена по дням."""
    today = now_of(user(uid)).date()
    tomorrow = today + timedelta(days=1)
    rows = db.execute(
        "SELECT * FROM homework WHERE user_id=? AND done=0 ORDER BY lesson_date, id", (uid,)
    ).fetchall()
    if not rows:
        return (
            "📚 Невыполненной домашки нет 🎉\n\nЧтобы записать задание, просто напиши мне: Математика №12–15",
            kb([MENU]),
        )
    if show_all:
        parts, buttons, cur = ["📚 Вся домашка по дням"], [], None
        for r in rows:
            d = date.fromisoformat(r["lesson_date"])
            if d != cur:
                cur = d
                parts.append(f"\n📌 {day_label(d, today)}")
            parts.append(f"• {r['subject']}: {r['text']}")
            buttons.append([(f"✅ {r['subject']}: {r['text'][:25]}", f"hda:{r['id']}")])
        return "\n".join(parts), kb(*buttons[:20], [("⬅️ На завтра", "hw")], [MENU])

    late = [r for r in rows if date.fromisoformat(r["lesson_date"]) <= today]
    tom = [r for r in rows if date.fromisoformat(r["lesson_date"]) == tomorrow]
    later = [r for r in rows if date.fromisoformat(r["lesson_date"]) > tomorrow]
    parts, buttons = [], []
    if tom:
        parts.append(f"📚 Домашка на завтра, {fmt(tomorrow)}\n⏰ Сделать сегодня до полуночи\n")
        parts += [f"• {r['subject']}: {r['text']}" for r in tom]
    else:
        parts.append(f"📚 На завтра, {fmt(tomorrow)}, домашки нет 🎉")
        if later:
            first = later[0]["lesson_date"]
            n = sum(1 for r in later if r["lesson_date"] == first)
            parts.append(f"\nБлижайшая: {fmt(date.fromisoformat(first))}, заданий: {n}")
    if late:
        parts.append("\n⚠️ Не отмечено, урок уже был:")
        parts += [f"• {r['subject']}: {r['text']}" for r in late]
    for r in late + tom:
        buttons.append([(f"✅ {r['subject']}: {r['text'][:25]}", f"hd:{r['id']}")])
    return "\n".join(parts), kb(*buttons[:20], [("📋 Посмотреть все домашние задания", "hw_all")], [MENU])


@router.callback_query(F.data == "hw")
async def hw(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = hw_screen(cb.from_user.id)
    await edit(cb, text, markup)


@router.callback_query(F.data == "hw_all")
async def hw_all(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = hw_screen(cb.from_user.id, show_all=True)
    await edit(cb, text, markup)


@router.callback_query(F.data.startswith("hd:"))
async def hw_done(cb: CallbackQuery):
    db.execute("UPDATE homework SET done=1 WHERE id=? AND user_id=?", (int(cb.data[3:]), cb.from_user.id))
    text, markup = hw_screen(cb.from_user.id)
    await edit(cb, text, markup)


@router.callback_query(F.data.startswith("hda:"))
async def hw_done_all(cb: CallbackQuery):
    db.execute("UPDATE homework SET done=1 WHERE id=? AND user_id=?", (int(cb.data[4:]), cb.from_user.id))
    text, markup = hw_screen(cb.from_user.id, show_all=True)
    await edit(cb, text, markup)


HW_AFTER = kb([("📚 Домашка", "hw")], [MENU])


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
        return await m.answer("Сначала добавь расписание: 📅 Расписание → Уроки по дням", reply_markup=MAIN_REPLY)
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
                reply_markup=MAIN_REPLY,
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
        markup = kb(*[[(f"✅ {t['subject']}", f"hd:{t['id']}")] for t in tasks], [("📚 Вся домашка", "hw_all")])
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
    # при перегрузке (503, 500, 502, 504) повторяем запрос, потом пробуем запасную модель
    plan = [(GEMINI_MODEL, 0), (GEMINI_MODEL, 3), (GEMINI_MODEL, 6)]
    if GEMINI_FALLBACK and GEMINI_FALLBACK != GEMINI_MODEL:
        plan += [(GEMINI_FALLBACK, 2), (GEMINI_FALLBACK, 5)]
    last = None
    data = None
    for model, delay in plan:
        if delay:
            await asyncio.sleep(delay)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
                async with s.post(url, json=body, headers={"x-goog-api-key": GEMINI_KEY}) as r:
                    data = await r.json(content_type=None)
                    status = r.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last = RuntimeError(f"сеть: {e}")
            continue
        if status == 200:
            last = None
            break
        last = RuntimeError(f"{status}: {data.get('error', {}).get('message', data)}")
        if status not in (500, 502, 503, 504):
            break  # 429, 400, 404: повторять бесполезно
    if last:
        raise last
    try:
        text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError):
        raise RuntimeError("пустой ответ от Gemini")
    return text.strip()


# ---------- красивая математика: дроби столбиком, π, степени ----------
SUP = dict(zip("0123456789+-−=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ⁿⁱ"))
SUB = dict(zip("0123456789+-−=()aeoxhklmnpst", "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜ"))
CMD_SYMBOLS = {
    "pi": "π", "cdot": "·", "times": "×", "div": "÷", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "neq": "≠", "ne": "≠", "approx": "≈", "infty": "∞", "pm": "±", "mp": "∓", "to": "→", "rightarrow": "→",
    "Rightarrow": "⇒", "Leftrightarrow": "⇔", "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "Delta": "Δ",
    "theta": "θ", "lambda": "λ", "mu": "μ", "sigma": "σ", "varphi": "φ", "phi": "φ", "omega": "ω", "Omega": "Ω",
    "in": "∈", "cup": "∪", "cap": "∩", "circ": "°", "degree": "°", "ldots": "…", "dots": "…", "cdots": "…",
    "sum": "Σ", "int": "∫", "angle": "∠", "perp": "⊥", "parallel": "∥", "sin": "sin", "cos": "cos", "tan": "tg",
    "tg": "tg", "ctg": "ctg", "cot": "ctg", "log": "log", "ln": "ln", "lg": "lg", "lim": "lim", "arcsin": "arcsin",
    "arccos": "arccos", "arctan": "arctg", "arctg": "arctg", "max": "max", "min": "min",
}
PI_RE = re.compile(r"(?<![A-Za-z\\])pi(?![A-Za-z])")


class Box:
    """Кусок текста из нескольких строк; base: номер строки, на которой стоит «базовая линия»."""

    def __init__(self, rows, base=0):
        rows = rows or [""]
        w = max(len(r) for r in rows)
        self.rows = [r.ljust(w) for r in rows]
        self.base = base

    @property
    def w(self):
        return len(self.rows[0])

    @property
    def h(self):
        return len(self.rows)


def hcat(boxes):
    boxes = [b for b in boxes if b.w]
    if not boxes:
        return Box([""])
    above = max(b.base for b in boxes)
    below = max(b.h - b.base - 1 for b in boxes)
    rows = []
    for r in range(above + below + 1):
        line = ""
        for b in boxes:
            i = r - (above - b.base)
            line += b.rows[i] if 0 <= i < b.h else " " * b.w
        rows.append(line)
    return Box(rows, above)


def frac_box(n, d):
    w = max(n.w, d.w) + 2
    rows = [r.center(w) for r in n.rows] + ["─" * w] + [r.center(w) for r in d.rows]
    return Box(rows, n.h)


def _sqrt_box(g, pre):
    if g.h == 1:
        flat = g.rows[0].strip()
        if re.fullmatch(r"[\w.,]+", flat):
            return Box([pre + flat])
        return Box([pre + "(" + flat + ")"])
    return hcat([Box([pre + "("]), g, Box([")"])])


def _script(b, table, mark):
    if b.h == 1:
        flat = b.rows[0].strip()
        if flat and all(ch in table for ch in flat):
            return Box(["".join(table[ch] for ch in flat)])
        if len(flat) == 1:
            return Box([mark + flat])
        return Box([mark + "(" + flat + ")"])
    return hcat([Box([mark + "("]), b, Box([")"])])


def _group(s, i):
    while i < len(s) and s[i] == " ":
        i += 1
    if i >= len(s):
        return Box([""]), i
    if s[i] == "{":
        return _parse(s, i + 1, True)
    if s[i] == "\\":
        m = re.match(r"\\[A-Za-z]+|\\.", s[i:])
        tok = m.group(0) if m else s[i]
    else:
        tok = s[i]
    return _parse(tok)[0], i + len(tok)


def _parse(s, i=0, closing=False):
    parts, buf = [], []

    def flush():
        if buf:
            parts.append(Box(["".join(buf)]))
            buf.clear()

    n = len(s)
    while i < n:
        c = s[i]
        if c == "}":
            i += 1
            if closing:
                break
            continue
        if c == "{":
            flush()
            b, i = _parse(s, i + 1, True)
            parts.append(b)
            continue
        if c == "$":
            i += 1
            continue
        if c == "~":
            buf.append(" ")
            i += 1
            continue
        if c in "^_":
            g, i = _group(s, i + 1)
            b = _script(g, SUP if c == "^" else SUB, c)
            if b.h == 1:
                buf.append(b.rows[0])
            else:
                flush()
                parts.append(b)
            continue
        if c == "\\":
            m = re.match(r"\\([A-Za-z]+|.)", s[i:], re.S)
            if not m:
                i += 1
                continue
            name, i = m.group(1), i + len(m.group(0))
            if name in ("frac", "dfrac", "tfrac"):
                a, i = _group(s, i)
                b, i = _group(s, i)
                flush()
                parts.append(frac_box(a, b))
            elif name == "sqrt":
                idx = ""
                if i < n and s[i] == "[":
                    j = s.find("]", i)
                    if j != -1:
                        idx, i = s[i + 1 : j], j + 1
                g, i = _group(s, i)
                flush()
                parts.append(_sqrt_box(g, {"3": "∛", "4": "∜"}.get(idx, "√")))
            elif name in ("text", "mathrm", "mathbf", "mathit", "textbf", "operatorname", "overline", "vec", "boldsymbol"):
                g, i = _group(s, i)
                flush()
                parts.append(g)
            elif name in ("left", "right", "big", "Big", "displaystyle", "limits"):
                pass
            elif name in ("quad", "qquad"):
                buf.append("  ")
            elif name in (",", ";", ":", " ", "\\"):
                buf.append(" ")
            elif name == "!":
                pass
            elif len(name) == 1 and name in "{}%#_&":
                buf.append(name)
            elif name in ("(", ")", "[", "]"):
                pass
            else:
                buf.append(CMD_SYMBOLS.get(name, name))
            continue
        buf.append(c)
        i += 1
    flush()
    return hcat(parts), i


def _plain_line(line):
    line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
    line = re.sub(r"^\s*#{1,6}\s*", "", line)
    line = re.sub(r"^(\s*)\* +", r"\1• ", line)
    return PI_RE.sub("π", line)


def math_blocks(text):
    """Текст ответа ИИ -> список (html, обычный_текст). Строки с дробями становятся блоками <pre>."""
    blocks = []
    for raw in text.replace("\r", "").split("\n"):
        line = _plain_line(raw)
        if re.search(r"[\\^_]", line):
            try:
                box = _parse(line)[0]
            except Exception:
                box = Box([line])
        else:
            box = Box([line])
        rows = [PI_RE.sub("π", r.rstrip()) for r in box.rows]
        if len(rows) == 1:
            blocks.append((html.escape(rows[0], quote=False), rows[0]))
        else:
            joined = "\n".join(rows)
            blocks.append(("<pre>" + html.escape(joined, quote=False) + "</pre>", joined))
    return blocks


async def send_math(msg, text, markup=None):
    """Отправляет ответ ИИ: формулы красиво, длинный текст делится на сообщения."""
    blocks = math_blocks(text) if text and text.strip() else [("ИИ ничего не ответил.", "ИИ ничего не ответил.")]
    chunks, cur, size = [], [], 0
    for h, p in blocks:
        if cur and size + len(h) + 1 > 3500:
            chunks.append(cur)
            cur, size = [], 0
        cur.append((h, p))
        size += len(h) + 1
    chunks.append(cur)
    for i, ch in enumerate(chunks):
        mk = markup if i == len(chunks) - 1 else None
        try:
            await msg.answer("\n".join(h for h, _ in ch), parse_mode="HTML", reply_markup=mk)
        except TelegramBadRequest:
            await msg.answer("\n".join(p for _, p in ch), parse_mode=None, reply_markup=mk)


MATH_STYLE = (
    "Оформление: простой текст без Markdown (никаких **, #, ``` и таблиц). Формулы записывай в LaTeX-записи, но БЕЗ знаков $: "
    "дроби только как \\frac{числитель}{знаменатель} (даже простые числовые, например \\frac{3}{4}), корни \\sqrt{...}, "
    "степени x^{2}, индексы x_{1}, число пи только как \\pi (никогда не пиши pi буквами), умножение \\cdot, "
    "а также \\leq \\geq \\neq \\approx \\infty \\pm \\to. "
    "Не используй окружения \\begin и \\end и команды \\text, \\left, \\right. Каждый шаг вычислений пиши с новой строки."
)


def ai_error_text(e):
    if "429" in str(e):
        return "Лимит бесплатных запросов исчерпан, попробуй позже."
    if any(c in str(e) for c in ("503", "500", "502", "504")):
        return "ИИ от Google сейчас перегружен. Подожди минуту и отправь снова."
    return "Не получилось получить ответ от ИИ, попробуй ещё раз позже."


# ---------- графики ----------
GRAPH_RE = re.compile(r"<<<GRAPH(.*?)(?:GRAPH>>>|$)", re.S)
GRAPH_FUNCS = {"sin", "cos", "tan", "arcsin", "arccos", "arctan", "sqrt", "ln", "log", "log10", "exp", "abs"}


class _ToFloat(ast.NodeTransformer):
    def visit_Constant(self, node):  # все числа делаем float, чтобы 9**9**9 не повесило бота
        return ast.copy_location(ast.Constant(float(node.value)), node)


def _safe_expr(expr):
    """Разбираем формулу из ответа ИИ и пропускаем только арифметику, x, pi, e и известные функции."""
    tree = ast.parse(expr.replace("^", "**").replace("π", "pi").strip(), mode="eval")
    ok_nodes = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
                ast.USub, ast.UAdd, ast.Load)
    for n in ast.walk(tree):
        if isinstance(n, ok_nodes):
            continue
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            continue
        if isinstance(n, ast.Name) and (n.id in ("x", "pi", "e") or n.id in GRAPH_FUNCS):
            continue
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in GRAPH_FUNCS and not n.keywords:
            continue
        raise ValueError("недопустимая формула")
    tree = ast.fix_missing_locations(_ToFloat().visit(tree))
    return compile(tree, "<expr>", "eval")


def split_graph(text):
    m = GRAPH_RE.search(text)
    if not m:
        return text.strip(), None
    return (text[: m.start()] + text[m.end() :]).strip(), m.group(1)


def render_graph(spec):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    def num(v, default):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    x0, x1 = num(spec.get("xmin"), -10.0), num(spec.get("xmax"), 10.0)
    if not x0 < x1:
        x0, x1 = -10.0, 10.0
    funcs = {"sin": np.sin, "cos": np.cos, "tan": np.tan, "arcsin": np.arcsin, "arccos": np.arccos,
             "arctan": np.arctan, "sqrt": np.sqrt, "ln": np.log, "log": np.log, "log10": np.log10,
             "exp": np.exp, "abs": np.abs}
    fig, ax = plt.subplots(figsize=(7, 5), dpi=130)
    seen = []
    with np.errstate(all="ignore"):
        for f in (spec.get("functions") or [])[:6]:
            a, b = num(f.get("xmin"), x0), num(f.get("xmax"), x1)
            if not a < b:
                a, b = x0, x1
            xs = np.linspace(a, b, 2000)
            env = {"__builtins__": {}, "x": xs, "pi": np.pi, "e": np.e, **funcs}
            ys = eval(_safe_expr(str(f.get("expr", ""))), env)
            ys = np.array(np.broadcast_to(np.asarray(ys, dtype=float), xs.shape))
            ys[~np.isfinite(ys)] = np.nan
            ok = ys[np.isfinite(ys)]
            if ok.size:
                ys[np.abs(ys) > 100 * (1 + float(np.median(np.abs(ok))))] = np.nan  # хвосты у асимптот
            jump = (ys[:-1] * ys[1:] < 0) & (np.abs(ys[:-1]) > 20) & (np.abs(ys[1:]) > 20)
            ys[:-1][jump] = np.nan  # не соединяем ветви гиперболы линией через асимптоту
            label = str(f.get("label") or "").replace("$", "") or None
            ax.plot(xs, ys, linewidth=2, label=label)
            seen.append(ys[np.isfinite(ys)])
    for p in (spec.get("points") or [])[:20]:
        px, py = num(p.get("x"), None), num(p.get("y"), None)
        if px is None or py is None:
            continue
        ax.plot([px], [py], "o", color="black", zorder=5)
        if p.get("label"):
            ax.annotate(str(p["label"]).replace("$", ""), (px, py), textcoords="offset points", xytext=(6, 6), fontsize=9)
        seen.append(np.array([py]))
    for v in (spec.get("vlines") or [])[:6]:
        if num(v, None) is not None:
            ax.axvline(float(v), linestyle="--", color="gray")
    for v in (spec.get("hlines") or [])[:6]:
        if num(v, None) is not None:
            ax.axhline(float(v), linestyle="--", color="gray")
    y0, y1 = num(spec.get("ymin"), None), num(spec.get("ymax"), None)
    if y0 is None or y1 is None or not y0 < y1:
        allv = np.concatenate(seen) if seen else np.array([])
        if allv.size:
            lo, hi = float(np.percentile(allv, 0.5)), float(np.percentile(allv, 99.5))
            if hi - lo < 1e-9:
                lo, hi = lo - 1, hi + 1
            pad = (hi - lo) * 0.15
            y0, y1 = lo - pad, hi + pad
        else:
            y0, y1 = -10.0, 10.0
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.axhline(0, color="black", linewidth=1)
    ax.axvline(0, color="black", linewidth=1)
    ax.grid(True, alpha=0.3)
    ax.set_xlabel("x")
    ax.set_ylabel("y")
    title = str(spec.get("title") or "").replace("$", "")
    if title:
        ax.set_title(title)
    if ax.get_legend_handles_labels()[1]:
        ax.legend()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


async def send_graph(msg, raw, markup=None):
    try:
        a, b = raw.find("{"), raw.rfind("}")
        spec = json.loads(raw[a : b + 1])
        png = await asyncio.to_thread(render_graph, spec)
        await msg.answer_photo(BufferedInputFile(png, filename="graph.png"), caption="📈 График", reply_markup=markup)
    except ImportError:
        await msg.answer(
            "📈 Чтобы бот рисовал графики, на телефоне нужен matplotlib. В Termux: pkg install matplotlib",
            reply_markup=markup,
        )
    except Exception:
        logging.exception("graph")
        await msg.answer("⚠️ Решение есть, но график нарисовать не получилось. Попробуй ещё раз.", reply_markup=markup)


# ---------- решение заданий ----------
def tutor_prompt(grade, mode):
    who = f"ученику {grade} класса школы в Казахстане" if grade else "школьнику"
    base = (
        f"Ты школьный репетитор. Помоги {who} с заданием на фото. "
        "Отвечай строго на том же языке, на котором написано задание на фото (русский, казахский, английский и т. д.), "
        "включая пояснения и итоговый ответ. Если язык определить нельзя, отвечай на русском. " + MATH_STYLE + " "
    )
    graph = (
        " Если в задании нужно построить, нарисовать или изобразить график (функции, уравнения, неравенства, зависимости), "
        "всё равно напиши решение текстом, а в самом конце ответа добавь блок строго такого вида, без ``` и пояснений:\n"
        '<<<GRAPH\n{"title": "Название", "xmin": -5, "xmax": 5, "functions": [{"expr": "x**2 - 4*x + 3", '
        '"label": "y = x² − 4x + 3"}], "points": [{"x": 1, "y": 0, "label": "(1; 0)"}], "vlines": [], "hlines": []}\nGRAPH>>>\n'
        "В expr используй только переменную x, числа, + - * / **, скобки, функции sin cos tan arcsin arccos arctan sqrt ln "
        "log10 exp abs и константы pi и e. Для окружности и других кривых, которые не являются функцией y(x), разбей их на "
        "несколько функций (например, верхняя и нижняя половины). Для кусочных функций задай xmin и xmax внутри каждой "
        "функции. Отметь на графике ключевые точки: нули, вершину, пересечения. Если график не нужен, блок GRAPH не добавляй."
    )
    if mode == "solve":
        return base + "Дай полное решение и чётко выдели итоговый ответ. Без лишних рассуждений." + graph
    return (
        base
        + "Объясни решение по шагам на уровне этого класса: что делаем, почему так, какое правило применяем. "
        "В конце напиши ответ и подскажи, как себя проверить, чтобы ученик понял и смог повторить сам."
        + graph
    )


# ---------- конспект по фото и вопросы по теме ----------
notes_ids = {}  # uid -> file_id фотографий параграфа
notes_group = {}  # uid -> media_group_id (на альбом отвечаем один раз)
NOTE_LEN = {
    "short": ("Короткий", "Объём очень маленький: 5-8 строк, только самое главное: определения, ключевые факты, формулы."),
    "mid": ("Средний", "Объём средний: примерно 1-1,5 страницы: основные определения, правила, формулы и короткие примеры."),
    "long": ("Длинный", "Объём большой и подробный: все важные понятия, правила, формулы, примеры и связи между ними, с подзаголовками."),
}
NOTES_DONE_KB = kb([("✅ Готово", "notes_done")], [("❌ Отмена", "menu")])
QUIZ_KB = kb([("⏭ Пропустить", "quiz_skip")], [("🏁 Закончить", "menu")])


def notes_prompt(grade, rule):
    who = f"для ученика {grade} класса" if grade else "для школьника"
    return (
        "Ты школьный репетитор. На фотографиях страницы одного параграфа учебника по порядку. Прочитай все фото и составь "
        f"конспект {who}. {rule} Отвечай на языке учебника. Сначала тема, затем главные пункты в логическом порядке. "
        "Определения, правила и формулы начинай словами «Определение:», «Правило:», «Формула:». Списки делай через «•». "
        + MATH_STYLE
    )


async def photo_parts(bot, ids):
    parts = []
    for fid in ids:
        buf = io.BytesIO()
        await bot.download(fid, destination=buf)
        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}})
    return parts


@router.callback_query(F.data == "notes")
async def notes_start(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    if not GEMINI_KEY:
        return await edit(cb, "Не задан ключ GEMINI_API_KEY на хостинге, см. README.", kb([MENU]))
    notes_ids[uid] = []
    notes_group.pop(uid, None)
    await state.set_state(S.notes)
    await edit(
        cb,
        "📝 Конспект\n\nПришли фото параграфа: можно сразу несколько страниц (до 10). Когда всё пришлёшь, нажми «Готово».",
        kb([("❌ Отмена", "menu")]),
    )


@router.message(S.notes, F.photo)
async def notes_photo(m: Message, state: FSMContext):
    uid = m.from_user.id
    ids = notes_ids.setdefault(uid, [])
    if len(ids) >= 10:
        return await m.answer("Максимум 10 фото. Нажми «Готово».", reply_markup=NOTES_DONE_KB)
    ids.append(m.photo[-1].file_id)
    gid = m.media_group_id
    if gid and notes_group.get(uid) == gid:
        return  # остальные фото этого же альбома: отвечать на каждое не нужно
    notes_group[uid] = gid
    await m.answer("📷 Фото принято. Пришли ещё или нажми «Готово».", reply_markup=NOTES_DONE_KB)


@router.message(S.notes)
async def notes_other(m: Message):
    await m.answer("Пришли фото параграфа или нажми «Готово».", reply_markup=NOTES_DONE_KB)


def notes_len_kb():
    return kb(
        [("⚡ Короткий", "nl:short")],
        [("📄 Средний", "nl:mid")],
        [("📚 Длинный", "nl:long")],
        [MENU],
    )


@router.callback_query(F.data == "notes_done")
async def notes_done(cb: CallbackQuery):
    ids = notes_ids.get(cb.from_user.id)
    if not ids:
        return await cb.answer("Сначала пришли фото", show_alert=True)
    await edit(cb, f"📷 Фото: {len(ids)}. Какой конспект нужен?", notes_len_kb())


@router.callback_query(F.data == "notes_len")
async def notes_len(cb: CallbackQuery):
    if not notes_ids.get(cb.from_user.id):
        return await cb.answer("Пришли фото заново", show_alert=True)
    await cb.answer()
    await cb.message.answer("Какой конспект нужен?", reply_markup=notes_len_kb())


@router.callback_query(F.data.startswith("nl:"))
async def notes_make(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    ids = notes_ids.get(uid)
    if not ids:
        return await cb.answer("Пришли фото заново", show_alert=True)
    title, rule = NOTE_LEN.get(cb.data[3:], NOTE_LEN["mid"])
    await state.clear()
    try:
        await cb.message.edit_text("⏳ Читаю фото и пишу конспект, это может занять до минуты…")
    except TelegramBadRequest:
        pass
    await cb.answer()
    try:
        parts = await photo_parts(cb.bot, ids)
        parts.append({"text": "Составь конспект параграфа по этим фотографиям."})
        text = await gemini(parts, system=notes_prompt(user(uid)["grade"], rule))
    except Exception as e:
        logging.exception("notes")
        return await cb.message.answer("⚠️ " + ai_error_text(e), reply_markup=kb([MENU]))
    await send_math(
        cb.message,
        f"📝 Конспект ({title.lower()})\n\n{text}",
        kb([("❓ 5 вопросов по теме", "quiz")], [("🔁 Другая длина", "notes_len")], [MENU]),
    )


QUIZ_SYS = (
    "Ты школьный репетитор. По параграфу на фотографиях составь ровно 5 вопросов, чтобы проверить, что ученик понял тему: "
    "от простых к более сложным (понимание, объяснение, применение). Ответь только JSON-массивом вида "
    '[{"q": "вопрос", "a": "краткий эталонный ответ"}]. Язык как в учебнике. ' + MATH_STYLE
)
CHECK_SYS = (
    "Ты проверяешь ответ школьника на вопрос. Сравни с эталонным ответом по смыслу, не придирайся к формулировкам и опечаткам. "
    'Ответь только JSON-объектом {"verdict": "correct" | "partial" | "wrong", "comment": "1-2 коротких предложения: что верно, '
    'чего не хватает"}. Пиши на языке ответа ученика, простым текстом. ' + MATH_STYLE
)


def quiz_text(qs, i):
    return f"❓ Вопрос {i + 1} из {len(qs)}\n\n{qs[i]['q']}\n\nНапиши ответ сообщением."


@router.callback_query(F.data == "quiz")
async def quiz_start(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    ids = notes_ids.get(uid)
    if not ids:
        return await cb.answer("Пришли фото параграфа заново", show_alert=True)
    await cb.answer()
    wait = await cb.message.answer("⏳ Придумываю вопросы…")
    try:
        parts = await photo_parts(cb.bot, ids)
        parts.append({"text": "Составь 5 вопросов по этому параграфу."})
        data = json.loads(await gemini(parts, system=QUIZ_SYS, json_mode=True))
        if isinstance(data, dict):
            data = data.get("questions") or next(iter(data.values()), [])
        qs = [{"q": str(x["q"]), "a": str(x["a"])} for x in data][:5]
        if not qs:
            raise ValueError("нет вопросов")
    except Exception as e:
        logging.exception("quiz")
        return await wait.edit_text("⚠️ " + ai_error_text(e), reply_markup=kb([MENU]))
    await state.set_state(S.quiz)
    await state.update_data(qs=qs, i=0, score=0.0)
    try:
        await wait.delete()
    except TelegramBadRequest:
        pass
    await send_math(cb.message, quiz_text(qs, 0), QUIZ_KB)


async def quiz_next(msg, state, qs, i, score, feedback):
    i += 1
    if i >= len(qs):
        await state.clear()
        s = f"{score:g}".replace(".", ",")
        if score >= len(qs) * 0.8:
            note = "Отлично, тема усвоена! 🎉"
        elif score >= len(qs) * 0.5:
            note = "Неплохо 👍 Перечитай конспект и попробуй ещё раз."
        else:
            note = "Стоит перечитать параграф и повторить 📖"
        return await send_math(
            msg,
            f"{feedback}\n\n🏁 Итог: {s} из {len(qs)}. {note}",
            kb([("🔁 Ещё 5 вопросов", "quiz")], [MENU]),
        )
    await state.update_data(i=i, score=score)
    await send_math(msg, f"{feedback}\n\n{quiz_text(qs, i)}", QUIZ_KB)


@router.message(S.quiz, F.text)
async def quiz_answer(m: Message, state: FSMContext):
    d = await state.get_data()
    qs, i, score = d["qs"], d["i"], d["score"]
    wait = await m.answer("⏳ Проверяю…")
    try:
        raw = await gemini(
            [{"text": f"Вопрос: {qs[i]['q']}\nЭталонный ответ: {qs[i]['a']}\nОтвет ученика: {m.text[:1500]}"}],
            system=CHECK_SYS,
            json_mode=True,
        )
        r = json.loads(raw)
        verdict, comment = r.get("verdict"), str(r.get("comment") or "")
    except Exception as e:
        logging.exception("quiz check")
        return await wait.edit_text("⚠️ " + ai_error_text(e) + " Отправь ответ ещё раз.")
    try:
        await wait.delete()
    except TelegramBadRequest:
        pass
    if verdict == "correct":
        score += 1
        fb = "✅ Верно! " + comment
    elif verdict == "partial":
        score += 0.5
        fb = f"🟡 Почти. {comment}\nПолный ответ: {qs[i]['a']}"
    else:
        fb = f"❌ Не совсем. {comment}\nПравильный ответ: {qs[i]['a']}"
    await quiz_next(m, state, qs, i, score, fb)


@router.message(S.quiz)
async def quiz_other(m: Message):
    await m.answer("Напиши ответ текстом, пожалуйста.", reply_markup=QUIZ_KB)


@router.callback_query(F.data == "quiz_skip")
async def quiz_skip(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if "qs" not in d:
        return await cb.answer("Вопросы уже закончились", show_alert=True)
    qs, i = d["qs"], d["i"]
    await cb.answer()
    await quiz_next(cb.message, state, qs, i, d["score"], f"⏭ Пропущено. Правильный ответ: {qs[i]['a']}")


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
        return await cb.message.answer("⚠️ " + ai_error_text(e), reply_markup=kb([MENU]))
    other = ("📖 Объяснить по шагам", "ai:explain") if mode == "solve" else ("✅ Только решение", "ai:solve")
    answer, graph = split_graph(answer)
    markup = kb([other], [MENU])
    await send_math(cb.message, answer, None if graph else markup)
    if graph:
        await send_graph(cb.message, graph, markup)


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
