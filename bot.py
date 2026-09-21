import os
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import telebot
from telebot import types
from openai import OpenAI

# --- 1. ВЕБ-СЕРВЕР ДЛЯ ПОДДЕРЖАНИЯ СТАТУСА LIVE НА RENDER ---
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

# --- 2. НАСТРОЙКИ, КЛЮЧИ И АДМИНИСТРАТОРЫ ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
PROXYAPI_KEY = os.environ.get("PROXYAPI_KEY")

# ⚠️ ВСТАВЬТЕ СЮДА ВАШ ЦИФРОВОЙ ID И ID ВЛАДЕЛЬЦА ЧЕРЕЗ ЗАПЯТУЮ (узнать в @userinfobot):
ADMIN_IDS = [8725167633, 1368485826]  # [Ваш ID, ID владельца]

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = OpenAI(
    api_key=PROXYAPI_KEY,
    base_url="https://api.proxyapi.ru/v1"
)

user_state = {}

# --- 3. БАЗА ДАННЫХ (SQLite) ---
def init_db():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    # Таблица пользователей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            credits INTEGER DEFAULT 2
        )
    ''')
    # Таблица платежей
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
        # Админам на старте даем 10 попыток, обычным пользователям — 2
        start_credits = 10 if user_id in ADMIN_IDS else 2
        safe_username = username if username else "no_username"
        cursor.execute("INSERT INTO users (user_id, username, credits) VALUES (?, ?, ?)", 
                       (user_id, safe_username, start_credits))
        conn.commit()
        credits = start_credits
    else:
        credits = row[0]
        # Если вы уже были в базе, но баланс меньше 10 — автоматически поднимаем до 10!
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
    cursor.execute("INSERT INTO payments (user_id, username, amount) VALUES (?, ?, ?)", (user_id, username, amount))
    conn.commit()
    conn.close()

# --- 4. МЕНЮ И КЛАВИАТУРЫ ---
def get_main_keyboard(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    credits_left = get_user_credits(user_id)
    
    btn1 = types.InlineKeyboardButton("🔥 Факты / Топы", callback_data="genre_facts")
    btn2 = types.InlineKeyboardButton("💡 Экспертный / Польза", callback_data="genre_expert")
    btn3 = types.InlineKeyboardButton("😱 Мистика / Истории", callback_data="genre_story")
    btn4 = types.InlineKeyboardButton("💰 Деньги / Бизнес", callback_data="genre_business")
    btn_buy = types.InlineKeyboardButton(f"⭐ Купить 20 генераций (Баланс: {credits_left})", callback_data="buy_credits")
    btn_rules = types.InlineKeyboardButton("📄 Правила использования", url="https://telegra.ph")
    
    markup.add(btn1, btn2)
    markup.add(btn3, btn4)
    markup.add(btn_buy)
    markup.add(btn_rules)
    return markup

# --- 5. АДМИН-КОМАНДЫ (ТОЛЬКО ДЛЯ ВАС И ВЛАДЕЛЬЦА) ---

# 📊 Команда /stats — Отчет по продажам
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
        f"👥 Пользователей всего: **{total_users} чел.**\n"
        f"💳 Платящих клиентов: **{paying_users} чел.**\n"
        f"🛍 Успешных покупок: **{total_payments} шт.**\n\n"
        f"⭐ **СУММАРНАЯ ВЫРУЧКА:** **{total_revenue_stars} Stars**\n\n"
        "*(Отчет доступен только разработчику и владельцу)*"
    )
    bot.send_message(message.chat.id, stats_text, parse_mode="Markdown")

# ➕ Команда /add — Самостоятельное добавление попыток
@bot.message_handler(commands=['add'])
def handle_add_credits(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    # Можно написать просто /add (добавит 10), а можно /add 50
    parts = message.text.split()
    amount = 10
    if len(parts) > 1 and parts[1].isdigit():
        amount = int(parts[1])
        
    update_credits(message.from_user.id, amount)
    new_balance = get_user_credits(message.from_user.id)
    bot.reply_to(message, f"👑 **Успешно начислено +{amount} попыток!**\nВаш текущий баланс: **{new_balance}**.", parse_mode="Markdown")

# --- 6. КОМАНДА /START ДЛЯ ВСЕХ ---
@bot.message_handler(commands=['start'])
def handle_start(message):
    try:
        user_id = message.from_user.id
        first_name = message.from_user.first_name.replace("_", " ").replace("*", "")
        username = message.from_user.username or ""
        
        credits_left = get_user_credits(user_id, username)
        
        welcome_text = (
            f"👋 **Привет, {first_name}!**\n\n"
            "Я — твой ИИ-продюсер вирусных роликов для **Shorts, Reels и TikTok**.\n\n"
            f"🎁 Твой баланс: **{credits_left} генераций**.\n\n"
            "Выбери категорию ролика ниже:"
        )
        bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")
    except Exception as e:
        print(f"Ошибка в start: {e}")

# --- 7. ВЫСТАВЛЕНИЕ СЧЕТА И ОПЛАТА ЗВЕЗДАМИ ---
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id
    
    if call.data.startswith("genre_"):
        genre = call.data.replace("genre_", "")
        user_state[user_id] = {"stage": "waiting_theme", "genre": genre}
        bot.send_message(call.message.chat.id, "✍️ **Напиши тему для ролика одним сообщением.**\n\nНапример: *«3 вещи, разрушающие мозг»*.", parse_mode="Markdown")
    
    elif call.data == "buy_credits":
        prices = [types.LabeledPrice(label="20 сценариев для Reels/Shorts", amount=50)]
        bot.send_invoice(
            chat_id=call.message.chat.id,
            title="Пакет: 20 сценариев Shorts/Reels",
            description="Пополнение баланса на 20 полных сценариев с промптами для визуализаций и тегами.",
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
    username = message.from_user.username or message.from_user.first_name
    stars_amount = message.successful_payment.total_amount
    
    # 1. Начисляем пользователю +20 генераций и сохраняем в базу
    update_credits(user_id, 20)
    log_payment(user_id, username, stars_amount)
    
    # 2. Пишем радостное сообщение покупателю
    bot.send_message(
        message.chat.id,
        "🎉 **Оплата 50 звёзд успешно прошла!**\n\nВам начислено +20 генераций.",
        reply_markup=get_main_keyboard(user_id),
        parse_mode="Markdown"
    )
    
    # 3. 🔔 Мгновенно отправляем уведомление ВАМ и ВЛАДЕЛЬЦУ:
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

# --- 8. ГЕНЕРАЦИЯ СЦЕНАРИЯ С НЕЙРОСЕТЬЮ ---
@bot.message_handler(func=lambda message: True)
def handle_user_text(message):
    user_id = message.from_user.id
    current_credits = get_user_credits(user_id)
    
    if current_credits <= 0:
        bot.reply_to(
            message,
            "⛔ **Бесплатные генерации закончились.**\n\nПополните баланс кнопкой ниже, чтобы продолжить создавать вирусные сценарии.",
            reply_markup=get_main_keyboard(user_id),
            parse_mode="Markdown"
        )
        return
    
    state = user_state.get(user_id, {})
    genre = state.get("genre", "произвольный")
    theme = message.text
    
    status_msg = bot.reply_to(message, "⏳ *ИИ анализирует тренды и пишет сценарий... (10-15 сек)*", parse_mode="Markdown")
    
    system_prompt = (
        "Ты — профессиональный продюсер вирусных коротких видео (YouTube Shorts, Instagram Reels, TikTok). "
        "Твоя задача — создать сценарий с удержанием 100%. "
        "Структура ответа строго следующая:\n"
        "1. 🎯 ВИРУСНЫЙ ХУК (первые 3 секунды: визуальный и текстовый триггер, интрига).\n"
        "2. 📜 СЦЕНАРИЙ (до 45 секунд, разбит по секундам [0-5], [5-15] и т.д., живой дикторский текст).\n"
        "3. 🎨 ВИЗУАЛЬНЫЙ РЯД (покадровое описание + детальный промпт на английском для Midjourney/FLUX).\n"
        "4. 🎵 РЕКОМЕНДАЦИЯ ПО МУЗЫКЕ (темп, жанр).\n"
        "5. 🏷 ХЭШТЕГИ (5-7 трендовых тегов)."
    )
    
    user_prompt = f"Формат/Жанр: {genre}. Тема ролика: {theme}."
    
    try:
        response = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.7
        )
        
        update_credits(user_id, -1)
        result_text = response.choices[0].message.content
        
        bot.delete_message(message.chat.id, status_msg.message_id)
        bot.send_message(message.chat.id, result_text)
        bot.send_message(
            message.chat.id,
            f"✅ Готово! Списана 1 генерация. Осталось на балансе: **{current_credits - 1}**.",
            reply_markup=get_main_keyboard(user_id),
            parse_mode="Markdown"
        )
        user_state[user_id] = {}
        
    except Exception as e:
        bot.edit_message_text(f"❌ Ошибка генерации: {e}", message.chat.id, status_msg.message_id)

# --- 9. ЗАПУСК БОТА ---
if __name__ == "__main__":
    bot.infinity_polling()
