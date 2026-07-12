import os
import sys
import asyncio
import logging
import sqlite3
import threading
import random
from datetime import datetime
from flask import Flask, jsonify, render_template_string
import telebot
from telebot import types
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, FloodWaitError

# ==========================================
# SOZLAMALAR VA LOGGING
# ==========================================

# Flask Dashboard porti (Render platformasi uchun standart)
PORT = int(os.environ.get("PORT", 5000))

# Telegram Bot Tokenini olish
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Muhit o'zgaruvchilarida BOT_TOKEN topilmadi!")

# Global API_ID va API_HASH qiymatlarini olish
TG_API_ID_RAW = os.environ.get("TG_API_ID")
TG_API_HASH = os.environ.get("TG_API_HASH")

if not TG_API_ID_RAW or not TG_API_HASH:
    raise ValueError("Muhit o'zgaruvchilarida TG_API_ID yoki TG_API_HASH topilmadi!")

try:
    TG_API_ID = int(TG_API_ID_RAW)
except ValueError:
    raise ValueError("TG_API_ID faqat raqamlardan iborat bo'lishi kerak!")

# Admin Telegram ID (faqat /accounts buyrug'ini ko'rish uchun)
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0

# Har bir foydalanuvchi uchun tagger kutish vaqti (cooldown) soniyada
COOLDOWN_SECONDS = 60

# Loglarni standart chiqish oqimiga (stdout) sozlash
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("MultiAccountBot")

# Flask ilovasini sozlash
app = Flask(__name__)

# pyTelegramBotAPI sozlash
bot = telebot.TeleBot(BOT_TOKEN, threaded=True)

# ==========================================
# TELETHON UCHUN ASYNCIO EVENT LOOP
# ==========================================

# Telethon klientlarini boshqarish uchun maxsus asyncio oqimi
loop = asyncio.new_event_loop()

def start_asyncio_loop(loop_instance):
    asyncio.set_event_loop(loop_instance)
    loop_instance.run_forever()

loop_thread = threading.Thread(target=start_asyncio_loop, args=(loop,), daemon=True)
loop_thread.start()

# ==========================================
# MA'LUMOTLAR BAZASI (SQLITE)
# ==========================================

DB_PATH = "database.db"

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Tizim boshlanganda ma'lumotlar bazasi jadvallarini yaratadi."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Foydalanuvchilarning ulangan Telethon akkountlari (api_id va api_hash olib tashlandi)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            telegram_user_id INTEGER PRIMARY KEY,
            phone TEXT,
            session_string TEXT,
            telegram_account_id INTEGER,
            telegram_name TEXT,
            username TEXT,
            connected_at TEXT,
            last_used TEXT
        )
    """)
    
    # Guruhlar tarixi jadvali
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            group_id INTEGER PRIMARY KEY,
            title TEXT,
            added_at TEXT
        )
    """)
    
    # Tizim statistikasi
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            key TEXT PRIMARY KEY,
            value INTEGER
        )
    """)
    
    # Cooldown (Kutish vaqti) jadvali
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cooldowns (
            user_id INTEGER PRIMARY KEY,
            last_used TEXT
        )
    """)
    
    # Boshlang'ich statistika qiymatlarini kiritish
    cursor.execute("INSERT OR IGNORE INTO stats (key, value) VALUES ('total_tags', 0)")
    cursor.execute("INSERT OR IGNORE INTO stats (key, value) VALUES ('total_groups', 0)")
    cursor.execute("INSERT OR IGNORE INTO stats (key, value) VALUES ('success_tags', 0)")
    cursor.execute("INSERT OR IGNORE INTO stats (key, value) VALUES ('failed_tags', 0)")
    
    conn.commit()
    conn.close()
    logger.info("Ma'lumotlar bazasi muvaffaqiyatli ishga tushirildi.")

# --- Ma'lumotlar Bazasi Yordamchi Funksiyalari ---

def db_save_account(user_id, phone, session_string, tg_acc_id, tg_name, username):
    conn = get_db_connection()
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT OR REPLACE INTO accounts 
        (telegram_user_id, phone, session_string, telegram_account_id, telegram_name, username, connected_at, last_used)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, phone, session_string, tg_acc_id, tg_name, username, now_str, now_str))
    conn.commit()
    conn.close()

def db_get_account(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM accounts WHERE telegram_user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def db_delete_account(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM accounts WHERE telegram_user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def db_get_all_accounts():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM accounts ORDER BY connected_at DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def db_update_last_used(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("UPDATE accounts SET last_used = ? WHERE telegram_user_id = ?", (now_str, user_id))
    conn.commit()
    conn.close()

def db_add_group(group_id, title):
    conn = get_db_connection()
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("INSERT OR REPLACE INTO groups (group_id, title, added_at) VALUES (?, ?, ?)", (group_id, title, now_str))
    
    # Jami guruhlar sonini hisoblash
    cursor.execute("SELECT COUNT(*) FROM groups")
    count = cursor.fetchone()[0]
    cursor.execute("INSERT OR REPLACE INTO stats (key, value) VALUES ('total_groups', ?)", (count,))
    
    conn.commit()
    conn.close()

def db_increment_stat(key, amount=1):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE stats SET value = value + ? WHERE key = ?", (amount, key))
    conn.commit()
    conn.close()

def db_get_stats():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM stats")
    rows = cursor.fetchall()
    conn.close()
    return {r['key']: r['value'] for r in rows}

def db_set_cooldown(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()
    cursor.execute("INSERT OR REPLACE INTO cooldowns (user_id, last_used) VALUES (?, ?)", (user_id, now_str))
    conn.commit()
    conn.close()

def db_get_cooldown(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT last_used FROM cooldowns WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        try:
            return datetime.fromisoformat(row[0])
        except Exception:
            return None
    return None

# ==========================================
# KLIENT KESHI VA INTEGRATSIYA
# ==========================================

# Xotirada saqlanadigan faol klientlar ro'yxati
clients = {}

def get_client(user_id):
    """Foydalanuvchi uchun Telethon klientini keshdan oladi yoki qaytadan yaratadi (global API_ID va API_HASH dan foydalanadi)."""
    if user_id in clients:
        client = clients[user_id]
        try:
            # Klient faolligini orqa fonda tekshirish
            is_authorized = asyncio.run_coroutine_threadsafe(
                client.is_user_authorized(), loop
            ).result(timeout=5)
            if is_authorized:
                return client
        except Exception:
            pass
        
        # Buzilgan klientni o'chirish va yopish
        try:
            asyncio.run_coroutine_threadsafe(client.disconnect(), loop).result(timeout=5)
        except Exception:
            pass
        clients.pop(user_id, None)

    # Bazadan yangi seans ma'lumotlarini yuklash
    acc = db_get_account(user_id)
    if not acc:
        return None

    # StringSession va global API hisoblari yordamida klient ob'ektini yaratish
    client = TelegramClient(StringSession(acc['session_string']), TG_API_ID, TG_API_HASH)
    
    async def connect_and_validate():
        await client.connect()
        return await client.is_user_authorized()

    try:
        is_authorized = asyncio.run_coroutine_threadsafe(connect_and_validate(), loop).result(timeout=15)
        if is_authorized:
            clients[user_id] = client
            db_update_last_used(user_id)
            return client
    except Exception as e:
        logger.error(f"{user_id} foydalanuvchisi uchun Telethon klienti tiklanmadi: {e}")
        
    return None

# ==========================================
# RO'YXATDAN O'TISH BOSQIChLARI
# ==========================================

login_states = {}
state_lock = threading.Lock()

# ==========================================
# TELEGRAM BOT BUYRUQLARI (pyTelegramBotAPI)
# ==========================================

@bot.message_handler(commands=['start'])
def handle_start(message):
    welcome_text = (
        "🤖 **Ko'p akkountli Tagger Botga xush kelibsiz!** 🚀\n\n"
        "Ushbu bot guruh a'zolarini ommaviy reklama botlari orqali emas, "
        "balki shaxsiy profilingiz orqali xavfsiz va qulay tarzda chaqirish (tag qilish) imkonini beradi!\n\n"
        "📜 **Buyruqlar ro'yxati:**\n"
        "• /addaccount - Shaxsiy Telegram akkountingizni ulash\n"
        "• /myaccount - Ulangan akkount haqida ma'lumotlarni tekshirish\n"
        "• /removeaccount - Ulangan akkount seansini o'chirish va tizimdan chiqish\n"
        "• /tagger [xabar] - Guruh a'zolariga o'z akkountingizdan tag qilish!\n"
        "• /help - Yo'riqnoma va yordam\n\n"
        "💡 *Eslatma: Sizning ma'lumotlaringiz va yaratilgan seans kalitingiz SQLite ma'lumotlar bazasida xavfsiz saqlanadi va uchinchi shaxslarga berilmaydi.*"
    )
    bot.reply_to(message, welcome_text, parse_mode="Markdown")

@bot.message_handler(commands=['help'])
def handle_help(message):
    help_text = (
        "📖 **Tizim qoidalari va cheklovlar**\n\n"
        "1. **Xavfsizlik:** Telethon seansini (StringSession) yaratish uchun biz sizdan faqat telefon raqamingiz va tasdiqlash kodini so'raymiz. "
        "Seans kaliti faqat serverda saqlanadi va faqat tagger buyrug'ini ishlatganingizda ishlaydi.\n"
        "2. **Guruhda tag qilish:** Istalgan guruhga qo'shiling, `/tagger` buyrug'ini va undan keyin xabaringizni yozing. Ulangan shaxsiy akkountingiz a'zolarni chaqirishni boshlaydi.\n"
        "3. **Spamdan himoya:** Akkountlar bloklanishining oldini olish uchun har bir tagger jarayoni orasida **60 soniyalik kutish vaqti (cooldown)** amal qiladi.\n"
        "4. **Admin panel:** Administratorlar faol seanslarni /accounts buyrug'i orqali ko'rishlari mumkin."
    )
    bot.reply_to(message, help_text, parse_mode="Markdown")

# --- AKKOUNT QO'ShISh: /addaccount ---

@bot.message_handler(commands=['addaccount'])
def handle_add_account(message):
    user_id = message.from_user.id
    
    # Akkount bor yoki yo'qligini tekshirish
    acc = db_get_account(user_id)
    if acc:
        bot.reply_to(
            message, 
            "⚠️ Siz allaqachon akkountingizni ulagansiz!\n"
            "Batafsil ma'lumot uchun /myaccount buyrug'idan foydalaning yoki yangisini qo'shishdan oldin hozirgisini /removeaccount orqali o'chiring."
        )
        return

    # Lichkada yozayotganini tekshirish
    if message.chat.type != 'private':
        bot.reply_to(message, "⚠️ Ma'lumotlaringiz xavfsizligi uchun, iltimos, akkountni faqat Shaxsiy Xabarlarda (Lichka) ulang!")
        return

    with state_lock:
        login_states[user_id] = {
            'step': 'WAITING_PHONE',
            'phone': None,
            'client': None,
            'phone_code_hash': None
        }
        
    msg = (
        "📱 **Ko'p akkountli ulanish jarayoni** 📱\n\n"
        "👉 Iltimos, **Telefon raqamingizni** xalqaro formatda yuboring (masalan: `+998901234567`):"
    )
    bot.reply_to(message, msg, parse_mode="Markdown")

# --- INPUT QABUL QILISh ---

@bot.message_handler(func=lambda message: message.from_user.id in login_states)
def handle_login_inputs(message):
    user_id = message.from_user.id
    text = message.text.strip() if message.text else ""
    
    with state_lock:
        state = login_states.get(user_id)
    
    if not state:
        return
        
    step = state['step']
    
    # Bekor qilish buyrug'i
    if text.lower() == '/cancel':
        if state['client']:
            async def close_temp_client(cli):
                try:
                    await cli.disconnect()
                except Exception:
                    pass
            asyncio.run_coroutine_threadsafe(close_temp_client(state['client']), loop)
        with state_lock:
            login_states.pop(user_id, None)
        bot.reply_to(message, "❌ Ro'yxatdan o'tish bekor qilindi.")
        return

    # Kirish jarayonida boshqa buyruqlarni bloklash
    if text.startswith('/') and text.lower() != '/cancel':
        bot.reply_to(
            message, 
            "⚠️ Siz hozir ro'yxatdan o'tish jarayonidasiz!\n"
            "Iltimos, so'ralgan ma'lumotni yuboring yoki jarayonni to'xtatish uchun `/cancel` buyrug'ini yuboring.", 
            parse_mode="Markdown"
        )
        return

    if step == 'WAITING_PHONE':
        if not (text.startswith('+') and text[1:].isdigit() and len(text) >= 8):
            bot.reply_to(
                message, 
                "❌ Noto'g'ri format. Telefon raqami '+' belgisi bilan boshlanishi va to'g'ri xalqaro kodga ega bo'lishi kerak:"
            )
            return
            
        phone = text
        bot.reply_to(message, "⏳ Telegram serverlariga ulanish va tasdiqlash kodini so'rash jarayoni ketmoqda...")
        
        async def initiate_client_session():
            client = TelegramClient(StringSession(), TG_API_ID, TG_API_HASH)
            await client.connect()
            result = await client.send_code_request(phone)
            return client, result.phone_code_hash

        try:
            future = asyncio.run_coroutine_threadsafe(initiate_client_session(), loop)
            client, phone_code_hash = future.result(timeout=45)
            
            with state_lock:
                state['phone'] = phone
                state['client'] = client
                state['phone_code_hash'] = phone_code_hash
                state['step'] = 'WAITING_CODE'
                
            bot.reply_to(
                message, 
                "📩 Kod muvaffaqiyatli yuborildi! Telegram ilovangizga (yoki SMS orqali) kelgan kodni tekshiring.\n\n"
                "👉 Iltimos, **Kirish kodini** yuboring (masalan: `12345`):",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"{user_id} uchun ulanish kodi so'rashda xatolik: {e}", exc_info=True)
            bot.reply_to(message, f"❌ Kodni so'rash muvaffaqiyatsiz yakunlandi: `{e}`\n\nIltimos, telefon raqamingizni qayta yuboring:")
            
    elif step == 'WAITING_CODE':
        code = text.replace(" ", "")
        if not code.isdigit():
            bot.reply_to(message, "❌ Noto'g'ri kod. Iltimos, faqat raqamlarni yuboring:")
            return
            
        bot.reply_to(message, "⏳ Shaxsiy seans tasdiqlanmoqda...")
        
        async def submit_auth_code():
            client = state['client']
            try:
                me = await client.sign_in(phone=state['phone'], code=code, phone_code_hash=state['phone_code_hash'])
                return me, None
            except SessionPasswordNeededError:
                return None, "2FA"
            except Exception as e:
                return None, e

        try:
            future = asyncio.run_coroutine_threadsafe(submit_auth_code(), loop)
            me, result_err = future.result(timeout=45)
            
            if result_err == "2FA":
                with state_lock:
                    state['step'] = 'WAITING_PASSWORD'
                bot.reply_to(message, "🔐 **Ikki bosqichli parol (2FA) yoqilgan.**\n\n👉 Iltimos, **2FA Parolingizni** yuboring:")
                return
                
            elif result_err is not None:
                bot.reply_to(message, f"❌ Tizimga kirish muvaffaqiyatsiz yakunlandi: `{result_err}`\n\nIltimos, tekshirib qaytadan urinib ko'ring:")
                return
                
            # Muvaffaqiyatli kirish (2FA talab qilinmagan holat)
            client = state['client']
            async def get_session_and_details():
                me_obj = await client.get_me()
                session_str = client.session.save()
                await client.disconnect()
                return me_obj, session_str
                
            me, session_str = asyncio.run_coroutine_threadsafe(get_session_and_details(), loop).result(timeout=30)
            
            first_name = me.first_name or ""
            last_name = me.last_name or ""
            full_name = f"{first_name} {last_name}".strip() or "No Name"
            username = me.username or ""
            
            db_save_account(
                user_id=user_id,
                phone=state['phone'],
                session_string=session_str,
                tg_acc_id=me.id,
                tg_name=full_name,
                username=username
            )
            
            with state_lock:
                login_states.pop(user_id, None)
                
            bot.reply_to(
                message, 
                f"🎉 **Ro'yxatdan o'tish muvaffaqiyatli yakunlandi!** 🎉\n\n"
                f"**{full_name}** (@{username}) akkounti tizimga ulandi!\n"
                f"Endi /tagger buyrug'ini yuborganingizda, xabarlar sizning akkountingizdan yuboriladi. 🚀",
                parse_mode="Markdown"
            )
            
        except Exception as e:
            logger.error(f"{user_id} uchun kod tekshirishda xatolik: {e}", exc_info=True)
            bot.reply_to(message, f"❌ Tasdiqlash xatoligi: `{e}`. Kodni qayta yuboring:")
            
    elif step == 'WAITING_PASSWORD':
        password = text
        bot.reply_to(message, "⏳ 2FA paroli tekshirilmoqda...")
        
        async def submit_password_auth():
            client = state['client']
            try:
                me = await client.sign_in(password=password)
                return me, None
            except Exception as e:
                return None, e

        try:
            future = asyncio.run_coroutine_threadsafe(submit_password_auth(), loop)
            me, result_err = future.result(timeout=45)
            
            if result_err is not None:
                bot.reply_to(message, f"❌ Parol noto'g'ri yoki ulanishda xatolik: `{result_err}`\n\nIltimos, parolni qaytadan yuboring:")
                return
                
            # Muvaffaqiyatli kirish (2FA bilan)
            client = state['client']
            async def get_session_details_2fa():
                me_obj = await client.get_me()
                session_str = client.session.save()
                await client.disconnect()
                return me_obj, session_str
                
            me, session_str = asyncio.run_coroutine_threadsafe(get_session_details_2fa(), loop).result(timeout=30)
            
            first_name = me.first_name or ""
            last_name = me.last_name or ""
            full_name = f"{first_name} {last_name}".strip() or "No Name"
            username = me.username or ""
            
            db_save_account(
                user_id=user_id,
                phone=state['phone'],
                session_string=session_str,
                tg_acc_id=me.id,
                tg_name=full_name,
                username=username
            )
            
            with state_lock:
                login_states.pop(user_id, None)
                
            bot.reply_to(
                message, 
                f"🎉 **Ro'yxatdan o'tish muvaffaqiyatli yakunlandi (2FA tasdiqlandi)!** 🎉\n\n"
                f"**{full_name}** (@{username}) akkounti tizimga ulandi!\n"
                f"Guruhlardagi barcha tag xabarlaringiz endi sizning shaxsiy seansingizdan yuboriladi. 🚀",
                parse_mode="Markdown"
            )
            
        except Exception as e:
            logger.error(f"{user_id} uchun 2FA tekshirishda xatolik: {e}", exc_info=True)
            bot.reply_to(message, f"❌ Tizimda 2FA tasdiqlash xatoligi: `{e}`. Parolni qaytadan yuboring:")

# --- AKKOUNT MA'LUMOTLARI: /myaccount ---

@bot.message_handler(commands=['myaccount'])
def handle_my_account(message):
    user_id = message.from_user.id
    acc = db_get_account(user_id)
    if not acc:
        bot.reply_to(message, "❌ Ulangan akkountlar topilmadi. Akkount ulash uchun /addaccount yozing.")
        return
        
    status = "Ulanmagan"
    try:
        client = get_client(user_id)
        if client:
            status = "🟢 Ulangan va Faol"
        else:
            status = "🔴 Seans muddati tugagan"
    except Exception as e:
        status = f"🔴 Ulanishda xatolik ({e})"
        
    username_display = f"@{acc['username']}" if acc['username'] else "Mavjud emas"
    
    msg = (
        f"👤 **Ulangan seans ma'lumotlari**\n\n"
        f"📞 **Telefon:** `{acc['phone']}`\n"
        f"🏷️ **Telegram ism:** {acc['telegram_name']}\n"
        f"🔗 **Foydalanuvchi nomi:** {username_display}\n"
        f"ℹ️ **Holati:** {status}\n"
        f"📅 **Ulangan sana:** {acc['connected_at']}\n"
        f"🕒 **Oxirgi marta ishlatilgan:** {acc['last_used']}\n"
    )
    bot.reply_to(message, msg, parse_mode="Markdown")

# --- AKKOUNTNI O'ChIRISh: /removeaccount ---

@bot.message_handler(commands=['removeaccount'])
def handle_remove_account(message):
    user_id = message.from_user.id
    acc = db_get_account(user_id)
    if not acc:
        bot.reply_to(message, "❌ Foydalanuvchi IDsi bo'yicha hech qanday ma'lumot topilmadi.")
        return
        
    try:
        if user_id in clients:
            client = clients[user_id]
            async def shutdown_session():
                try:
                    await client.log_out()
                except Exception:
                    pass
                try:
                    await client.disconnect()
                except Exception:
                    pass
            asyncio.run_coroutine_threadsafe(shutdown_session(), loop).result(timeout=10)
            del clients[user_id]
    except Exception as e:
        logger.error(f"{user_id} uchun Telethon seansini yopishda xatolik: {e}")
        
    db_delete_account(user_id)
    bot.reply_to(message, "✅ Sizning profilingiz SQLite ma'lumotlar bazasidan muvaffaqiyatli o'chirildi va tizimdan chiqildi.")

# --- ADMIN PANEL: /accounts ---

@bot.message_handler(commands=['accounts'])
def handle_accounts(message):
    user_id = message.from_user.id
    if ADMIN_ID != 0 and user_id != ADMIN_ID:
        bot.reply_to(message, "🔒 Bu buyruq faqat tizim administratorlari uchun ruxsat etilgan.")
        return
        
    accounts = db_get_all_accounts()
    total = len(accounts)
    
    recent_list = []
    for acc in accounts[:5]:
        username_display = f"@{acc['username']}" if acc['username'] else "Foydalanuvchi nomi yo'q"
        recent_list.append(f"• {acc['telegram_name']} ({username_display}) - `{acc['phone']}`")
        
    recent_str = "\n".join(recent_list) if recent_list else "Yaqinda ulanganlar yo'q"
    online_count = len(clients)
    
    msg = (
        f"📊 **Tizim holati - Foydalanuvchilar sozlamalari**\n\n"
        f"👥 **Bazadagi jami profillar:** {total}\n"
        f"⚡ **Faol keshdagi ulanishlar:** {online_count}\n\n"
        f"🕒 **Yaqinda qo'shilgan profillar:**\n{recent_str}"
    )
    bot.reply_to(message, msg, parse_mode="Markdown")

# ==========================================
# ASYNC TAGGER RUNNER VA CHUNKING TIZIMI
# ==========================================

async def async_run_tagger(user_id, chat_id, custom_message=""):
    """Foydalanuvchi seansini yuklaydi va a'zolarni 5 tadan bo'lib tag qiladi (global API_ID va API_HASH dan foydalanadi)."""
    acc = db_get_account(user_id)
    if not acc:
        return "NO_ACCOUNT", None
        
    # Keshdagi klientni tekshirish yoki yangitdan olish
    client = None
    if user_id in clients:
        client = clients[user_id]
        if not await client.is_user_authorized():
            try:
                await client.disconnect()
            except Exception:
                pass
            client = None
            
    if not client:
        client = TelegramClient(StringSession(acc['session_string']), TG_API_ID, TG_API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            return "EXPIRED", None
        clients[user_id] = client
        db_update_last_used(user_id)
        
    # Guruh obyektini olish
    try:
        entity = await client.get_entity(chat_id)
    except Exception as e:
        logger.error(f"{user_id} akkounti orqali {chat_id} guruhini aniqlashda xatolik: {e}")
        return "CHAT_NOT_FOUND", str(e)
        
    # Guruh a'zolarini yig'ish
    participants = []
    try:
        async for user in client.iter_participants(entity):
            if not user.bot and not user.deleted:
                participants.append(user)
    except Exception as e:
        logger.error(f"{chat_id} guruh a'zolarini yuklashda xatolik: {e}")
        return "FETCH_FAILED", str(e)
        
    if not participants:
        return "NO_PARTICIPANTS", None
        
    # A'zolarni 5 tadan guruhlash (chunking)
    chunk_size = 5
    chunks = [participants[i:i + chunk_size] for i in range(0, len(participants), chunk_size)]
    
    # Qiziqarli va hazilomuz chaqiriq sarlavhalari
    funny_headers = [
        "Hamma diqqat qilsin! Quyida muhim xabar bor:",
        "Uyg'oning, uyquchilar! ⏰",
        "Sizni bu yerda kutishyapti, tezroq keling:",
        "Guruhdagilar diqqatiga! 🚨",
        "A'zolarni yig'moqdamiz... ⚡",
        "Diqqat qiling! 📣"
    ]
    
    summon_prefix = custom_message if custom_message else random.choice(funny_headers)
    
    for idx, chunk in enumerate(chunks):
        mentions = []
        for u in chunk:
            name = u.first_name or ""
            if u.last_name:
                name += f" {u.last_name}"
            name = name.strip() or "Foydalanuvchi"
            # Markdown orqali har bir a'zoni havolali tag qilish
            mentions.append(f"[{name}](tg://user?id={u.id})")
            
        tag_payload = f"📣 **{summon_prefix}** ({idx+1}/{len(chunks)}-qism)\n\n" + ", ".join(mentions)
        
        try:
            # Shaxsiy akkount nomidan guruhga xabar yuborish
            await client.send_message(entity, tag_payload, link_preview=False)
            db_increment_stat('success_tags', len(chunk))
            db_increment_stat('total_tags', len(chunk))
            # Bloklanishga qarshi xavfsiz interval
            await asyncio.sleep(2.5)
        except FloodWaitError as fwe:
            logger.warning(f"FloodWait cheklovi yuz berdi! {fwe.seconds} soniya kutilmoqda.")
            await asyncio.sleep(fwe.seconds + 1)
            try:
                await client.send_message(entity, tag_payload, link_preview=False)
                db_increment_stat('success_tags', len(chunk))
                db_increment_stat('total_tags', len(chunk))
            except Exception as re_err:
                logger.error(f"Kutishdan keyin ham yuborib bo'lmadi: {re_err}")
                db_increment_stat('failed_tags', len(chunk))
        except Exception as e:
            logger.error(f"Guruhdan tag xabari yuborishda muammo: {e}")
            db_increment_stat('failed_tags', len(chunk))
            
    return "SUCCESS", len(participants)

# --- TAGGER BUYRUG'I: /tagger ---

@bot.message_handler(commands=['tagger', 'all', 'tagall'])
def handle_tagger_command(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Faqat guruhlarda ishlashni ta'minlash
    if message.chat.type not in ['group', 'supergroup']:
        bot.reply_to(message, "❌ Guruh a'zolarini chaqirish buyrug'i faqat guruhlarda ishlaydi!")
        return
        
    # Guruh ma'lumotlarini saqlash
    db_add_group(chat_id, message.chat.title)
    
    # Cooldown (kutish) tekshiruvi
    last_used = db_get_cooldown(user_id)
    if last_used:
        elapsed_sec = (datetime.now() - last_used).total_seconds()
        if elapsed_sec < COOLDOWN_SECONDS:
            remaining = int(COOLDOWN_SECONDS - elapsed_sec)
            funny_cooldowns = [
                f"Sabr qiling! ⏳ Qayta tag qilish uchun yana {remaining} soniya kuting.",
                f"Barmoqlaringiz juda tez yozmoqda! 🏎️ Kutish vaqti: {remaining} soniya.",
                f"Guruhga biroz dam bering! {remaining} soniya kuting.",
                f"Xavfsizlik protokollari tufayli buyruq {remaining} soniyaga cheklangan. 🤖"
            ]
            bot.reply_to(message, random.choice(funny_cooldowns))
            return
            
    # Ulangan akkount borligini tekshirish
    acc = db_get_account(user_id)
    if not acc:
        bot.reply_to(
            message, 
            "⚠️ **Ulangan Telegram akkounti topilmadi!**\n\n"
            "Bu bot ko'p akkountli tizimda ishlaydi, ya'ni tag xabarlari shaxsiy akkountingizdan yuboriladi. "
            "Iltimos, menga shaxsiy xabarlarda `/addaccount` yozib ulaning.",
            parse_mode="Markdown"
        )
        return

    # Maxsus xabar matnini ajratib olish
    split_cmd = message.text.split(maxsplit=1)
    custom_message = split_cmd[1] if len(split_cmd) > 1 else ""
    
    progress_indicator = bot.reply_to(message, "⏳ Akkount ulanmoqda va partiyalar shakllantirilmoqda... Iltimos, kuting.")
    
    # Kutish vaqtini yangilash
    db_set_cooldown(user_id)

    def tagger_background_worker():
        try:
            future = asyncio.run_coroutine_threadsafe(
                async_run_tagger(user_id, chat_id, custom_message),
                loop
            )
            result, extra = future.result(timeout=600)
            
            if result == "SUCCESS":
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_indicator.message_id,
                    text=f"✅ **Tag jarayoni yakunlandi!**\nSizning shaxsiy akkountingiz ({acc['telegram_name']}) orqali a'zolarga muvaffaqiyatli tag qilindi. ✨",
                    parse_mode="Markdown"
                )
            elif result == "NO_ACCOUNT":
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_indicator.message_id,
                    text="❌ Ulangan profilingiz ma'lumotlar bazasidan topilmadi. Ishga tushirish uchun /addaccount yozing."
                )
            elif result == "EXPIRED":
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_indicator.message_id,
                    text="⚠️ Sizning akkount seansingiz muddati tugagan. Iltimos, avval /removeaccount yozib, keyin /addaccount orqali qayta ulaning."
                )
            elif result == "CHAT_NOT_FOUND":
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_indicator.message_id,
                    text=f"❌ Userbot guruhni topa olmadi. Shaxsiy akkountingiz ushbu guruh a'zosi ekanligiga ishonch hosil qiling. Xatolik: `{extra}`",
                    parse_mode="Markdown"
                )
            elif result == "FETCH_FAILED":
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_indicator.message_id,
                    text=f"❌ Guruh a'zolarini yig'ishda xatolik yuz berdi: `{extra}`",
                    parse_mode="Markdown"
                )
            elif result == "NO_PARTICIPANTS":
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=progress_indicator.message_id,
                    text="❌ Guruhda tag qilish uchun mos keladigan a'zolar topilmadi."
                )
        except Exception as e:
            logger.error(f"Orqa fondagi tag jarayonida kutilmagan xatolik: {e}", exc_info=True)
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_indicator.message_id,
                text=f"❌ Guruh a'zolarini chaqirishda kutilmagan xatolik yuz berdi: `{e}`",
                parse_mode="Markdown"
            )

    # Botning asosiy oqimini band qilmaslik uchun alohida oqimga yuklash
    threading.Thread(target=tagger_background_worker, daemon=True).start()

# ==========================================
# FLASK INTERFEYSI (UZBEK TILIDA DASHBOARD)
# ==========================================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="uz">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ko'p akkountli Telegram Tagger Boshqaruv Paneli</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="bg-gray-900 text-gray-100 font-sans">
    <div class="min-h-screen flex flex-col">
        <!-- Header -->
        <header class="bg-gray-800 shadow-md py-4 px-6 flex justify-between items-center border-b border-gray-700">
            <div class="flex items-center space-x-3">
                <i class="fa-solid fa-tags text-teal-400 text-2xl animate-pulse"></i>
                <h1 class="text-xl font-bold tracking-wider text-teal-300">Tagger Boshqaruv Paneli</h1>
            </div>
            <div class="flex items-center space-x-2">
                <span class="inline-flex h-3 w-3 rounded-full bg-green-500"></span>
                <span class="text-sm font-semibold text-green-400">Tizim faol</span>
            </div>
        </header>

        <!-- Asosiy qism -->
        <main class="flex-1 p-6 max-w-7xl mx-auto w-full">
            <!-- Statistika kartalari -->
            <div class="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
                <div class="bg-gray-800 p-5 rounded-lg border border-gray-700 shadow-md hover:border-teal-500 transition-all">
                    <div class="flex justify-between items-center mb-3">
                        <span class="text-gray-400 font-medium">Jami akkountlar</span>
                        <i class="fa-solid fa-users text-teal-400 text-xl"></i>
                    </div>
                    <span class="text-3xl font-extrabold text-white">{{ stats.total_accounts }}</span>
                </div>
                <div class="bg-gray-800 p-5 rounded-lg border border-gray-700 shadow-md hover:border-teal-500 transition-all">
                    <div class="flex justify-between items-center mb-3">
                        <span class="text-gray-400 font-medium">Faol keshda</span>
                        <i class="fa-solid fa-memory text-amber-400 text-xl"></i>
                    </div>
                    <span class="text-3xl font-extrabold text-white">{{ stats.active_sessions }}</span>
                </div>
                <div class="bg-gray-800 p-5 rounded-lg border border-gray-700 shadow-md hover:border-teal-500 transition-all">
                    <div class="flex justify-between items-center mb-3">
                        <span class="text-gray-400 font-medium">Muvaffaqiyatli taglar</span>
                        <i class="fa-solid fa-circle-check text-emerald-400 text-xl"></i>
                    </div>
                    <span class="text-3xl font-extrabold text-white">{{ stats.success_tags }}</span>
                </div>
                <div class="bg-gray-800 p-5 rounded-lg border border-gray-700 shadow-md hover:border-teal-500 transition-all">
                    <div class="flex justify-between items-center mb-3">
                        <span class="text-gray-400 font-medium">Kuzatilayotgan guruhlar</span>
                        <i class="fa-solid fa-network-wired text-indigo-400 text-xl"></i>
                    </div>
                    <span class="text-3xl font-extrabold text-white">{{ stats.total_groups }}</span>
                </div>
            </div>

            <!-- Jadvallar va Loglar -->
            <div class="grid grid-cols-1 lg:grid-cols-3 gap-8">
                <!-- Ulangan akkountlar ro'yxati -->
                <div class="lg:col-span-2 bg-gray-800 rounded-lg border border-gray-700 shadow-md p-6">
                    <h2 class="text-lg font-bold text-teal-300 mb-4 flex items-center">
                        <i class="fa-solid fa-users-viewfinder mr-2"></i> Ulangan Akkountlar Ro'yxati
                    </h2>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left border-collapse">
                            <thead>
                                <tr class="border-b border-gray-700 text-gray-400 text-sm">
                                    <th class="py-3 px-4 font-semibold">Foydalanuvchi</th>
                                    <th class="py-3 px-4 font-semibold">Telefon</th>
                                    <th class="py-3 px-4 font-semibold">Foydalanuvchi nomi</th>
                                    <th class="py-3 px-4 font-semibold">Ulangan sana</th>
                                    <th class="py-3 px-4 font-semibold">Oxirgi marta ishlatilgan</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for acc in accounts %}
                                <tr class="border-b border-gray-700/50 hover:bg-gray-700/20 text-sm transition-all">
                                    <td class="py-3 px-4 font-semibold text-white">{{ acc.telegram_name }}</td>
                                    <td class="py-3 px-4 text-gray-300 font-mono">{{ acc.phone }}</td>
                                    <td class="py-3 px-4 text-teal-400">@{{ acc.username if acc.username else "Mavjud emas" }}</td>
                                    <td class="py-3 px-4 text-gray-400">{{ acc.connected_at }}</td>
                                    <td class="py-3 px-4 text-gray-400">{{ acc.last_used }}</td>
                                </tr>
                                {% else %}
                                <tr>
                                    <td colspan="5" class="py-8 text-center text-gray-500 font-medium">Hozircha akkountlar ulanmagan.</td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </div>

                <!-- Loglar paneli -->
                <div class="bg-gray-800 rounded-lg border border-gray-700 shadow-md p-6 flex flex-col justify-between">
                    <div>
                        <h2 class="text-lg font-bold text-amber-400 mb-4 flex items-center">
                            <i class="fa-solid fa-terminal mr-2"></i> Tizim Loglari
                        </h2>
                        <div class="bg-black/40 p-4 rounded-md font-mono text-xs text-green-400 h-64 overflow-y-auto space-y-2 border border-gray-700">
                            <div>[TIZIM] Tizim ishga tushdi, xizmatlar tayyor.</div>
                            <div>[TIZIM] SQLite ma'lumotlar bazasi xavfsiz holatda.</div>
                            <div>[TIZIM] Server muvaffaqiyatli yuklandi.</div>
                            {% if accounts %}
                            <div>[INFO] Bazada ulangan profillar aniqlandi.</div>
                            {% endif %}
                        </div>
                    </div>
                    <div class="mt-6 p-4 bg-teal-900/20 border border-teal-500/30 rounded-lg text-sm">
                        <p class="text-teal-300 font-semibold mb-1"><i class="fa-solid fa-quote-left mr-1"></i> Dasturchi tavsiyasi</p>
                        <p class="text-gray-300 text-xs">Ushbu boshqaruv paneli Render tizimida yagona SQLite ma'lumotlar bazasida ishlaydi! Ma'lumotlarni saqlab qolish uchun Render disklardan foydalanish tavsiya etiladi.</p>
                    </div>
                </div>
            </div>
        </main>

        <!-- Footer -->
        <footer class="bg-gray-800 border-t border-gray-700 py-4 px-6 text-center text-sm text-gray-500 mt-auto">
            &copy; 2026 Ko'p akkountli Tagger Bot. Keng miqyosda foydalaning.
        </footer>
    </div>
</body>
</html>
"""

@app.route('/')
def dashboard():
    accounts = db_get_all_accounts()
    sys_stats = db_get_stats()
    stats = {
        'total_accounts': len(accounts),
        'active_sessions': len(clients),
        'success_tags': sys_stats.get('success_tags', 0),
        'total_groups': sys_stats.get('total_groups', 0)
    }
    return render_template_string(DASHBOARD_HTML, accounts=accounts, stats=stats)

@app.route('/health')
def health():
    accounts = db_get_all_accounts()
    sys_stats = db_get_stats()
    return jsonify({
        'status': 'healthy',
        'connected_accounts': len(accounts),
        'active_cached_sessions': len(clients),
        'statistics': sys_stats,
        'timestamp': datetime.now().isoformat()
    })

# ==========================================
# TIZIMNI ISHGA TUSHIRISh
# ==========================================

if __name__ == '__main__':
    # SQLite ma'lumotlar bazasini tekshirish/ishga tushirish
    init_db()

    # Flask veb serverini alohida oqimda boshlash
    def run_flask():
        app.run(host="0.0.0.0", port=PORT, use_reloader=False)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Flask veb paneli {PORT}-portda faol")

    # Botni ishga tushirish (bloklovchi oqim)
    logger.info("pyTelegramBotAPI faol...")
    bot.infinity_polling()
