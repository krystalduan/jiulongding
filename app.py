from dotenv import load_dotenv

from flask import Flask, render_template, request, redirect, url_for, jsonify, session
from datetime import datetime, timedelta
from functools import wraps
from html import escape
from itsdangerous import URLSafeSerializer, BadSignature
import logging
from pytz import timezone
import threading
import atexit
import gspread
import re
from oauth2client.service_account import ServiceAccountCredentials
import requests
import base64
import json
import os
import secrets

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ['SECRET_KEY']
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('FLASK_ENV') != 'development',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=64 * 1024,
)

# =============================================================================
# GOOGLE SHEETS — lazy connection
# =============================================================================

SCOPE = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/drive"]

google_creds = os.environ.get('GOOGLE_CREDENTIALS')
if google_creds:
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f:
        f.write(google_creds)
        creds_file = f.name
    CREDENTIALS = ServiceAccountCredentials.from_json_keyfile_name(creds_file, SCOPE)
    def _cleanup_creds():
        try:
            os.unlink(creds_file)
        except OSError:
            pass
    atexit.register(_cleanup_creds)
else:
    CREDENTIALS = ServiceAccountCredentials.from_json_keyfile_name(
        "jiulongding-9e2cffe41bca.json", SCOPE)

gc = gspread.authorize(CREDENTIALS)

_sheets_lock = threading.Lock()
_spreadsheet = None
_sheet = None

# Which spreadsheet to use. SPREADSHEET_KEY is the id from the sheet's URL:
#   docs.google.com/spreadsheets/d/<THIS PART>/edit
# Prefer it over the name — gc.open() matches by title, so two spreadsheets
# called "Restaurant Reservations" resolve unpredictably and bookings can
# land in the wrong one. The key is unique.
SPREADSHEET_KEY = os.environ.get('SPREADSHEET_KEY', '').strip()
SPREADSHEET_NAME = os.environ.get('SPREADSHEET_NAME', 'Restaurant Reservations').strip()
MASTER_WORKSHEET = os.environ.get('MASTER_WORKSHEET', 'Master Data').strip()


def _open_spreadsheet():
    if SPREADSHEET_KEY:
        sp = gc.open_by_key(SPREADSHEET_KEY)
        logger.info(f"Opened spreadsheet by key: {sp.title}")
        return sp
    sp = gc.open(SPREADSHEET_NAME)
    logger.warning(
        f"Opened spreadsheet by name '{SPREADSHEET_NAME}' (id {sp.id}). "
        "Set SPREADSHEET_KEY to remove any ambiguity between sheets sharing a name.")
    return sp


def get_sheets():
    global _spreadsheet, _sheet
    if _sheet is not None:
        return _spreadsheet, _sheet
    with _sheets_lock:
        if _sheet is not None:
            return _spreadsheet, _sheet
        sp = _open_spreadsheet()
        try:
            sh = sp.worksheet(MASTER_WORKSHEET)
            logger.info(f"Connected to Google Sheets: {sp.title} / {sh.title}")
        except gspread.exceptions.WorksheetNotFound:
            sh = sp.get_worksheet(0)
            logger.warning(f"'{MASTER_WORKSHEET}' not found, using: {sh.title}")
        _spreadsheet, _sheet = sp, sh
        _ensure_booked_at_header(sh)
    return _spreadsheet, _sheet


MASTER_BOOKED_AT_COL = 10          # column J, appended after Notes
MASTER_BOOKED_AT_HEADER = 'Booked At'


def _ensure_booked_at_header(sheet):
    """Label column J once, so the new timestamp column isn't a blank header.

    Only writes when the cell is genuinely empty — never overwrites an
    existing label. Runs once per process, and any failure is swallowed:
    a missing header must never stop a booking being saved.
    """
    try:
        header = sheet.row_values(1)
        existing = header[MASTER_BOOKED_AT_COL - 1] if len(header) >= MASTER_BOOKED_AT_COL else ''
        if existing.strip():
            return
        sheet.update_cell(1, MASTER_BOOKED_AT_COL, MASTER_BOOKED_AT_HEADER)
        logger.info(f"Added '{MASTER_BOOKED_AT_HEADER}' header to Master Data column J")
    except Exception as e:
        logger.warning(f"Could not set '{MASTER_BOOKED_AT_HEADER}' header: {e}")


def _warmup_sheets():
    try:
        get_sheets()
    except Exception as e:
        logger.warning(f"Sheets warmup failed: {e}")

# =============================================================================
# SMS API
# =============================================================================

API_URL = "https://api.mobilemessage.com.au/v1/messages"
API_USERNAME = os.environ.get('API_USERNAME')
API_PASSWORD = os.environ.get('API_PASSWORD')
auth_string = f"{API_USERNAME}:{API_PASSWORD}"
AUTH_HEADER = base64.b64encode(auth_string.encode()).decode()

# =============================================================================
# TIMEZONE
# =============================================================================
#
# Day-of SMS used to be sent by an in-process APScheduler. That was removed:
#   - it ran once per gunicorn worker, so every customer got a duplicate SMS;
#   - fly.toml sets min_machines_running = 0 with auto_stop_machines, so at
#     8:30 AM the machine is usually stopped and the job never fired at all.
#
# The job is now driven entirely by GitHub Actions calling
# /api/send-sms-cron (see .github/workflows/send-daily-sms.yml), which also
# wakes the machine. send_sms_on_date() is idempotent, so repeat calls on the
# same day are safe.

sydney_tz = timezone('Australia/Sydney')

USE_RELOADER = __name__ == '__main__' and os.environ.get('FLASK_RELOAD', '1') != '0'

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def generate_reservation_id():
    _, sheet = get_sheets()
    all_data = sheet.get_all_values()
    return max(len(all_data) - 1, 0) + 1


def clean_phone(phone):
    """Normalise to 61XXXXXXXXX. Returns None if not a valid AU mobile number."""
    if not phone:
        return None
    cleaned = re.sub(r'\D', '', str(phone))
    if cleaned.startswith('0011'):
        cleaned = cleaned[4:]
    if cleaned.startswith('61'):
        normalised = cleaned
    elif cleaned.startswith('0'):
        normalised = '61' + cleaned[1:]
    elif len(cleaned) == 9 and cleaned.startswith('4'):
        normalised = '61' + cleaned
    else:
        normalised = cleaned

    # AU mobiles: 61 + 4XXXXXXXX  (11 digits total)
    if re.fullmatch(r'614\d{8}', normalised):
        return normalised

    logger.warning("Rejected invalid Australian mobile number")
    return None

# =============================================================================
# RESERVATION VALIDATION
# =============================================================================

# Server-side mirrors of the <option> values in index.html / book.html.
# The browser can only ever submit one of these; anything else is a bot.
VALID_TIMES = {"12:00", "12:30", "13:00", "13:30",
               "17:00", "17:30", "18:00", "18:30",
               "19:00", "19:30", "20:00", "20:30"}
VALID_PARTY_SIZES = {"1-2", "3-4", "4-6", "7-10", "10+"}
VALID_DISH_TYPES = {"大火锅", "小火锅", "炒菜"}

# Zero-padded HH:MM sorts correctly as text, so this is service order.
ORDERED_TIMES = sorted(VALID_TIMES)

MAX_ADVANCE_DAYS = 32      # client picker allows ~1 month; keep server slightly lenient
# How far ahead a customer may move an existing booking, counted from the day
# they are making the change — not from the date the booking currently holds,
# which would let a booking walk itself forward a month at a time.
MAX_RESCHEDULE_DAYS = 30
MIN_LEAD_MINUTES = 120     # same-day bookings must be at least 2 hours out
MIN_FILL_SECONDS = 3       # a human cannot complete the form faster than this
# Moving a booking that is already today needs the same 2 hours of notice as
# making a new one, so the kitchen never learns of a change inside its prep
# window. Moving a later booking *into* today is not self-service at all —
# see reschedule_dates().
MIN_RESCHEDULE_MINUTES = MIN_LEAD_MINUTES

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$')
NAME_RE = re.compile(r'^[^\d<>{}\[\]\\/|]+$')

MAX_LENGTHS = {'name': 80, 'email': 120, 'notes': 300}


# =============================================================================
# SELF-SERVICE MANAGE LINK
# =============================================================================
#
# The confirmation email carries a link to /manage/<token>. The token is the
# whole credential, so it is signed with SECRET_KEY and carries everything
# needed to find the booking. Nothing is stored: no extra sheet column and no
# lookup just to validate.
#
# Expiry is not baked into the signature — it comes from the booking date in
# the payload, which is itself signed and so cannot be edited. A link works
# until the day of the booking has passed.
#
# Note: rotating SECRET_KEY invalidates every outstanding link. That is the
# accepted trade for keeping this stateless.

MANAGE_TOKEN_SALT = 'jld-manage-booking-v1'
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', 'https://jiulongding.au').rstrip('/')

_manage_serializer = URLSafeSerializer(app.secret_key, salt=MANAGE_TOKEN_SALT)


def make_manage_token(reservation_id, date, email):
    """Signed handle for one booking. Safe to put in a URL."""
    return _manage_serializer.dumps({
        'r': str(reservation_id),
        'd': str(date),
        'e': str(email or '').strip().lower(),
    })


def read_manage_token(token):
    """Returns (payload, error_code). error_code is None when the token is good.

    Codes: 'invalid' (missing, tampered or signed with another key),
           'expired' (the booking date has passed).
    """
    try:
        payload = _manage_serializer.loads(token)
    except BadSignature:
        return None, 'invalid'
    except Exception:
        return None, 'invalid'

    if not isinstance(payload, dict) or not payload.get('d') or not payload.get('r'):
        return None, 'invalid'

    try:
        booking_date = datetime.strptime(payload['d'], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None, 'invalid'

    if booking_date < datetime.now(sydney_tz).date():
        return None, 'expired'

    return payload, None


def manage_url(reservation_id, date, email):
    return f"{PUBLIC_BASE_URL}/manage/{make_manage_token(reservation_id, date, email)}"


def available_times_for(target_date_str):
    """Slots a customer may move to on a given date.

    Every slot for a future date; on today, only those at least
    MIN_RESCHEDULE_MINUTES away. Past dates offer nothing.
    """
    try:
        target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return []

    now = datetime.now(sydney_tz)
    if target_date > now.date():
        return list(ORDERED_TIMES)
    if target_date < now.date():
        return []

    minutes_now = now.hour * 60 + now.minute
    out = []
    for slot in ORDERED_TIMES:
        hour, minute = (int(p) for p in slot.split(':'))
        if (hour * 60 + minute) - minutes_now >= MIN_RESCHEDULE_MINUTES:
            out.append(slot)
    return out


def reschedule_dates(current_date_str):
    """Dates a customer may move this booking to.

    Today is only ever offered to a booking that is *already* today. A booking
    for tomorrow or later cannot be pulled forward into today online: that is a
    same-day change to the kitchen's numbers, so it goes through the phone.
    Moving out of today to a later date is always fine — it frees a table
    rather than adding one.
    """
    try:
        current_date = datetime.strptime(current_date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return []

    today = datetime.now(sydney_tz).date()
    if current_date < today:
        return []

    # Only a booking already on today keeps today as an option, and then only
    # while a slot far enough out remains.
    first = today if (current_date == today and available_times_for(str(today))) \
        else today + timedelta(days=1)

    return [str(first + timedelta(days=n))
            for n in range((today + timedelta(days=MAX_RESCHEDULE_DAYS) - first).days + 1)]


def describe_date(date_str):
    """'2026-08-13' -> 'Thursday, 13 August 2026', for the picker and emails."""
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').strftime('%A, %d %B %Y')
    except (ValueError, TypeError):
        return date_str


WEEKDAYS_ZH = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日']


def describe_date_zh(date_str):
    """'2026-08-13' -> '2026年8月13日 星期四'."""
    try:
        d = datetime.strptime(date_str, '%Y-%m-%d')
    except (ValueError, TypeError):
        return date_str
    return f'{d.year}年{d.month}月{d.day}日 {WEEKDAYS_ZH[d.weekday()]}'


# The sheet stores the dish type in Chinese, because that is what the booking
# form submits and what the kitchen reads. An English reader needs it back in
# English rather than a character they cannot place.
DISH_TYPE_EN = {
    '大火锅': 'Shared Hotpot',
    '小火锅': 'Individual Hotpot',
    '炒菜': 'Stir-fry',
}


def dish_in_english(value):
    """Chinese dish type as stored -> English. Unknown values pass through."""
    return DISH_TYPE_EN.get(str(value or '').strip(), value)


def dish_bilingual(value):
    """'大火锅' -> '大火锅 · Shared Hotpot'.

    For the confirmation email and the page it links from, which have no
    language switch: the guest chose the dish in Chinese, so that stays, with
    the English alongside it rather than in place of it.
    """
    english = dish_in_english(value)
    return f'{value} · {english}' if english and english != value else (value or '')


def sanitize_for_sheet(value):
    """Neutralise spreadsheet formula injection (=IMPORTXML(...), @, +, -)."""
    text = str(value or '').replace('\x00', '').strip()
    if text and text[0] in ('=', '+', '@', '\t', '\r'):
        return "'" + text
    if text.startswith('-') and not re.fullmatch(r'-?\d+(\.\d+)?', text):
        return "'" + text
    return text


def validate_reservation(form):
    """Returns (data, error_message). data is None when validation fails."""
    name = (form.get("name") or "").strip()
    email = (form.get("email") or "").strip()
    people = (form.get("people") or "").strip()
    date = (form.get("date") or "").strip()
    time = (form.get("time") or "").strip()
    dish_type = (form.get("dish-type") or "").strip()
    notes = (form.get("notes") or "").strip()
    phone = clean_phone(form.get("phone"))

    if not name or not email or not people or not date or not time or not dish_type:
        return None, "All fields are required. Please fill out the entire form."

    for field, value in (('name', name), ('email', email), ('notes', notes)):
        if len(value) > MAX_LENGTHS[field]:
            return None, f"Your {field} is too long."

    if not NAME_RE.match(name):
        return None, "Please enter a valid name."

    if not EMAIL_RE.match(email) or len(email) < 6:
        return None, "Please enter a valid email address."

    if not phone:
        return None, "Please enter a valid Australian mobile number (e.g. 0412345678)."

    if people not in VALID_PARTY_SIZES:
        return None, "Please select a party size."

    if time not in VALID_TIMES:
        return None, "Please select a booking time."

    if dish_type not in VALID_DISH_TYPES:
        return None, "Please select a type of dish."

    try:
        booking_date = datetime.strptime(date, '%Y-%m-%d').date()
    except ValueError:
        return None, "Please select a valid date."

    now = datetime.now(sydney_tz)
    today = now.date()
    if booking_date < today:
        return None, "Bookings cannot be made for a past date."
    if booking_date > today + timedelta(days=MAX_ADVANCE_DAYS):
        return None, "Bookings can only be made up to one month in advance."

    if booking_date == today:
        hour, minute = (int(p) for p in time.split(':'))
        minutes_until = (hour * 60 + minute) - (now.hour * 60 + now.minute)
        if minutes_until < MIN_LEAD_MINUTES:
            return None, "Same-day bookings must be made at least 2 hours ahead. Please call us on +61 423 987 048."

    return {
        'name': sanitize_for_sheet(name),
        'email': sanitize_for_sheet(email),
        'phone': phone,
        'people': people,
        'date': date,
        'time': time,
        'dish_type': dish_type,
        'notes': sanitize_for_sheet(notes),
    }, None

# =============================================================================
# RATE LIMITING (in-memory, per worker process)
# =============================================================================

_rate_lock = threading.Lock()
_rate_hits = {}


def secure_equals(a, b):
    """Timing-safe comparison that tolerates non-ASCII secrets."""
    return secrets.compare_digest(str(a or '').encode('utf-8'), str(b or '').encode('utf-8'))


def client_ip():
    return (request.headers.get('Fly-Client-IP')
            or (request.headers.get('X-Forwarded-For', '').split(',')[0].strip())
            or request.remote_addr
            or 'unknown')


def rate_limited(bucket, limit, window_seconds):
    """True if this client has exceeded `limit` requests in `window_seconds`."""
    key = f"{bucket}:{client_ip()}"
    now = datetime.now().timestamp()
    cutoff = now - window_seconds
    with _rate_lock:
        hits = [t for t in _rate_hits.get(key, []) if t > cutoff]
        # opportunistic cleanup so the dict cannot grow without bound
        if len(_rate_hits) > 5000:
            for k in [k for k, v in _rate_hits.items() if not v or max(v) < cutoff]:
                _rate_hits.pop(k, None)
        if len(hits) >= limit:
            _rate_hits[key] = hits
            return True
        hits.append(now)
        _rate_hits[key] = hits
        return False


RESTAURANT_PHONE = '+61 423 987 048'
RESTAURANT_ADDRESS = '71 Dixon Street (up the stairs), Haymarket, Sydney NSW 2000'

# Web fonts are stripped by Gmail and most clients, so these are stacks of
# fonts already installed on the reader's machine. Both are old-style serifs
# for a 复古 feel: Garamond/Palatino for English, and a Song/Ming face for
# Chinese — the traditional typeface of printed Chinese text.
EMAIL_FONT = ("'EB Garamond', Garamond, 'Hoefler Text', 'Palatino Linotype', "
              "Palatino, 'Book Antiqua', Georgia, 'Times New Roman', serif")
EMAIL_FONT_CN = ("'Songti SC', STSong, 'Noto Serif SC', 'Source Han Serif SC', "
                 "SimSun, 'Songti TC', STKaiti, KaiTi, serif")


def format_phone_display(phone):
    """61412345678 -> +61 412 345 678"""
    digits = re.sub(r'\D', '', str(phone or ''))
    if re.fullmatch(r'61\d{9}', digits):
        return f'+61 {digits[2:5]} {digits[5:8]} {digits[8:]}'
    return phone or ''


def build_confirmation_email(customer_name, d):
    """Returns (subject, html_body, text_body) for a reservation confirmation."""
    try:
        date_obj = datetime.strptime(d['date'], '%Y-%m-%d')
        formatted_date = date_obj.strftime('%A, %d %B %Y')   # in the email body
        subject_date = date_obj.strftime('%d/%m/%Y')         # in the subject line
    except Exception:
        formatted_date = subject_date = d['date']

    phone = format_phone_display(d.get('phone'))
    # Chinese as chosen, with the English gloss beside it — an email has no
    # language switch, so it has to carry both.
    dish = dish_bilingual(d.get('dish_type')) or 'Not specified'
    people = d.get('people', '')

    # Self-service link. Only produced when we know which booking this is —
    # a resend without a reservation id simply omits the button.
    manage_link = ''
    if d.get('reservation_id'):
        try:
            manage_link = manage_url(d['reservation_id'], d['date'], d.get('email'))
        except Exception:
            logger.exception("Could not build manage link; sending email without it")

    subject = f"Your reservation at JiuLongDing Hotpot | {subject_date}"

    # Table-wrapped anchor rather than a padded <a>: Outlook renders mail with
    # Word, which drops padding on inline elements, so a plain styled link
    # collapses to bare text there.
    manage_button_html = ''
    if manage_link:
        manage_button_html = f"""
        <tr><td align="center" style="padding:4px 32px 30px;">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0">
            <tr><td bgcolor="#8B1A1A" style="border-radius:8px;">
              <a href="{escape(manage_link, quote=True)}"
                 style="display:inline-block;padding:13px 28px;font-family:{EMAIL_FONT};
                        font-size:15px;color:#ffffff;text-decoration:none;font-weight:600;
                        border-radius:8px;">Change or cancel your booking&nbsp;·&nbsp;<span
                        style="font-family:{EMAIL_FONT_CN};">更改或取消预订</span></a>
            </td></tr>
          </table>
          <p style="margin:12px 0 0;font-family:{EMAIL_FONT};font-size:12px;color:#a89a91;">
            Same-day changes need at least 2 hours' notice.<br>
            <span style="font-family:{EMAIL_FONT_CN};">当天更改需提前至少 2 小时。</span></p>
        </td></tr>"""

    # Raw values here — row_html escapes each one exactly once. The Chinese
    # label sits under the English one in its own font stack, because the
    # serif stack above has no Chinese glyphs and clients would substitute
    # something that does not match the rest of the mail.
    rows = [
        ('Name', '姓名', str(customer_name)),
        ('Date', '日期', formatted_date),
        ('Time', '时间', d['time']),
        ('Party size', '人数', f'{people} people'),
        ('Dish type', '锅底', dish),
        ('Contact', '电话', phone),
    ]
    row_html = ''.join(
        f'''
        <tr>
          <td style="padding:11px 0;border-bottom:1px solid #ece3dc;
                     font-size:13px;letter-spacing:.06em;text-transform:uppercase;
                     color:#8a7f77;width:38%;">{escape(label)}<br>
            <span style="font-family:{EMAIL_FONT_CN};font-size:12px;
                         letter-spacing:.04em;color:#a89a91;">{escape(zh)}</span></td>
          <td style="padding:11px 0;border-bottom:1px solid #ece3dc;
                     font-size:16px;color:#1C1008;font-weight:600;">{escape(str(value))}</td>
        </tr>''' for label, zh, value in rows)

    html_body = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(subject)}</title></head>
<body style="margin:0;padding:0;background:#FAF7F2;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;">
    {formatted_date} at {d['time']} for {people} people. We hold tables for 15 minutes.
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:#FAF7F2;padding:28px 12px;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="max-width:560px;background:#ffffff;border-radius:14px;overflow:hidden;
                    border:1px solid #eee2d8;">

        <tr><td align="center" style="background:#8B1A1A;padding:30px 32px 26px;">
          <div style="font-family:{EMAIL_FONT_CN};font-size:34px;color:#ffffff;
                      letter-spacing:.22em;line-height:1.15;text-indent:.22em;">九龙鼎</div>
          <div style="font-family:{EMAIL_FONT};font-size:19px;color:#f7e3d8;
                      margin-top:10px;letter-spacing:.05em;">JiuLongDing</div>
          <div style="font-family:{EMAIL_FONT};font-size:11px;color:#dda9a2;
                      margin-top:6px;letter-spacing:.24em;text-transform:uppercase;
                      text-indent:.24em;">Chongqing Hotpot</div>
        </td></tr>

        <tr><td style="padding:30px 32px 4px;">
         
        </td></tr>

        <tr><td style="padding:20px 32px 0;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                 style="font-family:{EMAIL_FONT};border-top:1px solid #ece3dc;">
            {row_html}
          </table>
        </td></tr>

        <tr><td style="padding:26px 32px 0;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                 style="background:#FAF7F2;border-radius:10px;">
            <tr><td style="padding:18px 20px;font-family:{EMAIL_FONT};">
              <div style="font-size:12px;letter-spacing:.1em;text-transform:uppercase;
                          color:#8a7f77;margin-bottom:7px;">Finding us</div>
              <div style="font-size:15px;color:#1C1008;line-height:1.6;">
                71 Dixon Street <span style="color:#8a7f77;">(up the stairs)</span><br>
                Haymarket, Sydney NSW 2000</div>
              <div style="margin-top:11px;font-size:15px;">
                <a href="tel:+61423987048"
                   style="color:#8B1A1A;text-decoration:none;font-weight:600;">
                   {RESTAURANT_PHONE}</a></div>
            </td></tr>
          </table>
        </td></tr>

        <tr><td style="padding:22px 32px {'14px' if manage_link else '30px'};">
          <p style="margin:0;font-family:{EMAIL_FONT};font-size:14px;
                    line-height:1.7;color:#5d534c;">
            We hold tables for <strong style="color:#1C1008;">15 minutes</strong>.
            {'Need to make a change? Use the button below.'
             if manage_link else
             'To change or cancel, please call us with your name and booking date.'}<br>
            <span style="font-family:{EMAIL_FONT_CN};font-size:13px;">
              我们为您保留座位 15 分钟。{'如需更改，请点击下方按钮。'
              if manage_link else '如需更改或取消，请致电我们。'}</span></p>
        </td></tr>

        {manage_button_html}

        <tr><td align="center"
                style="background:#faf5f1;padding:20px 32px;border-top:1px solid #f0e6dd;">
          <p style="margin:0;font-family:{EMAIL_FONT};font-size:13px;
                    line-height:1.7;color:#8a7f77;">
            JiuLongDing Chongqing Hotpot ·
            <span style="font-family:{EMAIL_FONT_CN};">九龙鼎重庆火锅</span><br>
            {RESTAURANT_ADDRESS}</p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    if manage_link:
        manage_text = (f"\nCHANGE OR CANCEL 更改或取消\n  {manage_link}\n"
                       "  (same-day changes need at least 2 hours' notice)\n"
                       "  (当天更改需提前至少 2 小时)\n")
    else:
        manage_text = ("To change or cancel, please call us with your name\n"
                       "and booking date. 如需更改或取消，请致电我们。\n")

    detail_lines = '\n'.join(f'  {label} {zh}: {value}' for label, zh, value in rows)
    text_body = f"""JIULONGDING 九龙鼎 - CHONGQING HOTPOT


YOUR BOOKING 您的预订
{detail_lines}

FINDING US 地址
  71 Dixon Street (up the stairs)
  Haymarket, Sydney NSW 2000
  {RESTAURANT_PHONE}

We hold tables for 15 minutes. 我们为您保留座位 15 分钟。
{manage_text}
JiuLongDing Chongqing Hotpot (九龙鼎重庆火锅)
{RESTAURANT_ADDRESS}
"""
    return subject, html_body, text_body


def send_confirmation_email(customer_email, customer_name, reservation_details):
    try:
        logger.info(f"Sending email to {customer_email}")
        subject, html_body, text_body = build_confirmation_email(
            customer_name, reservation_details)

        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {os.environ.get('RESEND_API_KEY')}"},
            json={
                "from": "JiuLongDing Hotpot <reservations@jiulongding.au>",
                "to": [customer_email],
                # A real reply-to is a positive signal and lets guests actually
                # reach the restaurant instead of a dead no-reply address.
                "reply_to": os.environ.get('REPLY_TO_EMAIL', 'reservations@jiulongding.au'),
                "subject": subject,
                "html": html_body,
                "text": text_body,
            },
            timeout=10
        )
        if response.status_code != 200:
            logger.error(f"Resend error {response.status_code}: {response.text}")
            return False

        logger.info(f"Confirmation email sent to {customer_email}")
        return True

    except Exception:
        logger.exception(f"Error sending email to {customer_email}")
        return False


def send_email_async(email, name, reservation_data):
    try:
        if send_confirmation_email(email, name, reservation_data):
            logger.info(f"Background email sent to {email}")
        else:
            logger.warning(f"Background email failed for {email}")
    except Exception as e:
        logger.error(f"Background email error: {e}")


DATE_TAB_HEADERS = ["Name", "Time", "People", "Phone", "Email", "Date",
                    "Dish Type", "Notes", "Confirmed", "Reservation ID",
                    "SMS Reply", "Confirmation Method"]


def get_or_create_date_sheet(spreadsheet, date):
    """The tab for one date, created with headers if it does not exist yet.

    Shared by new bookings and by a customer moving an existing booking to a
    date nobody has booked yet.
    """
    sheet_name = str(date).replace('/', '-')
    try:
        return spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        date_sheet = spreadsheet.add_worksheet(title=sheet_name, rows="100", cols="12")
        date_sheet.append_row(DATE_TAB_HEADERS)
        date_sheet.format("A1:L1", {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.2, "green": 0.6, "blue": 0.9}
        })
        return date_sheet


def create_date_sheet(name, phone, email, people, date, time, dish_type, notes, reservation_id):
    spreadsheet, _ = get_sheets()
    try:
        date_sheet = get_or_create_date_sheet(spreadsheet, date)
        date_sheet.append_row([name, time, people, phone, email, date,
                                dish_type, notes, "Pending", reservation_id or ""])
    except Exception as e:
        logger.error(f"Error creating/updating date sheet: {e}")


def send_sms(to_number, message_text, custom_ref=None):
    payload = {
        "messages": [{
            "to": to_number,
            "message": message_text,
            "sender": "61485900077"
        }]
    }
    if custom_ref:
        payload["messages"][0]["custom_ref"] = custom_ref

    logger.info("SMS payload: %s", json.dumps(payload))

    try:
        response = requests.post(
            API_URL,
            headers={"Content-Type": "application/json", "Authorization": f"Basic {AUTH_HEADER}"},
            json=payload,
            timeout=10
        )
        if response.status_code != 200:
            logger.error(f"SMS API error {response.status_code}: {response.text}")
            return None
        response_data = response.json()
        logger.info("SMS API response: %s", json.dumps(response_data))
        return response_data
    except Exception as e:
        logger.error(f"Error sending SMS: {e}")
        return None


def send_sms_on_date(target_date, message_type="day_of"):
    spreadsheet, _ = get_sheets()
    try:
        sheet_name = target_date.replace('/', '-')
        try:
            date_sheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            return f"No reservations found for {target_date}"

        all_data = date_sheet.get_all_values()
        sent_count = 0
        failed_count = 0
        skipped_count = 0
        batch_updates = []

        # Idempotency marker written into column K. It carries the send date,
        # so calling this twice in one day is a no-op while tomorrow's run
        # still goes ahead. Without this, nothing stops a duplicate SMS: the
        # loop keys off column I ("Pending"), which sending does not change.
        now = datetime.now(sydney_tz)
        today_stamp = now.strftime('%d/%m/%y')
        sent_marker_prefix = f"{message_type} sent {today_stamp}"

        for i, row in enumerate(all_data[1:], start=2):
            if len(row) < 9:
                continue
            name, time, people, phone = row[0], row[1], row[2], row[3]
            confirmed = row[8]
            sms_note = row[10] if len(row) > 10 else ''

            if confirmed != "Pending" or not phone:
                continue

            # A previous failure is left to retry; only a success blocks a resend.
            if sent_marker_prefix in sms_note:
                skipped_count += 1
                logger.info(f"Row {i}: {message_type} already sent today, skipping")
                continue

            sms_message = (
                f"Hi {name}! This is a reminder of your reservation today "
                f"at {time} for {people} people.\n"
                f"Reply Y to confirm or N to cancel.\n"
                f"Location: 71 Dixon St (up the stairs), Haymarket - JLD Hotpot"
            )
            result = send_sms(phone, sms_message,
                              custom_ref=f"{message_type}_{now.timestamp()}")
            stamp = now.strftime('%d/%m/%y %H:%M')
            if result:
                sent_count += 1
                batch_updates.append(
                    {'range': f'K{i}', 'values': [[f"{message_type} sent {stamp}"]]})
            else:
                failed_count += 1
                batch_updates.append(
                    {'range': f'K{i}', 'values': [[f"{message_type} failed {stamp}"]]})

        if batch_updates:
            date_sheet.batch_update(batch_updates)
        return (f"SMS Summary for {target_date}: {sent_count} sent, "
                f"{failed_count} failed, {skipped_count} already sent today")

    except Exception as e:
        return f"Error sending SMS for {target_date}: {e}"

# =============================================================================
# CRON ENDPOINT
# =============================================================================

@app.route('/api/send-sms-cron')
def send_sms_cron():
    secret = request.args.get('secret', '')
    cron_secret = os.environ.get('CRON_SECRET', '')
    if not cron_secret or not secure_equals(secret, cron_secret):
        return jsonify({'status': 'unauthorized'}), 401
    today = datetime.now(sydney_tz).strftime('%Y-%m-%d')
    result = send_sms_on_date(today, message_type="day_of")
    logger.info(f"Cron SMS job: {result}")
    return jsonify({'status': 'ok', 'result': result})

# =============================================================================
# SEO ROUTES
# =============================================================================

@app.before_request
def redirect_old_domain():
    if request.host == 'jiulongding.onrender.com':
        new_url = request.url.replace('jiulongding.onrender.com', 'jiulongding.au', 1)
        return redirect(new_url, code=301)


@app.route('/robots.txt')
def robots_txt():
    return "User-agent: *\nAllow: /\nSitemap: https://jiulongding.au/sitemap.xml\n", 200, {'Content-Type': 'text/plain'}


@app.route('/sitemap.xml')
def sitemap():
    xml = '''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://jiulongding.au/</loc>
    <changefreq>monthly</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://jiulongding.au/book</loc>
    <changefreq>monthly</changefreq>
    <priority>0.9</priority>
  </url>
</urlset>'''
    return xml, 200, {'Content-Type': 'application/xml'}

# =============================================================================
# CUSTOMER-FACING ROUTES
# =============================================================================

# A customer can legitimately have more than one booking page open — a tab on
# / and another on /book, or a page reopened from history. Tracking a single
# token meant the most recent page load silently invalidated every other one.
MAX_OPEN_FORMS = 6
MAX_REMEMBERED_SUBMISSIONS = 6

FORM_FIELDS = ('name', 'email', 'phone', 'people', 'date', 'time', 'dish-type', 'notes')


def issue_form_token():
    token = secrets.token_hex(16)
    pending = session.get('form_tokens', [])
    pending.append([token, datetime.now().timestamp()])
    session['form_tokens'] = pending[-MAX_OPEN_FORMS:]
    return token


def consume_form_token(submitted):
    """Claim a one-time form token.

    Returns (status, issued_at) where status is one of:
      'ok'        valid and unused; it is now spent
      'duplicate' already used — a refresh or back-button resubmit
      'unknown'   never issued to this session, or the session has expired
    """
    if not submitted:
        return 'unknown', None

    pending = session.get('form_tokens', [])
    for index, entry in enumerate(pending):
        token, issued_at = entry[0], entry[1]
        if secure_equals(token, submitted):
            session['form_tokens'] = pending[:index] + pending[index + 1:]
            spent = session.get('spent_form_tokens', [])
            spent.append(token)
            session['spent_form_tokens'] = spent[-MAX_REMEMBERED_SUBMISSIONS:]
            return 'ok', issued_at

    for token in session.get('spent_form_tokens', []):
        if secure_equals(token, submitted):
            return 'duplicate', None

    return 'unknown', None


@app.route("/")
def home():
    threading.Thread(target=_warmup_sheets, daemon=True).start()
    return render_template("index.html", form_token=issue_form_token())


@app.route("/book")
def book():
    threading.Thread(target=_warmup_sheets, daemon=True).start()
    return render_template("book.html", form_token=issue_form_token())


def reservation_error(message, status=400):
    """Re-render the booking form with the customer's answers still in it.

    Losing all eight fields to one mistyped digit was the quickest way to lose
    a booking, so everything except the honeypot comes back with the page.
    """
    source = request.form.get('form_source')
    if source not in ('index', 'book'):
        # Pages cached before form_source existed still fall back to the referrer.
        source = 'book' if (request.referrer or '').rstrip('/').endswith('/book') else 'index'
    template = 'book.html' if source == 'book' else 'index.html'
    values = {field: request.form.get(field, '') for field in FORM_FIELDS}
    return render_template(template, error=message, form_token=issue_form_token(),
                           values=values), status


@app.route("/submit_reservation", methods=["POST"])
def submit_reservation_route():
    logger.info("Reservation form submitted")

    # Generous on attempts (people fat-finger their phone number), strict on
    # bookings that actually get written.
    if rate_limited('reservation_attempt', limit=20, window_seconds=3600):
        logger.warning(f"Reservation attempt rate limit hit for {client_ip()}")
        return reservation_error(
            "Too many booking attempts. Please try again later or call us on +61 423 987 048.", status=429)

    token_status, issued_at = consume_form_token(request.form.get('form_token'))

    if token_status == 'duplicate':
        # A refresh or back-button resubmit of a booking we already saved.
        # Show them the confirmation again rather than taking the order twice.
        logger.info("Duplicate submission ignored; returning customer to their confirmation")
        return redirect(url_for('reservation_success'))

    if token_status == 'unknown':
        # Expired session, cleared cookies, or a page that has been open for
        # days. Previously this dropped the customer on the homepage with no
        # message and no booking.
        logger.warning(f"Unrecognised form token from {client_ip()}")
        return reservation_error(
            "Your booking page had been open for a while, so we couldn't confirm the submission. "
            "Your details are still below — please press Submit once more.")

    # Honeypot: hidden from real users, irresistible to form-filling bots.
    if request.form.get('website'):
        logger.warning(f"Honeypot triggered from {client_ip()} - discarding submission")
        return reservation_error(
            "Sorry, we couldn't process that booking. Please check your details and try again.")

    if issued_at and datetime.now().timestamp() - issued_at < MIN_FILL_SECONDS:
        logger.warning(f"Form submitted too fast from {client_ip()}")
        return reservation_error(
            "That came through too quickly for us to check. Please press Submit once more.")

    data, error = validate_reservation(request.form)
    if error:
        logger.warning(f"Reservation rejected from {client_ip()}: {error}")
        return reservation_error(error)

    if rate_limited('reservation_booked', limit=5, window_seconds=3600):
        logger.warning(f"Booking rate limit hit for {client_ip()}")
        return reservation_error(
            "You've made several bookings already. For more, please call us on +61 423 987 048.", status=429)

    _, sheet = get_sheets()
    reservation_id = generate_reservation_id()
    # Column J: when the booking was submitted (Sydney time), so staff can tell
    # a booking made this morning from one made three weeks ago.
    booked_at = datetime.now(sydney_tz).strftime('%d/%m/%y %H:%M')
    sheet.append_row([reservation_id, data['name'], data['date'], data['time'], data['people'],
                      data['dish_type'], data['phone'], data['email'], data['notes'],
                      booked_at])

    reservation_data = dict(data, reservation_id=reservation_id)
    create_date_sheet(data['name'], data['phone'], data['email'], data['people'], data['date'],
                      data['time'], data['dish_type'], data['notes'], reservation_id)

    threading.Thread(target=send_email_async,
                     args=(data['email'], data['name'], reservation_data)).start()

    session['last_reservation'] = reservation_data
    return redirect(url_for('reservation_success'))


@app.route("/reservation_success")
def reservation_success():
    # Deliberately not popped: refreshing the confirmation, or resubmitting an
    # already-saved booking, should show the details again instead of bouncing
    # the customer to the homepage. The next booking overwrites it.
    reservation_data = session.get('last_reservation')
    if not reservation_data:
        return redirect('/')
    # The session holds the raw submitted values: an ISO date and a Chinese
    # dish type. Both need a readable form in each language.
    return render_template(
        'reservation_success.html',
        date_en=describe_date(reservation_data.get('date')),
        date_zh=describe_date_zh(reservation_data.get('date')),
        dish_type_en=dish_in_english(reservation_data.get('dish_type')),
        dish_type_both=dish_bilingual(reservation_data.get('dish_type')),
        **reservation_data)

# =============================================================================
# CUSTOMER SELF-SERVICE — cancel or move a booking
# =============================================================================
#
# The emailed link is GET-only and never changes anything. Both mutations are
# POST, because corporate mail scanners and some clients pre-fetch every URL
# in a message: a one-click GET /cancel would be silently triggered by a spam
# filter and cancel real bookings.

# Column positions in a date tab (0-based), per DATE_TAB_HEADERS.
COL_NAME, COL_TIME, COL_PEOPLE, COL_PHONE, COL_EMAIL = 0, 1, 2, 3, 4
COL_DATE, COL_DISH, COL_NOTES, COL_CONFIRMED, COL_RES_ID = 5, 6, 7, 8, 9
COL_METHOD = 11                       # column L
CANCELLED_STATUS = 'Cancelled'
# Left behind in the old date's tab when a customer moves to another day, so
# staff can see the booking was here and where it went, instead of a row that
# silently disappears overnight.
MODIFIED_STATUS = 'Modified'
# Statuses that are a record of something that is no longer happening. They
# sort below the live bookings and are never texted.
FINISHED_STATUSES = {CANCELLED_STATUS.lower(), MODIFIED_STATUS.lower(), 'no'}

# Column positions in Master Data (0-based), per submit_reservation_route().
MASTER_ID, MASTER_DATE, MASTER_TIME, MASTER_EMAIL = 0, 2, 3, 7


def _scan_tab(date_sheet, want_id, want_email):
    """Find one booking's row inside an already-opened date tab.

    Rows marked Modified are skipped: they are the record of a booking that
    used to be on this date, and the live row is in another tab.
    """
    for i, row in enumerate(date_sheet.get_all_values()[1:], start=2):
        if len(row) <= COL_RES_ID:
            continue
        if str(row[COL_RES_ID]).strip() != want_id:
            continue
        if str(row[COL_CONFIRMED]).strip().lower() == MODIFIED_STATUS.lower():
            continue
        # Second check: ids come from a row count, so an edited sheet could in
        # principle reuse one. The email must match the signed token too.
        if want_email and str(row[COL_EMAIL]).strip().lower() != want_email:
            logger.warning(f"Manage link: id {want_id} found but email mismatch")
            continue
        return i, row
    return None, None


def master_date_for(want_id, want_email):
    """Where Master Data says this booking now sits, or None.

    Master Data is the only index from reservation id to date, so it is what
    lets a link emailed before a date change still find the booking after it.
    """
    try:
        _, master = get_sheets()
        for row in master.get_all_values()[1:]:
            if len(row) <= MASTER_EMAIL:
                continue
            if str(row[MASTER_ID]).strip() != want_id:
                continue
            if want_email and str(row[MASTER_EMAIL]).strip().lower() != want_email:
                continue
            return str(row[MASTER_DATE]).strip().replace('/', '-')
    except Exception:
        logger.exception("Manage link: Master Data lookup failed")
    return None


def find_booking(payload):
    """Locate a booking's row in its date tab.

    Returns (date_sheet, row_number, row) or (None, None, None).

    The token carries the date the booking had when the link was sent. If the
    customer has since moved it, that tab no longer holds the row, so fall back
    to Master Data for its current date — otherwise every link already sitting
    in an inbox would break the moment someone changed their date.
    """
    spreadsheet, _ = get_sheets()
    want_id = str(payload['r']).strip()
    want_email = str(payload.get('e') or '').strip().lower()

    tried = str(payload['d']).replace('/', '-')
    candidates = [tried]

    moved_to = master_date_for(want_id, want_email)
    if moved_to and moved_to != tried:
        logger.info(f"Manage link: booking {want_id} has moved {tried} -> {moved_to}")
        candidates.append(moved_to)

    for sheet_name in candidates:
        try:
            date_sheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            logger.warning(f"Manage link: no date sheet for {sheet_name}")
            continue
        row_number, row = _scan_tab(date_sheet, want_id, want_email)
        if row is not None:
            return date_sheet, row_number, row

    return None, None, None


def update_master_booking(want_id, want_email, new_date, new_time):
    """Keep Master Data's date and time in step with a change.

    Best-effort: a booking that has been changed but whose index is stale is
    still a booking the customer can reach through the new link, so a failure
    here must not fail their change.
    """
    try:
        _, master = get_sheets()
        for i, row in enumerate(master.get_all_values()[1:], start=2):
            if len(row) <= MASTER_EMAIL:
                continue
            if str(row[MASTER_ID]).strip() != want_id:
                continue
            if want_email and str(row[MASTER_EMAIL]).strip().lower() != want_email:
                continue
            master.batch_update([
                {'range': f'C{i}', 'values': [[new_date]]},
                {'range': f'D{i}', 'values': [[new_time]]},
            ])
            return True
    except Exception:
        logger.exception("Manage link: Master Data update failed")
    return False


def move_booking_row(spreadsheet, old_sheet, row_number, row, new_date, new_time, note):
    """Move a booking into the tab for new_date, returning the new tab.

    The old row is kept and marked Modified rather than deleted, so a table
    that was on the books for this date leaves a trace: staff opening the day
    can see the booking moved and where it went. The live row is the new one.

    Written in that order on purpose — the destination row exists before the
    old one is marked, so a failure between the two leaves a booking that is
    still visibly live on its original date rather than one that is nowhere.
    """
    moved = list(row) + [''] * max(0, len(DATE_TAB_HEADERS) - len(row))
    moved[COL_TIME] = new_time
    moved[COL_DATE] = new_date
    moved[COL_METHOD] = note

    target = get_or_create_date_sheet(spreadsheet, new_date)
    target.append_row(moved[:len(DATE_TAB_HEADERS)])

    old_sheet.batch_update([
        {'range': f'I{row_number}', 'values': [[MODIFIED_STATUS]]},
        {'range': f'L{row_number}', 'values': [[note]]},
    ])
    return target


def booking_view(row):
    """Template-friendly view of a date-tab row."""
    def cell(index):
        return row[index] if len(row) > index else ''

    return {
        'name': cell(COL_NAME),
        'time': cell(COL_TIME),
        'people': cell(COL_PEOPLE),
        'phone': format_phone_display(cell(COL_PHONE)),
        # Stored in Chinese; the page shows whichever half the reader asked for.
        'dish_type': cell(COL_DISH),
        'dish_type_en': dish_in_english(cell(COL_DISH)),
        'date_raw': cell(COL_DATE),
        'date': describe_date(cell(COL_DATE)),
        'date_zh': describe_date_zh(cell(COL_DATE)),
        'status': cell(COL_CONFIRMED) or 'Pending',
        'reservation_id': cell(COL_RES_ID),
    }


def render_manage(state, token=None, booking=None, times=None, dates=None,
                  selected_date=None, notice=None, status_code=200):
    return render_template(
        'manage_booking.html',
        state=state,
        token=token,
        booking=booking,
        times=times or [],
        # (value, English label, Chinese label) — a <select> can only hold one
        # label per option, so the page swaps them when the language changes.
        dates=[(d, describe_date(d), describe_date_zh(d)) for d in (dates or [])],
        selected_date=selected_date,
        today=str(datetime.now(sydney_tz).date()),
        notice=notice,
        restaurant_phone=RESTAURANT_PHONE,
    ), status_code


def render_active(token, booking, notice=None, on_date=None, status_code=200):
    """The editable page, with the pickers built for whichever date is shown."""
    dates = reschedule_dates(booking['date_raw'])
    shown = on_date or booking['date_raw']

    # Late in the evening a booking for today has no slot left today, but it can
    # still be moved to a later day. Show the first date it *can* move to rather
    # than an empty time list, which would read as "no changes possible".
    if not available_times_for(shown) and dates:
        shown = dates[0]

    return render_manage('active', token=token, booking=booking,
                         dates=dates,
                         times=available_times_for(shown),
                         selected_date=shown,
                         notice=notice, status_code=status_code)


def load_managed_booking(token):
    """Shared front door for all three routes.

    Returns (payload, date_sheet, row_number, row, error_response). When
    error_response is not None the caller should return it unchanged.
    """
    payload, error = read_manage_token(token)
    if error == 'expired':
        return None, None, None, None, render_manage('expired', status_code=410)
    if error:
        return None, None, None, None, render_manage('invalid', status_code=400)

    try:
        date_sheet, row_number, row = find_booking(payload)
    except Exception:
        logger.exception("Manage link: sheet lookup failed")
        return None, None, None, None, render_manage('error', status_code=503)

    if row is None:
        return None, None, None, None, render_manage('notfound', status_code=404)

    return payload, date_sheet, row_number, row, None


@app.route('/manage/<token>')
def manage_booking(token):
    _, _, _, row, error_response = load_managed_booking(token)
    if error_response:
        return error_response

    booking = booking_view(row)
    if booking['status'].strip().lower().startswith('cancelled'):
        return render_manage('cancelled', token=token, booking=booking)

    # A move redirects here rather than rendering in place, so the address bar
    # ends up holding a link that still works. The details below are re-read
    # from the sheet, so this message only appears over a change that landed.
    notice = None
    if request.args.get('moved'):
        notice = ('success',
                  f"Your booking has been moved to {booking['date']} at {booking['time']}.",
                  f"您的预订已改为 {booking['date_raw']} {booking['time']}。")

    # ?on=<date> previews another day's slots, so picking a date can refresh the
    # time list. Only dates the customer may actually move to are honoured.
    wanted = (request.args.get('on') or '').strip()
    on_date = wanted if wanted in reschedule_dates(booking['date_raw']) else None

    return render_active(token, booking, notice=notice, on_date=on_date)


@app.route('/manage/<token>/cancel', methods=['POST'])
def manage_booking_cancel(token):
    if rate_limited('manage', limit=20, window_seconds=3600):
        return render_manage('error', status_code=429)

    payload, date_sheet, row_number, row, error_response = load_managed_booking(token)
    if error_response:
        return error_response

    booking = booking_view(row)
    if booking['status'].strip().lower().startswith('cancelled'):
        return render_manage('cancelled', token=token, booking=booking)

    stamp = datetime.now(sydney_tz).strftime('%d/%m/%y %H:%M')
    try:
        date_sheet.batch_update([
            {'range': f'I{row_number}', 'values': [[CANCELLED_STATUS]]},
            {'range': f'L{row_number}', 'values': [[f'Cancelled by customer {stamp}']]},
        ])
    except Exception:
        logger.exception("Manage link: cancel write failed")
        return render_active(token, booking, status_code=503,
                             notice=('error', "Sorry, something went wrong. "
                                              f"Please call us on {RESTAURANT_PHONE}.",
                                      f"抱歉，出了一点问题，请致电我们 {RESTAURANT_PHONE}。"))

    logger.info(f"Booking {booking['reservation_id']} cancelled by customer")
    # Marking column I stops the 8:30 reminder: that job only texts "Pending".
    booking['status'] = CANCELLED_STATUS
    return render_manage('cancelled', token=token, booking=booking)


@app.route('/manage/<token>/reschedule', methods=['POST'])
def manage_booking_reschedule(token):
    if rate_limited('manage', limit=20, window_seconds=3600):
        return render_manage('error', status_code=429)

    payload, date_sheet, row_number, row, error_response = load_managed_booking(token)
    if error_response:
        return error_response

    booking = booking_view(row)
    if booking['status'].strip().lower().startswith('cancelled'):
        return render_manage('cancelled', token=token, booking=booking)

    old_date, old_time = booking['date_raw'], booking['time']
    new_time = (request.form.get('time') or '').strip()
    # No date field at all means an older cached copy of the form: treat it as
    # a time-only change rather than rejecting the customer.
    new_date = (request.form.get('date') or old_date).strip()

    def reject(message, zh, on_date=None):
        return render_active(token, booking, on_date=on_date,
                             notice=('error', message, zh), status_code=400)

    if not new_time:
        return reject('Please choose a new time.', '请选择新的时间。')
    if new_time not in VALID_TIMES:
        return reject('That is not one of our service times.', '该时间不在营业时段内。')

    try:
        target = datetime.strptime(new_date, '%Y-%m-%d').date()
        current = datetime.strptime(old_date, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return reject('Please choose a valid date.', '请选择有效的日期。')

    today = datetime.now(sydney_tz).date()
    if target < today:
        return reject('That date has already passed.', '该日期已过。')
    if target > today + timedelta(days=MAX_RESCHEDULE_DAYS):
        return reject(f'Bookings can only be moved up to {MAX_RESCHEDULE_DAYS} days ahead.',
                      f'预订最多只能改到 {MAX_RESCHEDULE_DAYS} 天内。')

    # Pulling a later booking forward into today is a same-day change to the
    # kitchen's numbers, so it is not self-service.
    if target == today and current != today:
        return reject('To move a booking to today, please call us on '
                      f'{RESTAURANT_PHONE} so we can check we have room.',
                      f'如需改到今天，请致电我们 {RESTAURANT_PHONE}。')

    if new_date == old_date and new_time == old_time:
        return reject('That is already your booking time.', '这已经是您当前的预订时间。')

    times = available_times_for(new_date)
    if new_time not in times:
        if not times:
            return reject("It's too late to move to that day online. "
                          f'Please call us on {RESTAURANT_PHONE}.',
                          f'现在已无法在线更改，请致电我们 {RESTAURANT_PHONE}。',
                          on_date=new_date)
        return reject("Changes to a booking today need at least 2 hours' notice. "
                      f'Please pick a later time or call us on {RESTAURANT_PHONE}.',
                      '当天更改需提前至少 2 小时，请选择更晚的时间或致电我们。',
                      on_date=new_date)

    stamp = datetime.now(sydney_tz).strftime('%d/%m/%y %H:%M')
    moving_day = new_date != old_date
    note = (f'Moved by customer {old_date} {old_time} to {new_date} {new_time} {stamp}'
            if moving_day else
            f'Time changed by customer {old_time} to {new_time} {stamp}')

    try:
        if moving_day:
            # The row lives in the tab named after its date, so a new date means
            # a different tab.
            spreadsheet, _ = get_sheets()
            move_booking_row(spreadsheet, date_sheet, row_number, row,
                             new_date, new_time, note)
        else:
            date_sheet.batch_update([
                {'range': f'B{row_number}', 'values': [[new_time]]},
                {'range': f'L{row_number}', 'values': [[note]]},
            ])
    except Exception:
        logger.exception("Manage link: reschedule write failed")
        return reject('Sorry, something went wrong. '
                      f'Please call us on {RESTAURANT_PHONE}.',
                      f'抱歉，出了一点问题，请致电我们 {RESTAURANT_PHONE}。')

    logger.info(f"Booking {booking['reservation_id']}: {note}")
    # Keeps the staff's master list truthful, and is what lets the link already
    # sitting in the customer's inbox find the booking in its new tab.
    update_master_booking(str(booking['reservation_id']),
                          str(payload.get('e') or ''), new_date, new_time)

    if moving_day:
        # Redirect so the address bar ends up holding a token that points at
        # the new tab, and so the page shown is a fresh read of the moved row.
        return redirect(url_for('manage_booking',
                                token=make_manage_token(booking['reservation_id'],
                                                        new_date, payload.get('e')),
                                moved=1))

    booking['time'] = new_time
    return render_active(token, booking,
                         notice=('success', f'Your booking has been moved to {new_time}.',
                                 f'您的预订时间已改为 {new_time}。'))

# =============================================================================
# STAFF ROUTES
# =============================================================================

def require_staff_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('staff_authenticated'):
            return redirect('/staff/login')
        return f(*args, **kwargs)
    return decorated_function


@app.route("/staff")
def staff_login():
    return render_template('staff_login.html')


@app.route("/staff/login", methods=["POST"])
def staff_login_post():
    if rate_limited('staff_login', limit=8, window_seconds=900):
        logger.warning(f"Staff login rate limit hit from {client_ip()}")
        return render_template('staff_login.html',
                               error="Too many attempts. Please wait 15 minutes."), 429

    password = request.form.get('password') or ''
    if secure_equals(password, os.environ['STAFF_PASSWORD']):
        session.clear()
        session['staff_authenticated'] = True
        session.permanent = True
        return redirect('/staff/dashboard')

    logger.warning(f"Failed staff login from {client_ip()}")
    return render_template('staff_login.html', error="Invalid password"), 401


@app.route("/staff/dashboard")
@require_staff_auth
def staff_dashboard():
    return render_template('dashboard.html', default_date=datetime.now().strftime('%Y-%m-%d'))


@app.route("/staff/api/reservations/<date>")
@require_staff_auth
def get_reservations(date):
    spreadsheet, _ = get_sheets()
    try:
        sheet_name = date.replace('/', '-')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', sheet_name):
            return jsonify({'success': False, 'message': 'Invalid date', 'reservations': []}), 400
        try:
            date_sheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            return jsonify({'success': False, 'message': f'No reservations found for {date}', 'reservations': []})

        all_data = date_sheet.get_all_values()
        if len(all_data) <= 1:
            return jsonify({'success': False, 'message': f'No reservations found for {date}', 'reservations': []})

        reservations = []
        for i, row in enumerate(all_data[1:], start=2):
            if len(row) >= 9:
                reservations.append({
                    'row_number': i,
                    'name': row[0] if len(row) > 0 else '',
                    'time': row[1] if len(row) > 1 else '',
                    'people': row[2] if len(row) > 2 else '',
                    'phone': row[3] if len(row) > 3 else '',
                    'email': row[4] if len(row) > 4 else '',
                    'date': row[5] if len(row) > 5 else '',
                    'dish_type': row[6] if len(row) > 6 else '',
                    'notes': row[7] if len(row) > 7 else '',
                    'confirmed': row[8] if len(row) > 8 else 'Pending',
                    'reservation_id': row[9] if len(row) > 9 else ''
                })

        def parse_time(time_str):
            for fmt in ('%H:%M', '%I:%M %p'):
                try:
                    return datetime.strptime(time_str, fmt).time()
                except ValueError:
                    continue
            return datetime.strptime('12:00', '%H:%M').time()

        # Tables still to serve come first, in service order. Cancelled and
        # moved bookings are kept for the record but sink to the bottom, so a
        # glance down the page is the run of the night.
        reservations.sort(key=lambda x: (
            x['confirmed'].strip().lower() in FINISHED_STATUSES,
            parse_time(x['time']),
        ))

        return jsonify({
            'success': True,
            'message': f'Found {len(reservations)} reservations for {date}',
            'reservations': reservations,
            'total_confirmed': len([r for r in reservations if r['confirmed'].lower() in ['confirmed', 'yes']]),
            'total_pending': len([r for r in reservations if r['confirmed'].lower() in ['pending', 'no', '']]),
            'total_people': sum([int(r['people']) for r in reservations if r['people'].isdigit()])
        })

    except Exception as e:
        return jsonify({'success': False, 'message': f'Error loading reservations: {str(e)}', 'reservations': []})


@app.route("/staff/api/update_status", methods=['POST'])
@require_staff_auth
def update_reservation_status():
    spreadsheet, _ = get_sheets()
    try:
        data = request.get_json(silent=True) or {}
        sheet_name = str(data.get('date') or '').replace('/', '-')
        status = str(data.get('status') or '')
        row_number = data.get('row_number')

        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', sheet_name):
            return jsonify({'success': False, 'message': 'Invalid date'}), 400
        if not isinstance(row_number, int) or row_number < 2:
            return jsonify({'success': False, 'message': 'Invalid row'}), 400
        if status not in ('Pending', 'Confirmed', 'Cancelled', 'Seated', 'No Show'):
            return jsonify({'success': False, 'message': 'Invalid status'}), 400

        date_sheet = spreadsheet.worksheet(sheet_name)
        date_sheet.update_cell(row_number, 9, status)
        return jsonify({'success': True, 'message': f"Reservation updated to {status}"})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error updating reservation: {str(e)}'})

# =============================================================================
# ADMIN ROUTES
# =============================================================================

@app.route("/admin")
@require_staff_auth
def admin_panel():
    today = datetime.now().strftime('%Y-%m-%d')
    return f"""<html>
    <head>
        <title>JLD Admin Panel</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: Arial, sans-serif; padding: 20px; background: #f5f5f5; }}
            .container {{ max-width: 600px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; }}
            h2 {{ color: #d32f2f; text-align: center; }}
            .btn {{ display: inline-block; padding: 12px 24px; margin: 10px; text-decoration: none;
                    border-radius: 8px; font-weight: bold; text-align: center; min-width: 200px; }}
            .btn-primary {{ background: #2196F3; color: white; }}
            .btn-success {{ background: #4CAF50; color: white; }}
            .btn-warning {{ background: #ff9800; color: white; }}
            .btn:hover {{ transform: translateY(-2px); transition: 0.3s; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h2>JLD Restaurant Admin Panel</h2>
            <p style="text-align: center;"><strong>Today: {today}</strong></p>
            <div style="text-align: center;">
                <a href="/staff/dashboard" class="btn btn-primary">Staff Dashboard</a><br>
                <a href="/send_today_confirmations" class="btn btn-success">Send Today's SMS</a><br>
                <a href="/send_tomorrow_confirmations" class="btn btn-warning">Send Tomorrow's SMS</a>
            </div>
        </div>
    </body>
    </html>"""


@app.route("/send_today_confirmations")
@require_staff_auth
def send_today_confirmations():
    today = datetime.now(sydney_tz).strftime('%Y-%m-%d')
    result = send_sms_on_date(today, message_type="day_of")
    return f"<h2>SMS Results for {today}</h2><p>{result}</p><a href='/admin'>Back to Admin</a>"


@app.route("/send_tomorrow_confirmations")
@require_staff_auth
def send_tomorrow_confirmations():
    tomorrow = (datetime.now(sydney_tz) + timedelta(days=1)).strftime('%Y-%m-%d')
    result = send_sms_on_date(tomorrow, message_type="day_before")
    return f"<h2>SMS Results for {tomorrow}</h2><p>{result}</p><a href='/admin'>Back to Admin</a>"

# =============================================================================
# SMS WEBHOOK
# =============================================================================

@app.route('/sms-webhook', methods=['POST'])
def receive_sms():
    webhook_secret = os.environ.get('SMS_WEBHOOK_SECRET')
    if webhook_secret:
        provided = request.args.get('secret') or request.headers.get('X-Webhook-Secret', '')
        if not secure_equals(provided, webhook_secret):
            logger.warning("SMS webhook rejected: invalid secret")
            return jsonify({"status": "unauthorized"}), 401

    try:
        data = request.get_json()
        success = process_sms_reply_smart(data.get('sender'), data.get('message'), data.get('received_at'))
        return jsonify({"status": "success" if success else "warning"}), 200
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


def get_reservation_date_from_sms(received_at):
    if received_at:
        try:
            return datetime.fromisoformat(received_at.replace('Z', '+00:00')).strftime('%Y-%m-%d')
        except Exception as e:
            logger.warning(f"Error parsing received_at: {e}")
    return None


def process_sms_reply_smart(phone_number, message, received_at):
    spreadsheet, _ = get_sheets()
    try:
        parsed_date = get_reservation_date_from_sms(received_at)
        if not parsed_date:
            logger.warning("Could not determine reservation date from SMS")
            log_unknown_reply(phone_number, message, received_at)
            return False

        try:
            date_sheet = spreadsheet.worksheet(parsed_date)
            cell = date_sheet.find(phone_number, in_column=4)

            if cell:
                logger.info(f"Found reservation in {parsed_date}, row {cell.row}")
                row_data = date_sheet.row_values(cell.row)
                name = row_data[0] if row_data else "Unknown"

                reply_timestamp = datetime.fromisoformat(
                    received_at.replace('Z', '+00:00')).strftime('%Y-%m-%d %H:%M')
                full_reply = f"{reply_timestamp}: {message}"
                message_upper = message.strip().upper()

                if message_upper in ['Y', 'YES', 'YEP', 'YUP', 'CONFIRM', 'CONFIRMED']:
                    status, method = "Confirmed", "Confirmed by SMS"
                    logger.info(f"Reservation CONFIRMED for {name}")
                elif message_upper in ['N', 'NO', 'NOPE', 'CANCEL', 'CANCELLED']:
                    status, method = "Cancelled", "Cancelled by SMS"
                    logger.info(f"Reservation CANCELLED for {name}")
                else:
                    status = f"Reply needs review: {message}"
                    method = "SMS"
                    logger.warning(f"SMS reply needs manual review: {message}")

                date_sheet.batch_update([
                    {'range': f'I{cell.row}', 'values': [[status]]},
                    {'range': f'K{cell.row}', 'values': [[full_reply]]},
                    {'range': f'L{cell.row}', 'values': [[method]]}
                ])
                logger.info(f"Updated reservation for {name}")
                return True

        except gspread.WorksheetNotFound:
            logger.warning(f"Sheet not found: {parsed_date}")
        except Exception as e:
            logger.error(f"Error checking sheet {parsed_date}: {e}")

        log_unknown_reply(phone_number, message, received_at)
        return False

    except Exception:
        logger.exception("Error processing SMS reply")
        return False


def log_unknown_reply(phone_number, message, received_at):
    spreadsheet, _ = get_sheets()
    try:
        try:
            unknown_sheet = spreadsheet.worksheet("Unknown Replies")
        except gspread.WorksheetNotFound:
            unknown_sheet = spreadsheet.add_worksheet("Unknown Replies", rows=100, cols=5)
            unknown_sheet.update('A1:E1', [['Timestamp', 'Phone Number', 'Message', 'Received At', 'Status']])
        unknown_sheet.append_row([
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            phone_number, message, received_at, "Needs manual review"
        ])
        logger.info("Logged unknown SMS reply")
    except Exception as e:
        logger.error(f"Error logging unknown reply: {e}")

# =============================================================================
# HEALTH CHECK
# =============================================================================

@app.route("/health")
def health():
    return "OK", 200


if __name__ == "__main__":
    # Local dev only — production runs under gunicorn on 8080 (see Dockerfile).
    # Default 5001 because port 5000 collides with macOS AirPlay and with the
    # `firebase serve` dev server from the ordering project.
    port = int(os.environ.get('PORT', 5001))
    # Auto-reload on save, so editing this file no longer needs a manual
    # restart. Set FLASK_RELOAD=0 to turn it off. debug stays False: the
    # Werkzeug debugger allows arbitrary code execution in the browser.
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=USE_RELOADER)
