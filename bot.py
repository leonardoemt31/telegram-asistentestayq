# bot.py
import os
import io
from datetime import datetime, timedelta
import pytz
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
from telegram import ParseMode
from apscheduler.schedulers.background import BackgroundScheduler
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# -------- CONFIG ----------
TOKEN = os.getenv("TOKEN")  # Token de BotFather
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # Tu chat id (opcional: si no, el bot enviará al chat que interactúe)
TZ = os.getenv("TIMEZONE", "America/Bogota")  # zona horaria; default a la tuya
REMINDER_LEAD_MINUTES = int(os.getenv("REMINDER_LEAD_MINUTES", "0"))  # enviar recordatorio X minutos antes
# --------------------------

if not TOKEN:
    raise Exception("Falta la variable de entorno TOKEN")

tz = pytz.timezone(TZ)

# --------- DB (SQLite local) ----------
engine = create_engine('sqlite:///tasks.db', connect_args={"check_same_thread": False})
Base = declarative_base()
Session = sessionmaker(bind=engine)
session = Session()

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String(64))  # quien creó la tarea (chat)
    title = Column(String(255))
    description = Column(Text, nullable=True)
    due = Column(DateTime, nullable=True)  # en UTC (almacenamos en UTC)
    created_at = Column(DateTime, default=lambda: datetime.utcnow())
    completed = Column(Boolean, default=False)
    completed_at = Column(DateTime, nullable=True)
    reminders_sent = Column(Integer, default=0)  # contador simple

Base.metadata.create_all(engine)

# --------- Bot ----------
updater = Updater(TOKEN, use_context=True)
dp = updater.dispatcher
bot = updater.bot

# ---------- Helpers ----------
def parse_datetime(text):
    """
    Espera formato 'YYYY-MM-DD HH:MM' en la zona horaria TZ.
    Devuelve objeto datetime en UTC.
    """
    text = text.strip()
    try:
        # soporta si solo ponen fecha 'YYYY-MM-DD' -> lo deja a las 09:00 local
        if len(text) == 10:
            local = tz.localize(datetime.strptime(text, "%Y-%m-%d").replace(hour=9, minute=0))
        else:
            local = tz.localize(datetime.strptime(text, "%Y-%m-%d %H:%M"))
        return local.astimezone(pytz.utc)
    except Exception:
        return None

def format_dt_for_user(dt_utc):
    if not dt_utc:
        return "-"
    local = dt_utc.astimezone(tz)
    return local.strftime("%Y-%m-%d %H:%M")

# ---------- Commands ----------
def cmd_start(update, context):
    update.message.reply_text(
        "Hola 👋 soy tu asistente de pendientes.\n\n"
        "Comandos:\n"
        "/add <titulo> | <descripcion opcional> | <YYYY-MM-DD HH:MM opcional>\n"
        "    Ejemplo: /add Revisar aire Crespo | llevar repuestos | 2025-09-07 10:00\n"
        "/listar  -> lista tus tareas\n"
        "/hecho <id> -> marca tarea como realizada\n"
        "/reporte <YYYY> <MM> -> te envío PDF del mes\n"
    )

def cmd_add(update, context):
    chat_id = str(update.effective_chat.id)
    text = " ".join(context.args)
    if not text:
        update.message.reply_text("Uso: /add <titulo> | <descripcion opcional> | <YYYY-MM-DD HH:MM opcional>")
        return
    parts = [p.strip() for p in text.split("|")]
    title = parts[0]
    description = parts[1] if len(parts) > 1 else ""
    due = None
    if len(parts) > 2 and parts[2]:
        due = parse_datetime(parts[2])
        if due is None:
            update.message.reply_text("Fecha inválida. Usa formato YYYY-MM-DD HH:MM (ej: 2025-09-07 10:00) o solo YYYY-MM-DD")
            return
    t = Task(chat_id=chat_id, title=title, description=description, due=due)
    session.add(t)
    session.commit()
    msg = f"✅ Pendiente creado (id {t.id}): *{title}*\nDue: {format_dt_for_user(due)}"
    update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

def cmd_listar(update, context):
    chat_id = str(update.effective_chat.id)
    tasks = session.query(Task).filter(Task.chat_id == chat_id).order_by(Task.due.is_(None), Task.due).all()
    if not tasks:
        update.message.reply_text("No tienes pendientes.")
        return
    lines = []
    for t in tasks:
        status = "✅" if t.completed else "⏳"
        due = format_dt_for_user(t.due)
        lines.append(f"{t.id}. {status} *{t.title}* — {due}")
    update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

def cmd_hecho(update, context):
    chat_id = str(update.effective_chat.id)
    if not context.args:
        update.message.reply_text("Uso: /hecho <id>")
        return
    try:
        tid = int(context.args[0])
    except ValueError:
        update.message.reply_text("Id inválido.")
        return
    t = session.query(Task).filter(Task.id == tid, Task.chat_id == chat_id).first()
    if not t:
        update.message.reply_text("Tarea no encontrada (verifica id).")
        return
    if t.completed:
        update.message.reply_text("Esa tarea ya estaba marcada como hecha.")
        return
    t.completed = True
    t.completed_at = datetime.utcnow()
    session.commit()
    update.message.reply_text(f"✅ Marcada como hecha: {t.id}. {t.title}")

def cmd_reporte(update, context):
    # Uso: /reporte 2025 9
    if len(context.args) < 2:
        update.message.reply_text("Uso: /reporte <YYYY> <MM> (ej: /reporte 2025 9)")
        return
    try:
        year = int(context.args[0])
        month = int(context.args[1])
    except:
        update.message.reply_text("Año o mes inválido.")
        return
    chat_id = str(update.effective_chat.id)
    send_monthly_pdf(chat_id, year, month)

# ---------- Scheduler job ----------
def check_due_and_send():
    now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    # selecciona tareas no completadas con due definido y reminders_sent == 0
    tasks = session.query(Task).filter(Task.completed == False, Task.due != None).all()
    for t in tasks:
        # enviar si estamos dentro del rango: due - lead <= now <= due + 3600s (evita enviar cosas muy viejas)
        lead = timedelta(minutes=REMINDER_LEAD_MINUTES)
        due = t.due.replace(tzinfo=pytz.utc)
        if (due - lead) <= now_utc <= (due + timedelta(hours=1)):
            # enviar recordatorio
            try:
                target_chat = int(t.chat_id) if t.chat_id else (int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else None)
                if not target_chat:
                    continue
                text = f"⏰ *Recordatorio* (id {t.id}): *{t.title}*\nDue: {format_dt_for_user(t.due)}\n\nUsa /hecho {t.id} si ya lo completaste."
                bot.send_message(chat_id=target_chat, text=text, parse_mode=ParseMode.MARKDOWN)
                t.reminders_sent += 1
                session.commit()
            except Exception as e:
                print("Error enviando recordatorio:", e)

# ---------- PDF generation ----------
def build_pdf_bytes(tasks, year, month):
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    w, h = letter
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, h - 40, f"Reporte de pendientes - {year}-{month:02d}")
    c.setFont("Helvetica", 10)
    y = h - 70
    c.drawString(40, y, "ID | Título | Estado | Due (hora local) | Creado")
    y -= 18
    for t in tasks:
        status = "Hecho" if t.completed else ("Atrasado" if (t.due and t.due < datetime.utcnow()) else "Pendiente")
        due_s = format_dt_for_user(t.due)
        created_s = (t.created_at.strftime("%Y-%m-%d") if t.created_at else "-")
        line = f"{t.id} | {t.title} | {status} | {due_s} | {created_s}"
        c.drawString(40, y, line[:110])
        y -= 14
        if y < 60:
            c.showPage()
            y = h - 40
    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer

def send_monthly_pdf(chat_id, year, month):
    # rango de fechas para el mes
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)
    # buscamos por created_at en UTC
    tasks = session.query(Task).filter(Task.created_at >= start, Task.created_at < end).order_by(Task.created_at).all()
    pdf_bytes = build_pdf_bytes(tasks, year, month)
    pdf_bytes.name = f"reporte_{year}_{month:02d}.pdf"
    bot.send_document(chat_id=chat_id, document=pdf_bytes, filename=pdf_bytes.name)

# ---------- Handlers ----------
dp.add_handler(CommandHandler("start", cmd_start))
dp.add_handler(CommandHandler("add", cmd_add))
dp.add_handler(CommandHandler("listar", cmd_listar))
dp.add_handler(CommandHandler("hecho", cmd_hecho))
dp.add_handler(CommandHandler("reporte", cmd_reporte))

# ---------- Scheduler ----------
scheduler = BackgroundScheduler()
scheduler.add_job(check_due_and_send, "interval", minutes=1)  # revisa cada minuto
scheduler.start()

# ---------- Run bot ----------
if __name__ == "__main__":
    print("Bot iniciado...")
    updater.start_polling()
    updater.idle()
