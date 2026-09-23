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

# ⚠️ ВСТАВЬТЕ СЮДА ВАШ ТЕЛЕГРАМ ID
ADMIN_IDS = [8725167633, 1368485826]

PACKAGE_PRICE_STARS = 50  # Стоимость пакета: 50 звёзд
PACKAGE_CREDITS = 20      # Генераций в пакете
MAX_PHOTOS = 5

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True)
client = OpenAI(
    api_key=PROXYAPI_KEY,
    base_url="https://api.proxyapi.ru/v1"
)

user_media_data = {}
user_state = {}

db_lock = threading.Lock()

# =====================================================================
# 3. БАЗА ДАННЫХ SQLITE
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
# 5. БЕЗОПАСНЫЙ ВЫЗОВ KLING 3.0
# =====================================================================
def run_kling_generation(prompt, image_url=None):
    headers = {
        "Authorization": f"Bearer {PROXYAPI_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "kwaivgi/kling-v3.0-std",
        "prompt": prompt,
        "aspect_ratio": "9:16",
        "duration": 5
    }
    if image_url:
        payload["image_url"] = image_url

    try:
        response = requests.post("https://api.proxyapi.ru/v1/videos", json=payload, headers=headers, timeout=30)
        data = response.json()
        print(f"[KLING REQUEST]: status={response.status_code}, data={data}")
        
        if response.status_code != 200:
            return None
            
        task_id = data.get("id") or data.get("task_id")
        if not task_id:
            return None
            
        for _ in range(40):
            time.sleep(5)
            check_res = requests.get(f"https://api.proxyapi.ru/v1/videos/{task_id}", headers=headers, timeout=15)
            check_data = check_res.json()
            
            status = check_data.get("status")
            if status in ["succeeded", "completed"]:
                video_url = (
                    check_data.get("video_url") 
                    or check_data.get("url") 
                    or (check_data.get("output", {}).get("url") if isinstance(check_data.get("output"), dict) else None)
                )
                if video_url:
                    return video_url
            elif status == "failed":
                print(f"[KLING FAILED]: {check_data}")
                return None
                
        return None
    except Exception as e:
        print(f"[KLING EXCEPTION]: {e}")
        return None

# =====================================================================
# 6. ГЛАВНОЕ МЕНЮ
# =====================================================================
def get_main_keyboard(user_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    credits_left = get_user_credits(user_id)
    
    btn_animate = types.InlineKeyboardButton("✨ Оживить фото (Kling 3.0)", callback_data="mode_animate_photo")
    btn_cartoon = types.InlineKeyboardButton("🧸 Сгенерировать видео по сюжету", callback_data="mode_cartoon")
    btn_slideshow = types.InlineKeyboardButton("🎬 Смонтировать слайдшоу с озвучкой", callback_data="mode_slideshow")
    btn_script = types.InlineKeyboardButton("✍️ Написать сценарий для соцсетей", callback_data="mode_script")
    btn_buy = types.InlineKeyboardButton(f"⭐ Купить 20 генераций (Баланс: {credits_left})", callback_data="buy_credits")
    btn_rules = types.InlineKeyboardButton("📄 Правила использования", url="https://telegra.ph")
    
    markup.add(btn_animate, btn_cartoon, btn_slideshow, btn_script, btn_buy, btn_rules)
    return markup

# =====================================================================
# 7. КОМАНДЫ ДЛЯ ВСЕХ И АДМИНИСТРАТОРОВ
# =====================================================================
@bot.message_handler(commands=['start'])
def handle_start(message):
    user_id = message.from_user.id
    name = message.from_user.first_name.replace("_", " ").replace("*", "")
    credits_left = get_user_credits(user_id, message.from_user.username)
    
    welcome_text = (
        f"👋 **Рад видеть тебя, {name}!**\n\n"
        "Я помогу создать вирусный контент для твоих соцсетей всего в пару кликов.\n"
        "Используй мощь нейросетей **Kling 3.0** и **GPT-4o**, чтобы привлекать тысячи просмотров без навыков сложного видеомонтажа!\n\n"
        f"🎁 Твой баланс: **{credits_left} генераций**.\n\n"
        "👇 **Выбери нужный инструмент ниже:**"
    )
    bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")

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
        
        cursor_pay = conn.cursor()
        cursor_pay.execute("SELECT COUNT(DISTINCT user_id), COUNT(*), SUM(amount) FROM payments")
        row = cursor_pay.fetchone()
        paying_users = row[0] or 0
        total_payments = row[1] or 0
        total_revenue = row[2] or 0
        conn.close()
    
    stats_text = (
        "📊 **ОТЧЕТ ПО ДОХОДАМ REELSGENIE**\n\n"
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
# 8. ОБРАБОТКА НАЖАТИЙ НА КНОПКИ РЕЖИМОВ
# =====================================================================
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id
    
    if call.data == "mode_animate_photo":
        user_state[user_id] = {"mode": "waiting_animate_photo"}
        bot.send_message(
            call.message.chat.id, 
            "✨ **Режим: Оживление фото**\n\n"
            "Пришлите **одно фото**, которое хотите превратить в живое видео.\n"
            "В подписи можно указать движение (например: *«камера приближается, персонаж моргает и улыбается»*).",
            parse_mode="Markdown"
        )

    elif call.data == "mode_cartoon":
        user_state[user_id] = {"mode": "waiting_cartoon_prompt"}
        bot.send_message(
            call.message.chat.id,
            "🧸 **Режим: Генерация видео по описанию**\n\n"
            "Опишите сюжет или сцену на русском или английском языке.\n"
            "Например: *«3D-мультфильм в стиле Pixar: пушистый лисенок в очках читает светящуюся книгу в волшебном лесу»*.",
            parse_mode="Markdown"
        )

    elif call.data == "mode_slideshow":
        user_state[user_id] = {"mode": "waiting_slideshow"}
        user_media_data[user_id] = {"photos": [], "caption": None}
        bot.send_message(
            call.message.chat.id,
            "🎬 **Режим: Слайдшоу с озвучкой**\n\n"
            "Отправьте от 1 до 5 фото альбомом или по одной. "
            "Затем напишите текст для дикторской озвучки (или слово **Готово**).",
            parse_mode="Markdown"
        )

    elif call.data == "mode_script":
        user_state[user_id] = {"mode": "waiting_script_theme"}
        bot.send_message(
            call.message.chat.id,
            "✍️ **Режим: Сценарий для Shorts/Reels**\n\n"
            "Напишите тему ролика (например: *«3 ошибки в бизнесе»*). ИИ составит хук, текст и покадровый план.",
            parse_mode="Markdown"
        )
        
    elif call.data == "buy_credits":
        prices = [types.LabeledPrice(label="20 генераций ReelsGenie", amount=PACKAGE_PRICE_STARS)]
        bot.send_invoice(
            chat_id=call.message.chat.id,
            title="Пакет: 20 генераций ReelsGenie",
            description="Пополнение баланса на 20 видео или сценариев.",
            invoice_payload="credits_pack_20_stars",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="buy-reelsgenie-stars"
        )

# =====================================================================
# 9. ПРИЁМ ФОТОГРАФИЙ
# =====================================================================
@bot.message_handler(content_types=['photo'])
def handle_incoming_photos(message):
    user_id = message.from_user.id
    credits_left = get_user_credits(user_id)
    
    if credits_left <= 0:
        bot.reply_to(message, "⛔ У вас закончились генерации. Пополните баланс.", reply_markup=get_main_keyboard(user_id))
        return
        
    state = user_state.get(user_id, {}).get("mode")
    file_info = bot.get_file(message.photo[-1].file_id)
    photo_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
    
    if state == "waiting_animate_photo":
        prompt_text = message.caption or "High quality, smooth realistic camera motion, cinematic lighting, 4k"
        status_msg = bot.reply_to(message, "✨ *Нейросеть Kling 3.0 оживляет ваше фото... Пожалуйста, подождите 1-2 минуты.*", parse_mode="Markdown")
        
        video_url = run_kling_generation(prompt_text, image_url=photo_url)
        
        if video_url:
            update_credits(user_id, -1)
            new_credits = get_user_credits(user_id)
            bot.delete_message(message.chat.id, status_msg.message_id)
            bot.send_video(
                message.chat.id, 
                video_url, 
                caption="✨ **Ваше ожившее видео от Kling 3.0 AI!**", 
                parse_mode="Markdown"
            )
            bot.send_message(
                message.chat.id, 
                f"✅ Списана 1 генерация. Осталось: **{new_credits}**.", 
                reply_markup=get_main_keyboard(user_id), 
                parse_mode="Markdown"
            )
        else:
            bot.edit_message_text(
                "😔 Сервер генерации сейчас сильно загружен. Пожалуйста, попробуйте еще раз через пару минут!", 
                message.chat.id, 
                status_msg.message_id
            )
            
        user_state[user_id] = {}
        return

    if user_id not in user_media_data:
        user_media_data[user_id] = {"photos": [], "caption": None}
        
    if len(user_media_data[user_id]["photos"]) >= MAX_PHOTOS:
        return
        
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
# 10. СБОРКА СЛАЙДШОУ В CREATOMATE
# =====================================================================
def assemble_video(chat_id, user_id, photos_list, text_script, status_msg_id):
    voice_filename = f"voice_{user_id}_{int(time.time())}.mp3"
    
    try:
        asyncio.run(generate_voice_file(text_script, voice_filename))
        
        with open(voice_filename, "rb") as audio_file:
            audio_msg = bot.send_audio(chat_id, audio_file, caption="🎙 Синхронизация звука...")
            
        audio_file_info = bot.get_file(audio_msg.audio.file_id)
        audio_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{audio_file_info.file_path}"
        
        try:
            bot.delete_message(chat_id, audio_msg.message_id)
        except Exception:
            pass
            
        sorted_photos = [url for msg_id, url in sorted(photos_list, key=lambda x: x[0])]
        count_photos = len(sorted_photos)
        
        char_count = len(text_script)
        estimated_total_duration = max(4.0, char_count / 12.0)
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
                    caption=f"🎬 **Готовое видео от ReelsGenie из {count_photos} фото!**", 
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
            bot.edit_message_text("❌ Ошибка сборки видео в облаке.", chat_id, status_msg_id)
            
    except Exception as e:
        print(f"[CREATOMATE ERROR]: {e}")
        bot.edit_message_text("❌ Произошла ошибка при сборке ролика.", chat_id, status_msg_id)
    finally:
        if os.path.exists(voice_filename):
            os.remove(voice_filename)
        if user_id in user_media_data:
            del user_media_data[user_id]

# =====================================================================
# 11. ОБРАБОТКА ТЕКСТА
# =====================================================================
@bot.message_handler(func=lambda message: True)
def handle_all_text_messages(message):
    user_id = message.from_user.id
    text = message.text.strip()
    credits_left = get_user_credits(user_id)
    state = user_state.get(user_id, {}).get("mode")
    
    if user_id in user_media_data and user_media_data[user_id]["photos"]:
        photos_list = user_media_data[user_id]["photos"]
        script_text = user_media_data[user_id].get("caption") if text.lower() == "готово" else text
        if not script_text:
            script_text = "Посмотрите на эти удивительные кадры вокруг нас."
            
        status_msg = bot.reply_to(message, f"🎬 *Монтируем видео из {len(photos_list)} фото с озвучкой...*", parse_mode="Markdown")
        assemble_video(message.chat.id, user_id, photos_list, script_text, status_msg.message_id)
        return

    if credits_left <= 0:
        bot.reply_to(message, "⛔ **Баланс исчерпан.** Пополните баланс звёздами ниже.", reply_markup=get_main_keyboard(user_id))
        return

    if state == "waiting_cartoon_prompt":
        status_msg = bot.reply_to(message, "🧸 *Нейросеть Kling 3.0 генерирует сцену по вашему сюжету... Подождите 1-2 минуты.*", parse_mode="Markdown")
        
        video_url = run_kling_generation(text)
        
        if video_url:
            update_credits(user_id, -1)
            new_credits = get_user_credits(user_id)
            bot.delete_message(message.chat.id, status_msg.message_id)
            bot.send_video(
                message.chat.id, 
                video_url, 
                caption=f"🎬 **Ваш ролик от Kling 3.0 AI!**\nСюжет: _{text}_", 
                parse_mode="Markdown"
            )
            bot.send_message(
                message.chat.id, 
                f"✅ Списана 1 генерация. Осталось: **{new_credits}**.", 
                reply_markup=get_main_keyboard(user_id), 
                parse_mode="Markdown"
            )
        else:
            bot.edit_message_text(
                "😔 Сервер генерации сейчас сильно загружен. Пожалуйста, попробуйте еще раз через пару минут!", 
                message.chat.id, 
                status_msg.message_id
            )
            
        user_state[user_id] = {}
        return

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
                {"role": "user", "content": f"Тема ролика: {text}"}
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
        print(f"[GPT ERROR]: {e}")
        bot.edit_message_text("❌ Ошибка при создании сценария. Попробуйте еще раз.", message.chat.id, status_msg.message_id)

# =====================================================================
# 12. ОПЛАТА ЗВЁЗДАМИ
# =====================================================================
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
        "💸 **НОВАЯ ОПЛАТА В REELSGENIE!**\n\n"
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
# 13. ЗАПУСК БОТА
# =====================================================================
if __name__ == "__main__":
    bot.infinity_polling()

