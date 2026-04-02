"""
Telegram-бот «кошелёк путешественника»: api.exchangerate.host (symbols + convert) через current_api.
"""

from __future__ import annotations

import html
import logging
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

import telebot
from dotenv import load_dotenv
from telebot import types

from country_currencies import COUNTRY_TO_CCY
from current_api import (
    convert_amount,
    convert_amount_with_meta,
    load_symbols,
    rate_home_per_one_dest,
    symbols,
)
import wallet_db as db

load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

USER_WIZARD: dict[int, dict] = {}
PENDING_EXPENSE: dict[int, dict] = {}

STEP_IDLE = "idle"
STEP_NEW_HOME = "new_home"
STEP_NEW_DEST = "new_dest"
STEP_NEW_RATE_CONFIRM = "new_rate_confirm"
STEP_NEW_MANUAL_RATE = "new_manual_rate"
STEP_NEW_INITIAL = "new_initial"
STEP_SET_RATE = "set_rate"
STEP_ADD_EXPENSE = "add_expense"

NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")

EXPENSE_CONFIRM_PREFIX = "expense_confirm:"

CURRENCY_UNRESOLVED_MSG = (
    "Не удалось определить валюту.\n"
    "Введите 3-буквенный код (например: USD, EUR, RUB)"
)


def _bot_token() -> str:
    t = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not t:
        raise SystemExit("Задайте TELEGRAM_BOT_TOKEN в .env")
    return t


def fmt_money(n: float, max_decimals: int = 2) -> str:
    if max_decimals == 0:
        body = f"{n:,.0f}"
    else:
        body = f"{n:,.{max_decimals}f}"
    return body.replace(",", " ")


def fmt_date(value: str | None) -> str:
    if not value:
        return "не указана"
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return dt.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value


def main_menu_markup() -> types.InlineKeyboardMarkup:
    m = types.InlineKeyboardMarkup(row_width=1)
    m.add(
        types.InlineKeyboardButton(
            "Создать новое путешествие", callback_data="menu_new"
        ),
        types.InlineKeyboardButton("Мои путешествия", callback_data="menu_trips"),
        types.InlineKeyboardButton("Добавить расход", callback_data="menu_expense"),
        types.InlineKeyboardButton("Баланс", callback_data="menu_balance"),
        types.InlineKeyboardButton("История расходов", callback_data="menu_history"),
        types.InlineKeyboardButton("Изменить курс", callback_data="menu_rate"),
        types.InlineKeyboardButton("Удалить путешествие", callback_data="menu_delete"),
    )
    return m


def trip_balance_text(trip: db.Trip) -> str:
    return (
        f"Остаток: <b>{fmt_money(trip.balance_dest)} {trip.dest_ccy}</b> = "
        f"<b>{fmt_money(trip.balance_home)} {trip.home_ccy}</b>\n"
        f"Курс: 1 {trip.dest_ccy} = {fmt_money(trip.rate_home_per_dest)} {trip.home_ccy}"
    )


def parse_float_user(text: str) -> float | None:
    m = NUM_RE.search(text.replace(" ", ""))
    if not m:
        return None
    s = m.group(0).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def normalize_country(text: str) -> str:
    text = text.strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.split())


def resolve_currency(user_input: str) -> str | None:
    """
    Сначала страна (словарь), чтобы «usa», «рус» и т.п. не терялись как «код валюты».
    Потом ровно 3 буквы — ISO-код валюты из symbols.
    """
    s = user_input.strip()
    if not s:
        return None

    key_country = normalize_country(s)
    print("DEBUG country input:", user_input)
    print("DEBUG normalized:", key_country)
    code = COUNTRY_TO_CCY.get(key_country)
    if code is not None and code in symbols:
        return code

    if len(s) == 3 and s.isalpha():
        code3 = s.upper()
        if code3 in symbols:
            return code3

    return None


def parse_message_as_expense_amount(text: str) -> float | None:
    try:
        return float(text.replace(",", ".").strip())
    except Exception:
        return None


def format_expense_callback_amount(amount: float) -> str:
    s = f"{amount:.12f}".rstrip("0").rstrip(".")
    return s if s else "0"


def parse_expense_callback_amount(s: str) -> float | None:
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def try_resolve_ccy(chat_id: int, raw: str) -> str | None:
    ccy = resolve_currency(raw)
    if ccy:
        return ccy
    bot.send_message(chat_id, CURRENCY_UNRESOLVED_MSG)
    return None


def get_user_id(message: types.Message) -> int | None:
    if message.from_user is None:
        return None
    return message.from_user.id


def get_chat_id(message: types.Message) -> int | None:
    if message.chat is None:
        return None
    return message.chat.id


def get_text(message: types.Message) -> str | None:
    return message.text


def wizard_step(uid: int) -> str:
    w = USER_WIZARD.get(uid)
    return w["step"] if w else STEP_IDLE


def clear_wizard(uid: int) -> None:
    USER_WIZARD.pop(uid, None)


bot = telebot.TeleBot(_bot_token(), parse_mode="HTML")


@bot.message_handler(commands=["start"])
def cmd_start(message: types.Message) -> None:
    uid = get_user_id(message)
    chat_id = get_chat_id(message)
    if uid is None or chat_id is None:
        return
    clear_wizard(uid)
    text = (
        "Привет! Я помогу вести бюджет в поездке: курс, баланс в двух валютах и учёт трат.\n\n"
        "Сначала создайте путешествие — укажите страну выезда и страну назначения. "
        "Потом можно вводить суммы трат в валюте пребывания.\n\n"
        "Выберите действие в меню ниже или используйте команды "
        "/newtrip, /switch, /balance, /history, /setrate."
    )
    bot.send_message(chat_id, text, reply_markup=main_menu_markup())


@bot.message_handler(commands=["newtrip"])
def cmd_newtrip(message: types.Message) -> None:
    uid = get_user_id(message)
    chat_id = get_chat_id(message)
    if uid is None or chat_id is None:
        return
    start_new_trip_flow(chat_id, uid)


@bot.message_handler(commands=["switch"])
def cmd_switch(message: types.Message) -> None:
    uid = get_user_id(message)
    chat_id = get_chat_id(message)
    if uid is None or chat_id is None:
        return
    show_trips_menu(chat_id, uid)


@bot.message_handler(commands=["balance"])
def cmd_balance(message: types.Message) -> None:
    uid = get_user_id(message)
    chat_id = get_chat_id(message)
    if uid is None or chat_id is None:
        return
    send_balance(chat_id, uid)


@bot.message_handler(commands=["expense"])
def cmd_expense(message: types.Message) -> None:
    uid = get_user_id(message)
    chat_id = get_chat_id(message)
    if uid is None or chat_id is None:
        return
    start_expense_flow(chat_id, uid)


@bot.message_handler(commands=["history"])
def cmd_history(message: types.Message) -> None:
    uid = get_user_id(message)
    chat_id = get_chat_id(message)
    if uid is None or chat_id is None:
        return
    send_history(chat_id, uid)


@bot.message_handler(commands=["deletetrip"])
def cmd_delete_trip(message: types.Message) -> None:
    uid = get_user_id(message)
    chat_id = get_chat_id(message)
    if uid is None or chat_id is None:
        return
    show_delete_trips_menu(chat_id, uid)


@bot.message_handler(commands=["setrate"])
def cmd_setrate(message: types.Message) -> None:
    uid = get_user_id(message)
    chat_id = get_chat_id(message)
    if uid is None or chat_id is None:
        return
    trip = db.get_active_trip(uid)
    if not trip:
        bot.send_message(
            chat_id,
            "Нет активного путешествия. Создайте его через меню или /newtrip.",
            reply_markup=main_menu_markup(),
        )
        return
    USER_WIZARD[uid] = {
        "step": STEP_SET_RATE,
        "trip_id": trip.id,
    }
    bot.send_message(
        chat_id,
        f"Введите новый курс: сколько <b>{trip.home_ccy}</b> за <b>1 {trip.dest_ccy}</b> "
        f"(как в обменнике).\nСейчас: 1 {trip.dest_ccy} = {fmt_money(trip.rate_home_per_dest)} {trip.home_ccy}.",
    )


def start_new_trip_flow(chat_id: int, uid: int) -> None:
    clear_wizard(uid)
    PENDING_EXPENSE.pop(uid, None)
    USER_WIZARD[uid] = {"step": STEP_NEW_HOME}
    bot.send_message(
        chat_id,
        "Новое путешествие.\nВведите страну <b>отправления</b> (откуда выезжаете) "
        "или трёхбуквенный код валюты (например, RUB).",
    )


def start_expense_flow(chat_id: int, uid: int) -> None:
    trip = db.get_active_trip(uid)
    if not trip:
        bot.send_message(
            chat_id,
            "Сначала выберите или создайте путешествие.",
            reply_markup=main_menu_markup(),
        )
        return
    clear_wizard(uid)
    USER_WIZARD[uid] = {"step": STEP_ADD_EXPENSE}
    bot.send_message(
        chat_id,
        f"Активно путешествие: <b>{html.escape(trip.title)}</b>.\n"
        f"Введите сумму расхода в <b>{trip.dest_ccy}</b> одним числом.",
    )


def show_trips_menu(chat_id: int, uid: int) -> None:
    trips = db.list_trips(uid)
    if not trips:
        bot.send_message(
            chat_id,
            "У вас пока нет путешествий. Создайте первое через «Создать новое путешествие».",
            reply_markup=main_menu_markup(),
        )
        return
    m = types.InlineKeyboardMarkup(row_width=1)
    active_id = db.get_active_trip_id(uid)
    for t in trips:
        mark = "✓ " if t.id == active_id else ""
        m.add(
            types.InlineKeyboardButton(
                f"{mark}{t.title} ({t.home_ccy} → {t.dest_ccy})",
                callback_data=f"sw_{t.id}",
            )
        )
    m.add(types.InlineKeyboardButton("« В главное меню", callback_data="menu_main"))
    bot.send_message(chat_id, "Ваши путешествия — выберите, какое сделать активным:", reply_markup=m)


def show_delete_trips_menu(chat_id: int, uid: int) -> None:
    trips = db.list_trips(uid)
    if not trips:
        bot.send_message(
            chat_id,
            "Удалять пока нечего — путешествий нет.",
            reply_markup=main_menu_markup(),
        )
        return
    active_id = db.get_active_trip_id(uid)
    m = types.InlineKeyboardMarkup(row_width=1)
    for t in trips:
        mark = "✓ " if t.id == active_id else ""
        m.add(
            types.InlineKeyboardButton(
                f"Удалить {mark}{t.title} ({t.home_ccy} → {t.dest_ccy})",
                callback_data=f"delask_{t.id}",
            )
        )
    m.add(types.InlineKeyboardButton("« В главное меню", callback_data="menu_main"))
    bot.send_message(chat_id, "Выберите путешествие для удаления:", reply_markup=m)


def send_balance(chat_id: int, uid: int) -> None:
    trip = db.get_active_trip(uid)
    if not trip:
        bot.send_message(
            chat_id,
            "Нет активного путешествия. Создайте или выберите в «Мои путешествия».",
            reply_markup=main_menu_markup(),
        )
        return
    bot.send_message(
        chat_id,
        f"<b>{html.escape(trip.title)}</b>\n{trip_balance_text(trip)}",
        reply_markup=main_menu_markup(),
    )


def send_history(chat_id: int, uid: int) -> None:
    trip = db.get_active_trip(uid)
    if not trip:
        bot.send_message(
            chat_id,
            "Нет активного путешествия.",
            reply_markup=main_menu_markup(),
        )
        return
    rows = db.list_expenses(trip.id, uid)
    if not rows:
        bot.send_message(
            chat_id,
            f"<b>{html.escape(trip.title)}</b>\nРасходов пока нет.",
            reply_markup=main_menu_markup(),
        )
        return
    lines = [f"<b>{html.escape(trip.title)}</b> — последние расходы:\n"]
    for r in rows[:30]:
        rate_info = (
            f"Курс: 1 {trip.dest_ccy} = {fmt_money(r.rate_home_per_dest or 0)} {trip.home_ccy}"
            if r.rate_home_per_dest is not None
            else f"Курс: 1 {trip.dest_ccy} = {fmt_money(trip.rate_home_per_dest)} {trip.home_ccy}"
        )
        lines.append(
            f"− {fmt_money(r.amount_dest)} {trip.dest_ccy} "
            f"(≈ {fmt_money(r.amount_home)} {trip.home_ccy})\n"
            f"Дата расхода: {fmt_date(r.created_at)}\n"
            f"Дата курса: {fmt_date(r.rate_date)}\n"
            f"{rate_info}"
        )
    bot.send_message(chat_id, "\n".join(lines), reply_markup=main_menu_markup())


@bot.callback_query_handler(func=lambda c: True)
def on_callback(call: types.CallbackQuery) -> None:
    if call.message is None or call.message.chat is None:
        return
    if call.from_user is None:
        return
    chat_id = call.message.chat.id
    uid = call.from_user.id
    data = call.data or ""

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if data == "menu_main":
        bot.send_message(
            chat_id,
            "Главное меню:",
            reply_markup=main_menu_markup(),
        )
        return

    if data == "menu_new":
        start_new_trip_flow(chat_id, uid)
        return
    if data == "menu_trips":
        show_trips_menu(chat_id, uid)
        return
    if data == "menu_expense":
        start_expense_flow(chat_id, uid)
        return
    if data == "menu_balance":
        send_balance(chat_id, uid)
        return
    if data == "menu_history":
        send_history(chat_id, uid)
        return
    if data == "menu_delete":
        show_delete_trips_menu(chat_id, uid)
        return
    if data == "menu_rate":
        trip = db.get_active_trip(uid)
        if not trip:
            bot.send_message(
                chat_id,
                "Нет активного путешествия.",
                reply_markup=main_menu_markup(),
            )
            return
        USER_WIZARD[uid] = {"step": STEP_SET_RATE, "trip_id": trip.id}
        bot.send_message(
            chat_id,
            f"Введите новый курс: сколько <b>{trip.home_ccy}</b> за <b>1 {trip.dest_ccy}</b>.\n"
            f"Сейчас: 1 {trip.dest_ccy} = {fmt_money(trip.rate_home_per_dest)} {trip.home_ccy}.",
        )
        return

    if data.startswith("sw_"):
        try:
            tid = int(data[3:])
        except ValueError:
            return
        trip = db.get_trip(tid, uid)
        if not trip:
            bot.send_message(chat_id, "Путешествие не найдено.", reply_markup=main_menu_markup())
            return
        db.set_active_trip(uid, tid)
        bot.send_message(
            chat_id,
            f"Активно: <b>{html.escape(trip.title)}</b>\n{trip_balance_text(trip)}",
            reply_markup=main_menu_markup(),
        )
        return

    if data.startswith("delask_"):
        try:
            tid = int(data[7:])
        except ValueError:
            return
        trip = db.get_trip(tid, uid)
        if not trip:
            bot.send_message(chat_id, "Путешествие не найдено.", reply_markup=main_menu_markup())
            return
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("🗑 Да, удалить", callback_data=f"delyes_{tid}"),
            types.InlineKeyboardButton("Отмена", callback_data="menu_delete"),
        )
        bot.send_message(
            chat_id,
            f"Удалить путешествие <b>{html.escape(trip.title)}</b>?\n"
            f"Все его расходы тоже будут удалены.",
            reply_markup=kb,
        )
        return

    if data.startswith("delyes_"):
        try:
            tid = int(data[7:])
        except ValueError:
            return
        trip = db.get_trip(tid, uid)
        if not trip:
            bot.send_message(chat_id, "Путешествие уже удалено.", reply_markup=main_menu_markup())
            return
        deleted, next_active_id = db.delete_trip(tid, uid)
        if not deleted:
            bot.send_message(chat_id, "Не удалось удалить путешествие.", reply_markup=main_menu_markup())
            return
        next_trip = db.get_trip(next_active_id, uid) if next_active_id is not None else None
        if next_trip:
            msg = (
                f"Путешествие <b>{html.escape(trip.title)}</b> удалено.\n"
                f"Теперь активно: <b>{html.escape(next_trip.title)}</b>."
            )
        else:
            msg = f"Путешествие <b>{html.escape(trip.title)}</b> удалено."
        bot.send_message(chat_id, msg, reply_markup=main_menu_markup())
        return

    if data == "rate_use_api":
        w = USER_WIZARD.get(uid)
        if not w or w.get("step") != STEP_NEW_RATE_CONFIRM:
            return
        rate = w.get("rate")
        if rate is None:
            return
        w["step"] = STEP_NEW_INITIAL
        w["rate_home_per_dest"] = float(rate)
        w["manual_rate"] = False
        bot.send_message(
            chat_id,
            f"Курс принят: 1 {w['dest_ccy']} = {fmt_money(float(rate))} {w['home_ccy']}.\n\n"
            f"Введите <b>стартовую сумму в {w['home_ccy']}</b> — сколько денег вы берёте в поездку.",
        )
        return

    if data == "rate_manual":
        w = USER_WIZARD.get(uid)
        if not w or w.get("step") != STEP_NEW_RATE_CONFIRM:
            return
        w["manual_rate"] = True
        w["step"] = STEP_NEW_MANUAL_RATE
        bot.send_message(
            chat_id,
            f"Введите курс вручную: сколько <b>{w['home_ccy']}</b> вы отдаёте за <b>1 {w['dest_ccy']}</b> "
            f"(по кассе обменника). Например: <code>12.85</code>",
        )
        return

    if data.startswith(EXPENSE_CONFIRM_PREFIX):
        suffix = data[len(EXPENSE_CONFIRM_PREFIX) :]
        cb_amount = parse_expense_callback_amount(suffix)
        if cb_amount is None:
            bot.send_message(chat_id, "Не удалось разобрать сумму. Пришлите число снова.")
            return
        p = PENDING_EXPENSE.pop(uid, None)
        if not p:
            bot.send_message(chat_id, "Нечего записывать — пришлите сумму ещё раз.")
            return
        if abs(float(p["amount_dest"]) - cb_amount) > 1e-6:
            bot.send_message(chat_id, "Сумма устарела. Пришлите трату числом заново.")
            return
        trip = db.get_trip(int(p["trip_id"]), uid)
        if not trip:
            bot.send_message(chat_id, "Путешествие недоступно.", reply_markup=main_menu_markup())
            return
        ad = float(p["amount_dest"])
        ah = float(p["amount_home"])
        if ad > trip.balance_dest + 1e-6 or ah > trip.balance_home + 1e-4:
            bot.send_message(
                chat_id,
                "Сумма больше доступного остатка — расход не записан.",
                reply_markup=main_menu_markup(),
            )
            return
        db.add_expense(
            trip.id,
            uid,
            ad,
            ah,
            float(p["rate_home_per_dest"]) if p.get("rate_home_per_dest") is not None else None,
            str(p["rate_date"]) if p.get("rate_date") else None,
        )
        trip2 = db.get_trip(trip.id, uid)
        if not trip2:
            bot.send_message(chat_id, "Ошибка чтения баланса.", reply_markup=main_menu_markup())
            return
        expense_date = fmt_date(str(p.get("expense_date") or ""))
        rate_date = fmt_date(str(p.get("rate_date") or ""))
        rate_used = (
            fmt_money(float(p["rate_home_per_dest"]))
            if p.get("rate_home_per_dest") is not None
            else fmt_money(trip.rate_home_per_dest)
        )
        bot.send_message(
            chat_id,
            "Расход учтён ✅\n\n"
            f"Дата расхода: {expense_date}\n"
            f"Дата курса: {rate_date}\n"
            f"Курс: 1 {trip2.dest_ccy} = {rate_used} {trip2.home_ccy}\n\n"
            f"Остаток:\n"
            f"{fmt_money(trip2.balance_dest)} {trip2.dest_ccy} = "
            f"{fmt_money(trip2.balance_home)} {trip2.home_ccy}",
            reply_markup=main_menu_markup(),
        )
        return

    if data == "expense_cancel":
        PENDING_EXPENSE.pop(uid, None)
        bot.send_message(chat_id, "Ок, не учитываю", reply_markup=main_menu_markup())
        return


def proceed_new_trip_home(message: types.Message) -> None:
    chat_id = get_chat_id(message)
    uid = get_user_id(message)
    text = get_text(message)
    if chat_id is None or uid is None or text is None:
        return
    w = USER_WIZARD[uid]
    ccy = try_resolve_ccy(chat_id, text.strip())
    if not ccy:
        return
    w["home_ccy"] = ccy
    w["home_label"] = text.strip()
    w["step"] = STEP_NEW_DEST
    bot.send_message(
        chat_id,
        f"Домашняя валюта: <b>{ccy}</b>.\nТеперь введите страну <b>назначения</b> или её валюту (3 буквы).",
    )


def proceed_new_trip_dest(message: types.Message) -> None:
    chat_id = get_chat_id(message)
    uid = get_user_id(message)
    text = get_text(message)
    if chat_id is None or uid is None or text is None:
        return
    w = USER_WIZARD[uid]
    ccy = try_resolve_ccy(chat_id, text.strip())
    if not ccy:
        return
    home = w["home_ccy"]
    if ccy == home:
        bot.send_message(chat_id, "Валюта назначения совпадает с домашней. Укажите другую страну/валюту.")
        return
    ok, res = rate_home_per_one_dest(home, ccy)
    if not ok:
        bot.send_message(
            chat_id,
            f"По API не получилось сопоставить пару <b>{home}</b> ↔ <b>{ccy}</b>: {res}\n"
            "Попробуйте другие коды валют.",
        )
        return
    rate = float(res)
    w["dest_ccy"] = ccy
    w["dest_label"] = text.strip()
    w["step"] = STEP_NEW_RATE_CONFIRM
    w["rate"] = rate
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Да, подходит", callback_data="rate_use_api"),
        types.InlineKeyboardButton("✎ Ввести вручную", callback_data="rate_manual"),
    )
    bot.send_message(
        chat_id,
        f"Для пары <b>{home}</b> → <b>{ccy}</b> сервис даёт курс:\n"
        f"1 {ccy} = <b>{fmt_money(rate)} {home}</b>\n\n"
        f"Подходит такой курс?",
        reply_markup=kb,
    )


def proceed_manual_rate(message: types.Message) -> None:
    chat_id = get_chat_id(message)
    uid = get_user_id(message)
    text = get_text(message)
    if chat_id is None or uid is None or text is None:
        return
    w = USER_WIZARD[uid]
    v = parse_float_user(text)
    if v is None or v <= 0:
        bot.send_message(chat_id, "Нужно положительное число, например 12.5.")
        return
    w["rate_home_per_dest"] = v
    w["step"] = STEP_NEW_INITIAL
    bot.send_message(
        chat_id,
        f"Курс сохранён: 1 {w['dest_ccy']} = {fmt_money(v)} {w['home_ccy']}.\n\n"
        f"Введите <b>стартовую сумму в {w['home_ccy']}</b>.",
    )


def proceed_initial_amount(message: types.Message) -> None:
    chat_id = get_chat_id(message)
    uid = get_user_id(message)
    text = get_text(message)
    if chat_id is None or uid is None or text is None:
        return
    w = USER_WIZARD[uid]
    v = parse_float_user(text)
    if v is None or v <= 0:
        bot.send_message(chat_id, "Нужна положительная сумма в домашней валюте.")
        return
    rate = float(w["rate_home_per_dest"])
    home_ccy = w["home_ccy"]
    dest_ccy = w["dest_ccy"]
    title = w.get("dest_label") or dest_ccy
    used_manual = bool(w.get("manual_rate"))

    bh = v
    if used_manual:
        bd = bh / rate if rate else 0.0
    else:
        ok, dest_amt = convert_amount(bh, home_ccy, dest_ccy)
        if not ok:
            bot.send_message(
                chat_id,
                f"Не удалось через API перевести сумму в {dest_ccy}: {dest_amt}\n"
                "Попробуйте другую сумму или начните путешествие заново.",
            )
            return
        bd = float(dest_amt)
    clear_wizard(uid)
    db.create_trip(
        uid,
        title.strip()[:80],
        home_ccy,
        dest_ccy,
        rate,
        bh,
        bd,
    )
    trip = db.get_active_trip(uid)
    if not trip:
        bot.send_message(
            chat_id,
            "Путешествие создано, но не удалось загрузить данные. Откройте «Баланс».",
            reply_markup=main_menu_markup(),
        )
        return
    conv_note = (
        "по курсу обменника (вручную)."
        if used_manual
        else "через API конвертации."
    )
    bot.send_message(
        chat_id,
        f"Путешествие «<b>{html.escape(trip.title)}</b>» создано.\n"
        f"Старт: <b>{fmt_money(bh)} {home_ccy}</b> ≈ <b>{fmt_money(bd)} {dest_ccy}</b> — {conv_note}\n\n"
        f"{trip_balance_text(trip)}",
        reply_markup=main_menu_markup(),
    )


def proceed_set_rate(message: types.Message) -> None:
    chat_id = get_chat_id(message)
    uid = get_user_id(message)
    text = get_text(message)
    if chat_id is None or uid is None or text is None:
        return
    w = USER_WIZARD[uid]
    tid = int(w["trip_id"])
    v = parse_float_user(text)
    if v is None or v <= 0:
        bot.send_message(chat_id, "Нужно положительное число — столько домашней валюты за 1 единицу валюты поездки.")
        return
    trip = db.update_trip_rate(tid, uid, v)
    clear_wizard(uid)
    if not trip:
        bot.send_message(chat_id, "Путешествие не найдено.", reply_markup=main_menu_markup())
        return
    bot.send_message(
        chat_id,
        f"Курс обновлён.\n{trip_balance_text(trip)}",
        reply_markup=main_menu_markup(),
    )


def try_handle_expense_message(message: types.Message) -> bool:
    print("DEBUG: try_handle_expense_message triggered")
    print("DEBUG: try_handle_expense_message called", message.text)
    uid = get_user_id(message)
    if uid is None:
        return False
    text = get_text(message)
    if text is None:
        return False
    if wizard_step(uid) in [
        STEP_NEW_HOME,
        STEP_NEW_DEST,
        STEP_NEW_INITIAL,
        STEP_NEW_MANUAL_RATE,
    ]:
        return False

    amount_dest = parse_message_as_expense_amount(text)
    print("DEBUG: parsed amount:", amount_dest)
    if amount_dest is None:
        return False

    chat_id = get_chat_id(message)
    if chat_id is None:
        return False

    trip = db.get_active_trip(uid)
    if not trip:
        bot.send_message(
            chat_id,
            "Сначала выберите или создайте путешествие",
            reply_markup=main_menu_markup(),
        )
        return True

    if amount_dest <= 0:
        bot.send_message(
            chat_id,
            "Для расхода нужно положительное число.",
            reply_markup=main_menu_markup(),
        )
        return True

    ok_meta, meta = convert_amount_with_meta(amount_dest, trip.dest_ccy, trip.home_ccy)
    if not ok_meta:
        amount_home = amount_dest * trip.rate_home_per_dest
        rate_used = trip.rate_home_per_dest
        rate_date = datetime.now().strftime("%Y-%m-%d")
    else:
        meta_dict = meta if isinstance(meta, dict) else {}
        amount_home = float(meta_dict.get("result", 0.0))
        rate_used = (
            float(meta_dict["rate"])
            if meta_dict.get("rate") is not None
            else trip.rate_home_per_dest
        )
        rate_date = str(meta_dict.get("rate_date") or datetime.now().strftime("%Y-%m-%d"))
    PENDING_EXPENSE[uid] = {
        "trip_id": trip.id,
        "amount_dest": amount_dest,
        "amount_home": amount_home,
        "rate_home_per_dest": rate_used,
        "rate_date": rate_date,
        "expense_date": datetime.now().isoformat(),
    }
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton(
            "✅ Да", callback_data=f"expense_confirm:{amount_dest}"
        ),
        types.InlineKeyboardButton("❌ Нет", callback_data="expense_cancel"),
    )
    bot.send_message(
        chat_id,
        f"{amount_dest} {trip.dest_ccy} = {amount_home} {trip.home_ccy}\n"
        f"Дата курса: {fmt_date(rate_date)}\n"
        f"Курс: 1 {trip.dest_ccy} = {fmt_money(rate_used)} {trip.home_ccy}\n\n"
        f"Учесть как расход?",
        reply_markup=kb,
    )
    if wizard_step(uid) == STEP_ADD_EXPENSE:
        clear_wizard(uid)
    return True


@bot.message_handler(content_types=["text"], func=lambda m: m.text and not m.text.startswith("/"))
def on_text(message: types.Message) -> None:
    uid = get_user_id(message)
    if uid is None:
        return
    step = wizard_step(uid)

    if step == STEP_NEW_HOME:
        proceed_new_trip_home(message)
        return
    if step == STEP_NEW_DEST:
        proceed_new_trip_dest(message)
        return
    if step == STEP_NEW_MANUAL_RATE:
        proceed_manual_rate(message)
        return
    if step == STEP_NEW_INITIAL:
        proceed_initial_amount(message)
        return
    if step == STEP_SET_RATE:
        proceed_set_rate(message)
        return
    if step == STEP_ADD_EXPENSE:
        if try_handle_expense_message(message):
            return
        bot.send_message(
            message.chat.id,
            "Введите сумму расхода одним числом, например 100 или 12.5.",
        )
        return
    if step == STEP_NEW_RATE_CONFIRM:
        bot.send_message(
            message.chat.id,
            "Пожалуйста, подтвердите курс кнопками под предыдущим сообщением "
            "(«Да, подходит» или «Ввести вручную»).",
        )
        return

    if try_handle_expense_message(message):
        return

    tx = get_text(message)
    if tx is not None:
        amount = parse_message_as_expense_amount(tx)
        if amount is not None:
            print("DEBUG: expense detected", amount)
            handled = try_handle_expense_message(message)
            if handled:
                return

    bot.send_message(
        message.chat.id,
        "Не понял сообщение. Откройте меню или введите сумму траты в валюте страны пребывания.",
        reply_markup=main_menu_markup(),
    )


def main() -> None:
    db.init_db()
    ok, err = load_symbols()
    if not ok:
        raise SystemExit(f"Не удалось загрузить валюты с API: {err}")
    log.info("Загружено кодов валют из API: %d", len(symbols))
    log.info("Бот запущен.")
    bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
    main()
