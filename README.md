# Sotuvchi Card PRO

Python Telegram bot MVP. Deploy uchun tayyor: Render Web Service + Telegram webhook.

## Funksiyalar
- Uzum Market, Wildberries, Yandex Market tanlash
- Mahsulot nomi va sotuvchi bergan xususiyatlardan SEO sarlavha/tavsif
- Kalit so‘zlar va hashtaglar
- Pillow yordamida PNG infografika; mahsulot rasmi ixtiyoriy
- Har foydalanuvchiga 3 ta bepul karta
- Admin: `/stats`, `/addquota USER_ID COUNT`

## Cheklovlar
- Bu versiya AI emas: SEO matn qoidaviy shablonlar orqali tuziladi.
- Marketplace'ga avtomatik joylamaydi.
- To‘lov qabul qilmaydi; admin qo‘shimcha limitni qo‘lda beradi.
- Render Free fayl tizimi doimiy emas. SQLite bazasi restart, spin-down yoki redeploy vaqtida yo‘qolishi mumkin. Haqiqiy mijozlar va to‘lovlarni boshlashdan oldin doimiy tashqi ma’lumotlar bazasiga o‘ting.
- Render Free web-service 15 daqiqa kiruvchi trafik bo‘lmasa uxlaydi; qayta uyg‘onishi taxminan bir daqiqa olishi mumkin.

## Render deploy
1. GitHub'da yangi repository oching va ushbu fayllarni repository root'iga yuklang.
2. Render -> New + -> Web Service -> GitHub repository'ni ulang.
3. Free plan tanlang.
4. Build command: `pip install -r requirements.txt`
5. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. Environment variables qo‘shing:
   - `BOT_TOKEN`: @BotFather token
   - `WEBHOOK_SECRET`: kamida 32 belgili tasodifiy matn; faqat harf, raqam, `_` va `-`
   - `ADMIN_ID`: Telegram numeric user ID
7. Deploy tugashini kuting. `RENDER_EXTERNAL_URL` Render tomonidan avtomatik beriladi.
8. Render service URL'ni oching. `{"status":"ok","service":"Sotuvchi Card PRO"}` javobi chiqishi kerak.
9. Telegram botga `/start` yuboring.

## Xavfsizlik
BOT_TOKEN va WEBHOOK_SECRET'ni GitHub kodiga yozmang. Faqat Render -> Environment bo‘limiga kiriting.
