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
    def log_message(self, *args): return

def run_webserver():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

threading.Thread(target=run_webserver, daemon=True).start()

# =====================================================================
# 2. КЛЮЧИ, НАСТРОЙКИ И АДМИНИСТРАТОРЫ
# =====================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
PROXYAPI_KEY = os.environ.get("PROXYAPI_KEY")
CREATOMATE_API_KEY = os.environ.get("CREATOMATE_API_KEY")

# ⚠️ ВСТАВЬТЕ СЮДА ВАШ TELEGRAM ID И ID ВЛАДЕЛЬЦА (узнать в @userinfobot)
ADMIN_IDS = [8725167633, 1368485826]

PACKAGE_PRICE_STARS = 50
PACKAGE_CREDITS = 20
MAX_PHOTOS = 5

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True)
client = OpenAI(
    api_key=PROXYAPI_KEY,
    base_url="https://api.proxyapi.ru/v1"
)

user_media_data = {}
user_state = {}

# Блокировка для потокобезопасной работы с SQLite
db_lock = threading.Lock()

# =====================================================================
# 3. БАЗА ДАННЫХ SQLITE С ПОЛНОЙ БЛОКИРОВКОЙ ПОТОКОВ
# =====================================================================
def get_db():
    conn = sqlite3.connect("bot_database.db", timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout = 60000;")
    return conn

def init_db():
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                credits INTEGER DEFAULT 2
            )
        ''')
        c.execute('''
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
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        
        if row is None:
            start_credits = 10 if user_id in ADMIN_IDS else 2
            safe_name = username if username else "user"
            c.execute("INSERT INTO users (user_id, username, credits) VALUES (?, ?, ?)", 
                      (user_id, safe_name, start_credits))
            conn.commit()
            credits = start_credits
        else:
            credits = row[0]
                
        conn.close()
        return credits

def update_credits(user_id, count):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (count, user_id))
        conn.commit()
        conn.close()

def log_payment(user_id, username, amount):
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO payments (user_id, username, amount) VALUES (?, ?, ?)", 
                  (user_id, username, amount))
        conn.commit()
        conn.close()

# =====================================================================
# 4. СИНТЕЗ ГОЛОСА
# =====================================================================
async def generate_voice_file(text, output_path):
    communicate = edge_tts.Communicate(text, "ru-RU-DmitryNeural")
    await communicate.save(output_path)

# =====================================================================
# 5. КЛАВИАТУРА
# =====================================================================
def get_main_keyboard(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    credits_left = get_user_credits(user_id)
    
    btn_video = types.InlineKeyboardButton("🎬 Создать видео из фото (до 5 шт)", callback_data="act_create_video")
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
# 6. КОМАНДЫ
# =====================================================================
@bot.message_handler(commands=['start'])
def handle_start(message):
    user_id = message.from_user.id
    name = message.from_user.first_name.replace("_", " ").replace("*", "")
    credits_left = get_user_credits(user_id, message.from_user.username)
    
    text = (
        f"👋 **Привет, {name}!**\n\n"
        "Я — твой ИИ-продюсер и видеомонтажёр для **Shorts, Reels и TikTok**.\n\n"
        "• 🎬 **Монтаж видео:** пришлите от 1 до 5 фото (можно с текстом в подписи)!\n"
        "• ✍️ **Сценарий ИИ:** выберите тематику в меню ниже.\n\n"
        f"🎁 Твой баланс: **{credits_left} генераций**."
    )
    bot.send_message(message.chat.id, text, reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")

@bot.message_handler(commands=['stats', 'admin'])
def handle_admin_stats(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.reply_to(message, "⛔ У вас нет доступа к этой команде.")
        return
    
    with db_lock:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        total_users = c.fetchone()[0]
        
        c.execute("SELECT COUNT(DISTINCT user_id), COUNT(*), SUM(amount) FROM payments")
        row = c.fetchone()
        paying_users = row[0] or 0
        total_payments = row[1] or 0
        total_revenue = row[2] or 0
        conn.close()
    
    stats_text = (
        "📊 **ОТЧЕТ ПО ДОХОДАМ И БОТУ**\n\n"
        f"👥 Всего пользователей: **{total_users} чел.**\n"
        f"💳 Платящих клиентов: **{paying_users} чел.**\n"
        f"🛍 Успешных покупок: **{total_payments} шт.**\n\n"
        f"⭐ **СУММАРНАЯ ВЫРУЧКА:** **{total_revenue} Stars**"
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
    new_bal = get_user_credits(message.from_user.id)
    bot.reply_to(message, f"👑 **Начислено +{amount} генераций!**\nТекущий баланс: **{new_bal}**.", parse_mode="Markdown")

# =====================================================================
# 7. ПРИЁМ ФОТОГРАФИЙ С СОРТИРОВКОЙ ПО MESSAGE_ID
# =====================================================================
@bot.message_handler(content_types=['photo'])
def handle_incoming_photos(message):
    user_id = message.from_user.id
    credits_left = get_user_credits(user_id)
    
    if credits_left <= 0:
        bot.reply_to(message, "⛔ У вас закончились генерации. Пополните баланс.", reply_markup=get_main_keyboard(user_id))
        return
        
    if user_id not in user_media_data:
        user_media_data[user_id] = {"photos": [], "caption": None}
        
    file_info = bot.get_file(message.photo[-1].file_id)
    photo_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
    
    if len(user_media_data[user_id]["photos"]) >= MAX_PHOTOS:
        return
        
    # Сохраняем кортеж (ID сообщения, ссылка на фото), чтобы потом отсортировать!
    user_media_data[user_id]["photos"].append((message.message_id, photo_url))
    current_count = len(user_media_data[user_id]["photos"])
    
    if message.caption:
        user_media_data[user_id]["caption"] = message.caption
        
    bot.reply_to(
        message, 
        f"✅ **Фото №{current_count} загружено!** ({current_count}/{MAX_PHOTOS})\n"
        f"Отправьте ещё фото или напишите текст для диктора (можно слово **Готово**).",
        parse_mode="Markdown"
    )

# =====================================================================
# 8. СБОРКА ВИДЕО В CREATOMATE С ДИНАМИЧЕСКИМ РАСЧЕТОМ ВРЕМЕНИ
# =====================================================================
def assemble_video(chat_id, user_id, photos_list, text_script, status_msg_id):
    voice_filename = f"voice_{user_id}_{int(time.time())}.mp3"
    
    try:
        # 1. Синтез дикторской речи
        asyncio.run(generate_voice_file(text_script, voice_filename))
        
        # 2. Скрытая загрузка аудио в Telegram для получения URL
        with open(voice_filename, "rb") as audio_file:
            audio_msg = bot.send_audio(chat_id, audio_file, caption="🎙 Синхронизация звука...")
            
        audio_file_info = bot.get_file(audio_msg.audio.file_id)
        audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{audio_file_info.file_path}"
        
        try:
            bot.delete_message(chat_id, audio_msg.message_id)
        except Exception:
            pass
            
        # 3. Сортируем фотографии по message_id (чтобы порядок не путался!)
        sorted_photos = [url for msg_id, url in sorted(photos_list, key=lambda x: x[0])]
        count_photos = len(sorted_photos)
        
        # 4. Динамический расчёт длины слайдов на основе длины текста
        # Средняя скорость диктора: 12 символов в секунду
        char_count = len(text_script)
        estimated_total_duration = max(4.0, char_count / 12.0)
        
        # Делим всю длину аудио на количество фотографий
        duration_per_slide = round(estimated_total_duration / count_photos, 2)
        
        headers = {
            "Authorization": f"Bearer {CREATOMATE_API_KEY}",
            "Content-Type": "application/json"
        }
        
        elements = []
        for idx, photo_url in enumerate(sorted_photos):
            elements.append({
                "type": "image",
                "track": 1,
                "time": idx * duration_per_slide,
                "duration": duration_per_slide,
                "source": photo_url,
                "animations": [
                    {
                        "time": "start",
                        "duration": duration_per_slide,
                        "transition": True,
                        "type": "scale",
                        "scope": "element",
                        "start_scale": "100%",
                        "end_scale": "115%",
                        "easing": "linear"
                    }
                ]
            })
            
        # Добавляем аудиодорожку на всю длину
        elements.append({
            "type": "audio",
            "track": 2,
            "duration": estimated_total_duration,
            "source": audio_url
        })
        
        payload = {
            "source": {
                "output_format": "mp4",
                "width": 1080,
                "height": 1920,
                "frame_rate": 30,
                "elements": elements
            }
        }
        
        resp = requests.post("https://api.creatomate.com/v1/renders", json=payload, headers=headers)
        render_res = resp.json()
        
        if resp.status_code in [200, 201, 202]:
            render_id = render_res[0]["id"]
            video_url = None
            
            for _ in range(30):
                time.sleep(3)
                check_resp = requests.get(f"https://api.creatomate.com/v1/renders/{render_id}", headers=headers)
                check_data = check_resp.json()
                
                if check_data.get("status") == "succeeded":
                    video_url = check_data.get("url")
                    break
                elif check_data.get("status") == "failed":
                    break
            
            if video_url:
                update_credits(user_id, -1)
                credits_left = get_user_credits(user_id)
                bot.delete_message(chat_id, status_msg_id)
                
                bot.send_video(
                    chat_id, 
                    video_url, 
                    caption=f"🎬 **Готовое видео из {count_photos} фото с синхронной озвучкой!**", 
                    parse_mode="Markdown"
                )
                bot.send_message(
                    chat_id, 
                    f"✅ Списана 1 генерация. Осталось: **{credits_left}**.", 
                    reply_markup=get_main_keyboard(user_id), 
                    parse_mode="Markdown"
                )
            else:
                bot.edit_message_text("❌ Рендер видео занял больше времени. Попробуйте ещё раз.", chat_id, status_msg_id)
        else:
            bot.edit_message_text(f"❌ Ошибка Creatomate: {render_res}", chat_id, status_msg_id)
            
    except Exception as e:
        bot.edit_message_text(f"❌ Ошибка сборки видео: {e}", chat_id, status_msg_id)
    finally:
        if os.path.exists(voice_filename):
            os.remove(voice_filename)
        if user_id in user_media_data:
            del user_media_data[user_id]

# =====================================================================
# 9. ОБРАБОТКА ТЕКСТА
# =====================================================================
@bot.message_handler(func=lambda message: True)
def handle_all_text_messages(message):
    user_id = message.from_user.id
    text = message.text.strip()
    credits_left = get_user_credits(user_id)
    
    if user_id in user_media_data and user_media_data[user_id]["photos"]:
        photos_list = user_media_data[user_id]["photos"]
        if text.lower() == "готово":
            script_text = user_media_data[user_id].get("caption") or "Посмотрите на эти кадры вокруг нас."
        else:
            script_text = text
            
        status_msg = bot.reply_to(message, f"🎬 *Монтируем видео из {len(photos_list)} фото с озвучкой...*", parse_mode="Markdown")
        assemble_video(message.chat.id, user_id, photos_list, script_text, status_msg.message_id)
        return

    if credits_left <= 0:
        bot.reply_to(message, "⛔ **Баланс исчерпан.** Пополните баланс звёздами ниже.", reply_markup=get_main_keyboard(user_id))
        return
        
    state = user_state.get(user_id, {})
    genre = state.get("genre", "произвольный")
    
    status_msg = bot.reply_to(message, "⏳ *ИИ анализирует тренды и пишет сценарий...*", parse_mode="Markdown")
    
    system_prompt = (
        "Ты — профессиональный продюсер вирусных коротких видео (YouTube Shorts, Instagram Reels, TikTok). "
        "Твоя цель — создать сценарий с удержанием 100%. "
        "Структура ответа строго:\n"
        "1. 🎯 ВИРУСНЫЙ ХУК (первые 3 сек);\n"
        "2. 📜 СЦЕНАРИЙ (до 45 сек, разбит по секундам [0-5], [5-15] и т.д.);\n"
        "3. 🎨 ВИЗУАЛЬНЫЙ РЯД (покадровый план + детальный промпт на английском);\n"
        "4. 🎵 МУЗЫКА (темп, жанр);\n"
        "5. 🏷 ХЭШТЕГИ (5-7 штук)."
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
        new_credits = get_user_credits(user_id)
        result_text = response.choices[0].message.content
        
        bot.delete_message(message.chat.id, status_msg.message_id)
        bot.send_message(message.chat.id, result_text)
        bot.send_message(
            message.chat.id,
            f"✅ Готово! Списана 1 генерация. Осталось: **{new_credits}**.",
            reply_markup=get_main_keyboard(user_id),
            parse_mode="Markdown"
        )
        user_state[user_id] = {}
        
    except Exception as e:
        bot.edit_message_text(f"❌ Ошибка генерации: {e}", message.chat.id, status_msg.message_id)

# =====================================================================
# 10. КНОПКИ И ЗВЁЗДЫ
# =====================================================================
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id
    
    if call.data == "act_create_video":
        bot.send_message(
            call.message.chat.id, 
            "📸 **Отправьте от 1 до 5 фотографий!**\n"
            "Вы можете прислать их альбомом или по одной. После этого напишите текст для диктора.", 
            parse_mode="Markdown"
        )
    elif call.data.startswith("genre_"):
        genre = call.data.replace("genre_", "")
        user_state[user_id] = {"stage": "waiting_theme", "genre": genre}
        bot.send_message(call.message.chat.id, "✍️ **Напишите тему для сценария:**", parse_mode="Markdown")
        
    elif call.data == "buy_credits":
        prices = [types.LabeledPrice(label="20 генераций Shorts/Reels", amount=PACKAGE_PRICE_STARS)]
        bot.send_invoice(
            chat_id=call.message.chat.id,
            title="Пакет: 20 генераций",
            description="Пополнение баланса на 20 видео или сценариев с промптами.",
            invoice_payload="credits_pack_20_stars",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="buy-shorts-stars"
        )

@bot.pre_checkout_query_handler(func=lambda q: True)
def process_pre_checkout(q):
    bot.answer_pre_checkout_query(q.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def process_payment(message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    stars_amount = message.successful_payment.total_amount
    
    update_credits(user_id, PACKAGE_CREDITS)
    log_payment(user_id, username, stars_amount)
    
    bot.send_message(
        message.chat.id, 
        f"🎉 **Оплата {stars_amount} звёзд успешно прошла!**\nВам начислено +{PACKAGE_CREDITS} генераций.", 
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
# 11. ЗАПУСК БОТА
# =====================================================================
if __name__ == "__main__":
    bot.infinity_polling()
