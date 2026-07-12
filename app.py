"""
Taggerchi Bot — bitta faylda: Telegram bot + SQLite baza + Flask dashboard.

Ishga tushirish:
    pip install Flask pyTelegramBotAPI
    export BOT_TOKEN="123456:AA...."
    python app.py
"""

import os
import time
import html
import random
import sqlite3
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
TAGGER_COOLDOWN_SECONDS = int(os.getenv("TAGGER_COOLDOWN_SECONDS", "0"))
# Agar guruhdagi tanish userlar shundan ko'p bo'lsa, individual jo'natish
# o'rniga (flood-limitga tushib qolmaslik uchun) guruhlab jo'natishga o'tadi
MAX_INDIVIDUAL_TAG = int(os.getenv("MAX_INDIVIDUAL_TAG", "40"))
INDIVIDUAL_SEND_DELAY = float(os.getenv("INDIVIDUAL_SEND_DELAY", "0.35"))
CHUNK_SEND_DELAY = float(os.getenv("CHUNK_SEND_DELAY", "0.5"))

if not BOT_TOKEN:
    print("OGOHLANTIRISH: BOT_TOKEN environment variable o'rnatilmagan!")

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


def get_group_users(chat_id):
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


def log_tagger_use(chat_id, triggered_by, users_tagged):
    conn = get_conn()
    conn.execute(
        "INSERT INTO tagger_logs (chat_id, triggered_by, users_tagged, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, triggered_by, users_tagged, now()),
    )
    conn.commit()


def get_recent_logs(limit=30):
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT l.created_at, l.triggered_by, l.users_tagged, g.title, l.chat_id
        FROM tagger_logs l
        LEFT JOIN groups g ON g.chat_id = l.chat_id
        ORDER BY l.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ============================================================
# TELEGRAM BOT
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
    "faollik pastlab ketti, biror narsa yoz! 📉😂",
    "sog'indik-ku, chiqib qol bir marta 🥺",
    "hazilakam chaqirdik, xafa bo'lma 😄",
    "sinov uchun chaqirildi, tekshiruvdan o'ting ✅😂",
    "imtihonga tayyormisan, degandik 📚😏",
    "kim ekaningni eslatib qo'ysak dedik 🤔",
    "500 dan biri aynan sen ekansan, tabriklaymiz 🎉",
]

FUNNY_INTROS = [
    "📢 E'lon vaqti keldi!",
    "🔔 Diqqat, diqqat, hammaga tegishli!",
    "🚨 Muhim xabar bor, o'qib chiqing!",
    "📣 Barchaga tegishli xabar!",
    "🎯 Eshiting-chi, bu sizga!",
    "📌 Yangilik bor, diqqat bilan o'qing!",
]


def safe_send(chat_id, text, **kwargs):
    """Flood-limit (429) va boshqa Telegram xatolarini boshqarib xabar yuboradi."""
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


def handle_tagger(message):
    chat_id = message.chat.id
    text = message.text or ""
    parts = text.split(maxsplit=1)

    # Foydalanuvchi yozgan qo'shimcha matn (kod bo'lsa ham) — HTML-safe qilib escape qilamiz,
    # shu tufayli ichida <, >, & bo'lsa ham Telegram "can't parse entities" xatosi bermaydi
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

    users = get_group_users(chat_id)
    if not users:
        bot.reply_to(
            message,
            "Hali botga tanish foydalanuvchi yo'q ⚠️\n"
            "Bot faqat guruhda xabar yozgan (yoki sync_members.py orqali import qilingan) "
            "userlarni tag qila oladi. Odamlar birrov guruhga xabar yozsin, keyin /tagger ishlaydi.",
        )
        return

    try:
        bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass

    total_tagged = 0

    if extra_text:
        # ---------- Matn bilan: 5 tadan guruhlab, chiroyli intro bilan ----------
        for chunk in chunk_list(users, CHUNK_SIZE):
            mentions = "\n".join(f"👤 {mention_html(u)}" for u in chunk)
            intro = random.choice(FUNNY_INTROS)
            msg_text = f"{intro}\n\n{mentions}\n\n💬 <i>{extra_text}</i>"
            if safe_send(chat_id, msg_text):
                total_tagged += len(chunk)
            time.sleep(CHUNK_SEND_DELAY)

    elif len(users) <= MAX_INDIVIDUAL_TAG:
        # ---------- Matnsiz, kam odamli guruh: har biriga alohida, qiziqarli gap bilan ----------
        for u in users:
            phrase = random.choice(FUNNY_TAG_PHRASES)
            msg_text = f"{mention_html(u)}, {phrase}"
            if safe_send(chat_id, msg_text):
                total_tagged += 1
            time.sleep(INDIVIDUAL_SEND_DELAY)

    else:
        # ---------- Juda katta guruh: flood-limitga tushmaslik uchun guruhlab yuboramiz ----------
        for chunk in chunk_list(users, CHUNK_SIZE):
            mentions = "\n".join(f"👤 {mention_html(u)}" for u in chunk)
            phrase = random.choice(FUNNY_TAG_PHRASES)
            msg_text = f"📢 Hammaga chaqiruv!\n\n{mentions}\n\n<i>({phrase})</i>"
            if safe_send(chat_id, msg_text):
                total_tagged += len(chunk)
            time.sleep(CHUNK_SEND_DELAY)

    triggered_by = f"@{message.from_user.username}" if message.from_user.username else str(message.from_user.id)
    log_tagger_use(chat_id, triggered_by, total_tagged)


def setup_bot_handlers():

    # ---------- /start (faqat shaxsiy chatda) ----------
    @bot.message_handler(commands=["start"], func=lambda m: m.chat.type == "private")
    def on_start(message):
        name = message.from_user.first_name or "do'stim"
        text = (
            f"Assalomu alaykum, <b>{name}</b>! 👋\n\n"
            f"Men <b>Taggerchi</b> botman 🏷️ — guruhlarda barcha a'zolarni bir zumda "
            f"chaqirib chiqaman.\n\n"
            f"⚙️ <b>Qanday ishlayman:</b>\n"
            f"• <code>/tagger</code> — guruhdagi tanish userlarni 5 tadan chaqiraman\n"
            f"• <code>/tagger matningiz</code> — har bir 5 kishilik chaqiruvga matningizni ham qo'shaman\n\n"
            f"Boshlash uchun meni guruhingizga qo'shing 👇"
        )
        markup = types.InlineKeyboardMarkup(row_width=1)
        add_url = f"https://t.me/{BOT_USERNAME}?startgroup=true"
        markup.add(types.InlineKeyboardButton("➕ Meni guruhga qo'shish", url=add_url))
        markup.add(types.InlineKeyboardButton("ℹ️ Qanday ishlataman?", callback_data="help"))
        bot.send_message(message.chat.id, text, reply_markup=markup)

    # ---------- Inline tugmalar ----------
    @bot.callback_query_handler(func=lambda call: call.data == "help")
    def on_help_callback(call):
        text = (
            "📖 <b>Qo'llanma</b>\n\n"
            "1️⃣ Meni guruhingizga qo'shing\n"
            "2️⃣ Ishlashim uchun meni <b>admin</b> qiling (Group Privacy o'chirilgan bo'lishi kerak)\n"
            "3️⃣ Guruh a'zolari bir marta xabar yozsin — shundagina men ularni \"tanib\" olaman\n"
            "4️⃣ <code>/tagger</code> yoki <code>/tagger salom hammaga</code> deb yozing 🚀\n\n"
            "Telegram qoidasiga ko'ra hech qanday bot guruhning to'liq a'zolar ro'yxatini "
            "ololmaydi — shuning uchun faqat yozgan userlarni chaqiraman."
        )
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, text)

    # ---------- Bot guruhga qo'shilishi / chiqarilishi / admin bo'lishi ----------
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
                    "hammani chaqirib beraman.\n\n"
                    "⚠️ To'liq ishlashim uchun meni <b>admin</b> qilib qo'ying — "
                    "shunda tayyor ekanligimni aytaman ✅"
                )
                if new_status == "administrator":
                    text += "\n\n🎉 Men allaqachon adminman — ishlashga tayyorman! /tagger deb yozing."
                bot.send_message(chat.id, text)
            elif became_admin:
                bot.send_message(
                    chat.id,
                    "✅ Rahmat! Endi men adminman va to'liq ishlashga tayyorman 🚀\n"
                    "Sinab ko'ring: <code>/tagger</code>",
                )
            elif got_kicked:
                pass  # guruhdan chiqarilganda xabar yuborib bo'lmaydi
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
# FLASK DASHBOARD (HTML shu faylning ichida, template sifatida)
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
            </tr>
          </thead>
          <tbody>
            {% for l in logs %}
            <tr class="border-b border-white/5">
              <td class="py-2 pr-4 text-slate-500">{{ l.created_at[:19] }}</td>
              <td class="py-2 pr-4">{{ l.title or l.chat_id }}</td>
              <td class="py-2 pr-4">{{ l.triggered_by }}</td>
              <td class="py-2 pr-4">{{ l.users_tagged }}</td>
            </tr>
            {% else %}
            <tr><td colspan="4" class="py-6 text-center text-slate-500">Hali /tagger ishlatilmagan.</td></tr>
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
    return {"status": "ok"}


# ============================================================
# ISHGA TUSHIRISH
# ============================================================
def start_bot_in_background():
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()


if __name__ == "__main__":
    init_db()
    start_bot_in_background()
    app.run(host="0.0.0.0", port=PORT)
