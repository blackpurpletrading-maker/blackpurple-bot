import os
import json
import logging
import re
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
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_LEFT, TA_CENTER
from reportlab.pdfgen import canvas
import tempfile

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
GMAIL_USER = os.environ.get('GMAIL_USER')
GMAIL_PASSWORD = os.environ.get('GMAIL_PASSWORD')

GOSEGO_EMAIL = "Gosego.Masiane@pepsico.com"
SHAUN_EMAIL = "Shaun.Jacobs@pepsico.com"
MY_EMAIL = "blackpurple.trading@gmail.com"

SA_TZ = pytz.timezone('Africa/Johannesburg')

# Company details
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

# State file for tracking invoices and quotes
STATE_FILE = "state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "last_ref_number": 356,
        "pending_quote": None,
        "pending_invoice": None,
        "invoices": [],
        "authorized_user": None,
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def next_ref(state):
    state["last_ref_number"] += 1
    save_state(state)
    return f"BPT{str(state['last_ref_number']).zfill(6)[2:]}"  # e.g. BPT250357

def is_weekend_or_holiday(date):
    return date.weekday() >= 5  # Saturday=5, Sunday=6

def parse_loads_message(text):
    """Parse messages like '01/04/2026 5 loads done ✔️'"""
    date_pattern = r'(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})'
    loads_pattern = r'(\d+)\s*loads?'
    date_match = re.search(date_pattern, text)
    loads_match = re.search(loads_pattern, text, re.IGNORECASE)
    if date_match and loads_match:
        date_str = date_match.group(1)
        loads = int(loads_match.group(1))
        try:
            for fmt in ['%d/%m/%Y', '%d-%m-%Y', '%d.%m.%Y', '%d/%m/%y']:
                try:
                    date = datetime.strptime(date_str, fmt)
                    break
                except:
                    continue
        except:
            date = datetime.now(SA_TZ)
        return date, loads
    return None, None

def calculate_amount(loads, date):
    liters = loads * LITERS_PER_LOAD
    rate = WEEKEND_RATE if is_weekend_or_holiday(date) else WEEKDAY_RATE
    subtotal = liters * rate
    vat = subtotal * VAT_RATE
    total = subtotal + vat
    return liters, rate, subtotal, vat, total

LETTERHEAD_PATH = os.path.join(os.path.dirname(__file__), 'letterhead.jpg')

def generate_pdf(doc_type, ref, po_number, date, loads, liters, rate, subtotal, vat, total):
    """Generate a professional PDF quote or invoice matching BlackPurple style"""
    filename = tempfile.mktemp(suffix='.pdf')
    
    c = canvas.Canvas(filename, pagesize=A4)
    width, height = A4

    # Draw letterhead image at the top spanning full width
    header_height = 38*mm
    if os.path.exists(LETTERHEAD_PATH):
        c.drawImage(LETTERHEAD_PATH, 0, height - header_height, width=width, height=header_height, preserveAspectRatio=False)
    
    # Document type and reference
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 16)
    c.drawRightString(width - 15*mm, height - 55*mm, doc_type)
    
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(width - 15*mm, height - 63*mm, f"{doc_type} REF: {ref}")
    if po_number:
        c.drawRightString(width - 15*mm, height - 70*mm, f"PO no: {po_number}")
    c.drawRightString(width - 15*mm, height - 77*mm, date.strftime("%d %B %Y"))
    
    # Bill to section
    c.setFont("Helvetica-Bold", 10)
    c.drawString(15*mm, height - 55*mm, f"{doc_type} TO:")
    c.setFont("Helvetica", 9)
    y = height - 63*mm
    # Client name first
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
    
    # Table
    table_top = height - 155*mm
    col_widths = [15*mm, 95*mm, 30*mm, 25*mm, 30*mm]
    
    # Table header
    c.setFillColor(colors.white)
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.5)
    
    headers = ["Item", "Description", "Quantity", "Unit Price", "Total Price"]
    x_positions = [15*mm, 30*mm, 125*mm, 155*mm, 180*mm]
    
    # Draw header row
    c.setFillColor(colors.white)
    c.rect(15*mm, table_top, width - 30*mm, 8*mm, fill=1, stroke=1)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 9)
    for i, header in enumerate(headers):
        c.drawString(x_positions[i] + 1*mm, table_top + 2*mm, header)
    
    # Data row
    row_y = table_top - 8*mm
    c.rect(15*mm, row_y, width - 30*mm, 8*mm, fill=0, stroke=1)
    c.setFont("Helvetica", 9)
    row_data = [
        "1",
        f"Supply of Water ({date.strftime('%d %B %Y')})",
        f"{liters:,.0f} Ltrs",
        f"R{rate:.2f}",
        f"R{subtotal:,.2f}",
    ]
    for i, cell in enumerate(row_data):
        c.drawString(x_positions[i] + 1*mm, row_y + 2*mm, cell)
    
    # Totals
    totals_y = row_y - 10*mm
    right_col_x = 155*mm
    amount_x = 180*mm
    
    c.setFont("Helvetica", 9)
    c.drawString(right_col_x, totals_y, "Sub-total")
    c.drawString(amount_x, totals_y, f"R{subtotal:,.2f}")
    
    totals_y -= 6*mm
    c.setFillColor(colors.blue)
    c.drawString(right_col_x, totals_y, "VAT@15%")
    c.setFillColor(colors.black)
    c.drawString(amount_x, totals_y, f"R{vat:,.2f}")
    
    totals_y -= 6*mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(right_col_x, totals_y, "Grand Total")
    c.drawString(amount_x, totals_y, f"R{total:,.2f}")
    
    # Terms and Conditions
    terms_y = totals_y - 25*mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(15*mm, terms_y, "Terms and Conditions:")
    c.setFont("Helvetica", 9)
    doc_word = "Invoice" if doc_type == "Invoice" else "Quotes"
    c.drawString(15*mm, terms_y - 6*mm, f"{doc_word} are valid for 30 Days. 3-5 working days.")
    c.drawString(15*mm, terms_y - 12*mm, "Goods or Services are subject to prior sales.")
    
    # Account Details
    acc_y = terms_y - 30*mm
    c.setFont("Helvetica-Bold", 9)
    c.drawString(15*mm, acc_y, "ACCOUNT DETAILS:")
    c.setFont("Helvetica", 9)
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

def send_email(to_email, subject, body, attachment_path=None, attachment_name=None):
    """Send email via Gmail"""
    try:
        msg = MIMEMultipart()
        msg['From'] = GMAIL_USER
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        
        if attachment_path and os.path.exists(attachment_path):
            with open(attachment_path, 'rb') as f:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename="{attachment_name or os.path.basename(attachment_path)}"')
            msg.attach(part)
        
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, to_email, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        logger.error(f"Email error: {e}")
        return False

def check_remittances():
    """Check Gmail for remittance emails from PepsiCo"""
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
                        body += part.get_payload(decode=True).decode()
            else:
                body = msg.get_payload(decode=True).decode()
            refs = re.findall(r'BPT\d+', body)
            paid_refs.extend(refs)
        mail.close()
        mail.logout()
    except Exception as e:
        logger.error(f"IMAP error: {e}")
    return paid_refs

# Bot handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    user_id = update.effective_user.id
    
    if state.get("authorized_user") is None:
        state["authorized_user"] = user_id
        save_state(state)
        await update.message.reply_text(
            "👋 Hello! I'm your BlackPurple business assistant!\n\n"
            "I'm now set up and ready to help you with:\n"
            "✅ Creating quotes and invoices\n"
            "✅ Sending documents to PepsiCo\n"
            "✅ Checking unpaid invoices every Thursday\n\n"
            "Just send me a message like:\n"
            "*01/04/2026 5 loads done ✔️*\n\n"
            "And I'll take care of the rest! 🚀",
            parse_mode='Markdown'
        )
    elif state.get("authorized_user") == user_id:
        await update.message.reply_text("Welcome back! Ready to work 💼")
    else:
        await update.message.reply_text("Sorry, this bot is private.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    user_id = update.effective_user.id
    
    if state.get("authorized_user") != user_id:
        await update.message.reply_text("Sorry, this bot is private.")
        return
    
    text = update.message.text.strip()
    
    # Check if approving a quote
    if state.get("pending_quote") and text.lower() in ["approved", "approve", "yes", "good", "send it", "ok"]:
        await handle_quote_approval(update, context, state)
        return
    
    # Check if approving an invoice
    if state.get("pending_invoice") and text.lower() in ["approved", "approve", "yes", "good", "send it", "ok"]:
        await handle_invoice_approval(update, context, state)
        return

    # Check for Thursday check command
    if "thursday" in text.lower() or "check invoices" in text.lower() or "unpaid" in text.lower():
        await check_unpaid_invoices(update, context, state)
        return

    # Check for loads message
    date, loads = parse_loads_message(text)
    if date and loads:
        await create_quote(update, context, state, date, loads)
        return
    
    # Check if it's a PO number being sent
    po_match = re.search(r'\b(44\d{8}|\d{10})\b', text)
    if po_match and state.get("last_quote_ref"):
        po_number = po_match.group(1)
        await create_invoice_from_po(update, context, state, po_number)
        return
    
    # General message - use Claude
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system="You are a helpful business assistant for BlackPurple (PTY) LTD, a water supply company in South Africa. Be concise and professional.",
        messages=[{"role": "user", "content": text}]
    )
    await update.message.reply_text(response.content[0].text)

async def create_quote(update, context, state, date, loads):
    liters, rate, subtotal, vat, total = calculate_amount(loads, date)
    ref = next_ref(state)
    
    # Generate PDF
    pdf_path = generate_pdf("Quote", ref, None, date, loads, liters, rate, subtotal, vat, total)
    
    # Store pending quote
    state["pending_quote"] = {
        "ref": ref,
        "date": date.isoformat(),
        "loads": loads,
        "liters": liters,
        "rate": rate,
        "subtotal": subtotal,
        "vat": vat,
        "total": total,
        "pdf_path": pdf_path,
    }
    state["last_quote_ref"] = ref
    save_state(state)
    
    # Send PDF to user for approval
    rate_type = "Weekend/Holiday" if rate == WEEKEND_RATE else "Weekday"
    caption = (
        f"📄 *Quote Ready for Approval*\n\n"
        f"📅 Date: {date.strftime('%d %B %Y')}\n"
        f"💧 Loads: {loads} ({liters:,.0f} Ltrs)\n"
        f"💰 Rate: R{rate:.2f}/L ({rate_type})\n"
        f"📊 Subtotal: R{subtotal:,.2f}\n"
        f"🏛️ VAT (15%): R{vat:,.2f}\n"
        f"✅ *Grand Total: R{total:,.2f}*\n\n"
        f"Ref: {ref}\n\n"
        f"Reply *APPROVED* to send to Gosego, or tell me what to change."
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
    
    # Send email to Gosego
    email_body = (
        f"Dear Gosego,\n\n"
        f"Please find attached our quote for the supply of water.\n\n"
        f"Quote REF: {q['ref']}\n"
        f"Date: {date.strftime('%d %B %Y')}\n"
        f"Quantity: {q['liters']:,.0f} Litres\n"
        f"Grand Total: R{q['total']:,.2f} (VAT incl.)\n\n"
        f"Please review and send the Purchase Order at your earliest convenience.\n\n"
        f"Kind regards,\n"
        f"BlackPurple (PTY) LTD\n"
        f"Tel: 079 076 9253 / 073 289 5865\n"
        f"info@blackpurple.co.za"
    )
    
    success = send_email(
        GOSEGO_EMAIL,
        f"Quote {q['ref']} - Supply of Water - BlackPurple",
        email_body,
        q["pdf_path"],
        f"Quote_{q['ref']}.pdf"
    )
    
    state["pending_quote"] = None
    save_state(state)
    
    if success:
        await update.message.reply_text(
            f"✅ Quote *{q['ref']}* sent to Gosego!\n\n"
            f"I'll wait for the PO. When you receive it, just send me the PO number.",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ Failed to send email. Please check Gmail settings.")

async def create_invoice_from_po(update, context, state, po_number):
    # Find the last quote details
    if not state.get("last_quote_data"):
        await update.message.reply_text(
            "I don't have the quote details. Please send me the loads message again so I can create the invoice."
        )
        return
    
    q = state["last_quote_data"]
    date = datetime.fromisoformat(q["date"])
    ref = next_ref(state)
    
    pdf_path = generate_pdf(
        "Invoice", ref, po_number, date,
        q["loads"], q["liters"], q["rate"],
        q["subtotal"], q["vat"], q["total"]
    )
    
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
    }
    save_state(state)
    
    caption = (
        f"🧾 *Invoice Ready for Approval*\n\n"
        f"📅 Date: {date.strftime('%d %B %Y')}\n"
        f"📋 PO Number: {po_number}\n"
        f"💧 {q['liters']:,.0f} Litres\n"
        f"✅ *Grand Total: R{q['total']:,.2f}*\n\n"
        f"Ref: {ref}\n\n"
        f"Reply *APPROVED* to send to PepsiCo."
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
    
    email_body = (
        f"Dear Gosego,\n\n"
        f"Please find attached our invoice for the supply of water.\n\n"
        f"Invoice REF: {inv['ref']}\n"
        f"PO Number: {inv['po_number']}\n"
        f"Date: {date.strftime('%d %B %Y')}\n"
        f"Quantity: {inv['liters']:,.0f} Litres\n"
        f"Grand Total: R{inv['total']:,.2f} (VAT incl.)\n\n"
        f"Payment is due within 30 days.\n\n"
        f"Kind regards,\n"
        f"BlackPurple (PTY) LTD\n"
        f"Tel: 079 076 9253 / 073 289 5865\n"
        f"info@blackpurple.co.za"
    )
    
    success = send_email(
        GOSEGO_EMAIL,
        f"Invoice {inv['ref']} - PO {inv['po_number']} - Supply of Water",
        email_body,
        inv["pdf_path"],
        f"Invoice_{inv['ref']}.pdf"
    )
    
    # Save to invoices list for Thursday check
    state["invoices"].append({
        "ref": inv["ref"],
        "po_number": inv["po_number"],
        "date": inv["date"],
        "total": inv["total"],
        "pdf_path": inv["pdf_path"],
        "paid": False,
    })
    state["pending_invoice"] = None
    save_state(state)
    
    if success:
        await update.message.reply_text(
            f"✅ Invoice *{inv['ref']}* sent to PepsiCo!\n\n"
            f"Amount: R{inv['total']:,.2f}\n"
            f"I'll track this and remind you on Thursday if not paid. 📅",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ Failed to send email. Please check Gmail settings.")

async def check_unpaid_invoices(update, context, state):
    # Check remittances from email
    paid_refs = check_remittances()
    
    # Update paid status
    for inv in state["invoices"]:
        if inv["ref"] in paid_refs:
            inv["paid"] = True
    
    unpaid = [inv for inv in state["invoices"] if not inv["paid"]]
    save_state(state)
    
    if not unpaid:
        await update.message.reply_text("✅ All invoices are paid! Nothing to follow up on.")
        return
    
    total_unpaid = sum(inv["total"] for inv in unpaid)
    inv_list = "\n".join([f"• {inv['ref']} - R{inv['total']:,.2f} (PO: {inv.get('po_number', 'N/A')})" for inv in unpaid])
    
    keyboard = [
        [InlineKeyboardButton("✅ Yes, email Shaun now", callback_data="email_shaun")],
        [InlineKeyboardButton("❌ Not yet", callback_data="cancel_shaun")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"⚠️ *Unpaid Invoices Found*\n\n"
        f"{inv_list}\n\n"
        f"💰 Total Outstanding: R{total_unpaid:,.2f}\n\n"
        f"Shall I email Shaun Jacobs to follow up?",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )
    
    # Store unpaid for callback
    state["pending_shaun_email"] = unpaid
    save_state(state)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    state = load_state()
    
    if query.data == "email_shaun":
        unpaid = state.get("pending_shaun_email", [])
        if not unpaid:
            await query.edit_message_text("No unpaid invoices found.")
            return
        
        total_unpaid = sum(inv["total"] for inv in unpaid)
        inv_list_text = "\n".join([f"- {inv['ref']}: R{inv['total']:,.2f} (PO: {inv.get('po_number', 'N/A')})" for inv in unpaid])
        
        email_body = (
            f"Dear Shaun,\n\n"
            f"I hope this email finds you well.\n\n"
            f"I would like to kindly request your assistance with the following outstanding invoices:\n\n"
            f"{inv_list_text}\n\n"
            f"Total Outstanding: R{total_unpaid:,.2f}\n\n"
            f"Please find the invoices attached for your reference. "
            f"Could you kindly assist with processing these payments?\n\n"
            f"Thank you for your assistance.\n\n"
            f"Kind regards,\n"
            f"BlackPurple (PTY) LTD\n"
            f"Tel: 079 076 9253 / 073 289 5865\n"
            f"info@blackpurple.co.za"
        )
        
        # Send with all unpaid invoice PDFs attached
        try:
            msg = MIMEMultipart()
            msg['From'] = GMAIL_USER
            msg['To'] = SHAUN_EMAIL
            msg['Subject'] = f"Outstanding Invoices - BlackPurple - R{total_unpaid:,.2f}"
            msg.attach(MIMEText(email_body, 'plain'))
            
            for inv in unpaid:
                if inv.get("pdf_path") and os.path.exists(inv["pdf_path"]):
                    with open(inv["pdf_path"], 'rb') as f:
                        part = MIMEBase('application', 'octet-stream')
                        part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition', f'attachment; filename="Invoice_{inv["ref"]}.pdf"')
                    msg.attach(part)
            
            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_USER, SHAUN_EMAIL, msg.as_string())
            server.quit()
            
            await query.edit_message_text(
                f"✅ Email sent to Shaun Jacobs!\n\n"
                f"Subject: Outstanding Invoices - R{total_unpaid:,.2f}\n"
                f"Invoices attached: {len(unpaid)}\n\n"
                f"I'll check again next Thursday. 📅"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to send email: {str(e)}")
    
    elif query.data == "cancel_shaun":
        await query.edit_message_text("Okay, I won't email Shaun yet. I'll remind you again next Thursday. 📅")

async def thursday_check(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled job to run every Thursday at 11am"""
    state = load_state()
    user_id = state.get("authorized_user")
    if not user_id:
        return
    
    now = datetime.now(SA_TZ)
    if now.hour < 11:
        return
    
    paid_refs = check_remittances()
    for inv in state["invoices"]:
        if inv["ref"] in paid_refs:
            inv["paid"] = True
    
    unpaid = [inv for inv in state["invoices"] if not inv["paid"]]
    save_state(state)
    
    if not unpaid:
        return
    
    total_unpaid = sum(inv["total"] for inv in unpaid)
    inv_list = "\n".join([f"• {inv['ref']} - R{inv['total']:,.2f}" for inv in unpaid])
    
    keyboard = [
        [InlineKeyboardButton("✅ Yes, email Shaun now", callback_data="email_shaun")],
        [InlineKeyboardButton("❌ Not yet", callback_data="cancel_shaun")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    state["pending_shaun_email"] = unpaid
    save_state(state)
    
    await context.bot.send_message(
        chat_id=user_id,
        text=(
            f"📅 *Thursday Invoice Check*\n\n"
            f"No remittance received for:\n\n"
            f"{inv_list}\n\n"
            f"💰 Total Outstanding: R{total_unpaid:,.2f}\n\n"
            f"Shall I email Shaun Jacobs now?"
        ),
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Schedule Thursday check - every Thursday at 11am SA time
    job_queue = app.job_queue
    job_queue.run_daily(
        thursday_check,
        time=datetime.strptime("11:00", "%H:%M").time().replace(tzinfo=SA_TZ),
        days=(3,),  # Thursday = 3
    )
    
    logger.info("BlackPurple Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
