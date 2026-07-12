"""
Bir martalik skript: shaxsiy Telegram akkauntingizga kirib, Taggerchi bot
doimiy foydalanishi uchun SESSION STRING generatsiya qiladi.

Buni faqat 1 marta, o'zingizning lokal kompyuteringizda (terminalda) ishga
tushiring — server logida yoki kod ichida telefon raqam/kod saqlanmaydi.

    pip install telethon
    python telethon_login.py

So'ralganda:
  1) TG_API_ID va TG_API_HASH -> https://my.telegram.org/apps dan oling
  2) Telefon raqamingiz (+998...)
  3) Telegram/SMS orqali kelgan kod
  4) Agar 2 bosqichli tasdiqlash (parol) yoqilgan bo'lsa — parolni ham so'raydi

Oxirida chiqadigan SESSION STRING'ni .env faylidagi TG_SESSION_STRING ga
qo'ying. Bu qator akkauntingizga TO'LIQ kirish huquqi beradi — hech kimga
yubormang, hech qayerga (masalan ochiq GitHub repo) joylamang.
"""

import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(input("TG_API_ID (my.telegram.org/apps dan): ").strip())
API_HASH = input("TG_API_HASH (my.telegram.org/apps dan): ").strip()


async def main():
    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        session_string = client.session.save()
        me = await client.get_me()
        display = f"{me.first_name or ''} (@{me.username})" if me.username else (me.first_name or "")
        print(f"\n✅ Muvaffaqiyatli login qilindi: {display}")
        print("\n=== SESSION STRING (buni .env faylida TG_SESSION_STRING= ga qo'ying) ===\n")
        print(session_string)
        print("\n⚠️  Bu stringni hech kimga bermang va hech qayerga oshkora joylamang!\n")


if __name__ == "__main__":
    asyncio.run(main())
