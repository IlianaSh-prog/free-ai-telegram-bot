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
# 1. ВЕБ-СЕРВЕР ДЛЯ СТАТУСА LIVE НА RENDER
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
# 2. КЛЮЧИ И НАСТРОЙКИ
# =====================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
PROXYAPI_KEY = os.environ.get("PROXYAPI_KEY")
CREATOMATE_API_KEY = os.environ.get("CREATOMATE_API_KEY")
CREATOMATE_TEMPLATE_ID = os.environ.get("CREATOMATE_TEMPLATE_ID")

ADMIN_IDS = [8725167633, 1368485826]  # Ваши Telegram ID

PACKAGE_PRICE_STARS = 50
PACKAGE_CREDITS = 20

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = OpenAI(
    api_key=PROXYAPI_KEY,
    base_url="https://api.proxyapi.ru/v1"
)

user_media_data = {}
user_state = {}

# =====================================================================
# 3. БАЗА ДАННЫХ SQLITE
# =====================================================================
def init_db():
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, credits INTEGER DEFAULT 2)')
    c.execute('CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, amount INTEGER, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)')
    conn.commit()
    conn.close()

init_db()

def get_user_credits(user_id, username=""):
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row is None:
        start_credits = 10 if user_id in ADMIN_IDS else 2
        safe_name = username if username else "user"
        c.execute("INSERT INTO users (user_id, username, credits) VALUES (?, ?, ?)", (user_id, safe_name, start_credits))
        conn.commit()
        credits = start_credits
    else:
        credits = row[0]
        if user_id in ADMIN_IDS and credits < 10:
            c.execute("UPDATE users SET credits = 10 WHERE user_id = ?", (user_id,))
            conn.commit()
            credits = 10
    conn.close()
    return credits

def update_credits(user_id, count):
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (count, user_id))
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
# 6. КОМАНДЫ
# =====================================================================
@bot.message_handler(commands=['start'])
def handle_start(message):
    user_id = message.from_user.id
    name = message.from_user.first_name.replace("_", " ").replace("*", "")
    credits_left = get_user_credits(user_id, message.from_user.username)
    
    text = (
        f"👋 **Привет, {name}!**\n\n"
        "Я — твой ИИ-генератор и видеомонтажёр для **Shorts, Reels и TikTok**.\n\n"
        "• 🎬 **Чтобы смонтировать видео:** просто пришли мне **фото с текстом в подписи** или нажми кнопку ниже!\n"
        "• ✍️ **Чтобы создать сценарий:** выбери тематику в меню.\n\n"
        f"🎁 Твой баланс: **{credits_left} генераций**."
    )
    bot.send_message(message.chat.id, text, reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")

@bot.message_handler(commands=['add'])
def handle_add(message):
    if message.from_user.id in ADMIN_IDS:
        update_credits(message.from_user.id, 10)
        bot.reply_to(message, "👑 **Вам начислено +10 генераций!**")

# =====================================================================
# 7. ОБРАБОТКА ФОТОГРАФИЙ (ДАЖЕ С ПОДПИСЬЮ И БЕЗ КНОПОК!)
# =====================================================================
@bot.message_handler(content_types=['photo'])
def handle_photos_instant(message):
    user_id = message.from_user.id
    credits_left = get_user_credits(user_id)
    
    if credits_left <= 0:
        bot.reply_to(message, "⛔ У вас закончились генерации. Пополните баланс.", reply_markup=get_main_keyboard(user_id))
        return
        
    # Получаем ссылку на фото
    file_info = bot.get_file(message.photo[-1].file_id)
    photo_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
    
    # Если под фото был написан текст (как на вашем скриншоте!)
    caption_text = message.caption
    
    if caption_text:
        # Сразу запускаем монтаж видео!
        status_msg = bot.reply_to(message, "🎬 *Фото и текст получены! Озвучиваем диктором и запускаем монтаж клипа...*", parse_mode="Markdown")
        assemble_video(message.chat.id, user_id, photo_url, caption_text, status_msg.message_id)
    else:
        # Если прислали фото без текста, сохраняем и просим текст
        user_media_data[user_id] = {"photo": photo_url}
        bot.reply_to(message, "📸 **Фото получено!**\nТеперь напишите текст для диктора, который будет звучать в ролике:", parse_mode="Markdown")

# =====================================================================
# 8. ФУНКЦИЯ СБОРКИ ВИДЕО В CREATOMATE
# =====================================================================
def assemble_video(chat_id, user_id, photo_url, text_script, status_msg_id):
    voice_filename = f"voice_{user_id}_{int(time.time())}.mp3"
    credits_left = get_user_credits(user_id)
    
    try:
        # 1. Синтез речи
        asyncio.run(generate_voice_file(text_script, voice_filename))
        
        # 2. Запрос в Creatomate
        headers = {
            "Authorization": f"Bearer {CREATOMATE_API_KEY}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "template_id": CREATOMATE_TEMPLATE_ID,
            "modifications": {
                "Image-1.source": photo_url
            }
        }
        
        resp = requests.post("https://api.creatomate.com/v1/renders", json=payload, headers=headers)
        render_res = resp.json()
        
        if resp.status_code in [200, 201, 202]:
            render_id = render_res[0]["id"]
            video_url = None
            
            for _ in range(25):
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
                bot.delete_message(chat_id, status_msg_id)
                bot.send_video(chat_id, video_url, caption="🎬 **Ваш готовый смонтированный клип!**", parse_mode="Markdown")
                bot.send_message(chat_id, f"✅ Списана 1 генерация. Осталось: **{credits_left - 1}**.", reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")
            else:
                bot.edit_message_text("❌ Рендер занял больше времени. Попробуйте ещё раз.", chat_id, status_msg_id)
        else:
            bot.edit_message_text(f"❌ Ошибка Creatomate: {render_res}", chat_id, status_msg_id)
            
    except Exception as e:
        bot.edit_message_text(f"❌ Ошибка сборки: {e}", chat_id, status_msg_id)
    finally:
        if os.path.exists(voice_filename):
            os.remove(voice_filename)

# =====================================================================
# 9. ТЕКСТОВЫЕ СООБЩЕНИЯ (СЦЕНАРИИ ИЛИ ТЕКСТ К ФОТО)
# =====================================================================
@bot.message_handler(func=lambda message: True)
def handle_text(message):
    user_id = message.from_user.id
    text = message.text.strip()
    credits_left = get_user_credits(user_id)
    
    # Если ранее было загружено фото без текста
    if user_id in user_media_data and "photo" in user_media_data[user_id]:
        photo_url = user_media_data[user_id]["photo"]
        user_media_data[user_id] = {}
        status_msg = bot.reply_to(message, "🎬 *Текст принят! Собираем видео...*", parse_mode="Markdown")
        assemble_video(message.chat.id, user_id, photo_url, text, status_msg.message_id)
        return
        
    # Обычная генерация сценария
    if credits_left <= 0:
        bot.reply_to(message, "⛔ Баланс исчерпан.", reply_markup=get_main_keyboard(user_id))
        return
        
    status_msg = bot.reply_to(message, "⏳ *ИИ пишет сценарий...*", parse_mode="Markdown")
    try:
        response = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты продюсер Shorts/Reels. Напиши хук, сценарий и промпты."},
                {"role": "user", "content": text}
            ]
        )
        update_credits(user_id, -1)
        bot.delete_message(message.chat.id, status_msg.message_id)
        bot.send_message(message.chat.id, response.choices[0].message.content)
        bot.send_message(message.chat.id, f"✅ Осталось генераций: **{credits_left - 1}**.", reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")
    except Exception as e:
        bot.edit_message_text(f"❌ Ошибка: {e}", message.chat.id, status_msg.message_id)

# =====================================================================
# 10. КНОПКИ И ЗВЁЗДЫ
# =====================================================================
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id
    if call.data == "act_create_video":
        bot.send_message(call.message.chat.id, "📸 **Пришлите мне фото с текстом в подписи!**\nИли просто отправьте фото, а текст напишите следующим сообщением.", parse_mode="Markdown")
    elif call.data.startswith("genre_"):
        bot.send_message(call.message.chat.id, "✍️ **Напишите тему для сценария:**", parse_mode="Markdown")
    elif call.data == "buy_credits":
        prices = [types.LabeledPrice(label="20 генераций", amount=PACKAGE_PRICE_STARS)]
        bot.send_invoice(call.message.chat.id, "Пакет: 20 генераций", "20 видео/сценариев", "stars_pack", "", "XTR", prices)

@bot.pre_checkout_query_handler(func=lambda q: True)
def process_pre_checkout(q):
    bot.answer_pre_checkout_query(q.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def process_payment(message):
    update_credits(message.from_user.id, PACKAGE_CREDITS)
    bot.send_message(message.chat.id, f"🎉 Начислено +{PACKAGE_CREDITS} генераций!", reply_markup=get_main_keyboard(message.from_user.id))

if __name__ == "__main__":
    bot.infinity_polling()
