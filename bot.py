import os
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import telebot
from telebot import types
from openai import OpenAI

# --- 1. ВЕБ-СЕРВЕР ДЛЯ СТАТУСА LIVE НА RENDER ---
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

# --- 2. НАСТРОЙКИ И КЛЮЧИ ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
PROXYAPI_KEY = os.environ.get("PROXYAPI_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = OpenAI(
    api_key=PROXYAPI_KEY,
    base_url="https://api.proxyapi.ru/v1"
)

# --- 3. РАБОТА С БАЗОЙ ДАННЫХ (SQLite) ---
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

def get_user_credits(user_id, username=""):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row is None:
        safe_username = username if username else "no_username"
        cursor.execute("INSERT INTO users (user_id, username, credits) VALUES (?, ?, 2)", (user_id, safe_username))
        conn.commit()
        credits = 2
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

# --- 4. МЕНЮ И КНОПКИ ---
def get_main_keyboard(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    credits_left = get_user_credits(user_id)
    
    btn1 = types.InlineKeyboardButton("🔥 Факты / Топы", callback_data="genre_facts")
    btn2 = types.InlineKeyboardButton("💡 Экспертный / Польза", callback_data="genre_expert")
    btn3 = types.InlineKeyboardButton("😱 Мистика / Истории", callback_data="genre_story")
    btn4 = types.InlineKeyboardButton("💰 Деньги / Бизнес", callback_data="genre_business")
    btn_buy = types.InlineKeyboardButton(f"⭐ Купить 20 генераций (Баланс: {credits_left})", callback_data="buy_credits")
    # Рабочая ссылка (замените на свою оферту на telegra.ph)
    btn_rules = types.InlineKeyboardButton("📄 Правила использования", url="https://telegra.ph")
    
    markup.add(btn1, btn2)
    markup.add(btn3, btn4)
    markup.add(btn_buy)
    markup.add(btn_rules)
    return markup

# --- 5. ОБРАБОТКА КОМАНДЫ /START ---
@bot.message_handler(commands=['start'])
def handle_start(message):
    try:
        user_id = message.from_user.id
        # Очищаем имя от спецсимволов для безопасности
        first_name = message.from_user.first_name.replace("_", " ").replace("*", "")
        username = message.from_user.username or ""
        
        credits_left = get_user_credits(user_id, username)
        
        welcome_text = (
            f"👋 **Привет, {first_name}!**\n\n"
            "Я — твой ИИ-продюсер вирусных роликов для **Shorts, Reels и TikTok**.\n\n"
            f"🎁 Твой баланс: **{credits_left} генерации**.\n\n"
            "Выбери категорию ролика ниже:"
        )
        bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")
    except Exception as e:
        print(f"Ошибка в start: {e}")

# --- 6. ОБРАБОТКА КНОПОК И ОПЛАТЫ ЗВЕЗДАМИ ---
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id
    
    if call.data.startswith("genre_"):
        bot.send_message(call.message.chat.id, "✍️ **Напиши тему для ролика одним сообщением.**", parse_mode="Markdown")
    
    elif call.data == "buy_credits":
        prices = [types.LabeledPrice(label="20 сценариев для Reels/Shorts", amount=50)]
        bot.send_invoice(
            chat_id=call.message.chat.id,
            title="Пакет: 20 сценариев Shorts/Reels",
            description="Пополнение баланса на 20 сценариев с промптами для FLUX и тегами.",
            invoice_payload="credits_pack_20_stars",
            provider_token="",  # Для Звёзд оставляем пустым!
            currency="XTR",     # Telegram Stars
            prices=prices,
            start_parameter="buy-shorts-stars"
        )

@bot.pre_checkout_query_handler(func=lambda query: True)
def process_pre_checkout_query(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def process_successful_payment(message):
    user_id = message.from_user.id
    update_credits(user_id, 20)
    
    bot.send_message(
        message.chat.id,
        "🎉 **Оплата 50 звёзд успешно прошла!**\n\nВам начислено +20 генераций.",
        reply_markup=get_main_keyboard(user_id),
        parse_mode="Markdown"
    )

# --- 7. ГЕНЕРАЦИЯ СЦЕНАРИЯ ---
@bot.message_handler(func=lambda message: True)
def handle_user_text(message):
    user_id = message.from_user.id
    credits_left = get_user_credits(user_id)
    
    if credits_left <= 0:
        bot.reply_to(message, "⛔ **Баланс исчерпан.** Пополните баланс кнопкой ниже.", reply_markup=get_main_keyboard(user_id))
        return
    
    status_msg = bot.reply_to(message, "⏳ *ИИ анализирует тренды и пишет сценарий...*", parse_mode="Markdown")
    
    try:
        response = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты продюсер вирусных Shorts/Reels. Напиши хук, сценарий по секундам и промпты для картинки."},
                {"role": "user", "content": message.text}
            ]
        )
        
        update_credits(user_id, -1)
        result_text = response.choices[0].message.content
        
        bot.delete_message(message.chat.id, status_msg.message_id)
        bot.send_message(message.chat.id, result_text)
        bot.send_message(message.chat.id, f"✅ Списана 1 генерация. Осталось: **{credits_left - 1}**.", reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")
        
    except Exception as e:
        bot.edit_message_text(f"❌ Ошибка генерации: {e}", message.chat.id, status_msg.message_id)

if __name__ == "__main__":
    bot.infinity_polling()
