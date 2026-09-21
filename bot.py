import os
import time
import sqlite3
import threading
import asyncio
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
import telebot
from telebot import types
from openai import OpenAI
import edge_tts

# =====================================================================
# 1. ВЕБ-СЕРВЕР ДЛЯ ПОДДЕРЖАНИЯ СТАТУСА "LIVE" НА RENDER
# =====================================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        return

def run_background_webserver():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

threading.Thread(target=run_background_webserver, daemon=True).start()

# =====================================================================
# 2. НАСТРОЙКИ, КЛЮЧИ И АДМИНИСТРАТОРЫ
# =====================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
PROXYAPI_KEY = os.environ.get("PROXYAPI_KEY")
CREATOMATE_API_KEY = os.environ.get("CREATOMATE_API_KEY")
CREATOMATE_TEMPLATE_ID = os.environ.get("CREATOMATE_TEMPLATE_ID")

# ⚠️ ВСТАВЬТЕ СЮДА ВАШ TELEGRAM ID И ID ВЛАДЕЛЬЦА (узнать в @userinfobot)
ADMIN_IDS = [8725167633, 1368485826]

PACKAGE_PRICE_STARS = 50  # Стоимость пакета: 50 звёзд
PACKAGE_CREDITS = 20      # Начисление генераций за покупку

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = OpenAI(
    api_key=PROXYAPI_KEY,
    base_url="https://api.proxyapi.ru/v1"
)

# Оперативные состояния диалогов
user_state = {}
user_media_data = {}

# =====================================================================
# 3. БАЗА ДАННЫХ SQLITE (Балансы и Платежи)
# =====================================================================
def init_db():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            credits INTEGER DEFAULT 2
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            amount INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def get_user_credits(user_id, username=""):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    if row is None:
        start_credits = 10 if user_id in ADMIN_IDS else 2
        safe_username = username if username else "no_username"
        cursor.execute("INSERT INTO users (user_id, username, credits) VALUES (?, ?, ?)", 
                       (user_id, safe_username, start_credits))
        conn.commit()
        credits = start_credits
    else:
        credits = row[0]
        if user_id in ADMIN_IDS and credits < 10:
            cursor.execute("UPDATE users SET credits = 10 WHERE user_id = ?", (user_id,))
            conn.commit()
            credits = 10
            
    conn.close()
    return credits

def update_credits(user_id, count):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (count, user_id))
    conn.commit()
    conn.close()

def log_payment(user_id, username, amount):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO payments (user_id, username, amount) VALUES (?, ?, ?)", 
                   (user_id, username, amount))
    conn.commit()
    conn.close()

# =====================================================================
# 4. ГЕНЕРАЦИЯ ГОЛОСА ЧЕРЕЗ EDGE-TTS
# =====================================================================
async def generate_voice_file(text, output_path):
    communicate = edge_tts.Communicate(text, "ru-RU-DmitryNeural")
    await communicate.save(output_path)

# =====================================================================
# 5. МЕНЮ И КЛАВИАТУРЫ
# =====================================================================
def get_main_keyboard(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    credits_left = get_user_credits(user_id)
    
    btn_video = types.InlineKeyboardButton("🎬 Создать видео из фото", callback_data="act_create_video")
    btn1 = types.InlineKeyboardButton("🔥 Факты / Топы", callback_data="genre_facts")
    btn2 = types.InlineKeyboardButton("💡 Экспертный / Польза", callback_data="genre_expert")
    btn3 = types.InlineKeyboardButton("😱 Мистика / Истории", callback_data="genre_story")
    btn4 = types.InlineKeyboardButton("💰 Деньги / Бизнес", callback_data="genre_business")
    btn_buy = types.InlineKeyboardButton(f"⭐ Купить 20 генераций (Баланс: {credits_left})", callback_data="buy_credits")
    btn_rules = types.InlineKeyboardButton("📄 Правила использования", url="https://telegra.ph")
    
    markup.add(btn_video)
    markup.add(btn1, btn2)
    markup.add(btn3, btn4)
    markup.add(btn_buy)
    markup.add(btn_rules)
    return markup

# =====================================================================
# 6. АДМИН-КОМАНДЫ (ДЛЯ ВАС И ВЛАДЕЛЬЦА)
# =====================================================================
@bot.message_handler(commands=['stats', 'admin'])
def handle_admin_stats(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.reply_to(message, "⛔ У вас нет доступа к этой команде.")
        return
    
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(DISTINCT user_id), COUNT(*), SUM(amount) FROM payments")
    row = cursor.fetchone()
    paying_users = row[0] or 0
    total_payments = row[1] or 0
    total_revenue_stars = row[2] or 0
    conn.close()
    
    stats_text = (
        "📊 **ФИНАНСОВЫЙ И ТЕХНИЧЕСКИЙ ОТЧЕТ**\n\n"
        f"👥 Всего пользователей: **{total_users} чел.**\n"
        f"💳 Платящих клиентов: **{paying_users} чел.**\n"
        f"🛍 Успешных покупок: **{total_payments} шт.**\n\n"
        f"⭐ **СУММАРНАЯ ВЫРУЧКА:** **{total_revenue_stars} Stars**\n\n"
        "*(Отчет виден только разработчику и владельцу)*"
    )
    bot.send_message(message.chat.id, stats_text, parse_mode="Markdown")

@bot.message_handler(commands=['add'])
def handle_add_credits(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    parts = message.text.split()
    amount = 10
    if len(parts) > 1 and parts[1].isdigit():
        amount = int(parts[1])
        
    update_credits(message.from_user.id, amount)
    new_balance = get_user_credits(message.from_user.id)
    bot.reply_to(message, f"👑 **Начислено +{amount} попыток!**\nТекущий баланс: **{new_balance}**.", parse_mode="Markdown")

# =====================================================================
# 7. КОМАНДА /START ДЛЯ ВСЕХ
# =====================================================================
@bot.message_handler(commands=['start'])
def handle_start(message):
    try:
        user_id = message.from_user.id
        first_name = message.from_user.first_name.replace("_", " ").replace("*", "")
        username = message.from_user.username or ""
        credits_left = get_user_credits(user_id, username)
        
        welcome_text = (
            f"👋 **Привет, {first_name}!**\n\n"
            "Я — твой ИИ-генератор и видеомонтажёр роликов для **Shorts, Reels и TikTok**.\n\n"
            "Что я умею:\n"
            "• 🎬 **Смонтировать готовый видеоклип MP4** из ваших фото под голос диктора;\n"
            "• ✍️ Написать вирусный сценарий, хуки и составить промпты для FLUX/Midjourney.\n\n"
            f"🎁 Твой баланс: **{credits_left} генераций**.\n\n"
            "Выберите действие в меню ниже:"
        )
        bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")
    except Exception as e:
        print(f"Ошибка в start: {e}")

# =====================================================================
# 8. ОБРАБОТКА ИНЛАЙН-КНОПОК И ОПЛАТЫ
# =====================================================================
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id
    
    if call.data == "act_create_video":
        user_media_data[user_id] = {"photos": [], "status": "waiting_photos"}
        bot.send_message(
            call.message.chat.id,
            "📸 **Отправьте мне от 1 до 3 фотографий.**\n"
            "Как закончите присылать — напишите в чат слово **Готово**.",
            parse_mode="Markdown"
        )
        
    elif call.data.startswith("genre_"):
        genre = call.data.replace("genre_", "")
        user_state[user_id] = {"stage": "waiting_theme", "genre": genre}
        bot.send_message(
            call.message.chat.id,
            "✍️ **Напишите тему для ролика одним сообщением.**\n"
            "Например: *«3 привычки, разрушающие мозг»*.",
            parse_mode="Markdown"
        )
        
    elif call.data == "buy_credits":
        prices = [types.LabeledPrice(label="20 генераций для Shorts/Reels", amount=PACKAGE_PRICE_STARS)]
        bot.send_invoice(
            chat_id=call.message.chat.id,
            title="Пакет: 20 генераций",
            description="Пополнение баланса на 20 полных генераций видео или сценариев с промптами.",
            invoice_payload="credits_pack_20_stars",
            provider_token="",  # Для звёзд всегда остаётся пустым
            currency="XTR",     # Код валюты Telegram Stars
            prices=prices,
            start_parameter="buy-shorts-stars"
        )

@bot.pre_checkout_query_handler(func=lambda query: True)
def process_pre_checkout_query(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def process_successful_payment(message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    stars_amount = message.successful_payment.total_amount
    
    update_credits(user_id, PACKAGE_CREDITS)
    log_payment(user_id, username, stars_amount)
    
    bot.send_message(
        message.chat.id,
        f"🎉 **Оплата {stars_amount} звёзд успешно завершена!**\nВам начислено +{PACKAGE_CREDITS} генераций.",
        reply_markup=get_main_keyboard(user_id),
        parse_mode="Markdown"
    )
    
    notify_text = (
        "💸 **НОВАЯ ОПЛАТА В БОТЕ!**\n\n"
        f"👤 Покупатель: @{username} (ID: `{user_id}`)\n"
        f"⭐ Сумма: **+{stars_amount} Stars**\n"
        f"📦 Пакет: 20 генераций"
    )
    for admin in ADMIN_IDS:
        try:
            bot.send_message(admin, notify_text, parse_mode="Markdown")
        except Exception:
            pass

# =====================================================================
# 9. ПРИЁМ ФОТОГРАФИЙ ДЛЯ МОНТАЖА
# =====================================================================
@bot.message_handler(content_types=['photo'])
def handle_incoming_photos(message):
    user_id = message.from_user.id
    if user_id not in user_media_data or user_media_data[user_id].get("status") != "waiting_photos":
        user_media_data[user_id] = {"photos": [], "status": "waiting_photos"}
    
    file_info = bot.get_file(message.photo[-1].file_id)
    photo_direct_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
    
    user_media_data[user_id]["photos"].append(photo_direct_url)
    count = len(user_media_data[user_id]["photos"])
    bot.reply_to(message, f"✅ Фото №{count} загружено. Отправьте следующее или напишите **Готово**.", parse_mode="Markdown")

# =====================================================================
# 10. ОСНОВНАЯ ОБРАБОТКА ТЕКСТОВЫХ СООБЩЕНИЙ
# =====================================================================
@bot.message_handler(func=lambda message: True)
def handle_all_text_messages(message):
    user_id = message.from_user.id
    text = message.text.strip()
    credits_left = get_user_credits(user_id)
    
    # Режим ожидания текста для видео после слова "Готово"
    if text.lower() == "готово":
        if user_id not in user_media_data or not user_media_data[user_id].get("photos"):
            bot.send_message(message.chat.id, "⚠️ Сначала пришлите хотя бы одну фотографию!")
            return
        user_media_data[user_id]["status"] = "waiting_script"
        bot.send_message(message.chat.id, "✍️ **Теперь напишите текст, который диктор озвучит в этом видео.**", parse_mode="Markdown")
        return
        
    # Сборка видеоролика через Creatomate
    if user_id in user_media_data and user_media_data[user_id].get("status") == "waiting_script":
        if credits_left <= 0:
            bot.reply_to(message, "⛔ У вас закончились генерации. Пополните баланс кнопкой ниже.", reply_markup=get_main_keyboard(user_id))
            return
            
        status_msg = bot.reply_to(message, "⏳ *Синтезируем голос диктора и запускаем облачный рендер...*", parse_mode="Markdown")
        voice_filename = f"voice_{user_id}_{int(time.time())}.mp3"
        
        try:
            # 1. Генерируем аудиофайл
            asyncio.run(generate_voice_file(text, voice_filename))
            
            # 2. Формируем запрос к Creatomate API
            headers = {
                "Authorization": f"Bearer {CREATOMATE_API_KEY}",
                "Content-Type": "application/json"
            }
            
            # Используем первую присланную пользователем фотографию
            selected_photo = user_media_data[user_id]["photos"][0]
            
            payload = {
                "template_id": CREATOMATE_TEMPLATE_ID,
                "modifications": {
                    "Image-1.source": selected_photo
                }
            }
            
            resp = requests.post("https://api.creatomate.com/v1/renders", json=payload, headers=headers)
            render_res = resp.json()
            
            if resp.status_code in [200, 201, 202]:
                render_id = render_res[0]["id"]
                video_download_url = None
                
                # Ждем завершения рендеринга (поллинг статуса)
                for _ in range(25):
                    time.sleep(3)
                    check_resp = requests.get(f"https://api.creatomate.com/v1/renders/{render_id}", headers=headers)
                    check_data = check_resp.json()
                    
                    if check_data.get("status") == "succeeded":
                        video_download_url = check_data.get("url")
                        break
                    elif check_data.get("status") == "failed":
                        break
                
                if video_download_url:
                    update_credits(user_id, -1)
                    bot.delete_message(message.chat.id, status_msg.message_id)
                    bot.send_video(message.chat.id, video_download_url, caption="🎬 **Ваше готовое видео для Shorts/Reels!**", parse_mode="Markdown")
                    bot.send_message(message.chat.id, f"✅ Списана 1 генерация. Осталось: **{credits_left - 1}**.", reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")
                else:
                    bot.edit_message_text("❌ Рендеринг видео занял больше времени, чем ожидалось. Попробуйте еще раз.", message.chat.id, status_msg.message_id)
            else:
                bot.edit_message_text(f"❌ Ошибка отправки в Creatomate: {render_res}", message.chat.id, status_msg.message_id)
                
        except Exception as e:
            bot.edit_message_text(f"❌ Ошибка генерации видео: {e}", message.chat.id, status_msg.message_id)
        finally:
            if os.path.exists(voice_filename):
                os.remove(voice_filename)
            user_media_data[user_id] = {}
        return

    # Генерация текстового сценария для Shorts/Reels
    if credits_left <= 0:
        bot.reply_to(message, "⛔ **Баланс исчерпан.** Пополните баланс звёздами ниже.", reply_markup=get_main_keyboard(user_id))
        return
        
    state = user_state.get(user_id, {})
    genre = state.get("genre", "произвольный")
    
    status_msg = bot.reply_to(message, "⏳ *ИИ анализирует тренды и пишет сценарий...*", parse_mode="Markdown")
    
    system_prompt = (
        "Ты — профессиональный продюсер вирусных коротких видео (YouTube Shorts, Instagram Reels, TikTok). "
        "Твоя цель — создать сценарий с удержанием внимания до конца. "
        "Структура ответа строго следующая:\n"
        "1. 🎯 ВИРУСНЫЙ ХУК (первые 3 секунды: зацепка, текстовый и визуальный триггер);\n"
        "2. 📜 СЦЕНАРИЙ (до 45 секунд, разбит по секундам [0-5], [5-15] и т.д., живой дикторский текст);\n"
        "3. 🎨 ВИЗУАЛЬНЫЙ РЯД (покадровый план + готовый детальный промпт на английском для FLUX/Midjourney);\n"
        "4. 🎵 МУЗЫКА (темп, жанр, настроение);\n"
        "5. 🏷 ХЭШТЕГИ (5-7 трендовых тегов)."
    )
    
    try:
        response = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Жанр: {genre}. Тема: {text}"}
            ],
            temperature=0.7
        )
        
        update_credits(user_id, -1)
        result_text = response.choices[0].message.content
        
        bot.delete_message(message.chat.id, status_msg.message_id)
        bot.send_message(message.chat.id, result_text)
        bot.send_message(
            message.chat.id,
            f"✅ Готово! Списана 1 генерация. Осталось на балансе: **{credits_left - 1}**.",
            reply_markup=get_main_keyboard(user_id),
            parse_mode="Markdown"
        )
        user_state[user_id] = {}
        
    except Exception as e:
        bot.edit_message_text(f"❌ Ошибка при генерации сценария: {e}", message.chat.id, status_msg.message_id)

# =====================================================================
# 11. ЗАПУСК БОТА
# =====================================================================
if __name__ == "__main__":
    bot.infinity_polling()
