import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from html import escape
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType, ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, ChatMemberUpdatedFilter, IS_MEMBER, IS_NOT_MEMBER
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineQuery, InlineQueryResultArticle,
    InputTextMessageContent, InlineKeyboardMarkup, InlineKeyboardButton,
    ChatMemberUpdated, ChosenInlineResult
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "evrik.db")
NO_EMOJI = os.getenv("NO_EMOJI", "❌")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# Кэш для принудительного обновления кастомных эмодзи
inline_cache: dict[str, str] = {}


# ==================== DATABASE ====================
@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


async def init_db():
    async with get_db() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                is_admin INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS groups (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                arp_enabled INTEGER DEFAULT 1,
                crp_enabled INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS rp_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                emoji TEXT NOT NULL,
                trigger TEXT NOT NULL UNIQUE,
                action TEXT NOT NULL,
                is_18plus INTEGER DEFAULT 0,
                is_basic INTEGER DEFAULT 1,
                is_active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS custom_rp (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                emoji TEXT NOT NULL,
                trigger TEXT NOT NULL,
                action TEXT NOT NULL,
                UNIQUE(user_id, trigger)
            );
        """)
        await db.commit()

        if ADMIN_ID:
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id, is_admin) VALUES (?, 1)",
                (ADMIN_ID,)
            )
            await db.execute(
                "UPDATE users SET is_admin = 1 WHERE user_id = ?",
                (ADMIN_ID,)
            )
            await db.commit()

        basic = [
            ("🤗", "обнять", "обнял(а)"),
            ("💋", "поцеловать", "поцеловал(а)"),
            ("👋", "погладить", "погладил(а)"),
            ("👊", "ударить", "ударил(а)"),
            ("🤝", "пожать руку", "пожал(а) руку"),
            ("🦶", "пнуть", "пнул(а)"),
            ("😜", "укусить", "укусил(а)"),
            ("👀", "посмотреть", "посмотрел(а) на"),
            ("😴", "усыпить", "усыпил(а)"),
            ("🎂", "подарить торт", "подарил(а) торт"),
            ("🌹", "подарить цветы", "подарил(а) цветы"),
            ("💪", "похвалить", "похвалил(а)"),
            ("🫣", "смутить", "смутил(а)"),
            ("😳", "удивить", "удивил(а)"),
            ("😢", "расстроить", "расстроил(а)"),
            ("😡", "разозлить", "разозлил(а)"),
            ("🤡", "высмеять", "высмеял(а)"),
            ("💀", "убить", "убил(а)"),
            ("👻", "напугать", "напутал(а)"),
            ("🪄", "заколдовать", "заколдовал(а)"),
        ]
        adult = [
            ("😈", "выебать", "выебал(а)"),
            ("🍑", "отшлёпать", "отшлёпал(а)"),
            ("👅", "лизнуть", "лизнул(а)"),
            ("🔥", "возбудить", "возбудил(а)"),
            ("💦", "кончить на", "кончил(а) на"),
            ("🍆", "засунуть", "засунул(а)"),
            ("🤤", "сосать", "сосал(а)"),
            ("🥵", "трахнуть", "трахнул(а)"),
            ("🫦", "укусить за губу", "укусил(а) за губу"),
            ("🔞", "раздеть", "раздел(а)"),
        ]

        for emoji, trigger, action in basic:
            await db.execute(
                "INSERT OR IGNORE INTO rp_commands (emoji, trigger, action, is_18plus, is_basic) VALUES (?, ?, ?, 0, 1)",
                (emoji, trigger.lower(), action)
            )
        for emoji, trigger, action in adult:
            await db.execute(
                "INSERT OR IGNORE INTO rp_commands (emoji, trigger, action, is_18plus, is_basic) VALUES (?, ?, ?, 1, 1)",
                (emoji, trigger.lower(), action)
            )
        await db.commit()


async def ensure_user(user_id: int, username: str = None, first_name: str = None):
    async with get_db() as db:
        await db.execute(
            """INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
               username = COALESCE(excluded.username, users.username),
               first_name = COALESCE(excluded.first_name, users.first_name)""",
            (user_id, username, first_name)
        )
        await db.commit()


async def is_admin(user_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute("SELECT is_admin FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return bool(row and row["is_admin"])


async def get_group(chat_id: int):
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM groups WHERE chat_id = ?", (chat_id,))
        return await cur.fetchone()


async def ensure_group(chat_id: int, title: str = None):
    async with get_db() as db:
        await db.execute(
            """INSERT INTO groups (chat_id, title) VALUES (?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET title = COALESCE(excluded.title, groups.title)""",
            (chat_id, title)
        )
        await db.commit()


# ==================== HELPERS ====================
def mention(user) -> str:
    uid = getattr(user, "id", user)

    name = None
    if hasattr(user, "full_name") and user.full_name:
        name = user.full_name.strip()
    else:
        first = (getattr(user, "first_name", None) or "").strip()
        last = (getattr(user, "last_name", None) or "").strip()
        name = f"{first} {last}".strip()

    if name:
        return f'<a href="tg://user?id={uid}">{escape(name)}</a>'

    username = getattr(user, "username", None)
    if username:
        return f'<a href="tg://user?id={uid}">@{escape(username)}</a>'

    return f'<a href="tg://user?id={uid}">{uid}</a>'


def render_emoji(emoji: str) -> str:
    if str(emoji).isdigit():
        return f'<tg-emoji emoji-id="{emoji}">✨</tg-emoji>'
    return emoji


def format_rp(emoji: str, user_name: str, action: str, target_name: str) -> str:
    return f"{render_emoji(emoji)} {user_name} {action} {target_name}"


def extract_custom_emoji_id(message: Message) -> Optional[str]:
    if not message.entities:
        return None
    for ent in message.entities:
        if ent.type == "custom_emoji" and ent.custom_emoji_id:
            return str(ent.custom_emoji_id)
    return None


# ==================== FSM ====================
class AdminFSM(StatesGroup):
    add_emoji = State()
    add_trigger = State()
    add_action = State()
    add_adult = State()
    edit_id = State()
    edit_field = State()
    edit_value = State()


# ==================== COMMANDS ====================
@router.message(Command("start"))
async def cmd_start(message: Message):
    await ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    text = (
        "Привет! Я <b>Еврик</b> — бот для РП-команд.\n\n"
        "• В группах ответь на сообщение командой (например <code>обнять</code>)\n"
        "• Или используй инлайн-режим: <code>@бот команда</code>\n"
        "• Создать свою РП: <code>/nrp эмодзи / триггер / действие</code>\n"
        "• Админам: <code>/admin</code>"
    )
    await message.answer(text)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Команды:</b>\n"
        "/nrp — создать кастомную РП\n"
        "/mrp — мои кастомные РП\n"
        "/drp — удалить свою РП\n"
        "/arp — вкл/выкл 18+ в группе (админ чата)\n"
        "/crp — вкл/выкл кастомные РП в группе (админ чата)\n"
        "/admin — админ-панель (только бот-админы)"
    )


@router.message(Command("nrp"))
async def cmd_nrp(message: Message, command: CommandObject):
    await ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if not command.args:
        await message.answer(
            "Формат:\n<code>/nrp эмодзи / триггер / действие</code>\n\n"
            "Пример:\n<code>/nrp 😡 / обматерить / обматерил</code>\n\n"
            "Можно отправить премиум-эмодзи — бот сам его сохранит.\n"
            "Если нет премиум — просто напиши ID эмодзи."
        )
        return

    custom_id = extract_custom_emoji_id(message)
    parts = [p.strip() for p in command.args.split("/")]
    if len(parts) != 3:
        await message.answer("Нужно ровно 3 части через / : эмодзи / триггер / действие")
        return

    emoji_raw, trigger, action = parts
    trigger = trigger.lower().strip()
    if not trigger or not action:
        await message.answer("Триггер и действие не могут быть пустыми")
        return

    emoji = custom_id if custom_id else emoji_raw.strip()

    async with get_db() as db:
        try:
            await db.execute(
                "INSERT INTO custom_rp (user_id, emoji, trigger, action) VALUES (?, ?, ?, ?)",
                (message.from_user.id, emoji, trigger, action)
            )
            await db.commit()
            shown = render_emoji(emoji)
            await message.answer(f"✅ Кастомная РП создана: {shown} <b>{trigger}</b>")
        except Exception:
            await message.answer("У тебя уже есть РП с таким триггером. Удали старую через /drp")


@router.message(Command("mrp"))
async def cmd_mrp(message: Message):
    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, emoji, trigger, action FROM custom_rp WHERE user_id = ?",
            (message.from_user.id,)
        )
        rows = await cur.fetchall()

    if not rows:
        await message.answer("У тебя пока нет кастомных РП.")
        return

    text = "<b>Твои кастомные РП:</b>\n\n"
    for r in rows:
        em = render_emoji(r["emoji"])
        text += f"#{r['id']} {em} <code>{r['trigger']}</code> → {r['action']}\n"
    await message.answer(text)


@router.message(Command("drp"))
async def cmd_drp(message: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Использование: <code>/drp ID</code>\nСписок своих РП: /mrp")
        return

    rid = int(command.args.strip())
    async with get_db() as db:
        cur = await db.execute(
            "DELETE FROM custom_rp WHERE id = ? AND user_id = ?",
            (rid, message.from_user.id)
        )
        await db.commit()
        if cur.rowcount:
            await message.answer("✅ Удалено")
        else:
            await message.answer("РП не найдена или не твоя")


@router.message(Command("arp"))
async def cmd_arp(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Только в группах")
        return

    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await message.answer("Только администраторы чата")
        return

    await ensure_group(message.chat.id, message.chat.title)
    async with get_db() as db:
        cur = await db.execute("SELECT arp_enabled FROM groups WHERE chat_id = ?", (message.chat.id,))
        row = await cur.fetchone()
        new_val = 0 if row["arp_enabled"] else 1
        await db.execute("UPDATE groups SET arp_enabled = ? WHERE chat_id = ?", (new_val, message.chat.id))
        await db.commit()

    status = "включены" if new_val else "выключены"
    await message.answer(f"18+ РП-команды теперь <b>{status}</b> в этом чате.")


@router.message(Command("crp"))
async def cmd_crp(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Только в группах")
        return

    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await message.answer("Только администраторы чата")
        return

    await ensure_group(message.chat.id, message.chat.title)
    async with get_db() as db:
        cur = await db.execute("SELECT crp_enabled FROM groups WHERE chat_id = ?", (message.chat.id,))
        row = await cur.fetchone()
        new_val = 0 if row["crp_enabled"] else 1
        await db.execute("UPDATE groups SET crp_enabled = ? WHERE chat_id = ?", (new_val, message.chat.id))
        await db.commit()

    status = "включены" if new_val else "выключены"
    await message.answer(f"Кастомные РП теперь <b>{status}</b> в этом чате.")


# ==================== ADMIN PANEL ====================
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Базовые РП", callback_data="adm:basic")
    kb.button(text="➕ Добавить базовую РП", callback_data="adm:add")
    kb.button(text="🎨 Все кастомные", callback_data="adm:customs")
    kb.button(text="💬 Чаты бота", callback_data="adm:chats")
    kb.adjust(1)
    await message.answer("<b>Админ-панель Еврика</b>", reply_markup=kb.as_markup())


@router.callback_query(F.data == "adm:back")
async def adm_back(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id):
        return
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Базовые РП", callback_data="adm:basic")
    kb.button(text="➕ Добавить базовую РП", callback_data="adm:add")
    kb.button(text="🎨 Все кастомные", callback_data="adm:customs")
    kb.button(text="💬 Чаты бота", callback_data="adm:chats")
    kb.adjust(1)
    await cb.message.edit_text("<b>Админ-панель Еврика</b>", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data == "adm:basic")
async def adm_basic(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)

    async with get_db() as db:
        cur = await db.execute(
            "SELECT id, emoji, trigger, is_18plus, is_active FROM rp_commands WHERE is_basic = 1 ORDER BY is_18plus, trigger"
        )
        rows = await cur.fetchall()

    text = "<b>Базовые РП</b>\nНажми на команду, чтобы управлять ей:\n\n"
    kb = InlineKeyboardBuilder()
    for r in rows:
        status = "✅" if r["is_active"] else "❌"
        adult = "🔞" if r["is_18plus"] else ""
        kb.button(
            text=f"{status} {r['trigger']} {adult}",
            callback_data=f"adm:rp:{r['id']}"
        )
    kb.button(text="◀️ Назад", callback_data="adm:back")
    kb.adjust(1)
    await cb.message.edit_text(text, reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("adm:rp:"))
async def adm_rp_manage(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)

    rid = int(cb.data.split(":")[2])
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM rp_commands WHERE id = ?", (rid,))
        rp = await cur.fetchone()

    if not rp:
        return await cb.answer("Не найдено", show_alert=True)

    em = render_emoji(rp["emoji"])
    text = (
        f"<b>РП #{rp['id']}</b>\n\n"
        f"Эмодзи: {em}\n"
        f"Триггер: <code>{rp['trigger']}</code>\n"
        f"Действие: {rp['action']}\n"
        f"18+: {'Да' if rp['is_18plus'] else 'Нет'}\n"
        f"Активна: {'Да' if rp['is_active'] else 'Нет'}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Вкл/Выкл", callback_data=f"adm:toggle:{rid}")
    kb.button(text="✏️ Изменить эмодзи", callback_data=f"adm:edit:{rid}:emoji")
    kb.button(text="✏️ Изменить триггер", callback_data=f"adm:edit:{rid}:trigger")
    kb.button(text="✏️ Изменить действие", callback_data=f"adm:edit:{rid}:action")
    kb.button(text="🗑 Удалить", callback_data=f"adm:del:{rid}")
    kb.button(text="◀️ Назад", callback_data="adm:basic")
    kb.adjust(1)
    await cb.message.edit_text(text, reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("adm:toggle:"))
async def adm_toggle(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)

    rid = int(cb.data.split(":")[2])
    async with get_db() as db:
        cur = await db.execute("SELECT is_active FROM rp_commands WHERE id = ?", (rid,))
        row = await cur.fetchone()
        if not row:
            return await cb.answer("Не найдено")
        new_val = 0 if row["is_active"] else 1
        await db.execute("UPDATE rp_commands SET is_active = ? WHERE id = ?", (new_val, rid))
        await db.commit()

    await cb.answer("Обновлено")
    cb.data = f"adm:rp:{rid}"
    await adm_rp_manage(cb)


@router.callback_query(F.data.startswith("adm:del:"))
async def adm_del(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)

    rid = int(cb.data.split(":")[2])
    async with get_db() as db:
        await db.execute("DELETE FROM rp_commands WHERE id = ? AND is_basic = 1", (rid,))
        await db.commit()

    await cb.answer("Удалено")
    await adm_basic(cb)


@router.callback_query(F.data.startswith("adm:edit:"))
async def adm_edit_start(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)

    _, _, rid, field = cb.data.split(":")
    await state.set_state(AdminFSM.edit_value)
    await state.update_data(edit_id=int(rid), edit_field=field)
    await cb.message.answer(f"Отправь новое значение для <b>{field}</b>:\n(или /cancel)")
    await cb.answer()


@router.message(AdminFSM.edit_value)
async def adm_edit_value(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/cancel"):
        await state.clear()
        return await message.answer("Отменено")

    data = await state.get_data()
    rid = data["edit_id"]
    field = data["edit_field"]
    value = message.text.strip() if message.text else ""

    if field == "emoji":
        custom_id = extract_custom_emoji_id(message)
        value = custom_id if custom_id else value

    if not value:
        return await message.answer("Пустое значение, попробуй ещё раз")

    allowed = {"emoji", "trigger", "action"}
    if field not in allowed:
        await state.clear()
        return await message.answer("Ошибка поля")

    async with get_db() as db:
        if field == "trigger":
            value = value.lower()
        await db.execute(f"UPDATE rp_commands SET {field} = ? WHERE id = ?", (value, rid))
        await db.commit()

    await state.clear()
    await message.answer("✅ Обновлено")


@router.callback_query(F.data == "adm:add")
async def adm_add_start(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)

    await state.set_state(AdminFSM.add_emoji)
    await cb.message.answer("Отправь эмодзи (можно премиум или ID):\n/cancel для отмены")
    await cb.answer()


@router.message(AdminFSM.add_emoji)
async def adm_add_emoji(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/cancel"):
        await state.clear()
        return await message.answer("Отменено")

    custom_id = extract_custom_emoji_id(message)
    emoji = custom_id if custom_id else (message.text or "").strip()
    if not emoji:
        return await message.answer("Не понял эмодзи, попробуй ещё")

    await state.update_data(emoji=emoji)
    await state.set_state(AdminFSM.add_trigger)
    await message.answer("Теперь отправь <b>триггер</b> (то, что будут писать):")


@router.message(AdminFSM.add_trigger)
async def adm_add_trigger(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/cancel"):
        await state.clear()
        return await message.answer("Отменено")

    trigger = (message.text or "").strip().lower()
    if not trigger:
        return await message.answer("Пустой триггер")

    await state.update_data(trigger=trigger)
    await state.set_state(AdminFSM.add_action)
    await message.answer("Теперь отправь <b>действие</b> (что будет в результате):")


@router.message(AdminFSM.add_action)
async def adm_add_action(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/cancel"):
        await state.clear()
        return await message.answer("Отменено")

    action = (message.text or "").strip()
    if not action:
        return await message.answer("Пустое действие")

    await state.update_data(action=action)
    await state.set_state(AdminFSM.add_adult)
    kb = InlineKeyboardBuilder()
    kb.button(text="Обычная", callback_data="adm:addadult:0")
    kb.button(text="18+", callback_data="adm:addadult:1")
    await message.answer("Это 18+ команда?", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("adm:addadult:"))
async def adm_add_finish(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id):
        return

    is_adult = int(cb.data.split(":")[2])
    data = await state.get_data()
    await state.clear()

    async with get_db() as db:
        try:
            await db.execute(
                "INSERT INTO rp_commands (emoji, trigger, action, is_18plus, is_basic, is_active) VALUES (?, ?, ?, ?, 1, 1)",
                (data["emoji"], data["trigger"], data["action"], is_adult)
            )
            await db.commit()
            await cb.message.edit_text(f"✅ Добавлено: <b>{data['trigger']}</b>")
        except Exception as e:
            await cb.message.edit_text(f"Ошибка (возможно такой триггер уже есть):\n{e}")
    await cb.answer()


@router.callback_query(F.data == "adm:customs")
async def adm_customs(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)

    async with get_db() as db:
        cur = await db.execute("""
            SELECT c.id, c.emoji, c.trigger, c.action, c.user_id, u.username, u.first_name
            FROM custom_rp c
            LEFT JOIN users u ON u.user_id = c.user_id
            ORDER BY c.id DESC LIMIT 50
        """)
        rows = await cur.fetchall()

    if not rows:
        text = "Кастомных РП пока нет."
    else:
        text = "<b>Последние кастомные РП:</b>\n\n"
        for r in rows:
            uname = f"@{r['username']}" if r["username"] else (r["first_name"] or str(r["user_id"]))
            em = render_emoji(r["emoji"])
            text += f"#{r['id']} {em} <code>{r['trigger']}</code> → {r['action']} (от {uname})\n"

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data="adm:back")
    await cb.message.edit_text(text[:4000], reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data == "adm:chats")
async def adm_chats(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)

    async with get_db() as db:
        cur = await db.execute("SELECT chat_id, title, arp_enabled, crp_enabled FROM groups ORDER BY title")
        rows = await cur.fetchall()

    if not rows:
        text = "Бот пока не добавлен ни в один чат (по БД)."
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data="adm:back")
        await cb.message.edit_text(text, reply_markup=kb.as_markup())
        await cb.answer()
        return

    text = "<b>Чаты бота</b>\nНажми на чат, чтобы управлять:\n\n"
    kb = InlineKeyboardBuilder()
    for r in rows:
        title = (r["title"] or str(r["chat_id"]))[:30]
        arp = "🔞✅" if r["arp_enabled"] else "🔞❌"
        crp = "🎨✅" if r["crp_enabled"] else "🎨❌"
        kb.button(
            text=f"{title} {arp}{crp}",
            callback_data=f"adm:chat:{r['chat_id']}"
        )
    kb.button(text="◀️ Назад", callback_data="adm:back")
    kb.adjust(1)
    await cb.message.edit_text(text, reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("adm:chat:"))
async def adm_chat_manage(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)

    chat_id = int(cb.data.split(":")[2])
    async with get_db() as db:
        cur = await db.execute("SELECT * FROM groups WHERE chat_id = ?", (chat_id,))
        g = await cur.fetchone()

    if not g:
        return await cb.answer("Чат не найден", show_alert=True)

    text = (
        f"<b>Чат:</b> {g['title'] or chat_id}\n"
        f"<code>{chat_id}</code>\n\n"
        f"18+ РП: {'✅ Вкл' if g['arp_enabled'] else '❌ Выкл'}\n"
        f"Кастомные РП: {'✅ Вкл' if g['crp_enabled'] else '❌ Выкл'}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 18+", callback_data=f"adm:chatarp:{chat_id}")
    kb.button(text="🔄 Кастомные", callback_data=f"adm:chatcrp:{chat_id}")
    kb.button(text="◀️ Назад", callback_data="adm:chats")
    kb.adjust(1)
    await cb.message.edit_text(text, reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("adm:chatarp:"))
async def adm_chat_arp(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return
    chat_id = int(cb.data.split(":")[2])
    async with get_db() as db:
        cur = await db.execute("SELECT arp_enabled FROM groups WHERE chat_id = ?", (chat_id,))
        row = await cur.fetchone()
        new_val = 0 if row["arp_enabled"] else 1
        await db.execute("UPDATE groups SET arp_enabled = ? WHERE chat_id = ?", (new_val, chat_id))
        await db.commit()

    await cb.answer("Обновлено")
    cb.data = f"adm:chat:{chat_id}"
    await adm_chat_manage(cb)


@router.callback_query(F.data.startswith("adm:chatcrp:"))
async def adm_chat_crp(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return
    chat_id = int(cb.data.split(":")[2])
    async with get_db() as db:
        cur = await db.execute("SELECT crp_enabled FROM groups WHERE chat_id = ?", (chat_id,))
        row = await cur.fetchone()
        new_val = 0 if row["crp_enabled"] else 1
        await db.execute("UPDATE groups SET crp_enabled = ? WHERE chat_id = ?", (new_val, chat_id))
        await db.commit()

    await cb.answer("Обновлено")
    cb.data = f"adm:chat:{chat_id}"
    await adm_chat_manage(cb)


# ==================== RP IN GROUPS ====================
@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.reply_to_message)
async def rp_handler(message: Message):
    text = (message.text or "").strip().lower()
    if not text or text.startswith("/"):
        return

    text = re.sub(r"@\w+", "", text).strip()

    await ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await ensure_group(message.chat.id, message.chat.title)

    group = await get_group(message.chat.id)
    arp_ok = group["arp_enabled"] if group else True
    crp_ok = group["crp_enabled"] if group else True

    async with get_db() as db:
        cur = await db.execute(
            "SELECT emoji, action, is_18plus FROM rp_commands WHERE trigger = ? AND is_active = 1 AND is_basic = 1",
            (text,)
        )
        rp = await cur.fetchone()
        if rp:
            if rp["is_18plus"] and not arp_ok:
                return
            user_m = mention(message.from_user)
            target_m = mention(message.reply_to_message.from_user)
            await message.reply(format_rp(rp["emoji"], user_m, rp["action"], target_m))
            return

        if crp_ok:
            cur = await db.execute(
                "SELECT emoji, action FROM custom_rp WHERE user_id = ? AND trigger = ?",
                (message.from_user.id, text)
            )
            custom = await cur.fetchone()
            if custom:
                user_m = mention(message.from_user)
                target_m = mention(message.reply_to_message.from_user)
                await message.reply(format_rp(custom["emoji"], user_m, custom["action"], target_m))


# ==================== INLINE MODE ====================
@router.inline_query()
async def inline_handler(query: InlineQuery):
    await ensure_user(query.from_user.id, query.from_user.username, query.from_user.first_name)
    q = (query.query or "").strip().lower()
    results = []

    async with get_db() as db:
        # Базовые команды
        if q:
            cur = await db.execute(
                "SELECT id, emoji, trigger, action, is_18plus FROM rp_commands "
                "WHERE is_active = 1 AND is_basic = 1 AND trigger LIKE ? "
                "ORDER BY trigger LIMIT 30",
                (f"%{q}%",)
            )
        else:
            cur = await db.execute(
                "SELECT id, emoji, trigger, action, is_18plus FROM rp_commands "
                "WHERE is_active = 1 AND is_basic = 1 ORDER BY trigger LIMIT 30"
            )
        basics = await cur.fetchall()

        # Кастомные
        if q:
            cur = await db.execute(
                "SELECT id, emoji, trigger, action FROM custom_rp "
                "WHERE user_id = ? AND trigger LIKE ? LIMIT 20",
                (query.from_user.id, f"%{q}%")
            )
        else:
            cur = await db.execute(
                "SELECT id, emoji, trigger, action FROM custom_rp "
                "WHERE user_id = ? LIMIT 20",
                (query.from_user.id,)
            )
        customs = await cur.fetchall()

    all_rps = []
    for r in basics:
        all_rps.append({
            "id": f"b{r['id']}",
            "emoji": r["emoji"],
            "trigger": r["trigger"],
            "action": r["action"],
            "adult": bool(r["is_18plus"])
        })
    for r in customs:
        all_rps.append({
            "id": f"c{r['id']}",
            "emoji": r["emoji"],
            "trigger": r["trigger"],
            "action": r["action"],
            "adult": False
        })

    for r in all_rps[:40]:
        em_show = r["emoji"] if not str(r["emoji"]).isdigit() else "✨"
        title = f"{em_show} {r['trigger']}"
        if r["adult"]:
            title += " 🔞"

        msg_text = f"{render_emoji(r['emoji'])} {mention(query.from_user)} хочет {r['trigger']}"

        cache_key = f"{query.from_user.id}:{r['id']}"
        inline_cache[cache_key] = msg_text

        results.append(
            InlineQueryResultArticle(
                id=r["id"],
                title=title,
                description=f"хочет {r['trigger']}",
                input_message_content=InputTextMessageContent(
                    message_text=msg_text,
                    parse_mode=ParseMode.HTML
                ),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Согласиться",
                            callback_data=f"inl:yes:{r['id']}:{query.from_user.id}"
                        ),
                        InlineKeyboardButton(
                            text="❌ Отказаться",
                            callback_data=f"inl:no:{r['id']}:{query.from_user.id}"
                        )
                    ]
                ])
            )
        )

    try:
        await query.answer(results, cache_time=5, is_personal=True)
    except TelegramBadRequest as e:
        # Query уже протух — это нормально, просто игнорируем
        if "query is too old" in str(e).lower() or "query id is invalid" in str(e).lower():
            logger.debug(f"Inline query expired: {e}")
        else:
            raise


@router.chosen_inline_result()
async def chosen_inline_handler(result: ChosenInlineResult):
    if not result.inline_message_id:
        return

    cache_key = f"{result.from_user.id}:{result.result_id}"
    text = inline_cache.pop(cache_key, None)
    if not text:
        return

    try:
        await bot.edit_message_text(
            text=text,
            inline_message_id=result.inline_message_id,
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.debug(f"Chosen inline edit: {e}")


@router.callback_query(F.data.startswith("inl:"))
async def inline_callback(cb: CallbackQuery):
    parts = cb.data.split(":")
    if len(parts) != 4:
        return await cb.answer()

    action, rid, owner_id = parts[1], parts[2], int(parts[3])

    if cb.from_user.id == owner_id:
        return await cb.answer("Это предложение для другого человека", show_alert=True)

    async with get_db() as db:
        if rid.startswith("b"):
            cur = await db.execute(
                "SELECT emoji, trigger, action FROM rp_commands WHERE id = ?",
                (int(rid[1:]),)
            )
        else:
            cur = await db.execute(
                "SELECT emoji, trigger, action FROM custom_rp WHERE id = ?",
                (int(rid[1:]),)
            )
        rp = await cur.fetchone()

    if not rp:
        return await cb.answer("РП не найдена", show_alert=True)

    try:
        owner = await bot.get_chat(owner_id)
        owner_mention = mention(owner)
    except Exception:
        owner_mention = f'<a href="tg://user?id={owner_id}">{owner_id}</a>'

    target_mention = mention(cb.from_user)

    if action == "yes":
        text = format_rp(rp["emoji"], owner_mention, rp["action"], target_mention)
    else:
        text = f"{render_emoji(NO_EMOJI)} {target_mention} не хочет {rp['trigger']}"

    try:
        if cb.inline_message_id:
            await bot.edit_message_text(
                text=text,
                inline_message_id=cb.inline_message_id,
                parse_mode=ParseMode.HTML
            )
        elif cb.message:
            await cb.message.edit_text(text)
    except Exception as e:
        logger.error(f"Inline edit error: {e}")
        await cb.answer("Не удалось обновить сообщение", show_alert=True)
        return

    await cb.answer()


# ==================== TRACK GROUPS ====================
@router.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added(event: ChatMemberUpdated):
    await ensure_group(event.chat.id, event.chat.title)
    logger.info(f"Bot added to {event.chat.id} ({event.chat.title})")


@router.my_chat_member(ChatMemberUpdatedFilter(IS_MEMBER >> IS_NOT_MEMBER))
async def bot_removed(event: ChatMemberUpdated):
    async with get_db() as db:
        await db.execute("DELETE FROM groups WHERE chat_id = ?", (event.chat.id,))
        await db.commit()
    logger.info(f"Bot removed from {event.chat.id}")


# ==================== MAIN ====================
async def main():
    await init_db()
    logger.info("Database initialized")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
