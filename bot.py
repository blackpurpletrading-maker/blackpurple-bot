import os
import json
import logging
import re
import io
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import tempfile
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import anthropic
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import imaplib
import email as email_lib
from email.header import decode_header
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
import requests

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
GMAIL_USER = os.environ.get('GMAIL_USER')
GMAIL_PASSWORD = os.environ.get('GMAIL_PASSWORD')
ELEVENLABS_API_KEY = os.environ.get('ELEVENLABS_API_KEY')
AUTHORIZED_USER_ID = int(os.environ.get('AUTHORIZED_USER_ID', 0))
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.environ.get('TWILIO_WHATSAPP_NUMBER', 'whatsapp:+14155238886')
YOUR_WHATSAPP_NUMBER = os.environ.get('YOUR_WHATSAPP_NUMBER', 'whatsapp:+27671032999')

JARVIS_VOICE_ID = "pNInz6obpgDQGcFmaJgB"
GOSEGO_EMAIL = "Gosego.Masiane@pepsico.com"
SHAUN_EMAIL = "Shaun.Jacobs@pepsico.com"
INVOICE_EMAIL = "SA.invoices@pepsico.com"
MY_EMAIL = "blackpurple.trading@gmail.com"
SA_TZ = pytz.timezone('Africa/Johannesburg')

COMPANY = {
    "name": "BlackPurple (PTY) LTD",
    "address": "1704 Mothotlung, Brits",
    "reg": "2018/534192/07",
    "tax": "9046960267 0250",
    "cell": "079 076 9253 / 073 289 5865",
    "email": "info@blackpurple.co.za",
    "vat": "4420309116",
    "bank": "STANDARD BANK",
    "account": "060645377",
    "account_type": "BUSINESS CURRENT ACCOUNT",
    "branch": "052546",
}

CLIENT = {
    "name": "Pioneer Foods (Pty) Ltd",
    "address": "PO Box 4091\nTyger Valley\n7536\nSouth Africa",
    "vat": "4610103865",
    "reg": "1957/000634/07",
}

WEEKDAY_RATE = 0.80
WEEKEND_RATE = 0.95
LITERS_PER_LOAD = 10000
VAT_RATE = 0.15
STATE_FILE = "state.json"
LETTERHEAD_PATH = os.path.join(os.path.dirname(__file__), 'letterhead.jpg')


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "last_ref_number": 359,
        "pending_quote": None,
        "pending_invoice": None,
        "last_quote_data": None,
        "invoices": [],
        "quotes": [],
        "authorized_user": AUTHORIZED_USER_ID,
        "conversation_history": [],
        "pending_shaun_email": [],
        "recent_emails": [],
        "pending_email_reply": None,
        "pending_po": None,
    }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def next_ref(state):
    state["last_ref_number"] += 1
    num = state["last_ref_number"]
    save_state(state)
    return f"BPT25{str(num).zfill(4)}"


def is_weekend_or_holiday(date):
    return date.weekday() >= 5


def calculate_amount(loads, date):
    liters = loads * LITERS_PER_LOAD
    rate = WEEKEND_RATE if is_weekend_or_holiday(date) else WEEKDAY_RATE
    subtotal = liters * rate
    vat = subtotal * VAT_RATE
    total = subtotal + vat
    return liters, rate, subtotal, vat, total


def send_whatsapp_message(to_number, message):
    try:
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(body=message, from_=TWILIO_WHATSAPP_NUMBER, to=to_number)
        return True
    except Exception as e:
        logger.error(f"WhatsApp send error: {e}")
        return False


def handle_whatsapp_message(from_number, message_body):
    state = load_state()
    text = message_body.strip()
    text_lower = text.lower()

    if state.get("pending_quote") and text_lower in ["approved", "approve", "yes", "send it", "send"]:
        q = state["pending_quote"]
        date = datetime.fromisoformat(q["date"])
        body = (
            f"Dear Gosego,\n\nPlease find attached our quote.\n\n"
            f"Quote REF: {q['ref']}\nDate: {date.strftime('%d %B %Y')}\n"
            f"Quantity: {q['liters']:,.0f} Litres\nTotal: R{q['total']:,.2f} (VAT incl.)\n\n"
            f"Kind regards,\nBlackPurple (PTY) LTD"
        )
        success = send_email(GOSEGO_EMAIL, f"Quote {q['ref']} - Supply of Water - BlackPurple", body, [q['pdf_path']])
        quotes = state.get("quotes", [])
        quotes.append(q)
        state["quotes"] = quotes
        state["pending_quote"] = None
        save_state(state)
        return f"✅ Quote {q['ref']} sent to Gosego!" if success else "❌ Failed to send."

    if state.get("pending_invoice") and text_lower in ["approved", "approve", "yes", "send it", "send"]:
        inv = state["pending_invoice"]
        date = datetime.fromisoformat(inv["date"])
        body = (
            f"Dear Gosego,\n\nInvoice REF: {inv['ref']}\nPO: {inv['po_number']}\n"
            f"Total: R{inv['total']:,.2f}\n\nKind regards,\nBlackPurple (PTY) LTD"
        )
        success = send_email(INVOICE_EMAIL, f"Invoice {inv['ref']} - BlackPurple", body, [inv["pdf_path"]], cc_email=GOSEGO_EMAIL)
        state["invoices"].append({"ref": inv["ref"], "po_number": inv["po_number"], "date": inv["date"], "total": inv["total"], "pdf_path": inv["pdf_path"], "paid": False})
        state["pending_invoice"] = None
        save_state(state)
        return f"✅ Invoice {inv['ref']} sent!" if success else "❌ Failed to send."

    if text_lower in ["emails", "check emails"]:
        emails = get_emails(limit=3)
        if not emails:
            return "No emails found."
        state["recent_emails"] = emails
        save_state(state)
        response = "📧 Your Latest Emails:\n\n"
        for i, em in enumerate(emails, 1):
            sender = em["sender"].split("<")[0].strip()
            response += f"{i}. From: {sender}\nSubject: {em['subject']}\n\n"
        return response

    if text_lower in ["quotes", "pending quotes"]:
        quotes = [q for q in state.get("quotes", []) if not q.get("invoiced")]
        if not quotes:
            return "No pending quotes! ✅"
        msg = f"📄 Pending Quotes ({len(quotes)}):\n\n"
        for q in quotes:
            date = datetime.fromisoformat(q["date"]).strftime('%d %b %Y')
            msg += f"• {q['ref']} — R{q['total']:,.2f} — {date}\n"
        return msg

    if text_lower in ["invoices", "unpaid invoices"]:
        unpaid = [inv for inv in state.get("invoices", []) if not inv.get("paid")]
        if not unpaid:
            return "All invoices are paid! ✅"
        total = sum(inv["total"] for inv in unpaid)
        msg = f"💰 Unpaid Invoices ({len(unpaid)}):\n\n"
        for inv in unpaid:
            msg += f"• {inv['ref']} — R{inv['total']:,.2f}\n"
        msg += f"\nTotal: R{total:,.2f}"
        return msg

    if text_lower in ["hi", "hello", "hey", "start"]:
        return (
            "🤖 Good day! I'm *Jarvis*, your BlackPurple assistant.\n\n"
            "Commands:\n"
            "• *emails* - Check emails\n"
            "• *quotes* - Pending quotes\n"
            "• *invoices* - Unpaid invoices\n\n"
            "Or send loads like: _6 loads 09/04/2026_"
        )

    date, loads = parse_loads_message(text)
    if date and loads:
        liters, rate, subtotal, vat, total = calculate_amount(loads, date)
        ref = next_ref(state)
        pdf_path = generate_pdf("Quote", ref, None, date, loads, liters, rate, subtotal, vat, total)
        quote_data = {
            "ref": ref, "date": date.isoformat(), "loads": loads,
            "liters": liters, "rate": rate, "subtotal": subtotal,
            "vat": vat, "total": total, "pdf_path": pdf_path, "invoiced": False,
        }
        state["pending_quote"] = quote_data
        state["last_quote_data"] = quote_data
        save_state(state)
        rate_type = "Weekend" if rate == WEEKEND_RATE else "Weekday"
        return (
            f"📄 Quote Ready!\n\n"
            f"Date: {date.strftime('%d %B %Y')}\n"
            f"Loads: {loads} ({liters:,.0f} Ltrs)\n"
            f"Rate: R{rate:.2f}/L ({rate_type})\n"
            f"Subtotal: R{subtotal:,.2f}\n"
            f"VAT: R{vat:,.2f}\n"
            f"TOTAL: R{total:,.2f}\n"
            f"Ref: {ref}\n\n"
            f"Reply APPROVED to send to Gosego."
        )

    history = state.get("conversation_history", [])
    response = ask_claude(text, history)
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": response})
    if len(history) > 20:
        history = history[-20:]
    state["conversation_history"] = history
    save_state(state)
    return response


def generate_pdf(doc_type, ref, po_number, date, loads, liters, rate, subtotal, vat, total):
    filename = tempfile.mktemp(suffix='.pdf')
    c = canvas.Canvas(filename, pagesize=A4)
    width, height = A4
    header_height = 38*mm
    if os.path.exists(LETTERHEAD_PATH):
        c.drawImage(LETTERHEAD_PATH, 0, height - header_height, width=width, height=header_height)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 16)
    c.drawRightString(width - 15*mm, height - 55*mm, doc_type)
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(width - 15*mm, height - 63*mm, f"{doc_type} REF: {ref}")
    if po_number:
        c.drawRightString(width - 15*mm, height - 70*mm, f"PO no: {po_number}")
        c.drawRightString(width - 15*mm, height - 77*mm, date.strftime("%d %B %Y"))
    else:
        c.drawRightString(width - 15*mm, height - 70*mm, date.strftime("%d %B %Y"))
    c.setFont("Helvetica-Bold", 10)
    c.drawString(15*mm, height - 55*mm, f"{doc_type} TO:")
    c.setFont("Helvetica", 9)
    y = height - 63*mm
    c.drawString(15*mm, y, CLIENT['name'])
    y -= 5*mm
    for line in CLIENT['address'].split('\n'):
        c.drawString(15*mm, y, line)
        y -= 5*mm
    c.drawString(15*mm, y, f"Vat No.: {CLIENT['vat']}")
    y -= 7*mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(15*mm, y, f"COMPANY REG NO: {CLIENT['reg']}")
    y -= 8*mm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(15*mm, y, "RE: Supply of Water")
    margin = 15*mm
    table_width = width - 2*margin
    table_top = height - 155*mm
    row_h = 8*mm
    col_right = width - margin
    col_item = margin
    col_desc = margin + 12*mm
    col_qty = margin + 100*mm
    col_unit = margin + 128*mm
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.5)
    c.setFillColor(colors.white)
    c.rect(margin, table_top, table_width, row_h, fill=1, stroke=1)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(col_item + 1*mm, table_top + 2.5*mm, "Item")
    c.drawString(col_desc + 1*mm, table_top + 2.5*mm, "Description")
    c.drawString(col_qty + 1*mm, table_top + 2.5*mm, "Quantity")
    c.drawString(col_unit + 1*mm, table_top + 2.5*mm, "Unit Price")
    c.drawRightString(col_right - 1*mm, table_top + 2.5*mm, "Total Price")
    row_y = table_top - row_h
    c.setFillColor(colors.white)
    c.rect(margin, row_y, table_width, row_h, fill=1, stroke=1)
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 9)
    c.drawString(col_item + 1*mm, row_y + 2.5*mm, "1")
    c.drawString(col_desc + 1*mm, row_y + 2.5*mm, f"Supply of Water ({date.strftime('%d %B %Y')})")
    c.drawString(col_qty + 1*mm, row_y + 2.5*mm, f"{liters:,.0f} Ltrs")
    c.drawString(col_unit + 1*mm, row_y + 2.5*mm, f"R{rate:.2f}")
    c.drawRightString(col_right - 1*mm, row_y + 2.5*mm, f"R{subtotal:,.2f}")
    totals_y = row_y - 10*mm
    label_x = col_unit + 1*mm
    c.setFont("Helvetica", 9)
    c.drawString(label_x, totals_y, "Sub-total")
    c.drawRightString(col_right - 1*mm, totals_y, f"R{subtotal:,.2f}")
    totals_y -= 6*mm
    c.setFillColor(colors.blue)
    c.drawString(label_x, totals_y, "VAT@15%")
    c.setFillColor(colors.black)
    c.drawRightString(col_right - 1*mm, totals_y, f"R{vat:,.2f}")
    totals_y -= 6*mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(label_x, totals_y, "Grand Total")
    c.drawRightString(col_right - 1*mm, totals_y, f"R{total:,.2f}")
    terms_y = totals_y - 25*mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(15*mm, terms_y, "Terms and Conditions:")
    c.setFont("Helvetica", 9)
    c.drawString(15*mm, terms_y - 6*mm, "Valid for 30 Days. 3-5 working days.")
    c.drawString(15*mm, terms_y - 12*mm, "Goods or Services are subject to prior sales.")
    acc_y = terms_y - 30*mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(15*mm, acc_y, "ACCOUNT DETAILS:")
    acc_details = [
        ("BANK NAME", f": {COMPANY['bank']}"),
        ("ACCOUNT NUMBER", f": {COMPANY['account']}"),
        ("ACCOUNT TYPE", f": {COMPANY['account_type']}"),
        ("BRANCH CODE", f" {COMPANY['branch']}"),
        ("VAT NUMBER.", f" {COMPANY['vat']}"),
    ]
    acc_y -= 6*mm
    for label, value in acc_details:
        c.setFont("Helvetica-Bold", 9)
        c.drawString(15*mm, acc_y, label)
        c.setFont("Helvetica", 9)
        c.drawString(55*mm, acc_y, value)
        acc_y -= 5*mm
    c.save()
    return filename


def send_email(to_email, subject, body, attachment_paths=None, cc_email=None):
    try:
        msg = MIMEMultipart()
        msg['From'] = GMAIL_USER
        msg['To'] = to_email
        if cc_email:
            msg['Cc'] = cc_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        if attachment_paths:
            for path in attachment_paths:
                if path and os.path.exists(path):
                    with open(path, 'rb') as f:
                        part = MIMEBase('application', 'octet-stream')
                        part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(path)}"')
                    msg.attach(part)
        recipients = [to_email]
        if cc_email:
            recipients.append(cc_email)
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, recipients, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        logger.error(f"Email error: {e}")
        return False


def get_emails(limit=3, unread_only=False):
    emails = []
    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com', 993)
        mail.login(GMAIL_USER, GMAIL_PASSWORD)
        for mailbox in ['INBOX', '"[Gmail]/All Mail"']:
            try:
                status, _ = mail.select(mailbox)
                if status == 'OK':
                    break
            except:
                continue
        if unread_only:
            _, messages = mail.search(None, 'UNSEEN')
        else:
            _, messages = mail.search(None, 'ALL')
        if not messages or not messages[0]:
            mail.close()
            mail.logout()
            return []
        message_ids = messages[0].split()
        message_ids = message_ids[-limit:] if len(message_ids) > limit else message_ids
        message_ids = list(reversed(message_ids))
        for msg_id in message_ids:
            _, msg_data = mail.fetch(msg_id, '(RFC822)')
            msg = email_lib.message_from_bytes(msg_data[0][1])
            subject = decode_header(msg['Subject'])[0]
            if isinstance(subject[0], bytes):
                subject = subject[0].decode(subject[1] or 'utf-8', errors='replace')
            else:
                subject = subject[0] or 'No Subject'
            sender = msg.get('From', 'Unknown')
            date_str = msg.get('Date', '')
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == 'text/plain' and 'attachment' not in str(part.get('Content-Disposition', '')):
                        try:
                            body += part.get_payload(decode=True).decode('utf-8', errors='replace')
                        except:
                            pass
            else:
                try:
                    body = msg.get_payload(decode=True).decode('utf-8', errors='replace')
                except:
                    body = ""
            emails.append({'id': msg_id.decode(), 'subject': subject, 'sender': sender, 'date': date_str, 'body': body[:300], 'message_id': msg.get('Message-ID', '')})
        mail.close()
        mail.logout()
    except Exception as e:
        logger.error(f"IMAP error: {e}")
    return emails


def ask_claude(user_message, conversation_history=None):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system = """You are Jarvis, the AI business assistant for BlackPurple (PTY) LTD, a water supply company.
Personality: Professional, efficient, intelligent - like Jarvis from Iron Man.
Company: BlackPurple (PTY) LTD, 1704 Mothotlung, Brits. VAT: 4420309116. Main client: Pioneer Foods / PepsiCo.
Rates: R0.80/L weekdays, R0.95/L weekends/holidays. 10,000L per load.
Bank: Standard Bank, Acc: 060645377, Branch: 052546.
Keep responses concise and professional."""
    messages = (conversation_history or [])[-10:]
    messages.append({"role": "user", "content": user_message})
    response = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=1000, system=system, messages=messages)
    return response.content[0].text


def parse_loads_message(text):
    date_pattern = r'(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})'
    loads_pattern = r'(\d+)\s*loads?'
    date_match = re.search(date_pattern, text)
    loads_match = re.search(loads_pattern, text, re.IGNORECASE)
    if date_match and loads_match:
        date_str = date_match.group(1)
        loads = int(loads_match.group(1))
        date = datetime.now(SA_TZ)
        for fmt in ['%d/%m/%Y', '%d-%m-%Y', '%d.%m.%Y', '%d/%m/%y']:
            try:
                date = datetime.strptime(date_str, fmt)
                break
            except:
                continue
        return date, loads
    return None, None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    user_id = update.effective_user.id
    state["authorized_user"] = AUTHORIZED_USER_ID or user_id
    save_state(state)
    if state.get("authorized_user") == user_id:
        await update.message.reply_text(
            "🤖 Good day! I'm *Jarvis*, your BlackPurple business assistant.\n\n"
            "✅ Quotes & Invoices\n✅ Email reading\n✅ PO tracking\n✅ Business conversations\n\n"
            "Or just talk to me naturally! 🚀",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("Sorry, this bot is private.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    user_id = update.effective_user.id
    state["authorized_user"] = AUTHORIZED_USER_ID or user_id
    if state.get("authorized_user") != user_id:
        await update.message.reply_text("Sorry, this bot is private.")
        return
    text = update.message.text.strip()
    text_lower = text.lower()

    if state.get("pending_quote") and text_lower in ["approved", "approve", "yes", "send it", "send"]:
        q = state["pending_quote"]
        date = datetime.fromisoformat(q["date"])
        body = f"Dear Gosego,\n\nQuote REF: {q['ref']}\nDate: {date.strftime('%d %B %Y')}\nTotal: R{q['total']:,.2f}\n\nKind regards,\nBlackPurple (PTY) LTD"
        success = send_email(GOSEGO_EMAIL, f"Quote {q['ref']} - Supply of Water", body, [q['pdf_path']])
        quotes = state.get("quotes", [])
        quotes.append(q)
        state["quotes"] = quotes
        state["pending_quote"] = None
        save_state(state)
        await update.message.reply_text(f"✅ Quote *{q['ref']}* sent to Gosego!" if success else "❌ Failed to send.", parse_mode='Markdown')
        return

    if state.get("pending_invoice") and text_lower in ["approved", "approve", "yes", "send it", "send"]:
        inv = state["pending_invoice"]
        body = f"Dear Gosego,\n\nInvoice REF: {inv['ref']}\nPO: {inv['po_number']}\nTotal: R{inv['total']:,.2f}\n\nKind regards,\nBlackPurple (PTY) LTD"
        success = send_email(INVOICE_EMAIL, f"Invoice {inv['ref']} - BlackPurple", body, [inv["pdf_path"]], cc_email=GOSEGO_EMAIL)
        state["invoices"].append({"ref": inv["ref"], "po_number": inv["po_number"], "date": inv["date"], "total": inv["total"], "pdf_path": inv["pdf_path"], "paid": False})
        state["pending_invoice"] = None
        save_state(state)
        await update.message.reply_text(f"✅ Invoice *{inv['ref']}* sent!" if success else "❌ Failed.", parse_mode='Markdown')
        return

    if text_lower in ["emails", "check emails"]:
        await update.message.reply_text("📧 Fetching emails...")
        emails = get_emails(limit=3)
        if not emails:
            await update.message.reply_text("No emails found.")
            return
        for i, em in enumerate(emails, 1):
            sender = em["sender"].split("<")[0].strip()
            await update.message.reply_text(f"📧 *Email {i}*\n👤 {sender}\n📌 {em['subject']}", parse_mode="Markdown")
        return

    if text_lower in ["quotes", "pending quotes"]:
        quotes = [q for q in state.get("quotes", []) if not q.get("invoiced")]
        if not quotes:
            await update.message.reply_text("No pending quotes! ✅")
            return
        msg = "📄 *Pending Quotes:*\n\n"
        for q in quotes:
            msg += f"• *{q['ref']}* — R{q['total']:,.2f}\n"
        await update.message.reply_text(msg, parse_mode='Markdown')
        return

    if text_lower in ["invoices", "unpaid invoices"]:
        unpaid = [inv for inv in state.get("invoices", []) if not inv.get("paid")]
        if not unpaid:
            await update.message.reply_text("All paid! ✅")
            return
        total = sum(inv["total"] for inv in unpaid)
        msg = "💰 *Unpaid:*\n\n"
        for inv in unpaid:
            msg += f"• *{inv['ref']}* — R{inv['total']:,.2f}\n"
        msg += f"\n*Total: R{total:,.2f}*"
        await update.message.reply_text(msg, parse_mode='Markdown')
        return

    date, loads = parse_loads_message(text)
    if date and loads:
        liters, rate, subtotal, vat, total = calculate_amount(loads, date)
        ref = next_ref(state)
        pdf_path = generate_pdf("Quote", ref, None, date, loads, liters, rate, subtotal, vat, total)
        quote_data = {"ref": ref, "date": date.isoformat(), "loads": loads, "liters": liters, "rate": rate, "subtotal": subtotal, "vat": vat, "total": total, "pdf_path": pdf_path, "invoiced": False}
        state["pending_quote"] = quote_data
        state["last_quote_data"] = quote_data
        save_state(state)
        rate_type = "Weekend" if rate == WEEKEND_RATE else "Weekday"
        caption = f"📄 *Quote Ready*\n\n📅 {date.strftime('%d %B %Y')}\n💧 {loads} loads ({liters:,.0f} Ltrs)\n💰 R{rate:.2f}/L ({rate_type})\n✅ *Total: R{total:,.2f}*\nRef: {ref}\n\nReply *APPROVED* to send."
        with open(pdf_path, 'rb') as f:
            await update.message.reply_document(document=f, filename=f"Quote_{ref}.pdf", caption=caption, parse_mode='Markdown')
        return

    po_match = re.search(r'\b(44\d{8}|\d{10})\b', text)
    if po_match and state.get("last_quote_data"):
        po_number = po_match.group(1)
        q = state["last_quote_data"]
        date = datetime.fromisoformat(q["date"]) if isinstance(q["date"], str) else q["date"]
        ref = next_ref(state)
        pdf_path = generate_pdf("Invoice", ref, po_number, date, q["loads"], q["liters"], q["rate"], q["subtotal"], q["vat"], q["total"])
        state["pending_invoice"] = {"ref": ref, "po_number": po_number, "date": q["date"], "loads": q["loads"], "liters": q["liters"], "rate": q["rate"], "subtotal": q["subtotal"], "vat": q["vat"], "total": q["total"], "pdf_path": pdf_path}
        save_state(state)
        caption = f"🧾 *Invoice Ready*\n\nPO: {po_number}\n✅ *Total: R{q['total']:,.2f}*\nRef: {ref}\n\nReply *APPROVED* to send."
        with open(pdf_path, 'rb') as f:
            await update.message.reply_document(document=f, filename=f"Invoice_{ref}.pdf", caption=caption, parse_mode='Markdown')
        return

    await update.message.chat.send_action("typing")
    history = state.get("conversation_history", [])
    response = ask_claude(text, history)
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": response})
    if len(history) > 20:
        history = history[-20:]
    state["conversation_history"] = history
    save_state(state)
    await update.message.reply_text(response)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()


async def thursday_check(context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    user_id = state.get("authorized_user") or AUTHORIZED_USER_ID
    if not user_id:
        return
    unpaid = [inv for inv in state.get("invoices", []) if not inv.get("paid")]
    if not unpaid:
        return
    total = sum(inv["total"] for inv in unpaid)
    inv_list = "\n".join([f"• {inv['ref']} - R{inv['total']:,.2f}" for inv in unpaid])
    await context.bot.send_message(chat_id=user_id, text=f"📅 *Thursday Check*\n\n{inv_list}\n\n*Total: R{total:,.2f}*", parse_mode='Markdown')
    send_whatsapp_message(YOUR_WHATSAPP_NUMBER, f"📅 Thursday Check\n{inv_list}\nTotal: R{total:,.2f}")


async def error_handler(update, context):
    logger.error(f"Error: {context.error}")
    if update and update.message:
        try:
            await update.message.reply_text("⚠️ Something went wrong.")
        except:
            pass


class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Jarvis is online!")

    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            params = parse_qs(body)
            path = urlparse(self.path).path

            if path == '/whatsapp':
                from_number = params.get('From', [''])[0]
                message_body = params.get('Body', [''])[0]
                logger.info(f"WhatsApp from {from_number}: {message_body}")
                response_text = handle_whatsapp_message(from_number, message_body)
                send_whatsapp_message(from_number, response_text)
                self.send_response(200)
                self.send_header('Content-Type', 'text/xml')
                self.end_headers()
                self.wfile.write(b'<?xml version="1.0" encoding="UTF-8"?><Response></Response>')
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            self.send_response(500)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def run_web_server():
    port = int(os.environ.get('PORT', 8080))
    server = HTTPServer(('', port), WebhookHandler)
    logger.info(f"Web server on port {port}")
    server.serve_forever()


def main():
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    job_queue = app.job_queue
    job_queue.run_daily(thursday_check, time=datetime.strptime("11:00", "%H:%M").time().replace(tzinfo=SA_TZ))

    logger.info("🤖 Jarvis is online!")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        read_timeout=30,
        write_timeout=30,
        connect_timeout=30,
        pool_timeout=30,
    )


if __name__ == '__main__':
    main()
