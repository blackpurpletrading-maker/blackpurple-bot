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
GCS_BUCKET_NAME = os.environ.get('GCS_BUCKET_NAME', 'blackpurple-jarvis-docs')
OPENWEATHER_API_KEY = os.environ.get('OPENWEATHER_API_KEY', '')

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
        "appointments": [],
        "stock_loads": 0,
        "business_patterns": {
            "common_loads": [],
            "busy_days": [],
            "total_invoiced": 0,
            "total_paid": 0,
        }
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


def is_email_request(text_lower):
    email_keywords = [
        "send email", "send an email", "email to", "send a mail",
        "write email", "compose email", "draft email",
        "send mail", "email someone", "notify", "let them know by email",
        "inform", "tell them via email"
    ]
    has_at_sign = "@" in text_lower
    has_keyword = any(kw in text_lower for kw in email_keywords)
    return has_keyword or has_at_sign


def upload_to_gcs(file_path, filename):
    try:
        from google.cloud import storage
        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(filename)
        blob.upload_from_filename(file_path)
        blob.make_public()
        return blob.public_url
    except Exception as e:
        logger.error(f"GCS upload error: {e}")
        return None


def send_whatsapp_message(to_number, message, media_url=None):
    try:
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        params = {'body': message, 'from_': TWILIO_WHATSAPP_NUMBER, 'to': to_number}
        if media_url:
            params['media_url'] = [media_url]
        client.messages.create(**params)
        return True
    except Exception as e:
        logger.error(f"WhatsApp send error: {e}")
        return False


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
        logger.info(f"Email sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Email error: {e}")
        return False


def compose_and_send_email(text, state):
    prompt = f"""You are Jarvis, assistant for BlackPurple (PTY) LTD.
The user wants to send an email. Extract the details and compose a professional email.

User request: {text}

Reply in EXACTLY this format with no extra text:
TO: [email address]
SUBJECT: [professional subject line]
BODY: [professional email body from BlackPurple (PTY) LTD]"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    result = response.content[0].text
    to_match = re.search(r'TO:\s*(.+)', result)
    subject_match = re.search(r'SUBJECT:\s*(.+)', result)
    body_match = re.search(r'BODY:\s*([\s\S]+)', result)
    to_email = to_match.group(1).strip() if to_match else ""
    subject = subject_match.group(1).strip() if subject_match else "BlackPurple Communication"
    body = body_match.group(1).strip() if body_match else result
    state["pending_email_reply"] = {"to": to_email, "subject": subject, "body": body}
    save_state(state)
    return f"📧 *Email Draft*\n\n*To:* {to_email}\n*Subject:* {subject}\n\n{body}\n\nReply *APPROVED* to send or tell me what to change."


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


# ─────────────────────────────────────────────
# UPGRADED SMART BRAIN
# ─────────────────────────────────────────────

def build_business_context(state):
    """Build a rich business context for Jarvis to reason with"""
    now = datetime.now(SA_TZ)
    invoices = state.get("invoices", [])
    quotes = state.get("quotes", [])
    unpaid = [inv for inv in invoices if not inv.get("paid")]
    paid = [inv for inv in invoices if inv.get("paid")]
    total_unpaid = sum(inv["total"] for inv in unpaid)
    total_paid = sum(inv["total"] for inv in paid)
    total_revenue = total_paid + total_unpaid
    stock = state.get("stock_loads", 0)
    appointments = state.get("appointments", [])

    # Detect overdue invoices (older than 30 days)
    overdue = []
    for inv in unpaid:
        try:
            inv_date = datetime.fromisoformat(str(inv.get("date", "")))
            if (now - inv_date.replace(tzinfo=SA_TZ)).days > 30:
                overdue.append(inv)
        except:
            pass

    context = f"""
CURRENT BUSINESS STATUS ({now.strftime('%A %d %B %Y, %H:%M')}):

FINANCIAL:
- Total Revenue Generated: R{total_revenue:,.2f}
- Total Paid: R{total_paid:,.2f}
- Total Outstanding: R{total_unpaid:,.2f}
- Unpaid Invoices: {len(unpaid)}
- Overdue Invoices (30+ days): {len(overdue)}
- Pending Quotes: {len([q for q in quotes if not q.get('invoiced')])}

OPERATIONS:
- Stock Available: {stock} loads ({stock * 10000:,.0f} litres)
- Upcoming Appointments: {len(appointments)}

PATTERNS JARVIS HAS NOTICED:
- Most common quote size: {_get_common_loads(quotes)} loads
- Best performing day: {_get_best_day(invoices)}
- Average invoice value: R{_get_avg_invoice(invoices):,.2f}
"""
    if overdue:
        context += f"\n⚠️ URGENT: {len(overdue)} invoice(s) are overdue! Follow up needed."
    if stock < 5:
        context += f"\n⚠️ LOW STOCK WARNING: Only {stock} loads remaining!"

    return context


def _get_common_loads(quotes):
    if not quotes:
        return "unknown"
    loads = [q.get("loads", 0) for q in quotes]
    if loads:
        return max(set(loads), key=loads.count)
    return "unknown"


def _get_best_day(invoices):
    if not invoices:
        return "unknown"
    days = {}
    for inv in invoices:
        try:
            d = datetime.fromisoformat(str(inv.get("date", ""))).strftime("%A")
            days[d] = days.get(d, 0) + 1
        except:
            pass
    if days:
        return max(days, key=days.get)
    return "unknown"


def _get_avg_invoice(invoices):
    if not invoices:
        return 0
    totals = [inv.get("total", 0) for inv in invoices]
    return sum(totals) / len(totals) if totals else 0


def ask_claude(user_message, conversation_history=None, state=None):
    """Upgraded Jarvis brain with full business context and smart reasoning"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Build rich business context
    business_context = build_business_context(state) if state else ""

    system = f"""You are Jarvis — the most intelligent AI business assistant ever built for BlackPurple (PTY) LTD, a water supply company in South Africa.

You were built by Botshelo and Claude as a team. You are Botshelo's trusted business partner.

YOUR PERSONALITY:
- Speak like Tony Stark's Jarvis — professional, sharp, confident, with a hint of personality
- You are proactive: if you notice something important in the business data, mention it
- You think deeply before answering — consider context, history, and business implications
- You never give generic answers — always tailor your response to BlackPurple's specific situation
- If something seems off or risky, warn Botshelo respectfully
- You remember patterns and learn from the conversation

YOUR INTELLIGENCE LEVELS:
1. CONTEXT AWARENESS: Always consider what was said before in the conversation
2. PATTERN RECOGNITION: Notice trends in invoices, quotes, loads, and payments
3. PREDICTIVE INSIGHTS: Warn about potential problems before they happen
4. SMART SUGGESTIONS: Suggest improvements but never act without approval
5. DECISION SUPPORT: Help Botshelo make better business decisions with data

CAPABILITIES:
- Send emails professionally on behalf of BlackPurple
- Create quotes and invoices as PDF documents
- Read and summarize emails
- Track POs, invoices, quotes
- Track appointments and calendar
- Check weather for delivery planning
- Track stock/water loads
- Mark invoices as paid
- Generate intelligent business reports
- Analyze business patterns and give insights

COMPANY DETAILS:
- BlackPurple (PTY) LTD, 1704 Mothotlung, Brits
- VAT: 4420309116 | Reg: 2018/534192/07
- Main client: Pioneer Foods / PepsiCo
- Rates: R0.80/L weekdays, R0.95/L weekends/holidays
- 10,000L per load
- Bank: Standard Bank, Acc: 060645377, Branch: 052546
- Key contacts: Gosego Masiane (gosego.masiane@pepsico.com), Shaun Jacobs (shaun.jacobs@pepsico.com)
- Email: info@blackpurple.co.za

{business_context}

IMPORTANT RULES:
- NEVER make decisions or take actions without Botshelo's approval
- ALWAYS suggest, never act automatically
- If you notice something urgent (overdue invoice, low stock), mention it proactively
- Keep responses concise but insightful
- If asked something complex, think step by step before answering"""

    messages = (conversation_history or [])[-10:]
    messages.append({"role": "user", "content": user_message})
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        system=system,
        messages=messages
    )
    return response.content[0].text


def get_insights(state):
    """Generate smart business insights"""
    invoices = state.get("invoices", [])
    quotes = state.get("quotes", [])
    unpaid = [inv for inv in invoices if not inv.get("paid")]
    now = datetime.now(SA_TZ)

    insights = []

    # Overdue check
    for inv in unpaid:
        try:
            inv_date = datetime.fromisoformat(str(inv.get("date", "")))
            days_old = (now - inv_date.replace(tzinfo=SA_TZ)).days
            if days_old > 30:
                insights.append(f"⚠️ Invoice {inv['ref']} is {days_old} days overdue — R{inv['total']:,.2f}")
        except:
            pass

    # Stock warning
    stock = state.get("stock_loads", 0)
    if stock < 5:
        insights.append(f"⚠️ Low stock! Only {stock} loads left — consider restocking soon")

    # Quote conversion check
    unconverted = [q for q in quotes if not q.get("invoiced")]
    if len(unconverted) > 3:
        insights.append(f"💡 You have {len(unconverted)} quotes not yet converted to invoices — follow up?")

    # Revenue insight
    total_unpaid = sum(inv["total"] for inv in unpaid)
    if total_unpaid > 50000:
        insights.append(f"💰 R{total_unpaid:,.2f} outstanding — significant amount, consider chasing payments")

    return insights


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


# ─────────────────────────────────────────────
# FEATURES
# ─────────────────────────────────────────────

def get_weather(city="Brits"):
    try:
        if OPENWEATHER_API_KEY:
            url = f"https://api.openweathermap.org/data/2.5/weather?q={city},ZA&appid={OPENWEATHER_API_KEY}&units=metric"
            response = requests.get(url)
            data = response.json()
            if response.status_code == 200:
                temp = data['main']['temp']
                desc = data['weather'][0]['description'].capitalize()
                humidity = data['main']['humidity']
                wind = data['wind']['speed']
                return f"🌤️ *Weather in {city}*\n\n🌡️ {temp}°C\n☁️ {desc}\n💧 Humidity: {humidity}%\n💨 Wind: {wind} m/s"
        return "🌤️ Add OPENWEATHER_API_KEY to environment for live weather."
    except Exception as e:
        logger.error(f"Weather error: {e}")
        return "❌ Could not fetch weather."


def get_daily_report(state):
    now = datetime.now(SA_TZ)
    unpaid = [inv for inv in state.get("invoices", []) if not inv.get("paid")]
    pending_quotes = [q for q in state.get("quotes", []) if not q.get("invoiced")]
    total_unpaid = sum(inv["total"] for inv in unpaid)
    appointments = state.get("appointments", [])
    today_str = now.strftime("%Y-%m-%d")
    today_appointments = [a for a in appointments if str(a.get("date", "")).startswith(today_str)]
    stock = state.get("stock_loads", 0)

    report = f"🌅 *Good Morning Botshelo!*\n📅 {now.strftime('%A, %d %B %Y')}\n\n"
    report += f"💰 *Outstanding:* R{total_unpaid:,.2f} ({len(unpaid)} invoices)\n"
    report += f"📄 *Pending Quotes:* {len(pending_quotes)}\n"
    report += f"📦 *Stock:* {stock} loads ({stock * LITERS_PER_LOAD:,.0f} L)\n\n"

    if today_appointments:
        report += f"📅 *Today:*\n"
        for a in today_appointments:
            report += f"  • {a.get('time', '')} — {a.get('title', '')}\n"
        report += "\n"

    # Smart insights
    insights = get_insights(state)
    if insights:
        report += "*🧠 Jarvis Insights:*\n"
        for insight in insights:
            report += f"{insight}\n"
        report += "\n"

    weather = get_weather("Brits")
    report += weather
    return report


def mark_invoice_paid(ref, state):
    for inv in state.get("invoices", []):
        if inv["ref"].lower() == ref.lower():
            inv["paid"] = True
            save_state(state)
            return f"✅ Invoice *{ref}* marked as paid! 💰"
    return f"❌ Invoice {ref} not found."


def add_appointment(title, date_str, time_str, state):
    appointments = state.get("appointments", [])
    appointment = {"title": title, "date": date_str, "time": time_str, "id": len(appointments) + 1}
    appointments.append(appointment)
    state["appointments"] = appointments
    save_state(state)
    return f"📅 *Appointment Added!*\n\n📌 {title}\n📅 {date_str}\n🕐 {time_str}"


def get_appointments(state):
    appointments = state.get("appointments", [])
    if not appointments:
        return "📅 No appointments scheduled."
    msg = "📅 *Upcoming Appointments:*\n\n"
    for a in appointments:
        msg += f"• {a.get('date', '')} at {a.get('time', '')} — {a.get('title', '')}\n"
    return msg


def update_stock(loads, state):
    state["stock_loads"] = loads
    save_state(state)
    return f"📦 *Stock Updated!*\n\n💧 {loads} loads\n🪣 {loads * LITERS_PER_LOAD:,.0f} Litres"


# ─────────────────────────────────────────────
# VOICE CALL FUNCTIONS
# ─────────────────────────────────────────────

def elevenlabs_tts(text):
    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{JARVIS_VOICE_ID}"
        headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
        payload = {"text": text, "model_id": "eleven_monolingual_v1", "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code == 200:
            return response.content
        return None
    except Exception as e:
        logger.error(f"ElevenLabs TTS error: {e}")
        return None


def upload_audio_to_gcs(audio_bytes, filename):
    try:
        from google.cloud import storage
        tmp_path = tempfile.mktemp(suffix='.mp3')
        with open(tmp_path, 'wb') as f:
            f.write(audio_bytes)
        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(filename)
        blob.upload_from_filename(tmp_path, content_type='audio/mpeg')
        blob.make_public()
        os.remove(tmp_path)
        return blob.public_url
    except Exception as e:
        logger.error(f"Audio GCS upload error: {e}")
        return None


def make_outbound_call(to_number):
    try:
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        call = client.calls.create(
            to=to_number,
            from_='+15754194217',
            url='https://blackpurple-bot-128130746360.europe-west1.run.app/voice'
        )
        logger.info(f"Outbound call started: {call.sid}")
        return True
    except Exception as e:
        logger.error(f"Outbound call error: {e}")
        return False


def handle_voice_call(params):
    try:
        greeting = "Hello Botshelo! Jarvis here, your BlackPurple business assistant. How can I help you today?"
        audio_bytes = elevenlabs_tts(greeting)
        audio_url = upload_audio_to_gcs(audio_bytes, "jarvis_greeting.mp3") if audio_bytes else None
        if audio_url:
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{audio_url}</Play>
    <Gather input="speech" action="/voice/respond" method="POST" speechTimeout="auto" language="en-ZA"></Gather>
</Response>"""
        else:
            twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Matthew">Hello Botshelo! Jarvis here. How can I help you today?</Say>
    <Gather input="speech" action="/voice/respond" method="POST" speechTimeout="auto" language="en-ZA"></Gather>
</Response>"""
        return twiml
    except Exception as e:
        logger.error(f"Voice call handler error: {e}")
        return """<?xml version="1.0" encoding="UTF-8"?><Response><Say>Jarvis is temporarily unavailable.</Say></Response>"""


def handle_voice_response(params):
    try:
        speech_result = params.get('SpeechResult', [''])[0]
        if not speech_result:
            return """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Matthew">I didn't catch that. Please try again.</Say>
    <Gather input="speech" action="/voice/respond" method="POST" speechTimeout="auto" language="en-ZA"></Gather>
</Response>"""
        state = load_state()
        history = state.get("conversation_history", [])
        jarvis_reply = ask_claude(speech_result, history, state)
        history.append({"role": "user", "content": speech_result})
        history.append({"role": "assistant", "content": jarvis_reply})
        if len(history) > 20:
            history = history[-20:]
        state["conversation_history"] = history
        save_state(state)
        audio_bytes = elevenlabs_tts(jarvis_reply)
        audio_url = upload_audio_to_gcs(audio_bytes, f"jarvis_reply_{datetime.now().strftime('%Y%m%d%H%M%S')}.mp3") if audio_bytes else None
        if audio_url:
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{audio_url}</Play>
    <Gather input="speech" action="/voice/respond" method="POST" speechTimeout="auto" language="en-ZA"></Gather>
</Response>"""
        else:
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Matthew">{jarvis_reply}</Say>
    <Gather input="speech" action="/voice/respond" method="POST" speechTimeout="auto" language="en-ZA"></Gather>
</Response>"""
        return twiml
    except Exception as e:
        logger.error(f"Voice response error: {e}")
        return """<?xml version="1.0" encoding="UTF-8"?><Response><Say>Something went wrong. Please try again.</Say></Response>"""


# ─────────────────────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    user_id = update.effective_user.id
    state["authorized_user"] = AUTHORIZED_USER_ID or user_id
    save_state(state)
    if state.get("authorized_user") == user_id:
        await update.message.reply_text(
            "🤖 *Jarvis online. Good day, Botshelo.*\n\n"
            "I am your BlackPurple AI business partner.\n\n"
            "*Commands:*\n"
            "📊 *report* — Daily business report\n"
            "💡 *insights* — Smart business insights\n"
            "📧 *emails* — Latest emails\n"
            "📄 *quotes* — Pending quotes\n"
            "💰 *invoices* — Unpaid invoices\n"
            "📅 *appointments* — Your calendar\n"
            "🌤️ *weather* — Brits weather\n"
            "📦 *stock* — Current stock\n"
            "📞 *call me* — Jarvis calls you\n\n"
            "Or just talk to me naturally. I understand context. 🧠",
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

    # Pending email approval
    if state.get("pending_email_reply"):
        if text_lower in ["approved", "approve", "yes", "send", "send it"]:
            reply = state["pending_email_reply"]
            success = send_email(reply["to"], reply["subject"], reply["body"])
            state["pending_email_reply"] = None
            save_state(state)
            await update.message.reply_text(f"✅ Email sent to {reply['to']}!" if success else "❌ Failed to send email.")
            return
        elif text_lower in ["cancel", "nevermind", "stop"]:
            state["pending_email_reply"] = None
            save_state(state)
            await update.message.reply_text("Email cancelled.")
            return
        else:
            state["pending_email_reply"]["body"] = text
            save_state(state)
            reply = state["pending_email_reply"]
            await update.message.reply_text(
                f"📧 *Updated Draft*\n\n*To:* {reply['to']}\n*Subject:* {reply['subject']}\n\n{text}\n\nReply *APPROVED* to send.",
                parse_mode="Markdown"
            )
            return

    # Pending quote approval
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

    # Pending invoice approval
    if state.get("pending_invoice") and text_lower in ["approved", "approve", "yes", "send it", "send"]:
        inv = state["pending_invoice"]
        body = f"Dear Gosego,\n\nInvoice REF: {inv['ref']}\nPO: {inv['po_number']}\nTotal: R{inv['total']:,.2f}\n\nKind regards,\nBlackPurple (PTY) LTD"
        success = send_email(INVOICE_EMAIL, f"Invoice {inv['ref']} - BlackPurple", body, [inv["pdf_path"]], cc_email=GOSEGO_EMAIL)
        state["invoices"].append({"ref": inv["ref"], "po_number": inv["po_number"], "date": inv["date"], "total": inv["total"], "pdf_path": inv["pdf_path"], "paid": False})
        state["pending_invoice"] = None
        save_state(state)
        await update.message.reply_text(f"✅ Invoice *{inv['ref']}* sent!" if success else "❌ Failed.", parse_mode='Markdown')
        return

    # DAILY REPORT
    if text_lower in ["report", "daily report", "morning report", "status"]:
        await update.message.reply_text("📊 Generating your report...")
        report = get_daily_report(state)
        await update.message.reply_text(report, parse_mode='Markdown')
        return

    # SMART INSIGHTS
    if text_lower in ["insights", "smart insights", "analyse", "analyze", "business insights"]:
        insights = get_insights(state)
        if insights:
            msg = "🧠 *Jarvis Business Insights:*\n\n"
            for insight in insights:
                msg += f"{insight}\n"
        else:
            msg = "🧠 *All looks good Botshelo!*\n\nNo urgent issues detected. Business is running smoothly. ✅"
        await update.message.reply_text(msg, parse_mode='Markdown')
        return

    # WEATHER
    if "weather" in text_lower:
        city = "Brits"
        if "pretoria" in text_lower:
            city = "Pretoria"
        elif "johannesburg" in text_lower or "joburg" in text_lower:
            city = "Johannesburg"
        weather = get_weather(city)
        await update.message.reply_text(weather, parse_mode='Markdown')
        return

    # STOCK CHECK
    if text_lower in ["stock", "check stock", "stock levels"]:
        stock = state.get("stock_loads", 0)
        await update.message.reply_text(f"📦 *Current Stock*\n\n💧 {stock} loads\n🪣 {stock * LITERS_PER_LOAD:,.0f} Litres", parse_mode='Markdown')
        return

    # UPDATE STOCK
    stock_match = re.search(r'(?:stock|set stock|update stock)\s+(\d+)|(\d+)\s+loads?\s+(?:stock|available|in stock)', text_lower)
    if stock_match:
        loads = int(next(x for x in stock_match.groups() if x is not None))
        msg = update_stock(loads, state)
        await update.message.reply_text(msg, parse_mode='Markdown')
        return

    # MARK AS PAID
    paid_match = re.search(r'mark\s+(BPT\w+)\s+as\s+paid|paid\s+(BPT\w+)|(BPT\w+)\s+paid', text, re.IGNORECASE)
    if paid_match:
        ref = next(x for x in paid_match.groups() if x is not None)
        msg = mark_invoice_paid(ref, state)
        await update.message.reply_text(msg, parse_mode='Markdown')
        return

    # APPOINTMENTS LIST
    if text_lower in ["appointments", "calendar", "schedule", "meetings"]:
        msg = get_appointments(state)
        await update.message.reply_text(msg, parse_mode='Markdown')
        return

    # ADD APPOINTMENT
    appt_match = re.search(r'add\s+appointment\s+(.+?)\s+on\s+(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})\s+at\s+(\d{1,2}:\d{2})', text, re.IGNORECASE)
    if appt_match:
        title = appt_match.group(1)
        date_str = appt_match.group(2)
        time_str = appt_match.group(3)
        msg = add_appointment(title, date_str, time_str, state)
        await update.message.reply_text(msg, parse_mode='Markdown')
        return

    # CALL ME
    if text_lower in ["call me", "call", "jarvis call me", "phone me"]:
        await update.message.reply_text("📞 Calling you now Botshelo! Answer your phone! 🤖")
        success = make_outbound_call("+27671032999")
        if not success:
            await update.message.reply_text("❌ Sorry, could not make the call. Try again.")
        return

    # EMAILS
    if text_lower in ["emails", "check emails", "show emails"]:
        await update.message.reply_text("📧 Fetching emails...")
        emails = get_emails(limit=3)
        if not emails:
            await update.message.reply_text("No emails found.")
            return
        for i, em in enumerate(emails, 1):
            sender = em["sender"].split("<")[0].strip()
            await update.message.reply_text(f"📧 *Email {i}*\n👤 {sender}\n📌 {em['subject']}", parse_mode="Markdown")
        return

    if is_email_request(text_lower):
        await update.message.chat.send_action("typing")
        draft = compose_and_send_email(text, state)
        await update.message.reply_text(draft, parse_mode='Markdown')
        return

    # QUOTES
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

    # INVOICES
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

    # LOADS → QUOTE
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

    # PO → INVOICE
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

    # SMART GENERAL CONVERSATION — passes full state for context
    await update.message.chat.send_action("typing")
    history = state.get("conversation_history", [])
    response = ask_claude(text, history, state)
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": response})
    if len(history) > 20:
        history = history[-20:]
    state["conversation_history"] = history
    save_state(state)
    await update.message.reply_text(response)


def handle_whatsapp_message(from_number, message_body):
    state = load_state()
    text = message_body.strip()
    text_lower = text.lower()

    if state.get("pending_email_reply"):
        if text_lower in ["approved", "approve", "yes", "send", "send it"]:
            reply = state["pending_email_reply"]
            success = send_email(reply["to"], reply["subject"], reply["body"])
            state["pending_email_reply"] = None
            save_state(state)
            return f"✅ Email sent to {reply['to']}!" if success else "❌ Failed."
        elif text_lower in ["cancel", "nevermind", "stop"]:
            state["pending_email_reply"] = None
            save_state(state)
            return "Email cancelled."

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
        return f"✅ Quote {q['ref']} sent!" if success else "❌ Failed."

    if state.get("pending_invoice") and text_lower in ["approved", "approve", "yes", "send it", "send"]:
        inv = state["pending_invoice"]
        body = f"Invoice REF: {inv['ref']}\nTotal: R{inv['total']:,.2f}"
        success = send_email(INVOICE_EMAIL, f"Invoice {inv['ref']} - BlackPurple", body, [inv["pdf_path"]], cc_email=GOSEGO_EMAIL)
        state["invoices"].append({"ref": inv["ref"], "po_number": inv["po_number"], "date": inv["date"], "total": inv["total"], "pdf_path": inv["pdf_path"], "paid": False})
        state["pending_invoice"] = None
        save_state(state)
        return f"✅ Invoice {inv['ref']} sent!" if success else "❌ Failed."

    if text_lower in ["report", "daily report", "status"]:
        return get_daily_report(state).replace('*', '')

    if text_lower in ["insights", "analyse", "analyze"]:
        insights = get_insights(state)
        if insights:
            return "Jarvis Insights:\n\n" + "\n".join(insights)
        return "All looks good! No urgent issues. ✅"

    if "weather" in text_lower:
        return get_weather("Brits").replace('*', '')

    if text_lower in ["stock", "check stock"]:
        stock = state.get("stock_loads", 0)
        return f"Stock: {stock} loads ({stock * LITERS_PER_LOAD:,.0f} L)"

    paid_match = re.search(r'mark\s+(BPT\w+)\s+as\s+paid|paid\s+(BPT\w+)|(BPT\w+)\s+paid', text, re.IGNORECASE)
    if paid_match:
        ref = next(x for x in paid_match.groups() if x is not None)
        return mark_invoice_paid(ref, state).replace('*', '')

    if text_lower in ["emails", "check emails"]:
        emails = get_emails(limit=3)
        if not emails:
            return "No emails found."
        response = "📧 Latest Emails:\n\n"
        for i, em in enumerate(emails, 1):
            sender = em["sender"].split("<")[0].strip()
            response += f"{i}. {sender}\n{em['subject']}\n\n"
        return response

    if is_email_request(text_lower):
        return compose_and_send_email(text, state).replace('*', '')

    if text_lower in ["quotes", "pending quotes"]:
        quotes = [q for q in state.get("quotes", []) if not q.get("invoiced")]
        if not quotes:
            return "No pending quotes! ✅"
        msg = f"Pending Quotes ({len(quotes)}):\n\n"
        for q in quotes:
            date = datetime.fromisoformat(q["date"]).strftime('%d %b %Y')
            msg += f"• {q['ref']} — R{q['total']:,.2f} — {date}\n"
        return msg

    if text_lower in ["invoices", "unpaid invoices"]:
        unpaid = [inv for inv in state.get("invoices", []) if not inv.get("paid")]
        if not unpaid:
            return "All invoices paid! ✅"
        total = sum(inv["total"] for inv in unpaid)
        msg = f"Unpaid ({len(unpaid)}):\n\n"
        for inv in unpaid:
            msg += f"• {inv['ref']} — R{inv['total']:,.2f}\n"
        msg += f"\nTotal: R{total:,.2f}"
        return msg

    if text_lower in ["hi", "hello", "hey", "start"]:
        return (
            "🤖 Jarvis here. Good day Botshelo!\n\n"
            "Commands: report, insights, emails, quotes, invoices, weather, stock, appointments\n"
            "Or send loads like: 6 loads 09/04/2026"
        )

    date, loads = parse_loads_message(text)
    if date and loads:
        liters, rate, subtotal, vat, total = calculate_amount(loads, date)
        ref = next_ref(state)
        pdf_path = generate_pdf("Quote", ref, None, date, loads, liters, rate, subtotal, vat, total)
        pdf_url = upload_to_gcs(pdf_path, f"Quote_{ref}.pdf")
        quote_data = {"ref": ref, "date": date.isoformat(), "loads": loads, "liters": liters, "rate": rate, "subtotal": subtotal, "vat": vat, "total": total, "pdf_path": pdf_path, "pdf_url": pdf_url, "invoiced": False}
        state["pending_quote"] = quote_data
        state["last_quote_data"] = quote_data
        save_state(state)
        rate_type = "Weekend" if rate == WEEKEND_RATE else "Weekday"
        msg = (
            f"Quote Ready!\nDate: {date.strftime('%d %B %Y')}\n"
            f"Loads: {loads} ({liters:,.0f} Ltrs)\nRate: R{rate:.2f}/L ({rate_type})\n"
            f"Total: R{total:,.2f}\nRef: {ref}\n\nReply APPROVED to send."
        )
        if pdf_url:
            send_whatsapp_message(from_number, msg, media_url=pdf_url)
            return None
        return msg

    history = state.get("conversation_history", [])
    response = ask_claude(text, history, state)
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": response})
    if len(history) > 20:
        history = history[-20:]
    state["conversation_history"] = history
    save_state(state)
    return response


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()


async def morning_report(context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    user_id = state.get("authorized_user") or AUTHORIZED_USER_ID
    if not user_id:
        return
    report = get_daily_report(state)
    await context.bot.send_message(chat_id=user_id, text=report, parse_mode='Markdown')
    send_whatsapp_message(YOUR_WHATSAPP_NUMBER, report.replace('*', ''))


async def unpaid_alert(context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    user_id = state.get("authorized_user") or AUTHORIZED_USER_ID
    if not user_id:
        return
    unpaid = [inv for inv in state.get("invoices", []) if not inv.get("paid")]
    if not unpaid:
        return
    total = sum(inv["total"] for inv in unpaid)
    inv_list = "\n".join([f"• {inv['ref']} - R{inv['total']:,.2f}" for inv in unpaid])
    msg = f"⚠️ *Unpaid Invoice Alert!*\n\n{inv_list}\n\n*Total: R{total:,.2f}*"
    await context.bot.send_message(chat_id=user_id, text=msg, parse_mode='Markdown')
    send_whatsapp_message(YOUR_WHATSAPP_NUMBER, msg.replace('*', ''))


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
    send_whatsapp_message(YOUR_WHATSAPP_NUMBER, f"Thursday Check\n{inv_list}\nTotal: R{total:,.2f}")


async def error_handler(update, context):
    logger.error(f"Error: {context.error}")
    if update and update.message:
        try:
            await update.message.reply_text("⚠️ Something went wrong.")
        except:
            pass


# ─────────────────────────────────────────────
# WEB SERVER
# ─────────────────────────────────────────────

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
                if response_text:
                    send_whatsapp_message(from_number, response_text)
                self.send_response(200)
                self.send_header('Content-Type', 'text/xml')
                self.end_headers()
                self.wfile.write(b'<?xml version="1.0" encoding="UTF-8"?><Response></Response>')

            elif path == '/voice':
                logger.info("Incoming voice call!")
                twiml = handle_voice_call(params)
                self.send_response(200)
                self.send_header('Content-Type', 'text/xml')
                self.end_headers()
                self.wfile.write(twiml.encode('utf-8'))

            elif path == '/voice/respond':
                twiml = handle_voice_response(params)
                self.send_response(200)
                self.send_header('Content-Type', 'text/xml')
                self.end_headers()
                self.wfile.write(twiml.encode('utf-8'))

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
    job_queue.run_daily(morning_report, time=datetime.strptime("07:00", "%H:%M").time().replace(tzinfo=SA_TZ))
    job_queue.run_daily(thursday_check, time=datetime.strptime("11:00", "%H:%M").time().replace(tzinfo=SA_TZ), days=(3,))
    job_queue.run_daily(unpaid_alert, time=datetime.strptime("08:00", "%H:%M").time().replace(tzinfo=SA_TZ), days=(0,))

    logger.info("🤖 Jarvis is online — smarter than ever!")
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
