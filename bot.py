
import os
import sqlite3
import threading
import asyncio
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
import telebot
from telebot import types
import edge_tts

# --- 1. ВЕБ-СЕРВЕР ДЛЯ РАБОТЫ НА RENDER ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args): return

def serve():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

threading.Thread(target=serve, daemon=True).start()

# --- 2. НАСТРОЙКИ КЛЮЧЕЙ И API ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CREATOMATE_API_KEY = os.environ.get("CREATOMATE_API_KEY")
CREATOMATE_TEMPLATE_ID = os.environ.get("CREATOMATE_TEMPLATE_ID")

ADMIN_IDS = [123456789]  # Укажите ваш Telegram ID

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Хранилище временных данных пользователя {user_id: {"photos": [], "text": ""}}
user_media_data = {}

# --- 3. ГЕНЕРАЦИЯ ГОЛОСА (EDGE-TTS) ---
async def generate_voice(text, output_file):
    # Голоса: ru-RU-DmitryNeural или ru-RU-SvetlanaNeural
    communicate = edge_tts.Communicate(text, "ru-RU-DmitryNeural")
    await communicate.save(output_file)

# --- 4. РАБОТА С БАЗОЙ ДАННЫХ ---
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
    conn.commit()
    conn.close()

init_db()

def get_credits(user_id):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row is None:
        credits = 10 if user_id in ADMIN_IDS else 2
        cursor.execute("INSERT INTO users (user_id, credits) VALUES (?, ?)", (user_id, credits))
        conn.commit()
    else:
        credits = row[0]
    conn.close()
    return credits

def update_credits(user_id, count):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (count, user_id))
    conn.commit()
    conn.close()

# --- 5. ОБРАБОТКА КОМАНД И МЕНЮ ---
@bot.message_handler(commands=['start'])
def handle_start(message):
    user_id = message.from_user.id
    credits_left = get_credits(user_id)
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("🎬 Создать видео из фото"))
    
    welcome = (
        f"👋 **Привет! Я ИИ-монтажёр видео.**\n\n"
        f"Я могу превратить твои фотографии и текст в готовый клип MP4 для Shorts/Reels!\n\n"
        f"🎁 Доступно генераций: **{credits_left}**\n\n"
        f"Нажми кнопку ниже, чтобы начать!"
    )
    bot.send_message(message.chat.id, welcome, reply_markup=markup, parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text == "🎬 Создать видео из фото")
def start_video_creation(message):
    user_id = message.from_user.id
    user_media_data[user_id] = {"photos": [], "text": ""}
    bot.send_message(message.chat.id, "📸 **Отправь мне от 1 до 3 фотографий по очереди.**\nКак закончишь присылать фото — напиши слово *«Готово»*.", parse_mode="Markdown")

# Приём фотографий от пользователя
@bot.message_handler(content_types=['photo'])
def handle_photos(message):
    user_id = message.from_user.id
    if user_id not in user_media_data:
        user_media_data[user_id] = {"photos": [], "text": ""}
    
    # Получаем ссылку на фото высокого качества
    file_info = bot.get_file(message.photo[-1].file_id)
    photo_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
    
    user_media_data[user_id]["photos"].append(photo_url)
    count = len(user_media_data[user_id]["photos"])
    bot.reply_to(message, f"✅ Фото №{count} получено! Отправь ещё или напиши слово *«Готово»*.", parse_mode="Markdown")

# Приём текста после того как фото загружены
@bot.message_handler(func=lambda m: m.text.lower() == "готово")
def ask_text_script(message):
    user_id = message.from_user.id
    if user_id not in user_media_data or not user_media_data[user_id]["photos"]:
        bot.send_message(message.chat.id, "⚠️ Сначала отправь хотя бы 1 фотографию!")
        return
    
    bot.send_message(message.chat.id, "✍️ **Теперь напиши текст, который диктор должен озвучить в видео.**", parse_mode="Markdown")

# Финальная сборка видео
@bot.message_handler(func=lambda m: True)
def process_video_generation(message):
    user_id = message.from_user.id
    
    if user_id in user_media_data and user_media_data[user_id]["photos"] and not user_media_data[user_id]["text"]:
        script_text = message.text
        user_media_data[user_id]["text"] = script_text
        
        status_msg = bot.reply_to(message, "⏳ *Генерируем голос диктора и собираем видеоклип... (30-40 секунд)*", parse_mode="Markdown")
        
        try:
            # 1. Генерируем аудиофайл
            audio_path = f"voice_{user_id}.mp3"
            asyncio.run(generate_voice(script_text, audio_path))
            
            # 2. Отправляем запрос в Creatomate API
            headers = {
                "Authorization": f"Bearer {CREATOMATE_API_KEY}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "template_id": CREATOMATE_TEMPLATE_ID,
                "modifications": {
                    "Audio-1.source": audio_path,
                    "Image-1.source": user_media_data[user_id]["photos"][0]
                }
            }
            
            response = requests.post("https://api.creatomate.com/v1/renders", json=payload, headers=headers)
            render_data = response.json()
            
            if response.status_code == 200 or response.status_code == 201:
                video_url = render_data[0]["url"]
                
                # Списываем 1 генерацию
                update_credits(user_id, -1)
                
                bot.delete_message(message.chat.id, status_msg.message_id)
                bot.send_video(message.chat.id, video_url, caption="🎬 **Ваше готовое видео для Shorts/Reels!**", parse_mode="Markdown")
                
                # Очищаем данные
                user_media_data[user_id] = {"photos": [], "text": ""}
                if os.path.exists(audio_path):
                    os.remove(audio_path)
            else:
                bot.edit_message_text(f"❌ Ошибка сборки видео: {render_data}", message.chat.id, status_msg.message_id)
                
        except Exception as e:
            bot.edit_message_text(f"❌ Произошла ошибка: {e}", message.chat.id, status_msg.message_id)

if __name__ == "__main__":
    bot.infinity_polling()
