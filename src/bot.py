"""Telegram-бот автоприёма заявок.

Воронка:
  /start -> приветствие с вопросом (имя + город)
  ответ клиента -> "специалист свяжется" + заявка с прямой ссылкой падает в общий чат
  админ в общем чате: reply -> ответить клиенту, кнопка "Взять в работу"
Админ-команды (только для админов): /base /export /broadcast /admin /help /whoami
"""
import asyncio
import csv
import html
import io
import logging
import re

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import content
import db
from config import ADMIN_CHAT_ID, ADMIN_IDS, ADMIN_SECRET, BOT_TOKEN, SPECIALIST_NAME

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("leadbot")

router = Router()

# текст рассылки, ожидающий подтверждения: {admin_id: text}
pending_broadcast: dict[int, str] = {}


class Lead(StatesGroup):
    waiting_answer = State()


# ---------------- helpers ----------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in db.db_admin_ids()


def esc(v) -> str:
    return html.escape(str(v)) if v is not None else ""


def parse_answer(text: str):
    """Наивно достаём имя и город. Полный ответ всё равно храним в raw_answer."""
    text = (text or "").strip()
    m = re.split(r"\s*[,\n;]\s*|\s+из\s+|\s+город\s+|\s+г\.\s*", text, maxsplit=1)
    if len(m) == 2 and m[0] and m[1]:
        return m[0].strip()[:100], m[1].strip()[:100]
    return text[:100], None


def contact_link(row) -> str:
    if row["username"]:
        return f"https://t.me/{row['username']}"
    return f"tg://user?id={row['user_id']}"


def lead_card(row) -> str:
    name = row["contact_name"] or row["first_name"] or "—"
    uname = f"@{row['username']}" if row["username"] else "нет username"
    lines = [
        "🆕 <b>Новая заявка</b>",
        f"👤 Имя: <b>{esc(name)}</b>",
        f"🏙 Город: <b>{esc(row['city']) or '—'}</b>",
    ]
    if row["phone"]:
        lines.append(f"📞 Телефон: <code>{esc(row['phone'])}</code>")
    lines += [
        f"💬 Полный ответ: {esc(row['raw_answer']) or '—'}",
        f"🔗 Telegram: {esc(uname)}",
        f'🔗 Профиль: <a href="tg://user?id={row["user_id"]}">открыть чат</a>',
        f"🆔 <code>{row['user_id']}</code>",
        "",
        "↩️ Ответить клиенту — <b>reply на это сообщение</b>.",
    ]
    return "\n".join(lines)


def lead_keyboard(row) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="📥 Взять в работу", callback_data=f"take:{row['user_id']}")]]
    if row["username"]:
        buttons.insert(
            0, [InlineKeyboardButton(text="✍️ Написать в ЛС", url=f"https://t.me/{row['username']}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _button(label: str, target: str) -> InlineKeyboardButton:
    # http/tg-ссылка -> URL-кнопка, иначе -> callback
    if target.startswith(("http://", "https://", "tg://")):
        return InlineKeyboardButton(text=label, url=target)
    return InlineKeyboardButton(text=label, callback_data=target)


def menu_kb(screen_id: str) -> InlineKeyboardMarkup:
    """Собирает inline-клавиатуру экрана из content.SCREENS."""
    rows = content.SCREENS[screen_id]["rows"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [_button(label, target) for label, target in row] for row in rows
    ])


async def show_screen(message: Message, screen_id: str, edit: bool) -> None:
    """Показывает экран меню: правит текущее сообщение либо отправляет новое."""
    scr = content.SCREENS[screen_id]
    if edit:
        try:
            await message.edit_text(scr["text"], reply_markup=menu_kb(screen_id))
            return
        except Exception:  # noqa: BLE001 — сообщение нельзя отредактировать
            pass
    await message.answer(scr["text"], reply_markup=menu_kb(screen_id))


def contact_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
            [KeyboardButton(text="Пропустить")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def push_lead_to_group(bot: Bot, user_id: int) -> None:
    row = db.get_user(user_id)
    if not row:
        return
    sent = await bot.send_message(ADMIN_CHAT_ID, lead_card(row), reply_markup=lead_keyboard(row))
    db.link_message(sent.message_id, user_id)


def start_card(row) -> str:
    name = row["first_name"] or "—"
    uname = f"@{row['username']}" if row["username"] else "нет username"
    return "\n".join([
        "🆕 <b>Новый лид</b> (запуск бота)",
        f"👤 Имя: <b>{esc(name)}</b>",
        f"🔗 Telegram: {esc(uname)}",
        f'🔗 Профиль: <a href="tg://user?id={row["user_id"]}">открыть чат</a>',
        f"🆔 <code>{row['user_id']}</code>",
        "",
        "↩️ Ответить клиенту — <b>reply на это сообщение</b>.",
    ])


async def push_start_to_group(bot: Bot, user_id: int) -> None:
    row = db.get_user(user_id)
    if not row:
        return
    sent = await bot.send_message(ADMIN_CHAT_ID, start_card(row), reply_markup=lead_keyboard(row))
    db.link_message(sent.message_id, user_id)


# ---------------- клиентский поток: меню и навигация ----------------

@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def on_start(message: Message, state: FSMContext):
    await state.clear()
    u = message.from_user
    is_new = db.upsert_start(u.id, u.username, u.first_name, u.last_name)
    greeting = f"Здравствуйте, {esc(u.first_name)}!" if u.first_name else "Здравствуйте!"
    await message.answer(f"{greeting}\n\n{content.WELCOME_BODY}", reply_markup=menu_kb("main"))
    if is_new:
        # новый пользователь -> сразу лид в общий чат, ему можно написать через reply
        try:
            await push_start_to_group(message.bot, u.id)
        except Exception as e:  # noqa: BLE001 — бот не в группе / нет прав
            log.warning("Не удалось отправить лид в общий чат: %s", e)


@router.message(Command("menu"), F.chat.type == ChatType.PRIVATE)
async def on_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(content.SCREENS["main"]["text"], reply_markup=menu_kb("main"))


@router.callback_query(F.data.startswith("nav:"))
async def on_nav(cb: CallbackQuery, state: FSMContext):
    await state.clear()  # выход из анкеты, если пользователь был в ней
    screen_id = cb.data.split(":", 1)[1]
    if screen_id not in content.SCREENS:
        await cb.answer()
        return
    await show_screen(cb.message, screen_id, edit=True)
    await cb.answer()


# ---------------- тест по ПДД ----------------

def quiz_kb(q_index: int, score: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=opt, callback_data=f"qz:a:{q_index}:{i}:{score}")]
        for i, opt in enumerate(content.QUIZ[q_index]["options"])
    ]
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def quiz_text(q_index: int) -> str:
    total = len(content.QUIZ)
    q = content.QUIZ[q_index]
    return f"Вопрос {q_index + 1} из {total}\n\n{q['q']}"


@router.callback_query(F.data == "qz:start")
async def on_quiz_start(cb: CallbackQuery):
    try:
        await cb.message.edit_text(quiz_text(0), reply_markup=quiz_kb(0, 0))
    except Exception:  # noqa: BLE001
        await cb.message.answer(quiz_text(0), reply_markup=quiz_kb(0, 0))
    await cb.answer()


@router.callback_query(F.data.startswith("qz:a:"))
async def on_quiz_answer(cb: CallbackQuery):
    _, _, q_idx, chosen, score = cb.data.split(":")
    q_idx, chosen, score = int(q_idx), int(chosen), int(score)
    q = content.QUIZ[q_idx]
    correct = chosen == q["correct"]
    if correct:
        score += 1
    await cb.answer(("Верно. " if correct else "Неверно. ") + q["note"], show_alert=False)

    nxt = q_idx + 1
    if nxt < len(content.QUIZ):
        await cb.message.edit_text(quiz_text(nxt), reply_markup=quiz_kb(nxt, score))
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Оставить заявку", callback_data="lead:start")],
            [InlineKeyboardButton(text="Пройти ещё раз", callback_data="qz:start"),
             InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:main")],
        ])
        await cb.message.edit_text(content.quiz_result(score), reply_markup=kb)


# ---------------- заявка (после контента) ----------------

@router.callback_query(F.data == "lead:start")
async def on_lead_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Lead.waiting_answer)
    await cb.message.answer(content.LEAD_PROMPT)
    await cb.answer()


@router.message(Lead.waiting_answer, F.chat.type == ChatType.PRIVATE, F.text, ~F.text.startswith("/"))
async def on_answer(message: Message, state: FSMContext, bot: Bot):
    name, city = parse_answer(message.text)
    db.save_answer(message.from_user.id, message.text, name, city)
    await state.clear()
    await message.answer(
        f"Благодарю за ответ. Наш специалист {SPECIALIST_NAME} свяжется с вами в "
        "ближайшее время. 🤝\n"
        "При желании можно поделиться номером телефона для связи.",
        reply_markup=contact_kb(),
    )
    await push_lead_to_group(bot, message.from_user.id)


@router.message(F.chat.type == ChatType.PRIVATE, F.contact)
async def on_contact(message: Message, bot: Bot):
    if message.contact.user_id and message.contact.user_id != message.from_user.id:
        return
    db.save_phone(message.from_user.id, message.contact.phone_number)
    await message.answer("Спасибо, данные приняты. ✅", reply_markup=ReplyKeyboardRemove())
    await message.answer("Можно вернуться в главное меню.", reply_markup=menu_kb("main"))
    await bot.send_message(
        ADMIN_CHAT_ID,
        f"📞 Клиент <code>{message.from_user.id}</code> оставил номер: "
        f"<code>{esc(message.contact.phone_number)}</code>",
    )


@router.message(F.chat.type == ChatType.PRIVATE, F.text == "Пропустить")
async def on_skip(message: Message):
    await message.answer("Хорошо. Специалист свяжется с вами по данным из профиля.",
                         reply_markup=ReplyKeyboardRemove())
    await message.answer("Можно вернуться в главное меню.", reply_markup=menu_kb("main"))


@router.message(F.chat.type == ChatType.PRIVATE, F.text, ~F.text.startswith("/"))
async def on_followup(message: Message, bot: Bot):
    """Свободный текст вне анкеты: подсказка с меню; для действующих лидов — релей в общий чат."""
    u = message.from_user
    row = db.get_user(u.id)
    if row and row["stage"] in (db.STAGE_ANSWERED, db.STAGE_CONTACTED, db.STAGE_CLOSED):
        sent = await bot.send_message(
            ADMIN_CHAT_ID,
            f'💬 Сообщение от <a href="tg://user?id={u.id}">'
            f"{esc(row['contact_name']) or 'клиента'}</a>:\n"
            f"{esc(message.text)}\n\n↩️ Ответить — reply на это сообщение.",
        )
        db.link_message(sent.message_id, u.id)
        await message.answer("Сообщение передано специалисту.", reply_markup=menu_kb("main"))
    else:
        if not row:
            db.upsert_start(u.id, u.username, u.first_name, u.last_name)
        await message.answer(
            "Ниже — основные разделы бота. Для консультации можно оставить заявку.",
            reply_markup=menu_kb("main"),
        )


# ---------------- общий чат: reply -> клиенту, кнопка "взять" ----------------

@router.message(F.chat.id == ADMIN_CHAT_ID, F.reply_to_message, F.text)
async def on_group_reply(message: Message, bot: Bot):
    if message.text.startswith("/"):
        return
    target = db.user_by_message(message.reply_to_message.message_id)
    if not target:
        return
    try:
        await bot.send_message(target, message.text)
        db.set_stage(target, db.STAGE_CONTACTED, assigned_to=(f"@{message.from_user.username}"
                                                              if message.from_user.username
                                                              else str(message.from_user.id)))
        await message.reply("✅ Отправлено клиенту")
    except Exception as e:  # noqa: BLE001
        await message.reply(f"⚠️ Не удалось отправить: {esc(e)}")


@router.callback_query(F.data.startswith("take:"))
async def on_take(cb: CallbackQuery):
    user_id = int(cb.data.split(":", 1)[1])
    who = f"@{cb.from_user.username}" if cb.from_user.username else cb.from_user.full_name
    db.set_stage(user_id, db.STAGE_CONTACTED, assigned_to=who)
    try:
        await cb.message.edit_text(cb.message.html_text + f"\n\n🔒 Взял в работу: <b>{esc(who)}</b>")
    except Exception:  # noqa: BLE001
        pass
    await cb.answer("Лид закреплён за вами")


# ---------------- админ-команды ----------------

@router.message(Command("whoami"))
async def cmd_whoami(message: Message):
    await message.answer(
        f"Ваш ID: <code>{message.from_user.id}</code>\n"
        f"Права администратора: {'да ✅' if is_admin(message.from_user.id) else 'нет ❌'}"
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    # /admin <секрет> — самостоятельная регистрация админом. Только в личке.
    if message.chat.type != ChatType.PRIVATE:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not ADMIN_SECRET or parts[1].strip() != ADMIN_SECRET:
        return  # молча игнорируем — чтобы не палить наличие команды
    db.add_admin(message.from_user.id, message.from_user.username)
    await set_commands(message.bot)  # показать этому админу меню админ-команд
    await message.answer("Готово. Вы теперь администратор ✅\nКоманды: /help")


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "<b>Админ-команды</b>\n"
        "/base — список всех обратившихся и их этап\n"
        "/export — выгрузка всех клиентов в CSV\n"
        "/broadcast <текст> — рассылка сообщения всем клиентам\n"
        "/wipe — очистить всю базу клиентов (с подтверждением)\n"
        "/whoami — узнать свой ID\n\n"
        "В общем чате: <b>reply</b> на заявку — ответ уходит клиенту; "
        "кнопка «Взять в работу» закрепляет лида."
    )


@router.message(Command("base"))
async def cmd_base(message: Message):
    if not is_admin(message.from_user.id):
        return
    rows = db.all_users()
    if not rows:
        await message.answer("База пуста.")
        return
    lines = [f"<b>Всего в базе: {len(rows)}</b>\n"]
    for r in rows[:60]:
        uname = f"@{r['username']}" if r["username"] else f"id{r['user_id']}"
        stage = db.STAGE_RU.get(r["stage"], r["stage"])
        name = r["contact_name"] or r["first_name"] or "—"
        lines.append(f"• {esc(name)} ({esc(r['city']) or '—'}) — {esc(uname)} — <i>{stage}</i>")
    if len(rows) > 60:
        lines.append(f"\n…и ещё {len(rows) - 60}. Полный список: /export")
    await message.answer("\n".join(lines))


@router.message(Command("export"))
async def cmd_export(message: Message):
    if not is_admin(message.from_user.id):
        return
    rows = db.all_users()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["user_id", "username", "имя", "город", "телефон", "этап",
                "полный_ответ", "закреплён_за", "ссылка", "создан", "обновлён"])
    for r in rows:
        w.writerow([
            r["user_id"], r["username"] or "", r["contact_name"] or "", r["city"] or "",
            r["phone"] or "", db.STAGE_RU.get(r["stage"], r["stage"]), r["raw_answer"] or "",
            r["assigned_to"] or "", contact_link(r), r["created_at"] or "", r["updated_at"] or "",
        ])
    data = ("﻿" + buf.getvalue()).encode("utf-8")  # BOM — чтобы Excel не ломал кириллицу
    await message.answer_document(
        BufferedInputFile(data, filename="leads.csv"),
        caption=f"Выгрузка: {len(rows)} клиентов",
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        await message.answer("Использование: <code>/broadcast текст сообщения</code>")
        return
    text = parts[1].strip()
    pending_broadcast[message.from_user.id] = text
    targets = db.broadcast_targets()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"📤 Отправить ({len(targets)})", callback_data="bc:yes"),
        InlineKeyboardButton(text="Отмена", callback_data="bc:no"),
    ]])
    await message.answer(f"Предпросмотр рассылки:\n\n{esc(text)}\n\nПолучателей: {len(targets)}", reply_markup=kb)


@router.callback_query(F.data.startswith("bc:"))
async def on_broadcast_confirm(cb: CallbackQuery, bot: Bot):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    action = cb.data.split(":", 1)[1]
    text = pending_broadcast.pop(cb.from_user.id, None)
    if action == "no" or not text:
        await cb.message.edit_text("Рассылка отменена.")
        await cb.answer()
        return
    await cb.message.edit_text("Рассылка запущена…")
    await cb.answer()
    ok = fail = 0
    for uid in db.broadcast_targets():
        try:
            await bot.send_message(uid, text)
            ok += 1
        except Exception:  # noqa: BLE001  — заблокировали бота и т.п.
            fail += 1
        await asyncio.sleep(0.05)  # ~20 сообщений/сек, лимит Telegram ~30
    await cb.message.answer(f"Готово ✅\nДоставлено: {ok}\nНе доставлено: {fail}")


@router.message(Command("wipe"))
async def cmd_wipe(message: Message):
    if not is_admin(message.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚠️ Да, очистить всё", callback_data="wipe:yes"),
        InlineKeyboardButton(text="Отмена", callback_data="wipe:no"),
    ]])
    await message.answer(
        "Очистить <b>всю базу клиентов</b>? Удалятся все лиды и их этапы <b>безвозвратно</b>.\n"
        "(Список администраторов сохранится.)",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("wipe:"))
async def on_wipe(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    if cb.data.split(":", 1)[1] == "no":
        await cb.message.edit_text("Очистка отменена.")
        await cb.answer()
        return
    n = db.wipe()
    await cb.message.edit_text(f"База очищена ✅\nУдалено записей: {n}")
    await cb.answer("Готово")


# ---------------- меню команд ----------------

CLIENT_CMDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="menu", description="Главное меню"),
]
ADMIN_CMDS = [
    BotCommand(command="base", description="📋 База заявок и этапы"),
    BotCommand(command="export", description="📥 Выгрузка в CSV"),
    BotCommand(command="broadcast", description="📤 Рассылка клиентам"),
    BotCommand(command="wipe", description="🗑 Очистить базу"),
    BotCommand(command="whoami", description="🆔 Мой ID и права"),
    BotCommand(command="help", description="❓ Помощь"),
]


async def set_commands(bot: Bot) -> None:
    """Клиенты видят только /start; админы (и общий чат) — полный список."""
    await bot.set_my_commands(CLIENT_CMDS, scope=BotCommandScopeDefault())
    targets = (set(ADMIN_IDS) | db.db_admin_ids()) | {ADMIN_CHAT_ID}
    for chat_id in targets:
        try:
            await bot.set_my_commands(ADMIN_CMDS, scope=BotCommandScopeChat(chat_id=chat_id))
        except Exception as e:  # noqa: BLE001 — админ ещё не открывал бота / бот не в группе
            log.warning("set_my_commands для %s не удалось: %s", chat_id, e)


# ---------------- запуск ----------------

async def main():
    db.init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    me = await bot.get_me()
    await set_commands(bot)
    try:
        await bot.set_my_description(content.BOT_DESCRIPTION)
        await bot.set_my_short_description(content.BOT_SHORT_DESCRIPTION)
    except Exception as e:  # noqa: BLE001 — не критично для работы бота
        log.warning("set_my_description не удалось: %s", e)
    log.info("Бот @%s запущен. Админов в env: %d", me.username, len(ADMIN_IDS))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлено")
