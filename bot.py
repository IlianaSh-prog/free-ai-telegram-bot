import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import telebot
from telebot import types
from openai import OpenAI

# --- 1. ВЕБ-СЕРВЕР ДЛЯ ПОДДЕРЖАНИЯ СТАТУСА НА RENDER ---
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

# --- 2. ИНИЦИАЛИЗАЦИЯ КЛИЕНТОВ ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
PROXYAPI_KEY = os.environ.get("PROXYAPI_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = OpenAI(
    api_key=PROXYAPI_KEY,
    base_url="https://api.proxyapi.ru/v1"
)

# Простейшее хранилище лимитов пользователей в памяти (user_id: количество генераций)
# Каждому новому пользователю даем 2 бесплатные генерации
user_balance = {}
user_state = {}

FREE_LIMIT = 2
PACKAGE_PRICE_STARS = 50  # Стоимость пакета: 50 звезд Telegram
PACKAGE_CREDITS = 20      # Количество генераций в пакете

def get_credits(user_id):
    if user_id not in user_balance:
        user_balance[user_id] = FREE_LIMIT
    return user_balance[user_id]

# --- 3. КЛАВИАТУРЫ И МЕНЮ ---
def get_main_keyboard(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    credits_left = get_credits(user_id)
    
    btn1 = types.InlineKeyboardButton("🔥 Факты / Топы", callback_data="genre_facts")
    btn2 = types.InlineKeyboardButton("💡 Экспертный / Польза", callback_data="genre_expert")
    btn3 = types.InlineKeyboardButton("😱 Мистика / Истории", callback_data="genre_story")
    btn4 = types.InlineKeyboardButton("💰 Деньги / Бизнес", callback_data="genre_business")
    btn_buy = types.InlineKeyboardButton(f"⭐ Купить 20 генераций (Баланс: {credits_left})", callback_data="buy_credits")
    
    markup.add(btn1, btn2)
    markup.add(btn3, btn4)
    markup.add(btn_buy)
    return markup

# --- 4. ОБРАБОТЧИКИ КОМАНД ---
@bot.message_handler(commands=['start'])
def handle_start(message):
    user_id = message.from_user.id
    credits_left = get_credits(user_id)
    welcome_text = (
        f"👋 **Привет, {message.from_user.first_name}!**\n\n"
        "Я — ИИ-генератор сценариев и визуальных концепций для **Shorts, Reels и TikTok**.\n\n"
        "Я умею:\n"
        "• Придумывать вирусные крючки (хуки) первых 3 секунд;\n"
        "• Писать сценарии с высоким удержанием;\n"
        "• Составлять промпты для генерации кадров в Midjourney/FLUX;\n"
        "• Подбирать хэштеги под тренды.\n\n"
        f"🎁 Твой доступный баланс: **{credits_left} генераций**.\n\n"
        "Выбери категорию ролика ниже:"
    )
    bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")

# --- 5. ОБРАБОТКА НАЖАТИЙ КНОПОК ---
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id
    
    if call.data.startswith("genre_"):
        genre = call.data.replace("genre_", "")
        user_state[user_id] = {"stage": "waiting_theme", "genre": genre}
        
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="✍️ **Напиши тему для ролика одним сообщением.**\n\n"
                 "Например: *«3 вещи, которые убивают молодость»* или *«Неожиданный факт про глубины океана»*.",
            parse_mode="Markdown"
        )
    
    elif call.data == "buy_credits":
        # Формирование инвойса на оплату звездами (Telegram Stars)
        prices = [types.LabeledPrice(label="20 генераций Shorts", amount=PACKAGE_PRICE_STARS)]
        bot.send_invoice(
            chat_id=call.message.chat.id,
            title="Пакет: 20 сценариев для Shorts/Reels",
            description="Пополнение баланса на 20 полных сценариев с промптами для визуализаций и хэштегами.",
            invoice_payload="credits_pack_20",
            provider_token="",  # Для Telegram Stars оставляется пустым!
            currency="XTR",     # XTR — официальный код Telegram Stars
            prices=prices,
            start_parameter="buy-shorts-pack"
        )

# --- 6. ОБРАБОТКА ОПЛАТЫ ЗВЕЗДАМИ ---
@bot.pre_checkout_query_handler(func=lambda query: True)
def process_pre_checkout_query(pre_checkout_query):
    # Подтверждаем готовность принять платёж
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def process_successful_payment(message):
    user_id = message.from_user.id
    # Начисляем пакет генераций
    current = get_credits(user_id)
    user_balance[user_id] = current + PACKAGE_CREDITS
    
    bot.send_message(
        message.chat.id,
        f"🎉 **Оплата успешно завершена!**\n\nВам начислено +{PACKAGE_CREDITS} генераций.\n"
        f"Текущий баланс: **{user_balance[user_id]} генераций**.",
        reply_markup=get_main_keyboard(user_id),
        parse_mode="Markdown"
    )

# --- 7. ГЕНЕРАЦИЯ СЦЕНАРИЯ ЧЕРЕЗ OPENAI ---
@bot.message_handler(func=lambda message: True)
def handle_user_text(message):
    user_id = message.from_user.id
    current_credits = get_credits(user_id)
    
    if current_credits <= 0:
        bot.reply_to(
            message,
            "⛔ **У вас закончились бесплатные генерации.**\n\n"
            "Пополните баланс звездами Telegram, чтобы продолжить создавать вирусный контент.",
            reply_markup=get_main_keyboard(user_id),
            parse_mode="Markdown"
        )
        return
    
    state = user_state.get(user_id, {})
    genre = state.get("genre", "произвольный")
    theme = message.text
    
    status_msg = bot.reply_to(message, "⏳ *ИИ анализирует тренды и генерирует ролик... Подождите 10-15 секунд.*", parse_mode="Markdown")
    
    system_prompt = (
        "Ты — профессиональный продюсер вирусных коротких видео (YouTube Shorts, Instagram Reels, TikTok). "
        "Твоя цель — создать сценарий с максимальным удержанием аудитории. "
        "Структура ответа строго следующая:\n"
        "1. 🎯 ВИРУСНЫЙ ХУК (первые 3 секунды: визуальный и текстовый триггер, интрига).\n"
        "2. 📜 СЦЕНАРИЙ (до 45 секунд, разбит по секундам: [0-5 сек], [5-15 сек] и т.д. Текст для диктора простой, динамичный).\n"
        "3. 🎨 ВИЗУАЛЬНЫЙ РЯД (покадровое описание видеоряда + точный промпт на английском для генерации картинки к каждой ключевой сцене в Midjourney/FLUX).\n"
        "4. 🎵 РЕКОМЕНДАЦИЯ ПО МУЗЫКЕ (темп, настроение, тип трека).\n"
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
        
        # Списываем 1 генерацию
        user_balance[user_id] = current_credits - 1
        result_text = response.choices[0].message.content
        
        bot.delete_message(message.chat.id, status_msg.message_id)
        bot.send_message(message.chat.id, result_text)
        
        # Напоминаем про баланс и меню
        bot.send_message(
            message.chat.id,
            f"✅ Готово! Списано: 1 генерация. Осталось на балансе: **{user_balance[user_id]}**.",
            reply_markup=get_main_keyboard(user_id),
            parse_mode="Markdown"
        )
        
        # Сбрасываем стейт ожидания
        user_state[user_id] = {}
        
    except Exception as e:
        bot.edit_message_text(f"❌ Ошибка генерации: {e}", message.chat.id, status_msg.message_id)

# --- 8. ЗАПУСК БОТА ---
if __name__ == "__main__":
    bot.infinity_polling()
