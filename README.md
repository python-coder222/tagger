# 🏷️ Taggerchi Bot

Guruhlarda `/tagger` buyrug'i orqali barcha a'zolarni chaqiradigan Telegram bot,
o'zining Flask dashboardi bilan birga — hammasi bitta `app.py` faylida.

![Taggerchi](taggerchi_avatar.png)

---

## 📋 Mundarija

- [Imkoniyatlar](#-imkoniyatlar)
- [Qanday ishlaydi](#-qanday-ishlaydi)
- [Muhim cheklov](#️-muhim-cheklov)
- [O'rnatish](#-ornatish)
- [BotFather sozlamalari](#-botfather-sozlamalari)
- [Mavjud guruh a'zolarini import qilish](#-mavjud-guruh-azolarini-import-qilish)
- [Environment o'zgaruvchilar](#️-environment-ozgaruvchilar)
- [Dashboard](#-dashboard)
- [Deploy qilish](#-deploy-qilish)
- [Fayllar tuzilishi](#-fayllar-tuzilishi)

---

## ✨ Imkoniyatlar

- `/tagger` — guruhdagi barcha tanish userlarni chaqiradi
  - Kam odamli guruhda **har biriga alohida xabar**, tasodifiy qiziqarli gap bilan 😄
  - Katta guruhda flood-limitga tushmaslik uchun avtomatik 5 tadan guruhlanadi
- `/tagger <matn>` — chaqiruv bilan birga o'zingiz yozgan matnni ham yuboradi (5 tadan guruhlab)
- `/start` — shaxsiy chatda chiroyli salomlashuv + inline tugmalar (guruhga qo'shish, yordam)
- Bot guruhga qo'shilganda avtomatik salomlashadi va admin bo'lishni so'raydi
- Admin qilib tayinlanganda "ishlashga tayyorman" deb alohida xabar beradi
- **Spamdan himoya** — cooldown mexanizmi (ketma-ket tez-tez ishlatib bo'lmaydi)
- **Flood-limit himoyasi** — Telegram cheklov qo'ysa, avtomatik kutib qayta uradi
- **Kod/maxsus belgilarni qo'llab-quvvatlaydi** — `/tagger` matnida `<`, `>`, `&` kabi belgilar bo'lsa ham xato bermaydi
- Bitta sahifali **Flask dashboard** — qaysi guruhlarda bot bor, qaysilari faol/nofaol, nechta user tanilgan, oxirgi `/tagger` chaqiruvlari

---

## ⚙️ Qanday ishlaydi

Bot guruhdagi xabarlarni kuzatib boradi va har bir yozgan userni o'z bazasiga
saqlab boradi. `/tagger` chaqirilganda o'sha bazadagi userlarni mention qiladi.

---

## ⚠️ Muhim cheklov

Telegram Bot API **hech qanday botga** guruhning to'liq a'zolar ro'yxatini
bermaydi — bu Telegram tomonidan maxfiylik siyosati asosida taqiqlangan va
hech qanday kod bilan aylanib o'tib bo'lmaydi. Shu sabab:

- Bot faqat guruhda **xabar yozgan** userlarni "tanийди" va tag qiladi
- Agar hozirdan boshlab ishlatmoqchi bo'lsangiz, guruh a'zolari bir marta
  yozguncha kutishingiz kerak
- Yoki — guruhda **allaqachon bor** a'zolarni darhol import qilish uchun
  quyidagi [`sync_members.py`](#-mavjud-guruh-azolarini-import-qilish) skriptidan
  foydalaning (shaxsiy akkountingiz orqali ishlaydi, chunki faqat shu yo'l
  bilan to'liq ro'yxatni olish mumkin)

---

## 🚀 O'rnatish

```bash
git clone <repo-url>
cd taggerchi
pip install -r requirements.txt
```

`.env` faylini yarating (`.env.example`dan nusxa oling):

```bash
cp .env.example .env
```

`.env` ichiga tokeningizni kiriting:

```env
BOT_TOKEN=123456789:AA...botfather_dan_olingan_token
DASHBOARD_USER=admin
DASHBOARD_PASS=kuchli_parol
```

Ishga tushiring:

```bash
python app.py
```

Bot polling rejimida ishga tushadi, Flask dashboard esa `http://localhost:5000`
manzilida ochiladi (login/parol so'raydi).

---

## 🤖 BotFather sozlamalari

@BotFather orqali botingizni sozlashda **[`botfather_matnlari.txt`](botfather_matnlari.txt)**
faylidagi tayyor matnlardan foydalaning (nom, tavsif, bio, buyruqlar ro'yxati).

Ikkita qadam **shart**:

1. **Group Privacy — o'chirilgan bo'lishi kerak**
   `@BotFather → Bot Settings → Group Privacy → Turn off`
   Aks holda bot guruh xabarlarini umuman ko'rmaydi va hech kimni tag qila olmaydi.

2. **Group qo'shish — yoqilgan bo'lishi kerak**
   `@BotFather → Bot Settings → Allow Groups? → Enable`

Profil rasmi uchun `taggerchi_avatar.png` faylini yuklang.

---

## 👥 Mavjud guruh a'zolarini import qilish

Agar guruhda odamlar hali yozmagan bo'lsa ham, **hozir turgan barcha a'zolarni**
darhol bazaga qo'shish mumkin — `sync_members.py` skripti orqali (shaxsiy
Telegram akkountingiz yordamida, Telethon asosida):

```bash
pip install telethon python-dotenv
```

`.env` fayliga qo'shing (https://my.telegram.org dan olinadi):

```env
TG_API_ID=123456
TG_API_HASH=sizning_hash_kodingiz
TG_PHONE=+998901234567
```

Ishga tushiring:

```bash
python sync_members.py
```

Birinchi marta Telegram tasdiqlash kodi so'raydi (SMS/ilova orqali), keyin
guruhlar ro'yxatidan kerakli guruhni tanlaysiz — barcha a'zolar avtomatik
bazaga yoziladi.

---

## 🛠️ Environment o'zgaruvchilar

| O'zgaruvchi | Tavsif | Standart qiymat |
|---|---|---|
| `BOT_TOKEN` | @BotFather'dan olingan token | — (majburiy) |
| `DB_PATH` | SQLite baza fayli yo'li | `taggerchi.db` |
| `DASHBOARD_USER` | Dashboard login | `admin` |
| `DASHBOARD_PASS` | Dashboard parol | `admin123` |
| `PORT` | Flask porti | `5000` |
| `CHUNK_SIZE` | Bir xabarda nechta user chaqirilishi (guruh rejimida) | `5` |
| `TAGGER_COOLDOWN_SECONDS` | `/tagger` orasidagi minimal kutish vaqti | `20` |
| `MAX_INDIVIDUAL_TAG` | Shundan ko'p user bo'lsa, guruhlab yuborishga o'tadi | `40` |
| `INDIVIDUAL_SEND_DELAY` | Individual xabarlar orasidagi pauza (soniya) | `0.35` |
| `CHUNK_SEND_DELAY` | Guruh xabarlari orasidagi pauza (soniya) | `0.5` |
| `TG_API_ID` / `TG_API_HASH` / `TG_PHONE` | `sync_members.py` uchun (Telethon) | — |

---

## 📊 Dashboard

`/` manzilida (Basic Auth bilan himoyalangan) quyidagilar ko'rinadi:

- Jami / faol / nofaol guruhlar soni
- Tanilgan (bazadagi) userlar soni
- Har bir guruh bo'yicha: nomi, chat ID, tanilgan userlar soni, oxirgi faollik, status
- Oxirgi 20 ta `/tagger` chaqiruvi logi (kim, qachon, nechta user)

30 soniyada avtomatik yangilanadi.

---

## ☁️ Deploy qilish

### Render.com

1. Repo'ni GitHub'ga joylang
2. Render → New → Web Service → repo tanlang
3. Build command: `pip install -r requirements.txt`
4. Start command: `python app.py`
5. Environment Variables bo'limiga barcha `.env` qiymatlarini kiriting
6. Deploy

> ⚠️ Free planda disk doimiy emas — har deploy'da `taggerchi.db` tozalanishi
> mumkin. Muhim bo'lsa, Persistent Disk yoqing yoki bazani Postgres'ga
> (masalan Neon) ko'chiring.

### Koyeb

`requirements.txt` va `Procfile` avtomatik taniladi (Python buildpack).
Start command: `python app.py`.

---

## 📁 Fayllar tuzilishi

```
taggerchi/
├── app.py                   # Bot + baza + Flask dashboard — hammasi shu yerda
├── sync_members.py          # Mavjud guruh a'zolarini import qilish (Telethon)
├── requirements.txt
├── Procfile
├── .env.example
├── taggerchi_avatar.png     # Bot profil rasmi
├── botfather_matnlari.txt   # @BotFather uchun tayyor matnlar
└── README.md
```

---

## 🔐 Xavfsizlik eslatmasi

- `.env` faylini **hech qachon** git'ga qo'shmang (`.gitignore`ga qo'shing)
- `BOT_TOKEN`, `TG_API_HASH`, `DASHBOARD_PASS` kabi maxfiy ma'lumotlarni
  hech kimga, hech qanday chatga (jumladan AI suhbatlariga ham) ochiq
  yubormang — kerak bo'lsa qayta generatsiya qiling (`revoke`)

---

**MindStudio** © 2026
