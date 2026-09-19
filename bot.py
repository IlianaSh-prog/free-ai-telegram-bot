import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import telebot
from openai import OpenAI

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

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
PROXYAPI_KEY = os.environ.get("PROXYAPI_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = OpenAI(
    api_key=PROXYAPI_KEY,
    base_url="https://api.proxyapi.ru/v1"
)

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "Привет! Я твой ИИ-помощник. Чем помочь?")

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    try:
        response = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты вежливый помощник. Отвечай кратко и понятно на русском языке."},
                {"role": "user", "content": message.text}
            ]
        )
        bot.reply_to(message, response.choices[0].message.content)
    except Exception as e:
        bot.reply_to(message, f"Ошибка: {e}")

if __name__ == "__main__":
    bot.infinity_polling()
