"""
Taggerchi Bot — bitta faylda: Telegram bot (buyruqlar) + Userbot (Telethon, real
akkaunt nomidan tag qilish) + SQLite baza + Flask dashboard.

MUHIM: Tag xabarlari endi BOT nomidan emas, balki sizning shaxsiy Telegram
akkauntingiz (userbot) nomidan yuboriladi. Buning uchun avval bir marta
`telethon_login.py` skriptini ishga tushirib SESSION STRING olishingiz kerak.

Ishga tushirish:
    pip install Flask pyTelegramBotAPI telethon python-dotenv

    # 1) Avval (faqat bir marta) userbot sessiyasini yarating:
    python telethon_login.py

    # 2) .env faylini to'ldiring (pastdagi SOZLAMALAR bo'limiga qarang)

    # 3) Botni ishga tushiring:
    python app.py
"""

import os
import time
import html
import random
import sqlite3
import asyncio
import threading
from functools import wraps
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()  # .env faylidan avtomatik o'qiydi (agar mavjud bo'lsa)
except ImportError:
    pass

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
from flask import Flask, request, Response

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, ChatWriteForbiddenError

# ============================================================
# SOZLAMALAR (env orqali o'zgartiriladi)
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "taggerchi.db")
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "admin123")
PORT = int(os.getenv("PORT", "5000"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "5"))

# Pro sozlamalar
TAGGER_COOLDOWN_SECONDS = int(os.getenv("TAGGER_COOLDOWN_SECONDS", "30"))
MAX_INDIVIDUAL_TAG = int(os.getenv("MAX_INDIVIDUAL_TAG", "40"))
INDIVIDUAL_SEND_DELAY = float(os.getenv("INDIVIDUAL_SEND_DELAY", "0.6"))
CHUNK_SEND_DELAY = float(os.getenv("CHUNK_SEND_DELAY", "1.0"))

# ---------- Userbot (Telethon) sozlamalari ----------
# TG_API_ID / TG_API_HASH -> https://my.telegram.org dan olinadi
# TG_SESSION_STRING -> telethon_login.py skripti orqali bir marta generatsiya qilinadi
TG_API_ID = int(os.getenv("TG_API_ID", "0"))
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_SESSION_STRING = os.getenv("TG_SESSION_STRING", "")

if not BOT_TOKEN:
    print("OGOHLANTIRISH: BOT_TOKEN environment variable o'rnatilmagan!")
if not (TG_API_ID and TG_API_HASH and TG_SESSION_STRING):
    print("OGOHLANTIRISH: TG_API_ID / TG_API_HASH / TG_SESSION_STRING to'liq emas — "
          "userbot orqali tag qilish ishlamaydi (telethon_login.py ni ishga tushiring).")

# ============================================================
# BAZA (SQLite, thread-safe)
# ============================================================
_local = threading.local()


def get_conn():
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
    return _local.conn


def now():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            added_at TEXT,
            last_activity TEXT,
            is_active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER,
            user_id INTEGER,
            username TEXT,
            first_name TEXT,
            last_seen TEXT,
            PRIMARY KEY (chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS tagger_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            triggered_by TEXT,
            users_tagged INTEGER,
            source TEXT DEFAULT 'userbot',
            created_at TEXT
        );
        """
    )
    conn.commit()


def upsert_group(chat_id, title, is_active=1):
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO groups (chat_id, title, added_at, last_activity, is_active)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title = excluded.title,
            last_activity = excluded.last_activity,
            is_active = excluded.is_active
        """,
        (chat_id, title, now(), now(), is_active),
    )
    conn.commit()


def set_group_active(chat_id, is_active):
    conn = get_conn()
    conn.execute(
        "UPDATE groups SET is_active = ?, last_activity = ? WHERE chat_id = ?",
        (is_active, now(), chat_id),
    )
    conn.commit()


def touch_group_activity(chat_id):
    conn = get_conn()
    conn.execute("UPDATE groups SET last_activity = ? WHERE chat_id = ?", (now(), chat_id))
    conn.commit()


def upsert_user(chat_id, user_id, username, first_name):
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO users (chat_id, user_id, username, first_name, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_seen = excluded.last_seen
        """,
        (chat_id, user_id, username, first_name, now()),
    )
    conn.commit()


def get_group_users_from_db(chat_id):
    """Zaxira variant: faqat botga yozgan (DB'da bor) userlar."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT user_id, username, first_name FROM users WHERE chat_id = ? ORDER BY first_name",
        (chat_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_groups():
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT g.chat_id, g.title, g.added_at, g.last_activity, g.is_active,
               (SELECT COUNT(*) FROM users u WHERE u.chat_id = g.chat_id) AS known_users
        FROM groups g
        ORDER BY g.is_active DESC, g.last_activity DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_stats():
    conn = get_conn()
    total_groups = conn.execute("SELECT COUNT(*) c FROM groups").fetchone()["c"]
    active_groups = conn.execute("SELECT COUNT(*) c FROM groups WHERE is_active = 1").fetchone()["c"]
    total_users = conn.execute("SELECT COUNT(DISTINCT user_id) c FROM users").fetchone()["c"]
    total_tags = conn.execute("SELECT COALESCE(SUM(users_tagged),0) c FROM tagger_logs").fetchone()["c"]
    return {
        "total_groups": total_groups,
        "active_groups": active_groups,
        "inactive_groups": total_groups - active_groups,
        "total_users": total_users,
        "total_tags": total_tags,
    }


def log_tagger_use(chat_id, triggered_by, users_tagged, source="userbot"):
    conn = get_conn()
    conn.execute(
        "INSERT INTO tagger_logs (chat_id, triggered_by, users_tagged, source, created_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, triggered_by, users_tagged, source, now()),
    )
    conn.commit()


def get_recent_logs(limit=30):
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT l.created_at, l.triggered_by, l.users_tagged, l.source, g.title, l.chat_id
        FROM tagger_logs l
        LEFT JOIN groups g ON g.chat_id = l.chat_id
        ORDER BY l.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ============================================================
# USERBOT (Telethon) — shaxsiy akkaunt nomidan ishlaydi
# ============================================================
_userbot_loop = None
_userbot_client = None
_userbot_ready = threading.Event()
_userbot_me = {}


def _userbot_thread_main():
    global _userbot_loop, _userbot_client
    _userbot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_userbot_loop)
    _userbot_client = TelegramClient(
        StringSession(TG_SESSION_STRING), TG_API_ID, TG_API_HASH, loop=_userbot_loop
    )

    async def _start():
        await _userbot_client.start()
        me = await _userbot_client.get_me()
        _userbot_me["id"] = me.id
        _userbot_me["name"] = f"{me.first_name or ''} (@{me.username})" if me.username else (me.first_name or "")
        print(f"✅ Userbot ulandi: {_userbot_me['name']}")

    _userbot_loop.run_until_complete(_start())
    _userbot_ready.set()
    _userbot_loop.run_forever()


def start_userbot():
    """Userbotni alohida threadda, o'zining asyncio event loopi bilan ishga tushiradi."""
    if not (TG_API_ID and TG_API_HASH and TG_SESSION_STRING):
        print("Userbot ishga tushmadi: TG_API_ID/TG_API_HASH/TG_SESSION_STRING yo'q.")
        return
    t = threading.Thread(target=_userbot_thread_main, daemon=True)
    t.start()
    _userbot_ready.wait(timeout=30)


def _run_on_userbot(coro, timeout=60):
    """Sync kod ichidan userbot event loopiga coroutine yuboradi va natijani kutadi."""
    if not _userbot_client or not _userbot_loop:
        raise RuntimeError("Userbot ulanmagan. TG_API_ID/TG_API_HASH/TG_SESSION_STRING tekshiring.")
    future = asyncio.run_coroutine_threadsafe(coro, _userbot_loop)
    return future.result(timeout=timeout)


async def _fetch_participants(chat_id):
    users = []
    async for p in _userbot_client.iter_participants(chat_id):
        if p.bot or p.id == _userbot_me.get("id"):
            continue
        users.append({
            "user_id": p.id,
            "username": p.username,
            "first_name": p.first_name or "Foydalanuvchi",
        })
    return users


def get_group_users_live(chat_id):
    """Guruh a'zolarini real-time, userbot orqali (to'liq ro'yxat) oladi."""
    return _run_on_userbot(_fetch_participants(chat_id))


async def _send_as_user(chat_id, text):
    for attempt in range(3):
        try:
            return await _userbot_client.send_message(chat_id, text, parse_mode="html")
        except FloodWaitError as e:
            wait_s = e.seconds + 1
            print(f"⏳ Userbot flood-limit: {wait_s}s kutamiz...")
            await asyncio.sleep(wait_s)
        except ChatWriteForbiddenError:
            print("Userbot bu guruhda yoza olmaydi (yozish taqiqlangan yoki a'zo emas).")
            return None
        except Exception as e:
            print(f"Userbot xabar yuborishda xatolik: {e}")
            return None
    return None


def userbot_send(chat_id, text):
    return _run_on_userbot(_send_as_user(chat_id, text))


# ============================================================
# TELEGRAM BOT (faqat buyruqlarni qabul qilish + boshqaruv uchun)
# ============================================================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML") if BOT_TOKEN else None
BOT_USERNAME = None  # run_bot() ichida bot.get_me() orqali to'ldiriladi


def mention_html(user: dict) -> str:
    """@username orqali, bo'lmasa ism + tg://user link orqali chaqiradi (HTML-safe)."""
    username = user.get("username")
    if username:
        return f"@{html.escape(username)}"
    name = html.escape(user.get("first_name") or "Foydalanuvchi")
    return f'<a href="tg://user?id={user["user_id"]}">{name}</a>'


def chunk_list(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ---------- Har bir userga tegishli qiziqarli random gaplar ----------
FUNNY_TAG_PHRASES = [
    "klaviatura tagida qolib ketdingmi? 😂",
    "nikoh to'yi qachon bo'ladi, kutyapmiz! 💍😄",
    "internet o'chib qoldimi yo o'zing yashiringdingmi? 📡🙈",
    "uxlab qolibsan shekilli 😴💤",
    "chaqiryapmiz, javob ber tezroq! 📢",
    "guruhda ko'rinmay ketding-ku, hormisan?! 👀",
    "sen borligingni unutdik deb o'ylama 😏",
    "choy-poy tayyor, kelsang bo'ladi ☕",
    "telefonni qo'lga ol endi! 📱",
    "kelasan-a, kutyapmiz o'zingni! 🙌",
    "qayerlarda sayr qilib yuribsan? 🚶‍♂️",
    "onlayn bo'lsang chiq, bo'lmasa keyin javob ber 😅",
    "guruh sensiz zerikib qoldi 🥱",
    "faollik pasayib ketdi, biror narsa yoz! 📉",
    "sog'indik-ku, chiqib qol bir marta 🥺",
    "hazilakam chaqirdik, xafa bo'lma 😄",
    "sinov uchun chaqirildi, tekshiruvdan o'ting ✅",
    "imtihonga tayyormisan? 📚",
    "kim ekaningni eslatib qo'ysak dedik 🤔",
    "500 kishidan biri aynan sen ekansan 🎉",
    "bugun navbat senga ekan 😁",
    "telefoning vibratsiya qilyaptimi? 📳",
    "qani ovoz ber-chi 😄",
    "yo'qolib ketganlar ro'yxatiga tushib qolma 😂",
    "sen bo'lmasang qiziq emas ekan 😅",
    "biz seni kutyapmiz! 🙃",
    "bir ko'rinish berib ket 😎",
    "hamma yig'ildi, seni kutyapmiz!",
    "ko'rinib qo'y endi 😄",
    "bir daqiqaga kirib ket!",
    "signal senga yetib bordimi? 📡",
    "shu yerga bir qarab qo'y 👀",
    "bugungi mehmon sensan 😁",
    "hamma seni chaqiryapti!",
    "telefoningni yoqib ol 😅",
    "bugun jim bo'lib qolibsan-ku 🤭",
    "guruh seni esladi 😄",
    "yo'qolgan topildi 😂",
    "seni qidiruvga beramiz hozir 😆",
    "hamma seni kutmoqda!",
    "tezroq yetib kel!",
    "o'zingni ko'rsatib qo'y 😎",
    "bugungi faol bo'lish navbati senga!",
    "bir salom yozib ket 😁",
    "shu yerga bir kirib chiq!",
    "hamma seni belgilayapti 😂",
    "nima gap, jimjitlik-ku 🤨",
    "senga maxsus chaqiriq!",
    "bugun dam olish yo'q 😅",
    "qayerdasan, qahramon? 🦸",
    "sen uchun maxsus signal 🚨",
    "uyg'onish vaqti keldi ⏰",
    "uyqudan tur endi 😴",
    "bir javob berib qo'y 😁",
    "bir emoji tashab ket 😄",
    "hamma seni sog'ingan shekilli 🥹",
    "bir ko'rinib ket, iltimos 😄",
    "telefoningni tekshir 📱",
    "signal qabul qilindi 📶",
    "kimdir seni esladi 😏",
    "bugun navbatchi sensan 😂",
    "shu yerda ekaningni bildir!",
    "faollar safiga qayt!",
    "jim turish taqiqlanadi 😁",
    "hamma seni kutmoqda 😊",
    "yozishni unutibsan shekilli 🤭",
    "bir kulgili gap yozib yubor 😄",
    "bizga qo'shil!",
    "hamma shu yerda, sen qolib ketding 😅",
    "qani, ovozingni eshitaylik!",
    "bir belgi ber 😊",
    "shu yerga kirib o't!",
    "senga navbat keldi 😁",
    "bugungi rekordchini kutyapmiz 😂",
    "guruh faolligini oshir!",
    "jim bo'lish rekordini yangilama 😆",
    "bir daqiqa vaqt ajrat!",
    "sen bo'lmasang bo'lmaydi!",
    "tezroq ko'rinib qo'y 😄",
    "bir xabar tashlab ket!",
    "guruh seni chaqiryapti 📢",
    "signal faqat senga yuborildi 😎",
    "bugun omadli odamsan 🍀",
    "seni esdan chiqarmadik 😁",
    "hamma seni kuzatyapti 😂",
    "biror narsa yozib yubor!",
    "faollikni boshlaymizmi? 😄",
    "bir salom yetarli 😊",
    "bugungi vazifa: javob berish 😁",
    "telefonni ushlab turgan bo'lsang javob ber 😆",
    "hamma seni kutib qolgan 😄",
    "guruhga qaytish vaqti keldi!",
    "bugun seni tanladik 😂",
    "qayerga g'oyib bo'lding? 🤔",
    "bir daqiqa shu yerga qarab qo'y!",
    "signalni o'tkazib yuborma 🚨",
    "yana bir marta chaqiramiz 😁",
    "endi javob berishga majbursan 😂",
    "hamma seni tag qilyapti 😆",
    "kel, suhbatni davom ettiramiz 😊",
    "oxirgi ogohlantirish emas albatta 😂",
    "bugungi kulgi uchun sen kerak 😄",
    "endi bahona qabul qilinmaydi 😁"
]

FUNNY_INTROS = [
    "📢 E'lon vaqti keldi!",
    "🔔 Diqqat, diqqat, hammaga tegishli!",
    "🚨 Muhim xabar bor, o'qib chiqing!",
    "📣 Barchaga tegishli xabar!",
    "🎯 Eshiting-chi, bu sizga!",
    "📌 Yangilik bor, diqqat bilan o'qing!",
    "⚡ Hamma bir daqiqaga shu yerga!",
    "👀 Qani, barchangiz shu tomonga qarang!",
    "🔥 Faollarni yig'yapmiz, marhamat!",
    "💬 Bir daqiqa e'tibor, muhim gap bor!"
]


def safe_send(chat_id, text, **kwargs):
    """Bot orqali (masalan xatolik/ogohlantirish xabarlari uchun) flood-safe yuborish."""
    for attempt in range(3):
        try:
            return bot.send_message(chat_id, text, parse_mode="HTML", **kwargs)
        except ApiTelegramException as e:
            if e.error_code == 429:
                retry_after = 3
                try:
                    retry_after = e.result_json["parameters"]["retry_after"]
                except Exception:
                    pass
                print(f"⏳ Flood-limit: {retry_after}s kutamiz...")
                time.sleep(retry_after + 1)
                continue
            print(f"Telegram API xatolik: {e}")
            return None
        except Exception as e:
            print(f"Xabar yuborishda kutilmagan xatolik: {e}")
            return None
    return None


# Guruh bo'yicha oxirgi /tagger ishlatilgan vaqt (spamdan himoya)
_last_tagger_use = {}

# Guruh bo'yicha hozir ishlayotgan /tagger jarayonini to'xtatish uchun eventlar.
# Kalit faqat jarayon davomida mavjud bo'ladi (boshlanganda qo'shiladi, tugaganda o'chiriladi).
_active_cancel_events = {}


def handle_cancel(message):
    chat_id = message.chat.id
    cancel_event = _active_cancel_events.get(chat_id)
    if cancel_event and not cancel_event.is_set():
        cancel_event.set()
        bot.reply_to(message, "🛑 /tagger to'xtatildi. Qolgan userlarga xabar yuborilmaydi.")
    else:
        bot.reply_to(message, "Hozir to'xtatadigan hech qanday /tagger jarayoni yo'q.")


def handle_tagger(message):
    chat_id = message.chat.id
    text = message.text or ""
    parts = text.split(maxsplit=1)

    extra_text = None
    if len(parts) > 1 and parts[1].strip():
        extra_text = html.escape(parts[1].strip())

    # ---------- Spamdan himoya (cooldown) ----------
    last_used = _last_tagger_use.get(chat_id, 0)
    elapsed = time.time() - last_used
    if elapsed < TAGGER_COOLDOWN_SECONDS:
        wait_left = int(TAGGER_COOLDOWN_SECONDS - elapsed)
        bot.reply_to(
            message,
            f"⏳ Bir oz sabr qiling, {wait_left} soniyadan keyin yana /tagger ishlatishingiz mumkin.",
        )
        return
    _last_tagger_use[chat_id] = time.time()

    if chat_id in _active_cancel_events and not _active_cancel_events[chat_id].is_set():
        bot.reply_to(message, "⚠️ Bu guruhda allaqachon /tagger ishlayapti. Avval /cancel qiling yoki kuting.")
        return

    cancel_event = threading.Event()
    _active_cancel_events[chat_id] = cancel_event

    # ---------- A'zolar ro'yxatini userbot orqali REAL-TIME olish ----------
    users = []
    source = "userbot"
    try:
        bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass

    try:
        users = get_group_users_live(chat_id)
    except Exception as e:
        print(f"Userbot orqali a'zolarni olishda xatolik: {e}")
        bot.reply_to(
            message,
            "⚠️ Userbot orqali guruh a'zolarini olib bo'lmadi.\n"
            "Tekshiring: 1) userbot akkaunt shu guruhga qo'shilganmi? "
            "2) TG_API_ID / TG_API_HASH / TG_SESSION_STRING to'g'rimi?\n\n"
            "Hozircha faqat botga avval yozgan userlar orqali davom etamiz...",
        )
        users = get_group_users_from_db(chat_id)
        source = "db_fallback"

    if not users:
        bot.reply_to(
            message,
            "Hech kimni tag qilib bo'lmadi ⚠️\n"
            "Guruhda hali hech qanday (bot yoki adminlardan tashqari) a'zo topilmadi.",
        )
        return

    total_tagged = 0
    was_cancelled = False

    try:
        if extra_text:
            for chunk in chunk_list(users, CHUNK_SIZE):
                if cancel_event.is_set():
                    was_cancelled = True
                    break
                mentions = "\n".join(f"👤 {mention_html(u)}" for u in chunk)
                intro = random.choice(FUNNY_INTROS)
                msg_text = f"{intro}\n\n{mentions}\n\n💬 <i>{extra_text}</i>"
                if userbot_send(chat_id, msg_text):
                    total_tagged += len(chunk)
                time.sleep(CHUNK_SEND_DELAY)

        elif len(users) <= MAX_INDIVIDUAL_TAG:
            for u in users:
                if cancel_event.is_set():
                    was_cancelled = True
                    break
                phrase = random.choice(FUNNY_TAG_PHRASES)
                msg_text = f"{mention_html(u)}, {phrase}"
                if userbot_send(chat_id, msg_text):
                    total_tagged += 1
                time.sleep(INDIVIDUAL_SEND_DELAY)

        else:
            for chunk in chunk_list(users, CHUNK_SIZE):
                if cancel_event.is_set():
                    was_cancelled = True
                    break
                mentions = "\n".join(f"👤 {mention_html(u)}" for u in chunk)
                phrase = random.choice(FUNNY_TAG_PHRASES)
                msg_text = f"📢 Hammaga chaqiruv!\n\n{mentions}\n\n<i>({phrase})</i>"
                if userbot_send(chat_id, msg_text):
                    total_tagged += len(chunk)
                time.sleep(CHUNK_SEND_DELAY)
    finally:
        # Jarayon tugadi (yakunlandi yoki to'xtatildi) — kalitni tozalaymiz
        _active_cancel_events.pop(chat_id, None)

    triggered_by = f"@{message.from_user.username}" if message.from_user.username else str(message.from_user.id)
    source_label = f"{source}_cancelled" if was_cancelled else source
    log_tagger_use(chat_id, triggered_by, total_tagged, source=source_label)

    if was_cancelled:
        bot.send_message(chat_id, f"🛑 To'xtatildi — jami {total_tagged} ta userga xabar yuborilgan edi.")


def setup_bot_handlers():

    # ---------- /start (faqat shaxsiy chatda) ----------
    @bot.message_handler(commands=["start"], func=lambda m: m.chat.type == "private")
    def on_start(message):
        name = message.from_user.first_name or "do'stim"
        text = (
            f"Assalomu alaykum, <b>{name}</b>! 👋\n\n"
            f"Men <b>Taggerchi</b> botman 🏷️ — guruhlarda barcha a'zolarni bir zumda "
            f"chaqirib chiqaman (tag xabarlari shaxsiy akkaunt nomidan yuboriladi).\n\n"
            f"⚙️ <b>Qanday ishlayman:</b>\n"
            f"• <code>/tagger</code> — guruhdagi barcha a'zolarni 5 tadan chaqiraman\n"
            f"• <code>/tagger matningiz</code> — har bir chaqiruvga matningizni ham qo'shaman\n"
            f"• <code>/cancel</code> — hozir ketayotgan /tagger jarayonini to'xtataman\n\n"
            f"Boshlash uchun meni guruhingizga qo'shing 👇"
        )
        markup = types.InlineKeyboardMarkup(row_width=1)
        add_url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
        markup.add(types.InlineKeyboardButton("➕ Meni guruhga qo'shish", url=add_url))
        markup.add(types.InlineKeyboardButton("ℹ️ Qanday ishlataman?", callback_data="help"))
        bot.send_message(message.chat.id, text, reply_markup=markup)

    @bot.callback_query_handler(func=lambda call: call.data == "help")
    def on_help_callback(call):
        text = (
            "📖 <b>Qo'llanma</b>\n\n"
            "1️⃣ Meni (botni) guruhingizga qo'shing — buyruqlarni shu orqali eshitaman\n"
            "2️⃣ Shaxsiy (userbot) akkauntingiz ham o'sha guruhga a'zo bo'lishi kerak — "
            "tag xabarlari shu akkaunt nomidan yuboriladi\n"
            "3️⃣ <code>/tagger</code> yoki <code>/tagger salom hammaga</code> deb yozing 🚀\n"
            "4️⃣ Kerak bo'lsa <code>/cancel</code> deb yozib, jarayonni to'xtatishingiz mumkin\n\n"
            "Userbot orqali guruhning TO'LIQ a'zolar ro'yxati olinadi — "
            "avval yozgan bo'lishlari shart emas."
        )
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, text)

    @bot.my_chat_member_handler()
    def on_bot_membership_change(update: types.ChatMemberUpdated):
        chat = update.chat
        if chat.type not in ("group", "supergroup"):
            return

        old_status = update.old_chat_member.status
        new_status = update.new_chat_member.status
        is_active = 1 if new_status in ("member", "administrator") else 0
        upsert_group(chat.id, chat.title or str(chat.id), is_active=is_active)

        just_added = old_status in ("left", "kicked") and new_status in ("member", "administrator")
        became_admin = old_status == "member" and new_status == "administrator"
        got_kicked = new_status in ("left", "kicked")

        try:
            if just_added:
                text = (
                    "Assalomu alaykum, guruh a'zolari! 👋🏷️\n\n"
                    "Men <b>Taggerchi</b> botman — <code>/tagger</code> buyrug'i orqali "
                    "hammani chaqirib beraman (tag xabarlari alohida shaxsiy akkaunt nomidan keladi).\n\n"
                    "⚠️ Ishlashim uchun bog'langan userbot akkaunt ham shu guruhga a'zo bo'lishi kerak."
                )
                bot.send_message(chat.id, text)
            elif became_admin:
                bot.send_message(chat.id, "✅ Rahmat! Endi to'liq ishlashga tayyorman 🚀")
            elif got_kicked:
                pass
        except Exception as e:
            print(f"Salomlashish xabarida xatolik: {e}")

    @bot.message_handler(
        func=lambda m: m.chat.type in ("group", "supergroup"),
        content_types=[
            "text", "photo", "video", "sticker", "document",
            "audio", "voice", "video_note", "animation", "location", "contact",
        ],
    )
    def track_and_handle(message):
        chat = message.chat
        user = message.from_user

        upsert_group(chat.id, chat.title or str(chat.id), is_active=1)
        if user and not user.is_bot:
            upsert_user(chat.id, user.id, user.username, user.first_name)
        touch_group_activity(chat.id)

        if message.content_type == "text" and message.text:
            cmd_base = message.text.split()[0].split("@")[0]
            if cmd_base == "/tagger":
                handle_tagger(message)
            elif cmd_base == "/cancel":
                handle_cancel(message)


def run_bot():
    global BOT_USERNAME
    if not bot:
        print("BOT_TOKEN yo'q, bot ishga tushmaydi.")
        return
    try:
        me = bot.get_me()
        BOT_USERNAME = me.username
        print(f"Bot username: @{BOT_USERNAME}")
    except Exception as e:
        print(f"Bot ma'lumotini olishda xatolik: {e}")
    setup_bot_handlers()
    print("Taggerchi bot ishga tushdi (polling)...")
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            print(f"Bot polling xatolik, 5 soniyadan keyin qayta urinish: {e}")
            time.sleep(5)


# ============================================================
# FLASK DASHBOARD
# ============================================================
app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Taggerchi — Bot Dashboard</title>
<meta http-equiv="refresh" content="30">
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body { background: radial-gradient(circle at top, #1b1730 0%, #0b0a17 60%); }
  .glass { background: rgba(255,255,255,0.05); backdrop-filter: blur(14px); border: 1px solid rgba(255,255,255,0.08); }
  .badge-active { background: rgba(34,197,94,0.15); color:#4ade80; border:1px solid rgba(74,222,128,0.3); }
  .badge-inactive { background: rgba(239,68,68,0.15); color:#f87171; border:1px solid rgba(248,113,113,0.3); }
  .badge-userbot { background: rgba(59,130,246,0.15); color:#60a5fa; border:1px solid rgba(96,165,250,0.3); }
  .badge-fallback { background: rgba(234,179,8,0.15); color:#facc15; border:1px solid rgba(250,204,21,0.3); }
</style>
</head>
<body class="min-h-screen text-slate-100 font-sans">
  <div class="max-w-6xl mx-auto px-6 py-10">
    <div class="flex items-center justify-between mb-8">
      <div>
        <h1 class="text-3xl font-bold tracking-tight">🏷️ Taggerchi</h1>
        <p class="text-slate-400 text-sm mt-1">Bot statusi va guruhlar monitoringi · 30s da yangilanadi</p>
      </div>
      <div class="text-xs text-slate-500">MindStudio</div>
    </div>

    <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-10">
      <div class="glass rounded-2xl p-5">
        <div class="text-slate-400 text-xs uppercase tracking-wide">Jami guruhlar</div>
        <div class="text-3xl font-bold mt-2">{{ stats.total_groups }}</div>
      </div>
      <div class="glass rounded-2xl p-5">
        <div class="text-slate-400 text-xs uppercase tracking-wide">Faol guruhlar</div>
        <div class="text-3xl font-bold mt-2 text-emerald-400">{{ stats.active_groups }}</div>
      </div>
      <div class="glass rounded-2xl p-5">
        <div class="text-slate-400 text-xs uppercase tracking-wide">Nofaol guruhlar</div>
        <div class="text-3xl font-bold mt-2 text-rose-400">{{ stats.inactive_groups }}</div>
      </div>
      <div class="glass rounded-2xl p-5">
        <div class="text-slate-400 text-xs uppercase tracking-wide">Tanilgan userlar</div>
        <div class="text-3xl font-bold mt-2">{{ stats.total_users }}</div>
      </div>
    </div>

    <div class="glass rounded-2xl p-6 mb-10">
      <h2 class="text-lg font-semibold mb-4">Guruhlar ro'yxati</h2>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-left text-slate-400 border-b border-white/10">
              <th class="py-2 pr-4">Guruh nomi</th>
              <th class="py-2 pr-4">Chat ID</th>
              <th class="py-2 pr-4">Tanilgan userlar</th>
              <th class="py-2 pr-4">Oxirgi faollik</th>
              <th class="py-2 pr-4">Status</th>
            </tr>
          </thead>
          <tbody>
            {% for g in groups %}
            <tr class="border-b border-white/5 hover:bg-white/5 transition">
              <td class="py-3 pr-4 font-medium">{{ g.title }}</td>
              <td class="py-3 pr-4 text-slate-500">{{ g.chat_id }}</td>
              <td class="py-3 pr-4">{{ g.known_users }}</td>
              <td class="py-3 pr-4 text-slate-500">{{ g.last_activity[:19] if g.last_activity else '—' }}</td>
              <td class="py-3 pr-4">
                {% if g.is_active %}
                  <span class="px-2 py-1 rounded-full text-xs badge-active">Faol</span>
                {% else %}
                  <span class="px-2 py-1 rounded-full text-xs badge-inactive">Nofaol (chiqarilgan)</span>
                {% endif %}
              </td>
            </tr>
            {% else %}
            <tr><td colspan="5" class="py-6 text-center text-slate-500">Hozircha hech qanday guruh yo'q.</td></tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>

    <div class="glass rounded-2xl p-6">
      <h2 class="text-lg font-semibold mb-4">Oxirgi /tagger chaqiruvlari</h2>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="text-left text-slate-400 border-b border-white/10">
              <th class="py-2 pr-4">Vaqt</th>
              <th class="py-2 pr-4">Guruh</th>
              <th class="py-2 pr-4">Kim chaqirdi</th>
              <th class="py-2 pr-4">Nechta user tag qilindi</th>
              <th class="py-2 pr-4">Manba</th>
            </tr>
          </thead>
          <tbody>
            {% for l in logs %}
            <tr class="border-b border-white/5">
              <td class="py-2 pr-4 text-slate-500">{{ l.created_at[:19] }}</td>
              <td class="py-2 pr-4">{{ l.title or l.chat_id }}</td>
              <td class="py-2 pr-4">{{ l.triggered_by }}</td>
              <td class="py-2 pr-4">{{ l.users_tagged }}</td>
              <td class="py-2 pr-4">
                {% if l.source == 'userbot' %}
                  <span class="px-2 py-1 rounded-full text-xs badge-userbot">userbot</span>
                {% else %}
                  <span class="px-2 py-1 rounded-full text-xs badge-fallback">db fallback</span>
                {% endif %}
              </td>
            </tr>
            {% else %}
            <tr><td colspan="5" class="py-6 text-center text-slate-500">Hali /tagger ishlatilmagan.</td></tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>
</body>
</html>
"""


def check_auth(username, password):
    return username == DASHBOARD_USER and password == DASHBOARD_PASS


def authenticate():
    return Response(
        "Kirish uchun login/parol kerak.", 401,
        {"WWW-Authenticate": 'Basic realm="Taggerchi Dashboard"'},
    )


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated


@app.route("/")
@requires_auth
def dashboard():
    from flask import render_template_string
    stats = get_stats()
    groups = get_all_groups()
    logs = get_recent_logs(20)
    return render_template_string(DASHBOARD_HTML, stats=stats, groups=groups, logs=logs)


@app.route("/health")
def health():
    return {
        "status": "ok",
        "userbot_connected": bool(_userbot_client and _userbot_ready.is_set()),
        "userbot_account": _userbot_me.get("name"),
    }


# ============================================================
# ISHGA TUSHIRISH
# ============================================================
def start_bot_in_background():
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()


if __name__ == "__main__":
    init_db()
    start_userbot()          # 1) shaxsiy akkaunt (userbot) ulanadi
    start_bot_in_background()  # 2) bot buyruqlarni eshita boshlaydi
    app.run(host="0.0.0.0", port=PORT)
