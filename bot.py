import os
import json
import logging
import re
import io
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


def generate_voice(text):
    try:
        if not ELEVENLABS_API_KEY:
            return None
        # Limit text length for free plan
        text = text[:500]
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{JARVIS_VOICE_ID}"
        headers = {
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": ELEVENLABS_API_KEY,
        }
        data = {
            "text": text,
            "model_id": "eleven_monolingual_v1",
            "voice_settings": {"stability": 0.75, "similarity_boost": 0.85}
        }
        response = requests.post(url, json=data, headers=headers, timeout=30)
        if response.status_code == 200:
            audio_file = tempfile.mktemp(suffix='.mp3')
            with open(audio_file, 'wb') as f:
                f.write(response.content)
            return audio_file
        else:
            logger.error(f"ElevenLabs error: {response.status_code} - {response.text[:200]}")
        return None
    except Exception as e:
        logger.error(f"Voice error: {e}")
        return None


async def send_voice_message(update, context, text):
    try:
        await update.message.reply_text("🎙️ Generating voice...")
        audio_path = generate_voice(text)
        if audio_path and os.path.exists(audio_path):
            with open(audio_path, 'rb') as audio:
                await update.message.reply_voice(voice=audio)
            os.remove(audio_path)
        else:
            await update.message.reply_text(f"🎙️ {text}")
    except Exception as e:
        logger.error(f"Send voice error: {e}")
        await update.message.reply_text(f"🎙️ {text}")


def generate_pdf(doc_type, ref, po_number, date, loads, liters, rate, subtotal, vat, total):
    filename = tempfile.mktemp(suffix='.pdf')
    c = canvas.Canvas(filename, pagesize=A4)
    width, height = A4

    header_height = 38*mm
    if os.path.exists(LETTERHEAD_PATH):
        c.drawImage(LETTERHEAD_PATH, 0, height - header_height, width=width, height=header_height, preserveAspectRatio=False)

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
    doc_word = "Invoice" if doc_type == "Invoice" else "Quotes"
    c.drawString(15*mm, terms_y - 6*mm, f"{doc_word} are valid for 30 Days. 3-5 working days.")
    c.drawString(15*mm, terms_y - 12*mm, "Goods or Services are subject to prior sales.")

    acc_y = terms_y - 30*mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(15*mm, acc_y, "ACCOUNT DETAILS:")
    acc_details = [
        ("BANK NAME", f": {COMPANY['bank']}"),
        ("ACCOUNT NUMBER", f": {COMPANY['account']}"),
        ("ACCOUNT TYPE", f": {COMPANY['account_type']}"),
        ("BRANCH CODE", f"  {COMPANY['branch']}"),
        ("VAT NUMBER.", f"  {COMPANY['vat']}"),
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


def get_emails(limit=5, unread_only=False):
    emails = []
    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com', 993)
        mail.login(GMAIL_USER, GMAIL_PASSWORD)
        
        # Try different mailbox names
        for mailbox in ['INBOX', '"[Gmail]/All Mail"', 'All Mail']:
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
                    content_type = part.get_content_type()
                    content_disposition = str(part.get('Content-Disposition', ''))
                    if content_type == 'text/plain' and 'attachment' not in content_disposition:
                        try:
                            body += part.get_payload(decode=True).decode('utf-8', errors='replace')
                        except:
                            pass
            else:
                try:
                    body = msg.get_payload(decode=True).decode('utf-8', errors='replace')
                except:
                    body = ""

            emails.append({
                'id': msg_id.decode(),
                'subject': subject,
                'sender': sender,
                'date': date_str,
                'body': body[:300],
                'message_id': msg.get('Message-ID', ''),
            })

        mail.close()
        mail.logout()
    except Exception as e:
        logger.error(f"IMAP error: {e}")
    return emails


def check_po_emails():
    po_emails = []
    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com')
        mail.login(GMAIL_USER, GMAIL_PASSWORD)
        mail.select('inbox')
        _, messages = mail.search(None, 'FROM', 'pepsico.com')
        
        if not messages[0]:
            mail.close()
            mail.logout()
            return []

        for msg_id in messages[0].split():
            _, msg_data = mail.fetch(msg_id, '(RFC822)')
            msg = email_lib.message_from_bytes(msg_data[0][1])

            subject = decode_header(msg['Subject'])[0]
            if isinstance(subject[0], bytes):
                subject = subject[0].decode(subject[1] or 'utf-8', errors='replace')
            else:
                subject = subject[0] or ''

            if 'purchase order' in subject.lower() or 'po' in subject.lower():
                attachments = []
                if msg.is_multipart():
                    for part in msg.walk():
                        filename = part.get_filename()
                        if filename and filename.lower().endswith('.pdf'):
                            attachments.append({
                                'filename': filename,
                                'data': part.get_payload(decode=True)
                            })

                po_emails.append({
                    'subject': subject,
                    'sender': msg.get('From', ''),
                    'attachments': attachments,
                    'message_id': msg.get('Message-ID', ''),
                })
                mail.store(msg_id, '+FLAGS', '\\Seen')

        mail.close()
        mail.logout()
    except Exception as e:
        logger.error(f"PO check error: {e}")
    return po_emails


def extract_pdf_text(pdf_data):
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_data)) as pdf:
            text = ""
            for page in pdf.pages:
                text += page.extract_text() or ""
        return text
    except Exception as e:
        logger.error(f"PDF extract error: {e}")
        return ""


def parse_po_from_text(text):
    po_info = {}
    po_match = re.search(r'PO\s*No\.?\s*[:\s]\s*(\d+)', text, re.IGNORECASE)
    if po_match:
        po_info['po_number'] = po_match.group(1)
    qty_match = re.search(r'(\d[\d,]*\.?\d*)\s*(?:000\.000)?\s*(?:EA|L|Ltrs)', text, re.IGNORECASE)
    if qty_match:
        po_info['quantity'] = float(qty_match.group(1).replace(',', ''))
    date_match = re.search(r'Delivery\s*date[:/\s]*(\d{1,2}[./]\d{1,2}[./]\d{2,4})', text, re.IGNORECASE)
    if date_match:
        po_info['delivery_date'] = date_match.group(1)
    value_match = re.search(r'(\d[\d,]*\.?\d*)\s*ZAR', text, re.IGNORECASE)
    if value_match:
        po_info['line_value'] = float(value_match.group(1).replace(',', ''))
    return po_info


def check_remittances():
    paid_refs = []
    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com')
        mail.login(GMAIL_USER, GMAIL_PASSWORD)
        mail.select('inbox')
        _, messages = mail.search(None, 'FROM', 'pepsico.com', 'SUBJECT', 'remittance')
        for msg_id in messages[0].split():
            _, msg_data = mail.fetch(msg_id, '(RFC822)')
            msg = email_lib.message_from_bytes(msg_data[0][1])
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        try:
                            body += part.get_payload(decode=True).decode('utf-8', errors='replace')
                        except:
                            pass
            else:
                try:
                    body = msg.get_payload(decode=True).decode('utf-8', errors='replace')
                except:
                    pass
            refs = re.findall(r'BPT\d+', body)
            paid_refs.extend(refs)
        mail.close()
        mail.logout()
    except Exception as e:
        logger.error(f"Remittance check error: {e}")
    return paid_refs


def ask_claude(user_message, conversation_history=None):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system = """You are Jarvis, the AI business assistant for BlackPurple (PTY) LTD, a water supply and maintenance company in South Africa. You assist the owner Botshelo Keroane.

Personality: Professional, efficient, intelligent - like Jarvis from Iron Man. Occasionally call the user "Sir". Be confident and helpful.

Company: BlackPurple (PTY) LTD, 1704 Mothotlung, Brits. VAT: 4420309116. Main client: Pioneer Foods (PepsiCo).
Rates: R0.80/L weekdays, R0.95/L weekends/holidays. 10,000L per load.
Bank: Standard Bank, Acc: 060645377, Branch: 052546.

Key contacts: Gosego Masiane (gosego.masiane@pepsico.com), Shaun Jacobs (shaun.jacobs@pepsico.com), Invoices: SA.invoices@pepsico.com (CC Gosego).

Keep responses concise and professional. When drafting emails, make them professional and from BlackPurple (PTY) LTD."""

    messages = (conversation_history or [])[-10:]
    messages.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=system,
        messages=messages
    )
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
            "👋 Good day! I'm *Jarvis*, your BlackPurple business assistant.\n\n"
            "I'm ready to assist you with:\n"
            "✅ Quotes & Invoices (water supply)\n"
            "✅ Email reading & replies\n"
            "✅ PO tracking & matching\n"
            "✅ Thursday payment checks\n"
            "✅ Business conversations\n\n"
            "*Commands:*\n"
            "📧 *emails* — Latest 3 emails\n"
            "📧 *unread* — Unread emails\n"
            "🎙️ *read emails* — Hear emails by voice\n"
            "📋 *check po* — Check for new POs\n"
            "📄 *quotes* — Pending quotes\n"
            "💰 *invoices* — Unpaid invoices\n\n"
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

    # Approval flows
    if state.get("pending_quote") and text_lower in ["approved", "approve", "yes", "good", "send it", "ok", "send"]:
        await handle_quote_approval(update, context, state)
        return

    if state.get("pending_invoice") and text_lower in ["approved", "approve", "yes", "good", "send it", "ok", "send"]:
        await handle_invoice_approval(update, context, state)
        return

    if state.get("pending_email_reply"):
        if text_lower in ["approved", "approve", "yes", "send", "send it"]:
            await handle_email_reply_send(update, context, state)
        else:
            state["pending_email_reply"]["body"] = text
            save_state(state)
            await update.message.reply_text(
                f"Updated draft:\n\n{text}\n\nReply *APPROVED* to send.",
                parse_mode='Markdown'
            )
        return

    # Commands
    if text_lower in ["emails", "check emails", "show emails"]:
        await show_emails(update, context, state)
        return

    if text_lower in ["unread", "unread emails", "new emails"]:
        await show_emails(update, context, state, unread_only=True)
        return

    if text_lower in ["read emails", "read my emails", "voice emails", "voice"]:
        await show_emails(update, context, state, voice=True)
        return

    if text_lower in ["check po", "check pos", "new po", "purchase orders"]:
        await check_for_po(update, context, state)
        return

    if text_lower in ["quotes", "pending quotes"]:
        await show_quotes(update, context, state)
        return

    if text_lower in ["invoices", "unpaid invoices", "outstanding"]:
        await show_invoices(update, context, state)
        return

    if text_lower in ["thursday", "check payments", "check unpaid"]:
        await check_unpaid_invoices(update, context, state)
        return

    # Reply to email
    if "reply to" in text_lower:
        await handle_email_reply_request(update, context, state, text)
        return

    # Send email
    if "send an email" in text_lower or "send email" in text_lower or ("email to" in text_lower and "reply" not in text_lower):
        await handle_send_email_request(update, context, state, text)
        return

    # Loads message → quote
    date, loads = parse_loads_message(text)
    if date and loads:
        await create_quote(update, context, state, date, loads)
        return

    # PO number → invoice
    po_match = re.search(r'\b(44\d{8}|\d{10})\b', text)
    if po_match and state.get("last_quote_data"):
        await create_invoice_from_po(update, context, state, po_match.group(1))
        return

    # General conversation
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


async def show_emails(update, context, state, voice=False, unread_only=False):
    await update.message.reply_text("📧 Fetching your emails...")
    emails = get_emails(limit=3, unread_only=unread_only)

    if not emails:
        msg = "No unread emails. You're all caught up! ✅" if unread_only else "No emails found."
        await update.message.reply_text(msg)
        return

    state["recent_emails"] = emails
    save_state(state)

    if voice:
        voice_text = f"Sir, you have {len(emails)} {'unread ' if unread_only else ''}emails. "
        for i, em in enumerate(emails, 1):
            sender_name = em['sender'].split('<')[0].strip().replace('"', '')
            voice_text += f"Email {i} is from {sender_name}, subject: {em['subject']}. "
            if em['body']:
                voice_text += f"Preview: {em['body'][:100]}. "
        await send_voice_message(update, context, voice_text)

    for i, em in enumerate(emails, 1):
        sender_name = em['sender'].split('<')[0].strip()
        await update.message.reply_text(
            f"📧 *Email {i}*\n"
            f"👤 *From:* {sender_name}\n"
            f"📌 *Subject:* {em['subject']}\n"
            f"📅 *Date:* {em['date'][:25] if em['date'] else 'N/A'}\n"
            f"💬 *Preview:* {em['body'][:150]}\n\n"
            f"_To reply: say 'reply to email {i} and say...'_",
            parse_mode='Markdown'
        )


async def handle_email_reply_request(update, context, state, text):
    emails = state.get("recent_emails", [])
    if not emails:
        await update.message.reply_text("Please check your emails first by typing *emails*", parse_mode='Markdown')
        return

    num_match = re.search(r'email\s*(\d+)', text, re.IGNORECASE)
    email_idx = int(num_match.group(1)) - 1 if num_match else 0
    target_email = emails[email_idx] if email_idx < len(emails) else emails[0]

    say_match = re.search(r'(?:and say|saying|with|to say)\s+(.+)', text, re.IGNORECASE)
    if say_match:
        reply_content = say_match.group(1)
        prompt = f"Write a professional email reply from BlackPurple (PTY) LTD.\nOriginal subject: {target_email['subject']}\nFrom: {target_email['sender']}\nOriginal message: {target_email['body'][:200]}\nThe reply should convey: {reply_content}\nWrite only the email body."
        professional_reply = ask_claude(prompt)

        sender_email_match = re.search(r'<(.+?)>', target_email['sender'])
        sender_email = sender_email_match.group(1) if sender_email_match else target_email['sender']

        state["pending_email_reply"] = {
            "to": sender_email,
            "subject": target_email['subject'],
            "body": professional_reply,
            "message_id": target_email.get('message_id', '')
        }
        save_state(state)

        await update.message.reply_text(
            f"📧 *Reply Draft*\n\n"
            f"*To:* {sender_email}\n"
            f"*Subject:* Re: {target_email['subject']}\n\n"
            f"{professional_reply}\n\n"
            f"Reply *APPROVED* to send.",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("What would you like to say in the reply?")


async def handle_email_reply_send(update, context, state):
    reply = state["pending_email_reply"]
    try:
        msg = MIMEMultipart()
        msg['From'] = GMAIL_USER
        msg['To'] = reply['to']
        subject = reply['subject']
        if not subject.startswith('Re:'):
            subject = f"Re: {subject}"
        msg['Subject'] = subject
        if reply.get('message_id'):
            msg['In-Reply-To'] = reply['message_id']
        msg.attach(MIMEText(reply['body'], 'plain'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, [reply['to']], msg.as_string())
        server.quit()

        state["pending_email_reply"] = None
        save_state(state)
        await update.message.reply_text("✅ Reply sent successfully!")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send: {str(e)}")


async def handle_send_email_request(update, context, state, text):
    prompt = f"""Compose a professional email for BlackPurple (PTY) LTD based on this request: {text}

Reply in EXACTLY this format:
TO: [email address]
SUBJECT: [subject line]
BODY: [email body only]"""

    response = ask_claude(prompt)
    to_match = re.search(r'TO:\s*(.+)', response)
    subject_match = re.search(r'SUBJECT:\s*(.+)', response)
    body_match = re.search(r'BODY:\s*([\s\S]+)', response)

    to_email = to_match.group(1).strip() if to_match else "unknown"
    subject = subject_match.group(1).strip() if subject_match else "BlackPurple Communication"
    body = body_match.group(1).strip() if body_match else response

    state["pending_email_reply"] = {"to": to_email, "subject": subject, "body": body}
    save_state(state)

    await update.message.reply_text(
        f"📧 *Email Draft*\n\n"
        f"*To:* {to_email}\n"
        f"*Subject:* {subject}\n\n"
        f"{body}\n\n"
        f"Reply *APPROVED* to send or tell me what to change.",
        parse_mode='Markdown'
    )


async def check_for_po(update, context, state):
    await update.message.reply_text("🔍 Checking for new Purchase Orders from PepsiCo...")
    po_emails = check_po_emails()

    if not po_emails:
        await update.message.reply_text("No new Purchase Orders found. I'll keep watching! 👀")
        return

    for po_email in po_emails:
        po_number = re.search(r'\d{10}', po_email['subject'])
        po_number = po_number.group() if po_number else "Unknown"

        pdf_text = ""
        for att in po_email.get('attachments', []):
            if att['data']:
                pdf_text = extract_pdf_text(att['data'])
                break

        po_info = parse_po_from_text(pdf_text) if pdf_text else {}
        if not po_info.get('po_number'):
            po_info['po_number'] = po_number

        # Match to quote
        quotes = state.get("quotes", [])
        matched = None
        for q in quotes:
            if not q.get('invoiced'):
                matched = q
                break

        match_text = f"✅ Matches Quote: *{matched['ref']}*" if matched else "⚠️ No matching quote found"
        if matched:
            state["last_quote_data"] = matched
            save_state(state)

        keyboard = [
            [InlineKeyboardButton("✅ Create Invoice", callback_data=f"inv_{po_info['po_number']}")],
            [InlineKeyboardButton("❌ Skip", callback_data="skip_po")]
        ]

        await update.message.reply_text(
            f"📋 *New Purchase Order!*\n\n"
            f"*From:* {po_email['sender']}\n"
            f"*Subject:* {po_email['subject']}\n"
            f"*PO Number:* {po_info.get('po_number', 'See PDF')}\n"
            f"*Quantity:* {po_info.get('quantity', 'See PDF'):,.0f} Ltrs\n"
            f"*Delivery:* {po_info.get('delivery_date', 'See PDF')}\n\n"
            f"{match_text}\n\n"
            f"Shall I create the invoice?",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        state["pending_po"] = po_info
        save_state(state)


async def create_quote(update, context, state, date, loads):
    liters, rate, subtotal, vat, total = calculate_amount(loads, date)
    ref = next_ref(state)
    pdf_path = generate_pdf("Quote", ref, None, date, loads, liters, rate, subtotal, vat, total)

    quote_data = {
        "ref": ref,
        "date": date.isoformat(),
        "loads": loads,
        "liters": liters,
        "rate": rate,
        "subtotal": subtotal,
        "vat": vat,
        "total": total,
        "pdf_path": pdf_path,
        "invoiced": False,
    }
    state["pending_quote"] = quote_data
    state["last_quote_data"] = quote_data
    save_state(state)

    rate_type = "Weekend/Holiday" if rate == WEEKEND_RATE else "Weekday"
    caption = (
        f"📄 *Quote Ready for Approval*\n\n"
        f"📅 *Date:* {date.strftime('%d %B %Y')}\n"
        f"💧 *Loads:* {loads} ({liters:,.0f} Ltrs)\n"
        f"💰 *Rate:* R{rate:.2f}/L ({rate_type})\n"
        f"📊 *Subtotal:* R{subtotal:,.2f}\n"
        f"🏛️ *VAT (15%):* R{vat:,.2f}\n"
        f"✅ *Grand Total: R{total:,.2f}*\n\n"
        f"*Ref:* {ref}\n\n"
        f"Reply *APPROVED* to send to Gosego."
    )

    with open(pdf_path, 'rb') as f:
        await update.message.reply_document(
            document=f,
            filename=f"Quote_{ref}.pdf",
            caption=caption,
            parse_mode='Markdown'
        )


async def handle_quote_approval(update, context, state):
    q = state["pending_quote"]
    date = datetime.fromisoformat(q["date"])

    body = (
        f"Dear Gosego,\n\n"
        f"Please find attached our quote for the supply of water.\n\n"
        f"Quote REF: {q['ref']}\n"
        f"Date: {date.strftime('%d %B %Y')}\n"
        f"Quantity: {q['liters']:,.0f} Litres\n"
        f"Grand Total: R{q['total']:,.2f} (VAT incl.)\n\n"
        f"Please review and send the Purchase Order at your earliest convenience.\n\n"
        f"Kind regards,\nBlackPurple (PTY) LTD\nTel: 079 076 9253 / 073 289 5865\ninfo@blackpurple.co.za"
    )

    success = send_email(GOSEGO_EMAIL, f"Quote {q['ref']} - Supply of Water - BlackPurple", body, [q["pdf_path"]])

    quotes = state.get("quotes", [])
    quotes.append(q)
    state["quotes"] = quotes
    state["pending_quote"] = None
    save_state(state)

    if success:
        await update.message.reply_text(
            f"✅ Quote *{q['ref']}* sent to Gosego!\n\nI'll watch your inbox for the PO automatically.",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ Failed to send. Check Gmail settings.")


async def create_invoice_from_po(update, context, state, po_number):
    q = state.get("last_quote_data")
    if not q:
        await update.message.reply_text("No quote data found. Please send the loads message first.")
        return

    date = datetime.fromisoformat(q["date"]) if isinstance(q["date"], str) else q["date"]
    ref = next_ref(state)
    pdf_path = generate_pdf("Invoice", ref, po_number, date, q["loads"], q["liters"], q["rate"], q["subtotal"], q["vat"], q["total"])

    state["pending_invoice"] = {
        "ref": ref,
        "po_number": po_number,
        "date": q["date"],
        "loads": q["loads"],
        "liters": q["liters"],
        "rate": q["rate"],
        "subtotal": q["subtotal"],
        "vat": q["vat"],
        "total": q["total"],
        "pdf_path": pdf_path,
        "quote_ref": q.get("ref", ""),
    }
    save_state(state)

    caption = (
        f"🧾 *Invoice Ready for Approval*\n\n"
        f"📅 *Service Date:* {date.strftime('%d %B %Y')}\n"
        f"📋 *PO Number:* {po_number}\n"
        f"💧 *Quantity:* {q['liters']:,.0f} Ltrs\n"
        f"✅ *Grand Total: R{q['total']:,.2f}*\n\n"
        f"*Invoice Ref:* {ref}\n"
        f"*Quote Ref:* {q.get('ref', 'N/A')}\n\n"
        f"Reply *APPROVED* to send to:\n"
        f"📧 SA.invoices@pepsico.com\n"
        f"📧 CC: Gosego Masiane"
    )

    with open(pdf_path, 'rb') as f:
        await update.message.reply_document(
            document=f,
            filename=f"Invoice_{ref}.pdf",
            caption=caption,
            parse_mode='Markdown'
        )


async def handle_invoice_approval(update, context, state):
    inv = state["pending_invoice"]
    date = datetime.fromisoformat(inv["date"])

    body = (
        f"Dear Gosego,\n\n"
        f"Please find attached our invoice for the supply of water.\n\n"
        f"Invoice REF: {inv['ref']}\n"
        f"PO Number: {inv['po_number']}\n"
        f"Service Date: {date.strftime('%d %B %Y')}\n"
        f"Quantity: {inv['liters']:,.0f} Litres\n"
        f"Grand Total: R{inv['total']:,.2f} (VAT incl.)\n\n"
        f"Payment is due within 30 days as per our agreed terms.\n\n"
        f"Kind regards,\nBlackPurple (PTY) LTD\nTel: 079 076 9253 / 073 289 5865\ninfo@blackpurple.co.za"
    )

    success = send_email(
        INVOICE_EMAIL,
        f"Invoice {inv['ref']} - PO {inv['po_number']} - Supply of Water - BlackPurple",
        body,
        [inv["pdf_path"]],
        cc_email=GOSEGO_EMAIL
    )

    quotes = state.get("quotes", [])
    for q in quotes:
        if q.get("ref") == inv.get("quote_ref"):
            q["invoiced"] = True

    state["invoices"].append({
        "ref": inv["ref"],
        "po_number": inv["po_number"],
        "date": inv["date"],
        "total": inv["total"],
        "pdf_path": inv["pdf_path"],
        "paid": False,
    })
    state["quotes"] = quotes
    state["pending_invoice"] = None
    save_state(state)

    if success:
        await update.message.reply_text(
            f"✅ Invoice *{inv['ref']}* sent!\n\n"
            f"📧 To: SA.invoices@pepsico.com\n"
            f"📧 CC: Gosego Masiane\n"
            f"💰 Amount: R{inv['total']:,.2f}\n\n"
            f"I'll track payment and remind you Thursday if unpaid. 📅",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ Failed to send. Check Gmail settings.")


async def show_quotes(update, context, state):
    quotes = [q for q in state.get("quotes", []) if not q.get("invoiced")]
    if not quotes:
        await update.message.reply_text("No pending quotes. All quotes have been invoiced! ✅")
        return
    msg = f"📄 *Pending Quotes ({len(quotes)})*\n\n"
    for q in quotes:
        date = datetime.fromisoformat(q["date"]).strftime('%d %b %Y')
        msg += f"• *{q['ref']}* — R{q['total']:,.2f} — {date}\n"
    await update.message.reply_text(msg, parse_mode='Markdown')


async def show_invoices(update, context, state):
    unpaid = [inv for inv in state.get("invoices", []) if not inv.get("paid")]
    if not unpaid:
        await update.message.reply_text("All invoices are paid! ✅")
        return
    total = sum(inv["total"] for inv in unpaid)
    msg = f"💰 *Unpaid Invoices ({len(unpaid)})*\n\n"
    for inv in unpaid:
        msg += f"• *{inv['ref']}* — R{inv['total']:,.2f} — PO: {inv.get('po_number', 'N/A')}\n"
    msg += f"\n*Total Outstanding: R{total:,.2f}*"
    await update.message.reply_text(msg, parse_mode='Markdown')


async def check_unpaid_invoices(update, context, state):
    paid_refs = check_remittances()
    for inv in state["invoices"]:
        if inv["ref"] in paid_refs:
            inv["paid"] = True
    unpaid = [inv for inv in state["invoices"] if not inv["paid"]]
    save_state(state)

    if not unpaid:
        await update.message.reply_text("✅ All invoices are paid!")
        return

    total_unpaid = sum(inv["total"] for inv in unpaid)
    inv_list = "\n".join([f"• {inv['ref']} - R{inv['total']:,.2f}" for inv in unpaid])
    keyboard = [
        [InlineKeyboardButton("✅ Yes, email Shaun now", callback_data="email_shaun")],
        [InlineKeyboardButton("❌ Not yet", callback_data="cancel_shaun")]
    ]
    state["pending_shaun_email"] = unpaid
    save_state(state)

    await update.message.reply_text(
        f"⚠️ *Unpaid Invoices*\n\n{inv_list}\n\n💰 *Total: R{total_unpaid:,.2f}*\n\nShall I email Shaun Jacobs?",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    state = load_state()

    if query.data == "email_shaun":
        unpaid = state.get("pending_shaun_email", [])
        total = sum(inv["total"] for inv in unpaid)
        inv_list = "\n".join([f"- {inv['ref']}: R{inv['total']:,.2f}" for inv in unpaid])

        body = (
            f"Dear Shaun,\n\nI hope this email finds you well.\n\n"
            f"I would like to request your assistance with the following outstanding invoices:\n\n"
            f"{inv_list}\n\nTotal Outstanding: R{total:,.2f}\n\n"
            f"Please find the invoices attached. Could you kindly assist with processing these payments?\n\n"
            f"Thank you.\n\nKind regards,\nBlackPurple (PTY) LTD\nTel: 079 076 9253 / 073 289 5865"
        )

        paths = [inv["pdf_path"] for inv in unpaid if inv.get("pdf_path") and os.path.exists(inv.get("pdf_path", ""))]
        success = send_email(SHAUN_EMAIL, f"Outstanding Invoices - BlackPurple - R{total:,.2f}", body, paths)

        if success:
            await query.edit_message_text(f"✅ Email sent to Shaun Jacobs!\nTotal: R{total:,.2f}\nInvoices: {len(unpaid)}\n\nI'll check again next Thursday. 📅")
        else:
            await query.edit_message_text("❌ Failed to send email.")

    elif query.data == "cancel_shaun":
        await query.edit_message_text("Understood. I'll remind you next Thursday. 📅")

    elif query.data.startswith("inv_"):
        po_number = query.data.replace("inv_", "")
        state_obj = load_state()
        if state_obj.get("last_quote_data"):
            await query.edit_message_text(f"Creating invoice for PO {po_number}...")
            class FakeUpdate:
                class message:
                    @staticmethod
                    async def reply_text(text, **kwargs): pass
                    @staticmethod
                    async def reply_document(**kwargs): pass
            await create_invoice_from_po(query, context, state_obj, po_number)
        else:
            await query.edit_message_text("No quote data found. Send loads message first.")

    elif query.data == "skip_po":
        await query.edit_message_text("PO skipped. I'll keep watching for new ones. 👀")


async def thursday_check(context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    user_id = state.get("authorized_user") or AUTHORIZED_USER_ID
    if not user_id:
        return

    paid_refs = check_remittances()
    for inv in state["invoices"]:
        if inv["ref"] in paid_refs:
            inv["paid"] = True
    unpaid = [inv for inv in state["invoices"] if not inv["paid"]]
    save_state(state)

    if not unpaid:
        return

    total = sum(inv["total"] for inv in unpaid)
    inv_list = "\n".join([f"• {inv['ref']} - R{inv['total']:,.2f}" for inv in unpaid])
    state["pending_shaun_email"] = unpaid
    save_state(state)

    keyboard = [
        [InlineKeyboardButton("✅ Yes, email Shaun now", callback_data="email_shaun")],
        [InlineKeyboardButton("❌ Not yet", callback_data="cancel_shaun")]
    ]

    await context.bot.send_message(
        chat_id=user_id,
        text=f"📅 *Thursday Check*\n\nUnpaid invoices:\n{inv_list}\n\n💰 *Total: R{total:,.2f}*\n\nShall I email Shaun?",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def email_monitor_job(context: ContextTypes.DEFAULT_TYPE):
    """Check for important emails every 15 minutes"""
    state = load_state()
    user_id = state.get("authorized_user") or AUTHORIZED_USER_ID
    if not user_id:
        return

    try:
        po_emails = check_po_emails()
        for po_email in po_emails:
            po_num = re.search(r'\d{10}', po_email['subject'])
            po_num = po_num.group() if po_num else "See email"
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"🚨 *New Purchase Order!*\n\n"
                    f"*From:* {po_email['sender']}\n"
                    f"*Subject:* {po_email['subject']}\n"
                    f"*PO:* {po_num}\n\n"
                    f"Type *check po* to process it."
                ),
                parse_mode='Markdown'
            )
    except Exception as e:
        logger.error(f"Email monitor error: {e}")


async def error_handler(update, context):
    logger.error(f"Error: {context.error}")
    if update and update.message:
        try:
            await update.message.reply_text("⚠️ Something went wrong. Please try again.")
        except:
            pass

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    job_queue = app.job_queue
    job_queue.run_daily(thursday_check, time=datetime.strptime("11:00", "%H:%M").time().replace(tzinfo=SA_TZ), days=(3,))
    job_queue.run_repeating(email_monitor_job, interval=900, first=60)

    logger.info("🤖 Jarvis is online!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
