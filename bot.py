"""
Еврик — РП-бот на aiogram 3 + aiosqlite
Один файл. Полная поддержка:
- РП реплаем в группах
- Инлайн-РП (везде)
- Кастомные РП (!рпк / !рпм / !рпу)
- Админка
- Настройки групп (NSFW + кастомные)
- Premium-эмодзи
- Упоминания с ссылкой
- Кнопки style="danger" / "success" / "primary"
"""

import asyncio
import html
import logging
import re
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    User,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties

# ====================== КОНФИГ ======================
BOT_TOKEN = "8994844279:AAEZ30IMcQNMuaniGPVCWz2l9-6fAenvBz0"
ADMIN_IDS = [8729666458]          # твои Telegram ID
DB_PATH = "evrik.db"

# Популярные premium-emoji (custom_emoji_id, fallback)
PREMIUM = {
    "fire":      ("5424972470023104089", "🔥"),
    "heart":     ("4996980495100150380", "❤️"),
    "star":      ("5438496463044752972", "⭐"),
    "check":     ("5206607081334906820", "✅"),
    "cross":     ("5210952531676504517", "❌"),
    "ban":       ("5260293700088511294", "🚫"),
    "warning":   ("5447644880824181073", "⚠️"),
    "info":      ("5323442290708985472", "ℹ️"),
    "settings":  ("5341715473882955310", "⚙️"),
    "pencil":    ("5395444784611480792", "✏️"),
    "trash":     ("5879896690210639947", "🗑"),
    "list":      ("5877597667231534929", "📋"),
    "plus":      ("5397916757333654639", "➕"),
    "new":       ("5382357040008021292", "🆕"),
    "help":      ("5334544901428229844", "❓"),
    "wave":      ("5870734657384877785", "👋"),
    "bot":       ("5931415565955503486", "🤖"),
    "like":      ("5337080053119336309", "👍"),
    "dislike":   ("5994368422031397063", "👎"),
    "sparkles":  ("5453969572354878595", "✨"),
}

def pe(key: str) -> str:
    """HTML-тег premium-эмодзи"""
    eid, fb = PREMIUM.get(key, ("", "•"))
    if not eid:
        return fb
    return f'<tg-emoji emoji-id="{eid}">{fb}</tg-emoji>'


# ====================== УТИЛИТЫ ======================
def mention(user: User) -> str:
    """Красивое упоминание с ссылкой"""
    name = html.escape(user.full_name or "Аноним")
    if user.username:
        return f'<a href="https://t.me/{user.username}">{name}</a>'
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def esc(text: str) -> str:
    return html.escape(str(text or ""))


# ====================== БАЗА ДАННЫХ ======================
class DB:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self.db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self._init()

    async def close(self):
        if self.db:
            await self.db.close()

    async def _init(self):
        await self.db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            full_name   TEXT,
            is_admin    INTEGER DEFAULT 0,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS groups (
            chat_id         INTEGER PRIMARY KEY,
            title           TEXT,
            allow_nsfw      INTEGER DEFAULT 0,
            allow_custom    INTEGER DEFAULT 1,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS rp_commands (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger     TEXT NOT NULL COLLATE NOCASE,
            action      TEXT NOT NULL,
            emoji       TEXT DEFAULT '',
            emoji_id    TEXT DEFAULT '',          -- premium custom_emoji_id
            is_nsfw     INTEGER DEFAULT 0,
            is_base     INTEGER DEFAULT 1,        -- 1 = базовая, 0 = кастомная
            owner_id    INTEGER DEFAULT 0,        -- 0 = базовая / админская
            UNIQUE(trigger, owner_id)
        );

        CREATE INDEX IF NOT EXISTS idx_rp_trigger ON rp_commands(trigger);
        CREATE INDEX IF NOT EXISTS idx_rp_owner ON rp_commands(owner_id);
        """)
        await self.db.commit()

        # Базовые РП (можно расширять)
        base = [
            ("обнять", "обнял(-а)", "🤗", "", 0),
            ("поцеловать", "поцеловал(-а)", "💋", "", 0),
            ("ударить", "ударил(-а)", "👊", "", 0),
            ("укусить", "укусил(-а)", "🧛", "", 0),
            ("погладить", "погладил(-а)", "✋", "", 0),
            ("пнуть", "пнул(-а)", "🦶", "", 0),
            ("датьпять", "дал(-а) пять", "🖐️", "", 0),
            ("поцеловатьвщеку", "поцеловал(-а) в щёку", "😘", "", 0),
            ("лизнуть", "лизнул(-а)", "👅", "", 1),
            ("шлёпнуть", "шлёпнул(-а)", "👋", "", 1),
            ("трахнуть", "трахнул(-а)", "🔞", "", 1),
            ("отсосать", "отсосал(-а)", "🔞", "", 1),
        ]
        for trigger, action, emoji, emoji_id, nsfw in base:
            await self.db.execute(
                """INSERT OR IGNORE INTO rp_commands
                   (trigger, action, emoji, emoji_id, is_nsfw, is_base, owner_id)
                   VALUES (?, ?, ?, ?, ?, 1, 0)""",
                (trigger, action, emoji, emoji_id, nsfw)
            )
        await self.db.commit()

    # ---------- users ----------
    async def upsert_user(self, user: User, is_admin: bool = False):
        await self.db.execute(
            """INSERT INTO users (user_id, username, full_name, is_admin)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   username=excluded.username,
                   full_name=excluded.full_name""",
            (user.id, user.username, user.full_name, int(is_admin or user.id in ADMIN_IDS))
        )
        await self.db.commit()

    async def is_admin(self, user_id: int) -> bool:
        if user_id in ADMIN_IDS:
            return True
        cur = await self.db.execute("SELECT is_admin FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return bool(row and row["is_admin"])

    async def set_admin(self, user_id: int, value: bool):
        await self.db.execute("UPDATE users SET is_admin=? WHERE user_id=?", (int(value), user_id))
        await self.db.commit()

    # ---------- groups ----------
    async def upsert_group(self, chat_id: int, title: str = ""):
        await self.db.execute(
            """INSERT INTO groups (chat_id, title) VALUES (?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title""",
            (chat_id, title)
        )
        await self.db.commit()

    async def get_group(self, chat_id: int) -> Optional[aiosqlite.Row]:
        cur = await self.db.execute("SELECT * FROM groups WHERE chat_id=?", (chat_id,))
        return await cur.fetchone()

    async def set_group_setting(self, chat_id: int, field: str, value: int):
        if field not in ("allow_nsfw", "allow_custom"):
            return
        await self.db.execute(f"UPDATE groups SET {field}=? WHERE chat_id=?", (value, chat_id))
        await self.db.commit()

    async def list_groups(self):
        cur = await self.db.execute("SELECT * FROM groups ORDER BY title")
        return await cur.fetchall()

    # ---------- rp ----------
    async def get_rp(self, trigger: str, owner_id: int = 0) -> Optional[aiosqlite.Row]:
        # Сначала кастом пользователя, потом базовая
        cur = await self.db.execute(
            "SELECT * FROM rp_commands WHERE trigger=? AND owner_id=? LIMIT 1",
            (trigger.lower(), owner_id)
        )
        row = await cur.fetchone()
        if row:
            return row
        if owner_id != 0:
            cur = await self.db.execute(
                "SELECT * FROM rp_commands WHERE trigger=? AND owner_id=0 LIMIT 1",
                (trigger.lower(),)
            )
            return await cur.fetchone()
        return None

    async def find_rp_any(self, trigger: str, user_id: int) -> Optional[aiosqlite.Row]:
        """Ищем сначала кастом юзера, потом базовую"""
        cur = await self.db.execute(
            """SELECT * FROM rp_commands
               WHERE trigger=? AND (owner_id=? OR owner_id=0)
               ORDER BY owner_id DESC LIMIT 1""",
            (trigger.lower(), user_id)
        )
        return await cur.fetchone()

    async def add_rp(self, trigger: str, action: str, emoji: str = "",
                     emoji_id: str = "", is_nsfw: int = 0, is_base: int = 0,
                     owner_id: int = 0) -> bool:
        try:
            await self.db.execute(
                """INSERT INTO rp_commands
                   (trigger, action, emoji, emoji_id, is_nsfw, is_base, owner_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (trigger.lower(), action, emoji, emoji_id, is_nsfw, is_base, owner_id)
            )
            await self.db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def delete_rp(self, trigger: str, owner_id: int) -> bool:
        cur = await self.db.execute(
            "DELETE FROM rp_commands WHERE trigger=? AND owner_id=?",
            (trigger.lower(), owner_id)
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def list_user_rp(self, owner_id: int):
        cur = await self.db.execute(
            "SELECT * FROM rp_commands WHERE owner_id=? ORDER BY trigger",
            (owner_id,)
        )
        return await cur.fetchall()

    async def list_base_rp(self):
        cur = await self.db.execute(
            "SELECT * FROM rp_commands WHERE is_base=1 ORDER BY trigger"
        )
        return await cur.fetchall()

    async def list_all_rp(self):
        cur = await self.db.execute(
            "SELECT * FROM rp_commands ORDER BY is_base DESC, trigger"
        )
        return await cur.fetchall()

    async def update_rp(self, rp_id: int, **kwargs):
        allowed = {"trigger", "action", "emoji", "emoji_id", "is_nsfw"}
        sets = []
        vals = []
        for k, v in kwargs.items():
            if k in allowed:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        vals.append(rp_id)
        await self.db.execute(f"UPDATE rp_commands SET {', '.join(sets)} WHERE id=?", vals)
        await self.db.commit()


db = DB()

# ====================== КЛАВИАТУРЫ ======================
def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="✨ Создать РП"),
                KeyboardButton(text="📋 Мои РП"),
            ],
            [
                KeyboardButton(text="📜 Все РП"),
                KeyboardButton(text="❓ Помощь"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def inline_agree_kb(rp_id: int, from_id: int, to_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Согласиться",
            callback_data=f"rp:yes:{rp_id}:{from_id}:{to_id}",
            style="success",
            icon_custom_emoji_id=PREMIUM["check"][0],
        ),
        InlineKeyboardButton(
            text="Отказаться",
            callback_data=f"rp:no:{rp_id}:{from_id}:{to_id}",
            style="danger",
            icon_custom_emoji_id=PREMIUM["cross"][0],
        ),
    ]])


def group_settings_kb(chat_id: int, allow_nsfw: bool, allow_custom: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'✅' if allow_nsfw else '❌'} NSFW-РП",
            callback_data=f"gset:nsfw:{chat_id}",
            style="success" if allow_nsfw else "danger",
        )],
        [InlineKeyboardButton(
            text=f"{'✅' if allow_custom else '❌'} Кастомные РП",
            callback_data=f"gset:custom:{chat_id}",
            style="success" if allow_custom else "danger",
        )],
        [InlineKeyboardButton(
            text="Закрыть",
            callback_data="gset:close",
            style="primary",
        )],
    ])


# ====================== FSM ======================
class CreateRP(StatesGroup):
    waiting_data = State()


# ====================== РОУТЕРЫ ======================
router = Router()

# ---------- /start ----------
@router.message(CommandStart())
async def cmd_start(message: Message):
    await db.upsert_user(message.from_user)
    text = (
        f"{pe('wave')} Привет, {mention(message.from_user)}!\n\n"
        f"{pe('bot')} Я <b>Еврик</b> — бот для ролевых действий.\n\n"
        f"{pe('sparkles')} <b>Как пользоваться:</b>\n"
        f"• В группе ответь на сообщение человека триггером (например <code>обнять</code>)\n"
        f"• Или напиши триггер без ответа — будет инлайн-предложение\n"
        f"• Создай свои РП командой <code>!рпк</code>\n\n"
        f"Используй кнопки ниже 👇"
    )
    await message.answer(text, reply_markup=main_kb())


# ---------- Помощь ----------
@router.message(F.text.in_({"❓ Помощь", "/help", "помощь"}))
async def cmd_help(message: Message):
    text = (
        f"{pe('help')} <b>Помощь по боту Еврик</b>\n\n"
        f"<b>1. РП реплаем</b>\n"
        f"Ответь на сообщение человека словом-триггером:\n"
        f"<code>обнять</code> → <i>Вася обнял(-а) Петю</i>\n\n"
        f"<b>2. Инлайн-РП</b>\n"
        f"Просто напиши триггер (без ответа):\n"
        f"<code>поцеловать</code> → предложение с кнопками\n\n"
        f"<b>3. Кастомные РП</b>\n"
        f"<code>!рпк 🔥 / обнятькрепко / крепко обнял(-а)</code>\n"
        f"<code>!рпм</code> — список своих\n"
        f"<code>!рпу обнятькрепко</code> — удалить\n\n"
        f"<b>4. Настройки группы</b> (для админов чата)\n"
        f"<code>/settings</code> — разрешить/запретить NSFW и кастомные РП\n\n"
        f"{pe('info')} Premium-эмодзи поддерживаются везде."
    )
    await message.answer(text)


# ---------- Список всех / моих ----------
@router.message(F.text.in_({"📜 Все РП", "/allrp"}))
async def cmd_all_rp(message: Message):
    rows = await db.list_base_rp()
    if not rows:
        await message.answer(f"{pe('warning')} Базовых РП пока нет.")
        return
    lines = [f"{pe('list')} <b>Базовые РП-команды:</b>\n"]
    for r in rows:
        nsfw = " 🔞" if r["is_nsfw"] else ""
        em = r["emoji"] or "•"
        lines.append(f"{em} <code>{esc(r['trigger'])}</code> → {esc(r['action'])}{nsfw}")
    await message.answer("\n".join(lines))


@router.message(F.text.in_({"📋 Мои РП", "/myrp", "!рпм"}))
async def cmd_my_rp(message: Message):
    rows = await db.list_user_rp(message.from_user.id)
    if not rows:
        await message.answer(
            f"{pe('info')} У тебя пока нет кастомных РП.\n"
            f"Создай: <code>!рпк 🔥 / триггер / действие</code>"
        )
        return
    lines = [f"{pe('list')} <b>Твои кастомные РП:</b>\n"]
    for r in rows:
        em = r["emoji"] or "•"
        lines.append(f"{em} <code>{esc(r['trigger'])}</code> → {esc(r['action'])}")
    await message.answer("\n".join(lines))


# ---------- Создание кастомного РП ----------
@router.message(F.text.in_({"✨ Создать РП"}))
async def btn_create_rp(message: Message, state: FSMContext):
    await state.set_state(CreateRP.waiting_data)
    await message.answer(
        f"{pe('pencil')} Отправь данные в формате:\n\n"
        f"<code>эмодзи / триггер / действие</code>\n\n"
        f"Пример:\n<code>🔥 / обнятькрепко / крепко обнял(-а)</code>\n\n"
        f"Или используй команду:\n<code>!рпк 🔥 / обнятькрепко / крепко обнял(-а)</code>"
    )


@router.message(CreateRP.waiting_data)
async def process_create_rp(message: Message, state: FSMContext):
    await state.clear()
    await _create_custom_rp(message, message.text)


@router.message(F.text.regexp(r"^!рпк\s+", flags=re.IGNORECASE))
async def cmd_rpk(message: Message):
    text = message.text[5:].strip()  # убираем !рпк
    await _create_custom_rp(message, text)


async def _create_custom_rp(message: Message, raw: str):
    parts = [p.strip() for p in raw.split("/")]
    if len(parts) < 3:
        await message.answer(
            f"{pe('cross')} Неверный формат.\n"
            f"Нужно: <code>эмодзи / триггер / действие</code>"
        )
        return

    emoji_part, trigger, action = parts[0], parts[1].lower(), parts[2]
    if not trigger or not action:
        await message.answer(f"{pe('cross')} Триггер и действие не могут быть пустыми.")
        return

    # Пытаемся вытащить premium-emoji из entities
    emoji_id = ""
    emoji = emoji_part
    if message.entities:
        for ent in message.entities:
            if ent.type == "custom_emoji":
                emoji_id = ent.custom_emoji_id
                break

    ok = await db.add_rp(
        trigger=trigger,
        action=action,
        emoji=emoji,
        emoji_id=emoji_id,
        is_base=0,
        owner_id=message.from_user.id,
    )
    if ok:
        await message.answer(
            f"{pe('check')} РП создано!\n"
            f"{emoji} <code>{esc(trigger)}</code> → {esc(action)}"
        )
    else:
        await message.answer(
            f"{pe('warning')} У тебя уже есть РП с триггером <code>{esc(trigger)}</code>."
        )


@router.message(F.text.regexp(r"^!рпу\s+", flags=re.IGNORECASE))
async def cmd_rpu(message: Message):
    trigger = message.text[5:].strip().lower()
    if not trigger:
        await message.answer(f"{pe('cross')} Укажи триггер: <code>!рпу триггер</code>")
        return
    ok = await db.delete_rp(trigger, message.from_user.id)
    if ok:
        await message.answer(f"{pe('trash')} РП <code>{esc(trigger)}</code> удалено.")
    else:
        await message.answer(f"{pe('warning')} РП с таким триггером не найдено.")


# ====================== ОСНОВНОЙ РП-ХЭНДЛЕР ======================
@router.message(F.text)
async def rp_handler(message: Message):
    if not message.text or message.text.startswith(("/", "!")):
        return
    if message.chat.type == ChatType.PRIVATE and message.text in {
        "✨ Создать РП", "📋 Мои РП", "📜 Все РП", "❓ Помощь"
    }:
        return

    trigger = message.text.strip().lower().split()[0]
    if len(trigger) < 2:
        return

    user = message.from_user
    await db.upsert_user(user)

    rp = await db.find_rp_any(trigger, user.id)
    if not rp:
        return

    # Проверки группы
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await db.upsert_group(message.chat.id, message.chat.title or "")
        g = await db.get_group(message.chat.id)
        if g:
            if rp["is_nsfw"] and not g["allow_nsfw"]:
                return  # тихо игнорируем
            if not rp["is_base"] and not g["allow_custom"]:
                return

    # Эмодзи (premium или обычный)
    if rp["emoji_id"]:
        emoji_html = f'<tg-emoji emoji-id="{rp["emoji_id"]}">{esc(rp["emoji"] or "•")}</tg-emoji>'
    else:
        emoji_html = esc(rp["emoji"] or "•")

    # === РЕПЛАЙ-режим (есть reply_to_message) ===
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        if target.id == user.id:
            await message.reply(f"{pe('warning')} Нельзя применять РП на самого себя.")
            return
        if target.is_bot:
            return

        text = (
            f"{emoji_html} {mention(user)} {esc(rp['action'])} {mention(target)}"
        )
        await message.answer(text)
        try:
            await message.delete()
        except Exception:
            pass
        return

    # === ИНЛАЙН-режим (нет reply) ===
    # Предложение: "Вася хочет обнять"
    text = (
        f"{emoji_html} {mention(user)} хочет <b>{esc(rp['trigger'])}</b>.\n"
        f"Кто готов?"
    )
    # Сохраняем временно через callback (rp_id уже есть)
    kb = inline_agree_kb(rp["id"], user.id, 0)  # to_id=0 → любой
    await message.answer(text, reply_markup=kb)


# ---------- Callback инлайн-РП ----------
@router.callback_query(F.data.startswith("rp:"))
async def cb_rp(call: CallbackQuery):
    parts = call.data.split(":")
    # rp:yes:rp_id:from_id:to_id
    if len(parts) < 5:
        await call.answer("Устаревшая кнопка", show_alert=True)
        return

    action, rp_id, from_id, to_id = parts[1], int(parts[2]), int(parts[3]), int(parts[4])
    clicker = call.from_user

    if clicker.id == from_id:
        await call.answer("Нельзя отвечать на своё предложение!", show_alert=True)
        return

    # Получаем РП
    cur = await db.db.execute("SELECT * FROM rp_commands WHERE id=?", (rp_id,))
    rp = await cur.fetchone()
    if not rp:
        await call.answer("РП больше не существует", show_alert=True)
        await call.message.edit_reply_markup(reply_markup=None)
        return

    # from_user
    try:
        from_user = await call.bot.get_chat(from_id)
        # get_chat возвращает Chat, нам нужен User-like
        class FakeUser:
            def __init__(self, c):
                self.id = c.id
                self.username = getattr(c, "username", None)
                self.full_name = c.full_name if hasattr(c, "full_name") else (c.first_name or "User")
                self.is_bot = False
        from_u = FakeUser(from_user)
    except Exception:
        from_u = clicker  # fallback

    if rp["emoji_id"]:
        emoji_html = f'<tg-emoji emoji-id="{rp["emoji_id"]}">{esc(rp["emoji"] or "•")}</tg-emoji>'
    else:
        emoji_html = esc(rp["emoji"] or "•")

    if action == "yes":
        text = (
            f"{emoji_html} {mention(from_u)} {esc(rp['action'])} {mention(clicker)}"
        )
        await call.message.edit_text(text)
        await call.answer("Принято!")
    else:
        text = (
            f"{pe('cross')} {mention(clicker)} отказался(-ась) "
            f"<b>{esc(rp['action'])}</b> от {mention(from_u)}"
        )
        await call.message.edit_text(text)
        await call.answer("Отклонено")


# ====================== НАСТРОЙКИ ГРУППЫ ======================
@router.message(Command("settings"))
async def cmd_settings(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Эта команда только для групп.")
        return

    member = await message.chat.get_member(message.from_user.id)
    if member.status not in ("creator", "administrator"):
        await message.answer(f"{pe('ban')} Только администраторы группы.")
        return

    await db.upsert_group(message.chat.id, message.chat.title or "")
    g = await db.get_group(message.chat.id)
    allow_nsfw = bool(g["allow_nsfw"]) if g else False
    allow_custom = bool(g["allow_custom"]) if g else True

    text = (
        f"{pe('settings')} <b>Настройки группы</b>\n\n"
        f"NSFW-РП: {'✅ разрешены' if allow_nsfw else '❌ запрещены'}\n"
        f"Кастомные РП: {'✅ разрешены' if allow_custom else '❌ запрещены'}"
    )
    await message.answer(text, reply_markup=group_settings_kb(message.chat.id, allow_nsfw, allow_custom))


@router.callback_query(F.data.startswith("gset:"))
async def cb_gset(call: CallbackQuery):
    parts = call.data.split(":")
    action = parts[1]

    if action == "close":
        await call.message.delete()
        await call.answer()
        return

    chat_id = int(parts[2])
    member = await call.bot.get_chat_member(chat_id, call.from_user.id)
    if member.status not in ("creator", "administrator"):
        await call.answer("Нет прав", show_alert=True)
        return

    g = await db.get_group(chat_id)
    if not g:
        await call.answer("Группа не найдена", show_alert=True)
        return

    if action == "nsfw":
        new_val = 0 if g["allow_nsfw"] else 1
        await db.set_group_setting(chat_id, "allow_nsfw", new_val)
    elif action == "custom":
        new_val = 0 if g["allow_custom"] else 1
        await db.set_group_setting(chat_id, "allow_custom", new_val)

    g = await db.get_group(chat_id)
    allow_nsfw = bool(g["allow_nsfw"])
    allow_custom = bool(g["allow_custom"])

    text = (
        f"{pe('settings')} <b>Настройки группы</b>\n\n"
        f"NSFW-РП: {'✅ разрешены' if allow_nsfw else '❌ запрещены'}\n"
        f"Кастомные РП: {'✅ разрешены' if allow_custom else '❌ запрещены'}"
    )
    await call.message.edit_text(text, reply_markup=group_settings_kb(chat_id, allow_nsfw, allow_custom))
    await call.answer("Сохранено")


# ====================== АДМИНКА ======================
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not await db.is_admin(message.from_user.id):
        return
    text = (
        f"{pe('settings')} <b>Админ-панель</b>\n\n"
        f"<code>/addbase эмодзи / триггер / действие / nsfw(0|1)</code>\n"
        f"<code>/delbase триггер</code>\n"
        f"<code>/listgroups</code>\n"
        f"<code>/setadmin user_id 1|0</code>\n"
        f"<code>/allrp_admin</code> — все РП включая кастомные"
    )
    await message.answer(text)


@router.message(Command("addbase"))
async def cmd_addbase(message: Message, command: CommandObject):
    if not await db.is_admin(message.from_user.id):
        return
    if not command.args:
        await message.answer("Формат: <code>/addbase эмодзи / триггер / действие / 0</code>")
        return
    parts = [p.strip() for p in command.args.split("/")]
    if len(parts) < 3:
        await message.answer("Мало аргументов")
        return
    emoji, trigger, action = parts[0], parts[1].lower(), parts[2]
    nsfw = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

    emoji_id = ""
    if message.entities:
        for ent in message.entities:
            if ent.type == "custom_emoji":
                emoji_id = ent.custom_emoji_id
                break

    ok = await db.add_rp(trigger, action, emoji, emoji_id, nsfw, is_base=1, owner_id=0)
    if ok:
        await message.answer(f"{pe('check')} Базовая РП добавлена: <code>{esc(trigger)}</code>")
    else:
        await message.answer(f"{pe('warning')} Уже существует")


@router.message(Command("delbase"))
async def cmd_delbase(message: Message, command: CommandObject):
    if not await db.is_admin(message.from_user.id):
        return
    if not command.args:
        return
    ok = await db.delete_rp(command.args.strip().lower(), 0)
    await message.answer(f"{pe('check') if ok else pe('cross')} Готово" if ok else "Не найдено")


@router.message(Command("listgroups"))
async def cmd_listgroups(message: Message):
    if not await db.is_admin(message.from_user.id):
        return
    rows = await db.list_groups()
    if not rows:
        await message.answer("Групп пока нет")
        return
    lines = [f"{pe('list')} <b>Группы:</b>\n"]
    for g in rows:
        lines.append(
            f"• {esc(g['title'] or g['chat_id'])} "
            f"(nsfw={g['allow_nsfw']}, custom={g['allow_custom']})"
        )
    await message.answer("\n".join(lines))


@router.message(Command("setadmin"))
async def cmd_setadmin(message: Message, command: CommandObject):
    if not await db.is_admin(message.from_user.id):
        return
    args = (command.args or "").split()
    if len(args) != 2 or not args[0].isdigit():
        await message.answer("Формат: <code>/setadmin user_id 1|0</code>")
        return
    uid, val = int(args[0]), args[1] == "1"
    await db.set_admin(uid, val)
    await message.answer(f"{pe('check')} is_admin={val} для {uid}")


@router.message(Command("allrp_admin"))
async def cmd_allrp_admin(message: Message):
    if not await db.is_admin(message.from_user.id):
        return
    rows = await db.list_all_rp()
    lines = [f"{pe('list')} Всего РП: {len(rows)}\n"]
    for r in rows[:80]:
        who = "base" if r["is_base"] else f"uid:{r['owner_id']}"
        lines.append(f"{r['emoji'] or '•'} <code>{esc(r['trigger'])}</code> [{who}]")
    await message.answer("\n".join(lines))


# ====================== ЗАПУСК ======================
async def main():
    logging.basicConfig(level=logging.INFO)
    await db.connect()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Регистрируем админов
    for aid in ADMIN_IDS:
        await db.db.execute(
            "INSERT OR IGNORE INTO users (user_id, is_admin) VALUES (?, 1)",
            (aid,)
        )
    await db.db.commit()

    print("Еврик запущен!")
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
