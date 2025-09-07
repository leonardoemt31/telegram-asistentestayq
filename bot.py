from telegram.ext import Updater, CommandHandler, MessageHandler, Filters

import os

TOKEN = os.getenv("TOKEN")  # el token se guardará en Railway como variable de entorno

def start(update, context):
    update.message.reply_text("👋 Hola, soy tu asistente en Telegram. Escribe tus pendientes y los guardaré.")

def echo(update, context):
    pendiente = update.message.text
    with open("pendientes.txt", "a") as f:
        f.write(pendiente + "\n")
    update.message.reply_text(f"✅ Pendiente guardado: {pendiente}")

updater = Updater(TOKEN, use_context=True)
dp = updater.dispatcher

dp.add_handler(CommandHandler("start", start))
dp.add_handler(MessageHandler(Filters.text & ~Filters.command, echo))

updater.start_polling()
updater.idle()
