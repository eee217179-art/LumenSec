"""
Юзербот: сохраняет сообщения из твоих личных чатов и показывает, что удалили
или изменили (текст, фото, видео, кружки, голосовые, файлы, гифки, стикеры).
Работает от имени твоего аккаунта (Telethon), Telegram Premium и Business НЕ нужны.

Переменные окружения:
  API_ID, API_HASH   — с https://my.telegram.org (API development tools)
  SESSION_STRING     — получить локально через gen_session.py
  BOT_TOKEN          — (необязательно) токен бота для уведомлений с пушем и команд.
                       Если не задан, всё приходит в «Избранное».
  TRACK_GROUPS=1     — (необязательно) следить ещё и за группами
  RETENTION_DAYS=30  — сколько хранить историю
  MAX_MEDIA_MB=30    — максимальный размер сохраняемого файла
"""
import asyncio
import html
import logging
import os
import time
from datetime import datetime
from types import SimpleNamespace

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, Message
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession

API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "deleted.db")
MEDIA_DIR = os.getenv("MEDIA_DIR", "media")
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "30"))
MAX_MEDIA_MB = int(os.getenv("MAX_MEDIA_MB", "30"))
TRACK_GROUPS = os.getenv("TRACK_GROUPS", "0") == "1"

S = SimpleNamespace(client=None, bot=None, db=None, owner=None)
dp = Dispatcher()

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
    chat_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    scoped INTEGER NOT NULL DEFAULT 0,
    sender_id INTEGER,
    sender_name TEXT,
    chat_name TEXT,
    out INTEGER NOT NULL DEFAULT 0,
    text TEXT,
    media_kind TEXT,
    media_path TEXT,
    ts INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    deleted_at INTEGER,
    edits INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_id ON messages(msg_id);
CREATE INDEX IF NOT EXISTS idx_ts ON messages(ts);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""

DEFAULTS = {"notify_del": True, "notify_edit": True, "notify_own": False}

LABEL = {
    "photo": "📷 Фото",
    "video": "🎬 Видео",
    "video_note": "⭕ Кружок",
    "voice": "🎤 Голосовое",
    "audio": "🎵 Аудио",
    "document": "📎 Файл",
    "gif": "🎞 Гифка",
    "sticker": "🙂 Стикер",
}

HELP = (
    "👋 <b>Я показываю удалённые и изменённые сообщения.</b>\n\n"
    "/last [N] — последние удалённые\n"
    "/edited [N] — последние изменённые\n"
    "/media [N] — удалённые фото/видео/файлы\n"
    "/search слово — поиск по истории\n"
    "/stats — статистика\n"
    "/dels — вкл/выкл уведомления об удалении\n"
    "/edits — вкл/выкл уведомления об изменении\n"
    "/own — уведомлять и о твоих сообщениях\n"
    "/wipe — стереть всю историю"
)


# ───────────────────────── helpers ─────────────────────────
def esc(s) -> str:
    return html.escape(s or "")


def fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M:%S")


def media_kind(m):
    if m.photo:
        return "photo"
    if m.sticker:
        return "sticker"
    if m.voice:
        return "voice"
    if m.video_note:
        return "video_note"
    if m.gif:
        return "gif"
    if m.video:
        return "video"
    if m.audio:
        return "audio"
    if m.document:
        return "document"
    return None


async def get_flag(key: str) -> bool:
    cur = await S.db.execute("SELECT value FROM settings WHERE key=?", (key,))
    r = await cur.fetchone()
    return bool(r["value"]) if r else DEFAULTS[key]


async def toggle_flag(key: str) -> bool:
    new = not await get_flag(key)
    await S.db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (key, int(new)))
    await S.db.commit()
    return new


# ───────────────────────── доставка уведомлений ─────────────────────────
async def deliver_text(text: str):
    if S.bot:
        await S.bot.send_message(S.owner, text[:4000])
    else:
        await S.client.send_message("me", text[:4000], parse_mode="html")


async def deliver_file(path: str, kind: str):
    if S.bot:
        f = FSInputFile(path)
        try:
            if kind == "photo":
                await S.bot.send_photo(S.owner, f)
            elif kind == "video":
                await S.bot.send_video(S.owner, f)
            elif kind == "video_note":
                await S.bot.send_video_note(S.owner, f)
            elif kind == "voice":
                await S.bot.send_voice(S.owner, f)
            elif kind == "audio":
                await S.bot.send_audio(S.owner, f)
            elif kind == "gif":
                await S.bot.send_animation(S.owner, f)
            else:
                await S.bot.send_document(S.owner, f)
        except Exception as e:  # noqa: BLE001
            logging.warning("send as %s failed (%s), sending as document", kind, e)
            await S.bot.send_document(S.owner, FSInputFile(path))
    else:
        await S.client.send_file(
            "me", path, voice_note=(kind == "voice"), video_note=(kind == "video_note")
        )


async def send_items(items):
    for it in items:
        if isinstance(it, str):
            await deliver_text(it)
        else:
            path, kind, caption = it
            await deliver_text(caption)
            if path and os.path.exists(path):
                await deliver_file(path, kind)
            else:
                await deliver_text("Файл не сохранён.")


# ───────────────────────── что отслеживаем ─────────────────────────
async def is_tracked(event) -> bool:
    if event.chat_id in (S.owner, 777000):  # «Избранное» и служебные уведомления
        return False
    if event.is_private:
        chat = await event.get_chat()
        return not getattr(chat, "bot", False)
    return TRACK_GROUPS and bool(event.is_group)


async def store(event):
    m = event.message
    chat = await event.get_chat()
    sender = await event.get_sender()
    kind = media_kind(m)
    path = None
    if kind and not getattr(m.media, "ttl_seconds", None):  # одноразовые медиа не трогаем
        size = m.file.size if (m.file and m.file.size) else 0
        if size <= MAX_MEDIA_MB * 1024 * 1024:
            try:
                os.makedirs(MEDIA_DIR, exist_ok=True)
                path = await m.download_media(
                    file=os.path.join(MEDIA_DIR, f"{abs(event.chat_id)}_{m.id}")
                )
            except Exception as e:  # noqa: BLE001
                logging.warning("download failed: %s", e)
    await S.db.execute(
        """INSERT OR REPLACE INTO messages
           (chat_id, msg_id, scoped, sender_id, sender_name, chat_name, out,
            text, media_kind, media_path, ts)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event.chat_id,
            m.id,
            int(bool(event.is_channel)),
            m.sender_id,
            utils.get_display_name(sender) if sender else None,
            utils.get_display_name(chat) if chat else None,
            int(bool(m.out)),
            m.raw_text,
            kind,
            path,
            int(m.date.timestamp()),
        ),
    )
    await S.db.commit()


# ───────────────────────── события Telethon ─────────────────────────
async def on_new(event):
    if await is_tracked(event):
        await store(event)


async def on_edit(event):
    if not await is_tracked(event):
        return
    m = event.message
    cur = await S.db.execute(
        "SELECT * FROM messages WHERE chat_id=? AND msg_id=?", (event.chat_id, m.id)
    )
    old = await cur.fetchone()
    if not old:
        await store(event)
        return
    new_text = m.raw_text or ""
    if (old["text"] or "") != new_text:
        if await get_flag("notify_edit") and (await get_flag("notify_own") or not old["out"]):
            await deliver_text(
                f"✏️ <b>Сообщение изменено</b>\n"
                f"👤 {esc(old['sender_name'])}\n"
                f"💬 Чат: {esc(old['chat_name'])}\n\n"
                f"<b>Было:</b>\n{esc((old['text'] or '—')[:1700])}\n\n"
                f"<b>Стало:</b>\n{esc(new_text[:1700] or '—')}"
            )
        await S.db.execute(
            "UPDATE messages SET text=?, edits=edits+1 WHERE chat_id=? AND msg_id=?",
            (new_text, event.chat_id, m.id),
        )
        await S.db.commit()


async def on_delete(event):
    rows = []
    for mid in event.deleted_ids:
        if event.chat_id:
            cur = await S.db.execute(
                "SELECT * FROM messages WHERE chat_id=? AND msg_id=? AND deleted=0",
                (event.chat_id, mid),
            )
        else:  # в личках и обычных группах Telegram не сообщает чат, только id
            cur = await S.db.execute(
                "SELECT * FROM messages WHERE msg_id=? AND scoped=0 AND deleted=0", (mid,)
            )
        row = await cur.fetchone()
        if row:
            rows.append(row)
            await S.db.execute(
                "UPDATE messages SET deleted=1, deleted_at=? WHERE chat_id=? AND msg_id=?",
                (int(time.time()), row["chat_id"], row["msg_id"]),
            )
    await S.db.commit()
    if not rows or not await get_flag("notify_del"):
        return
    notify_own = await get_flag("notify_own")
    rows = [r for r in rows if notify_own or not r["out"]]
    if not rows:
        return
    if len(rows) > 15:  # удалили целый чат: не спамим
        await deliver_text(
            f"🗑 <b>Удалено сообщений: {len(rows)}</b>\n💬 Чат: {esc(rows[0]['chat_name'])}\n"
            f"Посмотреть: /last {min(len(rows), 30)}"
        )
        return
    for r in rows:
        label = LABEL.get(r["media_kind"], "")
        await deliver_text(
            f"🗑 <b>Сообщение удалено</b>\n"
            f"👤 {'Ты' if r['out'] else esc(r['sender_name'])}\n"
            f"💬 Чат: {esc(r['chat_name'])}\n"
            f"🕒 Отправлено: {fmt_time(r['ts'])}\n"
            + (f"📦 {label}\n" if label else "")
            + (f"\n{esc((r['text'] or '')[:3000])}" if r["text"] else "")
        )
        if r["media_path"] and os.path.exists(r["media_path"]):
            try:
                await deliver_file(r["media_path"], r["media_kind"])
            except Exception as e:  # noqa: BLE001
                logging.warning("deliver_file failed: %s", e)
        elif r["media_kind"]:
            await deliver_text("⚠️ Файл не сохранился (слишком большой или одноразовый).")


# ───────────────────────── команды ─────────────────────────
def pack(parts, limit=3800):
    out, cur = [], ""
    for p in parts:
        if cur and len(cur) + len(p) + 2 > limit:
            out.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + p
    if cur:
        out.append(cur)
    return out


async def handle_command(text: str):
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lstrip("/").split("@")[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    n = max(1, min(int(arg), 30)) if arg.isdigit() else None

    if cmd in ("start", "help"):
        return [HELP]

    if cmd == "last":
        cur = await S.db.execute(
            "SELECT * FROM messages WHERE deleted=1 ORDER BY deleted_at DESC LIMIT ?", (n or 10,)
        )
        rows = await cur.fetchall()
        if not rows:
            return ["Удалённых сообщений пока нет."]
        return pack(
            [
                f"🗑 <b>{'Ты' if r['out'] else esc(r['sender_name'])}</b> · {esc(r['chat_name'])}\n"
                f"🕒 {fmt_time(r['ts'])}\n"
                + (f"{LABEL.get(r['media_kind'], '')} " if r["media_kind"] else "")
                + esc((r["text"] or "")[:300])
                for r in rows
            ]
        )

    if cmd == "edited":
        cur = await S.db.execute(
            "SELECT * FROM messages WHERE edits>0 ORDER BY ts DESC LIMIT ?", (n or 10,)
        )
        rows = await cur.fetchall()
        if not rows:
            return ["Изменённых сообщений пока нет."]
        return pack(
            [
                f"✏️ <b>{esc(r['sender_name'])}</b> (правок: {r['edits']})\n{esc((r['text'] or '')[:300])}"
                for r in rows
            ]
        )

    if cmd == "media":
        cur = await S.db.execute(
            "SELECT * FROM messages WHERE deleted=1 AND media_path IS NOT NULL "
            "ORDER BY deleted_at DESC LIMIT ?",
            (n or 5,),
        )
        rows = await cur.fetchall()
        if not rows:
            return ["Удалённых медиа пока нет."]
        return [
            (
                r["media_path"],
                r["media_kind"],
                f"{LABEL.get(r['media_kind'], '')} от {esc(r['sender_name'])} · {fmt_time(r['ts'])}",
            )
            for r in rows
        ]

    if cmd == "search":
        if not arg:
            return ["Использование: /search слово"]
        cur = await S.db.execute(
            "SELECT * FROM messages WHERE text LIKE ? ORDER BY ts DESC LIMIT 10", (f"%{arg}%",)
        )
        rows = await cur.fetchall()
        if not rows:
            return ["Ничего не найдено."]
        return pack(
            [
                f"{'🗑' if r['deleted'] else '💬'} <b>{esc(r['sender_name'])}</b> · {fmt_time(r['ts'])}\n"
                f"{esc((r['text'] or '')[:300])}"
                for r in rows
            ]
        )

    if cmd == "stats":
        cur = await S.db.execute(
            """SELECT COUNT(*) total, SUM(deleted) dels,
                      SUM(CASE WHEN edits>0 THEN 1 ELSE 0 END) eds,
                      SUM(CASE WHEN media_kind IS NOT NULL THEN 1 ELSE 0 END) media
               FROM messages"""
        )
        r = await cur.fetchone()
        cur = await S.db.execute(
            """SELECT sender_name, COUNT(*) c FROM messages
               WHERE deleted=1 AND out=0 GROUP BY sender_id ORDER BY c DESC LIMIT 3"""
        )
        top = await cur.fetchall()
        top_txt = "\n".join(f"  • {esc(t['sender_name'])}: {t['c']}" for t in top) or "  —"
        return [
            f"📊 <b>Статистика</b>\nСохранено сообщений: {r['total']}\n"
            f"Удалено: {r['dels'] or 0}\nИзменено: {r['eds'] or 0}\nС медиа: {r['media'] or 0}\n\n"
            f"<b>Чаще всего удаляют:</b>\n{top_txt}\n\nХранение: {RETENTION_DAYS} дн."
        ]

    if cmd in ("dels", "edits", "own"):
        key, title = {
            "dels": ("notify_del", "Уведомления об удалении"),
            "edits": ("notify_edit", "Уведомления об изменении"),
            "own": ("notify_own", "Уведомления о твоих сообщениях"),
        }[cmd]
        on = await toggle_flag(key)
        return [f"{title}: {'✅ включено' if on else '⛔ выключено'}"]

    if cmd == "wipe":
        cur = await S.db.execute("SELECT media_path FROM messages WHERE media_path IS NOT NULL")
        for r in await cur.fetchall():
            try:
                os.remove(r["media_path"])
            except OSError:
                pass
        await S.db.execute("DELETE FROM messages")
        await S.db.commit()
        return ["🧹 Вся сохранённая история удалена."]

    return []


@dp.message()
async def on_bot_message(m: Message):
    if not m.from_user or m.from_user.id != S.owner:
        return
    if m.text and m.text.startswith("/"):
        await send_items(await handle_command(m.text))


async def on_saved_command(event):  # режим без бота: команды пишешь в «Избранное»
    await send_items(await handle_command(event.raw_text))


# ───────────────────────── обслуживание и запуск ─────────────────────────
async def cleanup_loop():
    while True:
        cutoff = int(time.time()) - RETENTION_DAYS * 86400
        cur = await S.db.execute(
            "SELECT media_path FROM messages WHERE ts<? AND media_path IS NOT NULL", (cutoff,)
        )
        for r in await cur.fetchall():
            try:
                os.remove(r["media_path"])
            except OSError:
                pass
        await S.db.execute("DELETE FROM messages WHERE ts<?", (cutoff,))
        await S.db.commit()
        await asyncio.sleep(3600)


async def main():
    logging.basicConfig(level=logging.INFO)
    if not (API_ID and API_HASH and SESSION_STRING):
        raise SystemExit(
            "Нужны переменные API_ID, API_HASH и SESSION_STRING (получить: python gen_session.py)"
        )

    S.db = await aiosqlite.connect(DB_PATH)
    S.db.row_factory = aiosqlite.Row
    await S.db.executescript(SCHEMA)
    await S.db.commit()

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("SESSION_STRING недействителен. Создай новый через gen_session.py")
    S.client = client
    S.owner = (await client.get_me()).id

    client.add_event_handler(on_new, events.NewMessage())
    client.add_event_handler(on_edit, events.MessageEdited())
    client.add_event_handler(on_delete, events.MessageDeleted())

    tasks = [client.run_until_disconnected(), cleanup_loop()]
    if BOT_TOKEN:
        S.bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        await S.bot.delete_webhook(drop_pending_updates=True)
        tasks.append(dp.start_polling(S.bot))
    else:
        client.add_event_handler(
            on_saved_command, events.NewMessage(outgoing=True, chats="me", pattern=r"^/")
        )

    try:
        await deliver_text("✅ Юзербот запущен. Команды: /help")
    except Exception as e:  # noqa: BLE001
        logging.warning("Стартовое сообщение не доставлено (нажми /start у бота?): %s", e)

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
