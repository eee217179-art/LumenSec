"""
Бот, который показывает удалённые и изменённые сообщения (текст, фото, видео,
кружки, голосовые, файлы, стикеры, гифки) в твоих личных чатах.

Работает через Telegram Business API:
  Настройки Telegram -> Telegram для бизнеса -> Чат-боты -> выбрать бота.
Нужен Telegram Premium, а в @BotFather включить Bot Settings -> Business Mode.

Запуск:
  pip install -r requirements.txt
  export BOT_TOKEN="123:ABC..."
  python bot.py
"""
import asyncio
import html
import logging
import os
import time
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message

TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "deleted.db")
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "30"))  # сколько хранить историю

router = Router()

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections(
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    owner_chat_id INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings(
    owner_id INTEGER PRIMARY KEY,
    notify_del INTEGER NOT NULL DEFAULT 1,
    notify_edit INTEGER NOT NULL DEFAULT 1,
    notify_own INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages(
    conn_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    sender_id INTEGER,
    sender_name TEXT,
    chat_name TEXT,
    text TEXT,
    media_type TEXT,
    file_id TEXT,
    ts INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    deleted_at INTEGER,
    edits INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(conn_id, chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(ts);
"""

MEDIA_LABEL = {
    "photo": "📷 Фото",
    "video": "🎬 Видео",
    "video_note": "⭕ Кружок",
    "voice": "🎤 Голосовое",
    "audio": "🎵 Аудио",
    "document": "📎 Файл",
    "animation": "🎞 Гифка",
    "sticker": "🙂 Стикер",
}


# ───────────────────────── helpers ─────────────────────────
def extract_media(m: Message):
    if m.photo:
        return "photo", m.photo[-1].file_id
    if m.video:
        return "video", m.video.file_id
    if m.video_note:
        return "video_note", m.video_note.file_id
    if m.voice:
        return "voice", m.voice.file_id
    if m.audio:
        return "audio", m.audio.file_id
    if m.animation:
        return "animation", m.animation.file_id
    if m.document:
        return "document", m.document.file_id
    if m.sticker:
        return "sticker", m.sticker.file_id
    return None, None


def person(name, user_id=None, username=None) -> str:
    s = html.escape(name or "Неизвестный")
    if username:
        s += f" (@{html.escape(username)})"
    return s


def fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M:%S")


async def get_conn(db: aiosqlite.Connection, conn_id: str):
    cur = await db.execute("SELECT * FROM connections WHERE id=? AND enabled=1", (conn_id,))
    return await cur.fetchone()


async def get_settings(db: aiosqlite.Connection, owner_id: int):
    await db.execute("INSERT OR IGNORE INTO settings(owner_id) VALUES(?)", (owner_id,))
    cur = await db.execute("SELECT * FROM settings WHERE owner_id=?", (owner_id,))
    return await cur.fetchone()


async def save_message(db: aiosqlite.Connection, m: Message):
    mtype, fid = extract_media(m)
    sender = m.from_user
    chat_name = m.chat.full_name or m.chat.title or m.chat.username or str(m.chat.id)
    await db.execute(
        """INSERT OR REPLACE INTO messages
           (conn_id, chat_id, msg_id, sender_id, sender_name, chat_name, text,
            media_type, file_id, ts)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            m.business_connection_id,
            m.chat.id,
            m.message_id,
            sender.id if sender else None,
            (sender.full_name if sender else None),
            chat_name,
            m.text or m.caption,
            mtype,
            fid,
            int(time.time()),
        ),
    )
    await db.commit()


async def send_saved_media(bot: Bot, chat_id: int, mtype: str, fid: str):
    senders = {
        "photo": bot.send_photo,
        "video": bot.send_video,
        "video_note": bot.send_video_note,
        "voice": bot.send_voice,
        "audio": bot.send_audio,
        "document": bot.send_document,
        "animation": bot.send_animation,
        "sticker": bot.send_sticker,
    }
    func = senders.get(mtype)
    if not func:
        return
    arg = mtype if mtype != "document" else "document"
    await func(chat_id, **{arg: fid})


# ───────────────────────── Business updates ─────────────────────────
@router.business_connection()
async def on_business_connection(conn: BusinessConnection, bot: Bot, db: aiosqlite.Connection):
    await db.execute(
        """INSERT INTO connections(id, owner_id, owner_chat_id, enabled) VALUES(?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled,
                                         owner_chat_id=excluded.owner_chat_id""",
        (conn.id, conn.user.id, conn.user_chat_id, int(conn.is_enabled)),
    )
    await get_settings(db, conn.user.id)
    await db.commit()
    text = (
        "✅ Бот подключён к твоему аккаунту. Теперь я сохраняю сообщения "
        "и покажу, что удалили или изменили."
        if conn.is_enabled
        else "⛔ Бот отключён от аккаунта."
    )
    await bot.send_message(conn.user_chat_id, text)


@router.business_message()
async def on_business_message(m: Message, db: aiosqlite.Connection):
    if m.business_connection_id:
        await save_message(db, m)


@router.edited_business_message()
async def on_edited(m: Message, bot: Bot, db: aiosqlite.Connection):
    conn = await get_conn(db, m.business_connection_id)
    if not conn:
        return
    cur = await db.execute(
        "SELECT * FROM messages WHERE conn_id=? AND chat_id=? AND msg_id=?",
        (m.business_connection_id, m.chat.id, m.message_id),
    )
    old = await cur.fetchone()
    new_text = m.text or m.caption
    if old:
        st = await get_settings(db, conn["owner_id"])
        from_owner = old["sender_id"] == conn["owner_id"]
        if st["notify_edit"] and (st["notify_own"] or not from_owner) and (old["text"] or "") != (new_text or ""):
            await bot.send_message(
                conn["owner_chat_id"],
                f"✏️ <b>Сообщение изменено</b>\n"
                f"👤 {person(old['sender_name'], username=m.from_user.username if m.from_user else None)}\n"
                f"💬 Чат: {html.escape(old['chat_name'] or '')}\n\n"
                f"<b>Было:</b>\n{html.escape(old['text'] or '—')}\n\n"
                f"<b>Стало:</b>\n{html.escape(new_text or '—')}",
            )
        await db.execute(
            "UPDATE messages SET text=?, edits=edits+1 WHERE conn_id=? AND chat_id=? AND msg_id=?",
            (new_text, m.business_connection_id, m.chat.id, m.message_id),
        )
        await db.commit()
    else:
        await save_message(db, m)


@router.deleted_business_messages()
async def on_deleted(ev: BusinessMessagesDeleted, bot: Bot, db: aiosqlite.Connection):
    conn = await get_conn(db, ev.business_connection_id)
    if not conn:
        return
    st = await get_settings(db, conn["owner_id"])
    for mid in ev.message_ids:
        cur = await db.execute(
            "SELECT * FROM messages WHERE conn_id=? AND chat_id=? AND msg_id=?",
            (ev.business_connection_id, ev.chat.id, mid),
        )
        row = await cur.fetchone()
        if not row:
            continue  # сообщение пришло до подключения бота
        await db.execute(
            "UPDATE messages SET deleted=1, deleted_at=? WHERE conn_id=? AND chat_id=? AND msg_id=?",
            (int(time.time()), ev.business_connection_id, ev.chat.id, mid),
        )
        from_owner = row["sender_id"] == conn["owner_id"]
        if not st["notify_del"] or (from_owner and not st["notify_own"]):
            continue
        who = "Ты" if from_owner else person(row["sender_name"])
        label = MEDIA_LABEL.get(row["media_type"], "")
        body = html.escape(row["text"]) if row["text"] else ""
        await bot.send_message(
            conn["owner_chat_id"],
            f"🗑 <b>Сообщение удалено</b>\n"
            f"👤 {who}\n"
            f"💬 Чат: {html.escape(row['chat_name'] or '')}\n"
            f"🕒 Отправлено: {fmt_time(row['ts'])}\n"
            + (f"📦 {label}\n" if label else "")
            + (f"\n{body}" if body else ""),
        )
        if row["media_type"] and row["file_id"]:
            try:
                await send_saved_media(bot, conn["owner_chat_id"], row["media_type"], row["file_id"])
            except Exception as e:  # noqa: BLE001
                logging.warning("Не удалось отправить медиа: %s", e)
    await db.commit()


# ───────────────────────── Команды ─────────────────────────
@router.message(CommandStart())
async def cmd_start(m: Message):
    await m.answer(
        "👋 <b>Я показываю удалённые и изменённые сообщения.</b>\n\n"
        "<b>Как подключить:</b>\n"
        "1. Нужен Telegram Premium\n"
        "2. Настройки → Telegram для бизнеса → Чат-боты\n"
        "3. Выбери этого бота и дай доступ к нужным чатам\n\n"
        "<b>Команды:</b>\n"
        "/last [N] — последние удалённые сообщения\n"
        "/edited [N] — последние изменённые\n"
        "/media [N] — удалённые фото/видео/файлы\n"
        "/search слово — поиск по сохранённым сообщениям\n"
        "/stats — статистика\n"
        "/dels — вкл/выкл уведомления об удалении\n"
        "/edits — вкл/выкл уведомления об изменении\n"
        "/own — уведомлять и о твоих сообщениях\n"
        "/wipe — стереть всю мою историю"
    )


def _n(m: Message, default=10) -> int:
    parts = (m.text or "").split()
    if len(parts) > 1 and parts[1].isdigit():
        return max(1, min(int(parts[1]), 30))
    return default


async def _owner_conns(db, user_id: int):
    cur = await db.execute("SELECT id FROM connections WHERE owner_id=?", (user_id,))
    return [r["id"] for r in await cur.fetchall()]


def _in(ids):
    return ",".join("?" * len(ids))


@router.message(Command("last"))
async def cmd_last(m: Message, db: aiosqlite.Connection):
    ids = await _owner_conns(db, m.from_user.id)
    if not ids:
        return await m.answer("Бот ещё не подключён к твоему аккаунту.")
    cur = await db.execute(
        f"SELECT * FROM messages WHERE deleted=1 AND conn_id IN ({_in(ids)}) "
        f"ORDER BY deleted_at DESC LIMIT ?",
        (*ids, _n(m)),
    )
    rows = await cur.fetchall()
    if not rows:
        return await m.answer("Удалённых сообщений пока нет.")
    out = []
    for r in rows:
        label = MEDIA_LABEL.get(r["media_type"], "")
        out.append(
            f"🗑 <b>{person(r['sender_name'])}</b> · {html.escape(r['chat_name'] or '')}\n"
            f"🕒 {fmt_time(r['ts'])}\n"
            + (f"{label} " if label else "")
            + html.escape((r["text"] or "")[:300])
        )
    await m.answer("\n\n".join(out))


@router.message(Command("edited"))
async def cmd_edited(m: Message, db: aiosqlite.Connection):
    ids = await _owner_conns(db, m.from_user.id)
    if not ids:
        return await m.answer("Бот ещё не подключён к твоему аккаунту.")
    cur = await db.execute(
        f"SELECT * FROM messages WHERE edits>0 AND conn_id IN ({_in(ids)}) ORDER BY ts DESC LIMIT ?",
        (*ids, _n(m)),
    )
    rows = await cur.fetchall()
    if not rows:
        return await m.answer("Изменённых сообщений пока нет.")
    await m.answer(
        "\n\n".join(
            f"✏️ <b>{person(r['sender_name'])}</b> (правок: {r['edits']})\n{html.escape((r['text'] or '')[:300])}"
            for r in rows
        )
    )


@router.message(Command("media"))
async def cmd_media(m: Message, bot: Bot, db: aiosqlite.Connection):
    ids = await _owner_conns(db, m.from_user.id)
    if not ids:
        return await m.answer("Бот ещё не подключён к твоему аккаунту.")
    cur = await db.execute(
        f"SELECT * FROM messages WHERE deleted=1 AND file_id IS NOT NULL "
        f"AND conn_id IN ({_in(ids)}) ORDER BY deleted_at DESC LIMIT ?",
        (*ids, _n(m, 5)),
    )
    rows = await cur.fetchall()
    if not rows:
        return await m.answer("Удалённых медиа пока нет.")
    for r in rows:
        await m.answer(
            f"{MEDIA_LABEL.get(r['media_type'], '')} от {person(r['sender_name'])} · {fmt_time(r['ts'])}"
        )
        try:
            await send_saved_media(bot, m.chat.id, r["media_type"], r["file_id"])
        except Exception:  # noqa: BLE001
            await m.answer("Не удалось получить файл.")


@router.message(Command("search"))
async def cmd_search(m: Message, db: aiosqlite.Connection):
    q = (m.text or "").partition(" ")[2].strip()
    if not q:
        return await m.answer("Использование: /search слово")
    ids = await _owner_conns(db, m.from_user.id)
    if not ids:
        return await m.answer("Бот ещё не подключён к твоему аккаунту.")
    like = f"%{q}%"
    cur = await db.execute(
        f"SELECT * FROM messages WHERE text LIKE ? AND conn_id IN ({_in(ids)}) ORDER BY ts DESC LIMIT 10",
        (like, *ids),
    )
    rows = await cur.fetchall()
    if not rows:
        return await m.answer("Ничего не найдено.")
    await m.answer(
        "\n\n".join(
            f"{'🗑' if r['deleted'] else '💬'} <b>{person(r['sender_name'])}</b> · {fmt_time(r['ts'])}\n"
            f"{html.escape((r['text'] or '')[:300])}"
            for r in rows
        )
    )


@router.message(Command("stats"))
async def cmd_stats(m: Message, db: aiosqlite.Connection):
    ids = await _owner_conns(db, m.from_user.id)
    if not ids:
        return await m.answer("Бот ещё не подключён к твоему аккаунту.")
    cur = await db.execute(
        f"""SELECT COUNT(*) total,
                   SUM(deleted) dels,
                   SUM(CASE WHEN edits>0 THEN 1 ELSE 0 END) eds,
                   SUM(CASE WHEN file_id IS NOT NULL THEN 1 ELSE 0 END) media
            FROM messages WHERE conn_id IN ({_in(ids)})""",
        ids,
    )
    r = await cur.fetchone()
    cur = await db.execute(
        f"""SELECT sender_name, COUNT(*) c FROM messages
            WHERE deleted=1 AND conn_id IN ({_in(ids)})
            GROUP BY sender_id ORDER BY c DESC LIMIT 3""",
        ids,
    )
    top = await cur.fetchall()
    top_txt = "\n".join(f"  • {person(t['sender_name'])}: {t['c']}" for t in top) or "  —"
    await m.answer(
        f"📊 <b>Статистика</b>\n"
        f"Сохранено сообщений: {r['total']}\n"
        f"Удалено: {r['dels'] or 0}\n"
        f"Изменено: {r['eds'] or 0}\n"
        f"С медиа: {r['media'] or 0}\n\n"
        f"<b>Чаще всего удаляют:</b>\n{top_txt}\n\n"
        f"Хранение: {RETENTION_DAYS} дн."
    )


async def _toggle(m: Message, db, column: str, title: str):
    st = await get_settings(db, m.from_user.id)
    new = 0 if st[column] else 1
    await db.execute(f"UPDATE settings SET {column}=? WHERE owner_id=?", (new, m.from_user.id))
    await db.commit()
    await m.answer(f"{title}: {'✅ включено' if new else '⛔ выключено'}")


@router.message(Command("dels"))
async def cmd_dels(m: Message, db: aiosqlite.Connection):
    await _toggle(m, db, "notify_del", "Уведомления об удалении")


@router.message(Command("edits"))
async def cmd_edits(m: Message, db: aiosqlite.Connection):
    await _toggle(m, db, "notify_edit", "Уведомления об изменении")


@router.message(Command("own"))
async def cmd_own(m: Message, db: aiosqlite.Connection):
    await _toggle(m, db, "notify_own", "Уведомления о твоих сообщениях")


@router.message(Command("wipe"))
async def cmd_wipe(m: Message, db: aiosqlite.Connection):
    ids = await _owner_conns(db, m.from_user.id)
    if ids:
        await db.execute(f"DELETE FROM messages WHERE conn_id IN ({_in(ids)})", ids)
        await db.commit()
    await m.answer("🧹 Вся сохранённая история удалена.")


# ───────────────────────── Запуск ─────────────────────────
async def cleanup_loop(db: aiosqlite.Connection):
    while True:
        cutoff = int(time.time()) - RETENTION_DAYS * 86400
        await db.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
        await db.commit()
        await asyncio.sleep(3600)


async def main():
    if not TOKEN:
        raise SystemExit("Задай переменную окружения BOT_TOKEN")
    logging.basicConfig(level=logging.INFO)

    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()

    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp["db"] = db
    dp.include_router(router)

    asyncio.create_task(cleanup_loop(db))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
