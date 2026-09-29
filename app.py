from dotenv import load_dotenv

from flask import (Flask, render_template, request, redirect, url_for, jsonify, session,
                   got_request_exception)
from datetime import datetime, timedelta
from time import monotonic
from functools import wraps
from html import escape
from itsdangerous import URLSafeSerializer, BadSignature
import calendar
import logging
from pytz import timezone
import threading
import atexit
import gspread
import re
from oauth2client.service_account import ServiceAccountCredentials
import requests
import base64
import os
import secrets

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# --- Monitoring ---
# Every real failure is logged as "FAILED <kind>: ..." so one search for FAILED
# in the Fly log viewer finds them all. When one kind keeps failing, an alert
# email goes to ALERT_EMAIL. Counts live in memory, so a machine restart
# resets them.

ALERT_EMAIL = os.environ.get('ALERT_EMAIL', '').strip()
ALERT_THRESHOLD = 3                 # failures of one kind...
ALERT_WINDOW_SECONDS = 60 * 60      # ...within this long sends an alert
ALERT_COOLDOWN_SECONDS = 60 * 60    # then at most one alert per kind per hour
# Bad enough to alert on the first one.
ALERT_IMMEDIATELY = {'sms-job', 'unhandled', 'booking-change'}

_failure_times = {}
_last_alert_at = {}
_failures_lock = threading.Lock()


def mask_email(email):
    """kr***@gmail.com - enough to tell customers apart in the logs."""
    local, _, domain = str(email or '').partition('@')
    if not domain:
        return '***'
    return f"{local[:2]}***@{domain}"


def mask_phone(phone):
    digits = re.sub(r'\D', '', str(phone or ''))
    return f"***{digits[-3:]}" if digits else '***'


def log_failure(kind, message, exc_info=False):
    logger.error(f"FAILED {kind}: {message}", exc_info=exc_info)
    record_failure(kind, message)


def record_failure(kind, message, now=None):
    """Count a failure; returns True when it triggers an alert."""
    now = monotonic() if now is None else now
    with _failures_lock:
        recent = [t for t in _failure_times.get(kind, [])
                  if now - t < ALERT_WINDOW_SECONDS]
        recent.append(now)
        _failure_times[kind] = recent

        threshold = 1 if kind in ALERT_IMMEDIATELY else ALERT_THRESHOLD
        if len(recent) < threshold:
            return False
        last = _last_alert_at.get(kind)
        if last is not None and now - last < ALERT_COOLDOWN_SECONDS:
            return False
        _last_alert_at[kind] = now
        count = len(recent)

    if not ALERT_EMAIL:
        logger.warning(f"Alert for '{kind}' not emailed: ALERT_EMAIL is not set")
        return True
    threading.Thread(target=send_alert_email, args=(kind, message, count),
                     daemon=True).start()
    return True


def send_alert_email(kind, message, count):
    # Plain logger calls only here - a failing alert must not raise another.
    when = datetime.now(timezone('Australia/Sydney')).strftime('%d/%m/%y %H:%M')
    subject = (f"[JLD alert] {kind} failed" if count == 1 else
               f"[JLD alert] {kind} failed {count} times in the last hour")
    text = (f"{subject}\n\n"
            f"Latest ({when} Sydney):\n  {message}\n\n"
            f"Search the Fly logs for \"FAILED {kind}\" for details.\n"
            f"You won't get another alert for '{kind}' for an hour.\n")
    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {os.environ.get('RESEND_API_KEY')}"},
            json={
                "from": "JiuLongDing Alerts <reservations@jiulongding.au>",
                "to": [ALERT_EMAIL],
                "subject": subject,
                "text": text,
            },
            timeout=10
        )
        if response.status_code != 200:
            logger.warning(f"Alert email not sent: Resend {response.status_code}: {response.text}")
        else:
            logger.info(f"Alert email sent for '{kind}'")
    except Exception as e:
        logger.warning(f"Alert email not sent: {e}")


app = Flask(__name__)


def _log_unhandled(sender, exception, **extra):
    # Flask logs the traceback itself; this adds the tagged line and alert.
    request_id = request.headers.get('Fly-Request-Id', '-')
    log_failure('unhandled', f"{request.method} {request.path} "
                             f"({type(exception).__name__}: {exception}) request={request_id}")


got_request_exception.connect(_log_unhandled, app)
app.secret_key = os.environ['SECRET_KEY']
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('FLASK_ENV') != 'development',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=64 * 1024,
)

# --- Google Sheets ---

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

# SPREADSHEET_KEY is the id in the sheet URL. Prefer it - opening by name is
# ambiguous if two sheets share a title.
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


MASTER_BOOKED_AT_COL = 10  # column J
MASTER_BOOKED_AT_HEADER = 'Booked At'


def _ensure_booked_at_header(sheet):
    # Only fills an empty header, and never blocks a booking if it fails.
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

# --- SMS ---

API_URL = "https://api.mobilemessage.com.au/v1/messages"
API_USERNAME = os.environ.get('API_USERNAME')
API_PASSWORD = os.environ.get('API_PASSWORD')
auth_string = f"{API_USERNAME}:{API_PASSWORD}"
AUTH_HEADER = base64.b64encode(auth_string.encode()).decode()

sydney_tz = timezone('Australia/Sydney')

USE_RELOADER = __name__ == '__main__' and os.environ.get('FLASK_RELOAD', '1') != '0'

# --- Helpers ---

def generate_reservation_id():
    _, sheet = get_sheets()
    all_data = sheet.get_all_values()
    return max(len(all_data) - 1, 0) + 1


def mobile_number(phone):
    """Normalise to 61XXXXXXXXX, or None if it isn't an AU mobile."""
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

    # 61 + 4XXXXXXXX
    if re.fullmatch(r'614\d{8}', normalised):
        return normalised
    return None


def clean_phone(phone):
    """Like mobile_number() but logs the rejection."""
    normalised = mobile_number(phone)
    if normalised:
        return normalised
    logger.warning("Rejected invalid Australian mobile number")
    return None


def normalise_staff_phone(raw):
    """Staff can enter landlines/overseas numbers too - they just won't get texted.

    Returns (stored_value, is_mobile, error).
    """
    text = str(raw or '').strip()
    if not text:
        return '', False, 'Please enter a phone number.'
    if re.search(r'[A-Za-z]', text):
        return '', False, 'That does not look like a phone number.'

    mobile = mobile_number(text)
    if mobile:
        return mobile, True, None

    digits = re.sub(r'\D', '', text)
    if not 8 <= len(digits) <= 15:
        return '', False, 'That does not look like a phone number.'

    logger.info("Staff saved a non-mobile number; this booking will not be texted")
    return digits, False, None

# --- Validation ---

# Must match the <option> values in index.html / book.html.
VALID_TIMES = {"12:00", "12:30", "13:00", "13:30",
               "17:00", "17:30", "18:00", "18:30",
               "19:00", "19:30", "20:00", "20:30"}
VALID_PARTY_SIZES = {"1-2", "3-4", "5-6", "7-10", "10+"}
VALID_DISH_TYPES = {"大火锅", "小火锅", "炒菜"}

ORDERED_TIMES = sorted(VALID_TIMES)

# No lunch on Tue/Wed (weekday() Monday=0).
DINNER_ONLY_WEEKDAYS = {1, 2}
LUNCH_TIMES = {"12:00", "12:30", "13:00", "13:30"}
DINNER_ONLY_MESSAGE = ("We serve dinner only on Tuesdays and Wednesdays. "
                       "Please choose a time from 5:00 PM.")
DINNER_ONLY_MESSAGE_ZH = "周二、周三仅供应晚市，请选择下午 5:00 之后的时间。"


def times_on_date(booking_date):
    """Service times on a given date (drops lunch on dinner-only days)."""
    if booking_date.weekday() in DINNER_ONLY_WEEKDAYS:
        return [t for t in ORDERED_TIMES if t not in LUNCH_TIMES]
    return list(ORDERED_TIMES)


def is_dinner_only(date_str):
    try:
        return (datetime.strptime(date_str, '%Y-%m-%d').date().weekday()
                in DINNER_ONLY_WEEKDAYS)
    except (ValueError, TypeError):
        return False


def max_booking_date(today):
    """One calendar month from today, clamped (31 Jan -> 28 Feb)."""
    year = today.year + (today.month == 12)
    month = today.month % 12 + 1
    return today.replace(year=year, month=month,
                         day=min(today.day, calendar.monthrange(year, month)[1]))


# Counted from today, not from the booking's current date.
MAX_RESCHEDULE_DAYS = 30
MIN_LEAD_MINUTES = 120     # same-day bookings need 2 hours notice
MIN_FILL_SECONDS = 3       # faster than this is a bot
MIN_RESCHEDULE_MINUTES = MIN_LEAD_MINUTES

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$')
NAME_RE = re.compile(r'^[^\d<>{}\[\]\\/|]+$')

MAX_LENGTHS = {'name': 80, 'email': 120, 'notes': 300}


# --- Manage links ---
# /manage/<token> from the confirmation email. The token is signed, stateless,
# and valid until the booking date passes. Rotating SECRET_KEY kills all links.

MANAGE_TOKEN_SALT = 'jld-manage-booking-v1'
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', 'https://jiulongding.au').rstrip('/')

_manage_serializer = URLSafeSerializer(app.secret_key, salt=MANAGE_TOKEN_SALT)


def make_manage_token(reservation_id, date, email):
    return _manage_serializer.dumps({
        'r': str(reservation_id),
        'd': str(date),
        'e': str(email or '').strip().lower(),
    })


def read_manage_token(token):
    """Returns (payload, error) where error is None, 'invalid' or 'expired'."""
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
    """Slots a customer can move to. Today only offers slots 2+ hours out."""
    try:
        target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return []

    now = datetime.now(sydney_tz)
    if target_date < now.date():
        return []

    allowed = times_on_date(target_date)
    if target_date > now.date():
        return allowed

    minutes_now = now.hour * 60 + now.minute
    out = []
    for slot in allowed:
        hour, minute = (int(p) for p in slot.split(':'))
        if (hour * 60 + minute) - minutes_now >= MIN_RESCHEDULE_MINUTES:
            out.append(slot)
    return out


def reschedule_dates(current_date_str):
    """Dates a customer can move to. Moving *into* today has to be done by phone."""
    try:
        current_date = datetime.strptime(current_date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return []

    today = datetime.now(sydney_tz).date()
    if current_date < today:
        return []

    first = today if (current_date == today and available_times_for(str(today))) \
        else today + timedelta(days=1)

    return [str(first + timedelta(days=n))
            for n in range((today + timedelta(days=MAX_RESCHEDULE_DAYS) - first).days + 1)]


def describe_date(date_str):
    """'2026-08-13' -> 'Thursday, 13 August 2026'"""
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


# Dish type is stored in Chinese in the sheet.
DISH_TYPE_EN = {
    '大火锅': 'Shared Hotpot',
    '小火锅': 'Individual Hotpot',
    '炒菜': 'Stir-fry',
}


def dish_in_english(value):
    return DISH_TYPE_EN.get(str(value or '').strip(), value)


def dish_bilingual(value):
    """'大火锅' -> '大火锅 · Shared Hotpot'"""
    english = dish_in_english(value)
    return f'{value} · {english}' if english and english != value else (value or '')


def sanitize_for_sheet(value):
    """Stop formula injection (=, +, @, -) in the sheet."""
    text = str(value or '').replace('\x00', '').strip()
    if text and text[0] in ('=', '+', '@', '\t', '\r'):
        return "'" + text
    if text.startswith('-') and not re.fullmatch(r'-?\d+(\.\d+)?', text):
        return "'" + text
    return text


def validate_reservation(form):
    """Returns (data, error_message)."""
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

    if time not in times_on_date(booking_date):
        return None, DINNER_ONLY_MESSAGE

    now = datetime.now(sydney_tz)
    today = now.date()
    if booking_date < today:
        return None, "Bookings cannot be made for a past date."
    if booking_date > max_booking_date(today):
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

# --- Rate limiting (in memory, per process) ---

_rate_lock = threading.Lock()
_rate_hits = {}


def secure_equals(a, b):
    return secrets.compare_digest(str(a or '').encode('utf-8'), str(b or '').encode('utf-8'))


def client_ip():
    return (request.headers.get('Fly-Client-IP')
            or (request.headers.get('X-Forwarded-For', '').split(',')[0].strip())
            or request.remote_addr
            or 'unknown')


def rate_limited(bucket, limit, window_seconds):
    key = f"{bucket}:{client_ip()}"
    now = datetime.now().timestamp()
    cutoff = now - window_seconds
    with _rate_lock:
        hits = [t for t in _rate_hits.get(key, []) if t > cutoff]
        # stop the dict growing forever
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

# Emails always load assets from the live site.
ASSET_BASE_URL = os.environ.get('ASSET_BASE_URL', 'https://jiulongding.au').rstrip('/')

# Same fonts as the site. Gmail/Outlook ignore @font-face, so the rest of
# each stack is system fallbacks.
EMAIL_FONT = ("'EB Garamond', Garamond, 'Hoefler Text', 'Palatino Linotype', "
              "Palatino, 'Book Antiqua', Georgia, 'Times New Roman', serif")
EMAIL_FONT_CN = ("'Songti SC', STSong, 'Noto Serif SC', 'Source Han Serif SC', "
                 "SimSun, 'Songti TC', STKaiti, KaiTi, 'PingFang SC', serif")
# Masthead font, subset to 九龙鼎重庆火锅 only.
EMAIL_FONT_BRAND = "'chineseFont', " + EMAIL_FONT_CN

EMAIL_FONT_FACES = f"""
  @font-face {{
    font-family: 'EB Garamond';
    font-style: normal;
    font-weight: 100 900;
    font-display: swap;
    src: url({ASSET_BASE_URL}/static/fonts/EBGaramond-VariableFont_wght.woff2) format('woff2');
  }}
  @font-face {{
    font-family: 'chineseFont';
    font-display: swap;
    src: url({ASSET_BASE_URL}/static/fonts/chineseFont-subset.woff2) format('woff2');
    unicode-range: U+4E5D, U+9F99, U+9F0E, U+91CD, U+5E86, U+706B, U+9505;
  }}"""


def format_phone_display(phone):
    """61412345678 -> +61 412 345 678"""
    digits = re.sub(r'\D', '', str(phone or ''))
    if re.fullmatch(r'61\d{9}', digits):
        return f'+61 {digits[2:5]} {digits[5:8]} {digits[8:]}'
    return phone or ''


def _email_rows_html(rows):
    """Rows are (label, zh label, value[, old value]). Old values get struck through."""
    out = []
    for row in rows:
        label, zh, value = row[0], row[1], row[2]
        was = row[3] if len(row) > 3 else None

        if was and str(was) != str(value):
            value_html = (
                f'''<span style="color:#b3a79f;font-weight:400;
                         text-decoration:line-through;">{escape(str(was))}</span><br>
            <span style="color:#1C1008;">{escape(str(value))}</span>''')
        else:
            value_html = escape(str(value))

        out.append(f'''
        <tr>
          <td style="padding:11px 0;border-bottom:1px solid #ece3dc;
                     font-size:13px;letter-spacing:.06em;text-transform:uppercase;
                     color:#8a7f77;width:38%;">{escape(label)}<br>
            <span style="font-family:{EMAIL_FONT_CN};font-size:12px;
                         letter-spacing:.04em;color:#a89a91;">{escape(zh)}</span></td>
          <td style="padding:11px 0;border-bottom:1px solid #ece3dc;
                     font-size:16px;color:#1C1008;font-weight:600;">{value_html}</td>
        </tr>''')
    return ''.join(out)


def _email_button_html(href, label_en, label_zh, note_en='', note_zh=''):
    # Wrapped in a table because Outlook drops padding on a plain <a>.
    if not href:
        return ''

    fine_print = ''
    if note_en or note_zh:
        fine_print = f"""
          <p style="margin:12px 0 0;font-family:{EMAIL_FONT};font-size:12px;color:#a89a91;">
            {note_en}<br>
            <span style="font-family:{EMAIL_FONT_CN};">{note_zh}</span></p>"""

    return f"""
        <tr><td align="center" style="padding:4px 32px 30px;">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0">
            <tr><td bgcolor="#8B1A1A" style="border-radius:8px;">
              <a href="{escape(href, quote=True)}"
                 style="display:inline-block;padding:13px 28px;font-family:{EMAIL_FONT};
                        font-size:15px;color:#ffffff;text-decoration:none;font-weight:600;
                        border-radius:8px;">{label_en}&nbsp;·&nbsp;<span
                        style="font-family:{EMAIL_FONT_CN};">{label_zh}</span></a>
            </td></tr>
          </table>{fine_print}
        </td></tr>"""


def _email_manage_button_html(manage_link):
    return _email_button_html(
        manage_link, 'Change or cancel your booking', '更改或取消预订',
        "Same-day changes need at least 2 hours' notice.",
        '当天更改需提前至少 2 小时。')


def _email_document(subject, preheader, intro_html, row_html,
                    manage_button_html, note_html):
    """Shared layout for every booking email."""
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(subject)}</title>
<style>{EMAIL_FONT_FACES}</style></head>
<body style="margin:0;padding:0;background:#FAF7F2;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;">
    {preheader}
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:#FAF7F2;padding:28px 12px;">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="max-width:560px;background:#ffffff;border-radius:14px;overflow:hidden;
                    border:1px solid #eee2d8;">

        <tr><td align="center" style="background:#8B1A1A;padding:30px 32px 26px;">
          <!-- Image, since Gmail won't load the webfont. -->
          <img src="{ASSET_BASE_URL}/static/img/email-brand-zh.png" alt="九龙鼎重庆火锅"
               width="257" height="44"
               style="display:block;margin:0 auto;border:0;outline:none;text-decoration:none;
                      font-family:{EMAIL_FONT_BRAND};font-size:34px;color:#ffffff;
                      letter-spacing:.02em;line-height:1.25;">
          <div style="font-family:{EMAIL_FONT};font-size:12px;color:#dda9a2;
                      margin-top:10px;letter-spacing:.16em;text-transform:uppercase;
                      text-indent:.16em;white-space:nowrap;">JiuLongDing Chongqing Hotpot</div>
        </td></tr>

        <tr><td style="padding:30px 32px 4px;">
          {intro_html}
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

        {note_html}

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


def _booking_email_parts(customer_name, d):
    try:
        date_obj = datetime.strptime(d['date'], '%Y-%m-%d')
        formatted_date = date_obj.strftime('%A, %d %B %Y')
        subject_date = date_obj.strftime('%d/%m/%Y')
    except Exception:
        formatted_date = subject_date = d['date']

    # No reservation id -> no manage button.
    manage_link = ''
    if d.get('reservation_id'):
        try:
            manage_link = manage_url(d['reservation_id'], d['date'], d.get('email'))
        except Exception:
            log_failure('manage-link', "could not build manage link; sending email without it", exc_info=True)

    return {
        'formatted_date': formatted_date,
        'subject_date': subject_date,
        'phone': format_phone_display(d.get('phone')),
        'dish': dish_bilingual(d.get('dish_type')) or 'Not specified',
        'people': d.get('people', ''),
        'manage_link': manage_link,
    }


def _hold_note_html(manage_link, lead_en, lead_zh):
    return f"""
        <tr><td style="padding:22px 32px {'14px' if manage_link else '30px'};">
          <p style="margin:0;font-family:{EMAIL_FONT};font-size:14px;
                    line-height:1.7;color:#5d534c;">
            We hold tables for <strong style="color:#1C1008;">15 minutes</strong>.
            {lead_en}<br>
            <span style="font-family:{EMAIL_FONT_CN};font-size:13px;">
              我们为您保留座位 15 分钟。{lead_zh}</span></p>
        </td></tr>"""


def _manage_text(manage_link):
    if manage_link:
        return (f"\nCHANGE OR CANCEL 更改或取消\n  {manage_link}\n"
                "  (same-day changes need at least 2 hours' notice)\n"
                "  (当天更改需提前至少 2 小时)\n")
    return ("To change or cancel, please call us with your name\n"
            "and booking date. 如需更改或取消，请致电我们。\n")


def _text_body(heading, detail_lines, manage_text):
    return f"""JIULONGDING 九龙鼎 - CHONGQING HOTPOT


{heading}
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


def build_confirmation_email(customer_name, d):
    parts = _booking_email_parts(customer_name, d)
    manage_link = parts['manage_link']
    subject = f"Your reservation at JiuLongDing Hotpot | {parts['subject_date']}"

    rows = [
        ('Name', '姓名', str(customer_name)),
        ('Date', '日期', parts['formatted_date']),
        ('Time', '时间', d['time']),
        ('Party size', '人数', f"{parts['people']} people"),
        ('Dish type', '锅底', parts['dish']),
        ('Contact', '电话', parts['phone']),
    ]

    html_body = _email_document(
        subject=subject,
        preheader=(f"{parts['formatted_date']} at {d['time']} for "
                   f"{parts['people']} people. We hold tables for 15 minutes."),
        intro_html='',
        row_html=_email_rows_html(rows),
        manage_button_html=_email_manage_button_html(manage_link),
        note_html=_hold_note_html(
            manage_link,
            'Need to make a change? Use the button below.' if manage_link
            else 'To change or cancel, please call us with your name and booking date.',
            '如需更改，请点击下方按钮。' if manage_link else '如需更改或取消，请致电我们。'),
    )

    detail_lines = '\n'.join(f'  {label} {zh}: {value}' for label, zh, value in rows)
    text_body = _text_body('YOUR BOOKING 您的预订', detail_lines,
                           _manage_text(manage_link))
    return subject, html_body, text_body


def build_change_email(customer_name, d, previous):
    """`previous` holds the old values, shown struck through."""
    parts = _booking_email_parts(customer_name, d)
    manage_link = parts['manage_link']
    subject = f"Your reservation has been updated | {parts['subject_date']}"

    def replaced(key, now_value, render=lambda v: v):
        was = previous.get(key) or ''
        return render(was) if was and str(was) != str(now_value) else None

    rows = [
        ('Name', '姓名', str(customer_name)),
        ('Date', '日期', parts['formatted_date'], replaced('date', d['date'], describe_date)),
        ('Time', '时间', d['time'], replaced('time', d['time'])),
        ('Party size', '人数', f"{parts['people']} people",
         replaced('people', parts['people'], lambda v: f'{v} people')),
        ('Dish type', '锅底', parts['dish']),
        ('Contact', '电话', parts['phone'],
         replaced('phone', d.get('phone'), format_phone_display)),
    ]

    intro_html = f"""
          <p style="margin:0;font-family:{EMAIL_FONT};font-size:20px;
                    color:#1C1008;font-weight:600;">Your booking has been updated</p>
          <p style="margin:8px 0 0;font-family:{EMAIL_FONT_CN};font-size:15px;
                    color:#8a7f77;">您的预订已更新</p>
          <p style="margin:14px 0 0;font-family:{EMAIL_FONT};font-size:14px;
                    line-height:1.7;color:#5d534c;">
            Here are your new details. Anything crossed out is what it replaced.<br>
            <span style="font-family:{EMAIL_FONT_CN};font-size:13px;">
              以下是更新后的信息，划线部分为原预订内容。</span></p>"""

    html_body = _email_document(
        subject=subject,
        preheader=(f"Now {parts['formatted_date']} at {d['time']} for "
                   f"{parts['people']} people."),
        intro_html=intro_html,
        row_html=_email_rows_html(rows),
        manage_button_html=_email_manage_button_html(manage_link),
        note_html=_hold_note_html(
            manage_link,
            'Need to change it again? Use the button below.' if manage_link
            else 'To change or cancel, please call us with your name and booking date.',
            '如需再次更改，请点击下方按钮。' if manage_link else '如需更改或取消，请致电我们。'),
    )

    detail_lines = '\n'.join(
        f"  {r[0]} {r[1]}: {r[2]}" + (f"  (was {r[3]})" if len(r) > 3 and r[3] else '')
        for r in rows)
    text_body = _text_body('YOUR UPDATED BOOKING 您的最新预订', detail_lines,
                           _manage_text(manage_link))
    return subject, html_body, text_body


def build_cancellation_email(customer_name, d):
    parts = _booking_email_parts(customer_name, d)
    subject = f"Your reservation has been cancelled | {parts['subject_date']}"

    rows = [
        ('Name', '姓名', str(customer_name)),
        ('Date', '日期', parts['formatted_date']),
        ('Time', '时间', d['time']),
        ('Party size', '人数', f"{parts['people']} people"),
    ]

    intro_html = f"""
          <p style="margin:0;font-family:{EMAIL_FONT};font-size:20px;
                    color:#1C1008;font-weight:600;">Your reservation has been cancelled</p>
          <p style="margin:8px 0 0;font-family:{EMAIL_FONT_CN};font-size:15px;
                    color:#8a7f77;">您的预订已取消</p>
          <p style="margin:14px 0 0;font-family:{EMAIL_FONT};font-size:14px;
                    line-height:1.7;color:#5d534c;">
            Your table on <strong style="color:#1C1008;">{escape(parts['formatted_date'])}</strong>
            at <strong style="color:#1C1008;">{escape(str(d['time']))}</strong>
            is no longer booked. We hope to see you another time.<br>
            <span style="font-family:{EMAIL_FONT_CN};font-size:13px;">
              您在该时段的预订已取消，期待下次光临。</span></p>"""

    note_html = f"""
        <tr><td style="padding:22px 32px 14px;">
          <p style="margin:0;font-family:{EMAIL_FONT};font-size:14px;
                    line-height:1.7;color:#5d534c;">
            Changed your mind? You are very welcome to book again.<br>
            <span style="font-family:{EMAIL_FONT_CN};font-size:13px;">
              改变主意了？欢迎随时重新预订。</span></p>
        </td></tr>"""

    html_body = _email_document(
        subject=subject,
        preheader=(f"Cancelled: {parts['formatted_date']} at {d['time']}."),
        intro_html=intro_html,
        row_html=_email_rows_html(rows),
        manage_button_html=_email_button_html(
            f'{PUBLIC_BASE_URL}/book', 'Make a new booking', '重新预订'),
        note_html=note_html,
    )

    detail_lines = '\n'.join(f'  {label} {zh}: {value}' for label, zh, value in rows)
    text_body = _text_body(
        'CANCELLED BOOKING 已取消的预订', detail_lines,
        f"\nMAKE A NEW BOOKING 重新预订\n  {PUBLIC_BASE_URL}/book\n")
    return subject, html_body, text_body


def send_confirmation_email(customer_email, customer_name, reservation_details,
                            previous=None, kind=None):
    """kind='cancelled' -> cancellation, previous set -> change, else confirmation."""
    try:
        logger.info(f"Sending email to {mask_email(customer_email)}")
        if kind == 'cancelled':
            subject, html_body, text_body = build_cancellation_email(
                customer_name, reservation_details)
        elif previous:
            subject, html_body, text_body = build_change_email(
                customer_name, reservation_details, previous)
        else:
            subject, html_body, text_body = build_confirmation_email(
                customer_name, reservation_details)

        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {os.environ.get('RESEND_API_KEY')}"},
            json={
                "from": "JiuLongDing Hotpot <reservations@jiulongding.au>",
                "to": [customer_email],
                "reply_to": os.environ.get('REPLY_TO_EMAIL', 'reservations@jiulongding.au'),
                "subject": subject,
                "html": html_body,
                "text": text_body,
            },
            timeout=10
        )
        if response.status_code != 200:
            log_failure('email', f"Resend {response.status_code} for {mask_email(customer_email)}: {response.text}")
            return False

        logger.info(f"Email sent to {mask_email(customer_email)}")
        return True

    except Exception:
        log_failure('email', f"could not send to {mask_email(customer_email)}", exc_info=True)
        return False


def send_email_async(email, name, reservation_data, previous=None, kind=None):
    try:
        if send_confirmation_email(email, name, reservation_data, previous, kind):
            logger.info(f"Background email sent to {mask_email(email)}")
        else:
            logger.warning(f"Background email failed for {mask_email(email)}")
    except Exception as e:
        log_failure('email', f"background send crashed for {mask_email(email)}", exc_info=True)


DATE_TAB_HEADERS = ["Name", "Time", "People", "Phone", "Email", "Date",
                    "Dish Type", "Notes", "Confirmed", "Reservation ID",
                    "SMS Reply", "Confirmation Method"]


def get_or_create_date_sheet(spreadsheet, date):
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
        log_failure('sheets', f"booking {reservation_id} saved to Master Data but not to the {date} tab "
                             f"(no day-of SMS until fixed): {e}", exc_info=True)


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

    logger.info(f"Sending SMS to {mask_phone(to_number)} ({custom_ref or 'no ref'})")

    try:
        response = requests.post(
            API_URL,
            headers={"Content-Type": "application/json", "Authorization": f"Basic {AUTH_HEADER}"},
            json=payload,
            timeout=10
        )
        if response.status_code != 200:
            log_failure('sms', f"SMS API {response.status_code} for {mask_phone(to_number)}: {response.text}")
            return None
        response_data = response.json()
        logger.info(f"SMS accepted for {mask_phone(to_number)}")
        return response_data
    except Exception as e:
        log_failure('sms', f"could not send to {mask_phone(to_number)}: {e}", exc_info=True)
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
        no_mobile_count = 0
        batch_updates = []

        # Column K gets a dated "sent" marker so re-running on the same day
        # doesn't text anyone twice.
        now = datetime.now(sydney_tz)
        today_stamp = now.strftime('%d/%m/%y')
        sent_marker_prefix = f"{message_type} sent {today_stamp}"

        for i, row in enumerate(all_data[1:], start=2):
            if len(row) < 9:
                continue
            name, time, people, phone = row[0], row[1], row[2], row[3]
            confirmed = row[8]
            sms_note = row[10] if len(row) > 10 else ''

            if confirmed != "Pending":
                continue

            # Landlines / overseas numbers can't be texted.
            mobile = mobile_number(phone)
            if not mobile:
                no_mobile_count += 1
                logger.info(f"Row {i}: no mobile number, cannot send {message_type}")
                continue

            # Only a success blocks a resend - failures retry.
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
            result = send_sms(mobile, sms_message,
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
        summary = (f"SMS Summary for {target_date}: {sent_count} sent, "
                   f"{failed_count} failed, {skipped_count} already sent today")
        if no_mobile_count:
            summary += f", {no_mobile_count} with no mobile number (needs a call)"
        return summary

    except Exception as e:
        log_failure('sms-job', f"{message_type} run for {target_date} crashed: {e}", exc_info=True)
        return f"Error sending SMS for {target_date}: {e}"

# --- Cron ---
# Day-of texts go out at 8:30 Sydney, triggered by GitHub Actions. The workflow
# fires at 21:30 and 22:30 UTC to cover DST, and this window drops the early one.
SMS_SEND_HOUR = 8
SMS_SEND_MINUTE = 30
SMS_SEND_WINDOW_MINUTES = 90


def sms_window_state(now=None):
    """(is_open, minutes_from_target) for the day-of send, in Sydney time."""
    now = now or datetime.now(sydney_tz)
    delta = (now.hour * 60 + now.minute) - (SMS_SEND_HOUR * 60 + SMS_SEND_MINUTE)
    return 0 <= delta < SMS_SEND_WINDOW_MINUTES, delta


@app.route('/api/send-sms-cron')
def send_sms_cron():
    secret = request.args.get('secret', '')
    cron_secret = os.environ.get('CRON_SECRET', '')
    if not cron_secret or not secure_equals(secret, cron_secret):
        return jsonify({'status': 'unauthorized'}), 401

    now = datetime.now(sydney_tz)
    is_open, delta = sms_window_state(now)

    # force=1 = manual run, skips the time window
    if not is_open and request.args.get('force') != '1':
        logger.info(
            f"Cron SMS job skipped: {now.strftime('%H:%M')} Sydney is {delta:+d} min "
            f"from the {SMS_SEND_HOUR:02d}:{SMS_SEND_MINUTE:02d} send")
        return jsonify({
            'status': 'skipped',
            'reason': f"outside the send window ({now.strftime('%H:%M')} Sydney time)",
            'sydney_time': now.strftime('%Y-%m-%d %H:%M'),
        })

    result = send_sms_on_date(now.strftime('%Y-%m-%d'), message_type="day_of")
    logger.info(f"Cron SMS job: {result}")
    return jsonify({'status': 'ok', 'result': result,
                    'sydney_time': now.strftime('%Y-%m-%d %H:%M')})

# --- SEO ---

@app.before_request
def redirect_old_domain():
    if request.host == 'jiulongding.onrender.com':
        new_url = request.url.replace('jiulongding.onrender.com', 'jiulongding.au', 1)
        return redirect(new_url, code=301)


@app.after_request
def allow_cross_origin_fonts(response):
    # CORS so emails can load the webfonts.
    if request.path.startswith('/static/fonts/'):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    elif request.path.startswith(('/static/photos/', '/static/img/')):
        response.headers['Cache-Control'] = 'public, max-age=604800'
    return response


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

# --- Customer routes ---

# Several tokens so multiple open booking tabs all still work.
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
    """Returns (status, issued_at). status is 'ok', 'duplicate' or 'unknown'."""
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


@app.context_processor
def booking_window():
    # min/max for the date pickers, in Sydney time.
    today = datetime.now(sydney_tz).date()
    return {'min_date': str(today), 'max_date': str(max_booking_date(today))}


@app.route("/")
def home():
    threading.Thread(target=_warmup_sheets, daemon=True).start()
    return render_template("index.html", form_token=issue_form_token())


@app.route("/book")
def book():
    threading.Thread(target=_warmup_sheets, daemon=True).start()
    return render_template("book.html", form_token=issue_form_token())


def reservation_error(message, status=400):
    """Re-render the form with the customer's answers kept."""
    source = request.form.get('form_source')
    if source not in ('index', 'book'):
        # older cached pages don't send form_source
        source = 'book' if (request.referrer or '').rstrip('/').endswith('/book') else 'index'
    template = 'book.html' if source == 'book' else 'index.html'
    values = {field: request.form.get(field, '') for field in FORM_FIELDS}
    return render_template(template, error=message, form_token=issue_form_token(),
                           values=values), status


@app.route("/submit_reservation", methods=["POST"])
def submit_reservation_route():
    logger.info("Reservation form submitted")

    # Loose on attempts, strict on actual bookings.
    if rate_limited('reservation_attempt', limit=20, window_seconds=3600):
        logger.warning(f"Reservation attempt rate limit hit for {client_ip()}")
        return reservation_error(
            "Too many booking attempts. Please try again later or call us on +61 423 987 048.", status=429)

    token_status, issued_at = consume_form_token(request.form.get('form_token'))

    if token_status == 'duplicate':
        # refresh / back-button resubmit
        logger.info("Duplicate submission ignored; returning customer to their confirmation")
        return redirect(url_for('reservation_success'))

    if token_status == 'unknown':
        # expired session or a very old page
        logger.warning(f"Unrecognised form token from {client_ip()}")
        return reservation_error(
            "Your booking page had been open for a while, so we couldn't confirm the submission. "
            "Your details are still below — please press Submit once more.")

    # Honeypot field - only bots fill it in.
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
    # Not popped, so refreshing still shows the booking.
    reservation_data = session.get('last_reservation')
    if not reservation_data:
        return redirect('/')
    # Only this session sees the page, so showing the manage link is fine.
    manage_token = None
    if reservation_data.get('reservation_id'):
        try:
            manage_token = make_manage_token(reservation_data['reservation_id'],
                                             reservation_data['date'],
                                             reservation_data.get('email'))
        except Exception:
            log_failure('manage-link', "could not build manage link for the success page", exc_info=True)

    return render_template(
        'reservation_success.html',
        date_en=describe_date(reservation_data.get('date')),
        date_zh=describe_date_zh(reservation_data.get('date')),
        dish_type_en=dish_in_english(reservation_data.get('dish_type')),
        dish_type_both=dish_bilingual(reservation_data.get('dish_type')),
        manage_token=manage_token,
        **reservation_data)

# --- Manage booking (cancel / reschedule) ---
# Changes are POST only - mail scanners prefetch GET links.

# Date tab columns (0-based), see DATE_TAB_HEADERS.
COL_NAME, COL_TIME, COL_PEOPLE, COL_PHONE, COL_EMAIL = 0, 1, 2, 3, 4
COL_DATE, COL_DISH, COL_NOTES, COL_CONFIRMED, COL_RES_ID = 5, 6, 7, 8, 9
COL_SMS = 10     # K
COL_METHOD = 11  # L
CANCELLED_STATUS = 'Cancelled'
# Left on the old date's row when a booking moves to another day.
MODIFIED_STATUS = 'Modified'
# Not live anymore - sorted last and never texted.
FINISHED_STATUSES = {CANCELLED_STATUS.lower(), MODIFIED_STATUS.lower(), 'no'}

# Master Data columns (0-based).
MASTER_ID, MASTER_DATE, MASTER_TIME, MASTER_EMAIL = 0, 2, 3, 7
MASTER_PEOPLE, MASTER_PHONE = 4, 6


def _scan_tab(date_sheet, want_id, want_email):
    """Find a booking's row in a date tab, skipping Modified rows."""
    for i, row in enumerate(date_sheet.get_all_values()[1:], start=2):
        if len(row) <= COL_RES_ID:
            continue
        if str(row[COL_RES_ID]).strip() != want_id:
            continue
        if str(row[COL_CONFIRMED]).strip().lower() == MODIFIED_STATUS.lower():
            continue
        # ids come from a row count and could be reused, so check email too
        if want_email and str(row[COL_EMAIL]).strip().lower() != want_email:
            logger.warning(f"Manage link: id {want_id} found but email mismatch")
            continue
        return i, row
    return None, None


def master_date_for(want_id, want_email):
    """Current date for a booking according to Master Data."""
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
        log_failure('sheets', "manage link: Master Data lookup failed", exc_info=True)
    return None


def find_booking(payload):
    """Returns (date_sheet, row_number, row), or Nones.

    Falls back to Master Data if the booking moved since the link was sent.
    """
    spreadsheet, _ = get_sheets()
    want_id = str(payload['r']).strip()
    want_email = str(payload.get('e') or '').strip().lower()

    tried = str(payload['d']).replace('/', '-')

    def look_in(sheet_name):
        try:
            date_sheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            logger.warning(f"Manage link: no date sheet for {sheet_name}")
            return None, None, None
        row_number, row = _scan_tab(date_sheet, want_id, want_email)
        if row is None:
            return None, None, None
        return date_sheet, row_number, row

    # Try the token's date first - Master Data is big and rarely needed.
    found = look_in(tried)
    if found[1] is not None:
        return found

    moved_to = master_date_for(want_id, want_email)
    if moved_to and moved_to != tried:
        logger.info(f"Manage link: booking {want_id} has moved {tried} -> {moved_to}")
        return look_in(moved_to)

    return None, None, None


def update_master_booking(want_id, want_email, new_date, new_time,
                          new_people=None, new_phone=None):
    """Sync a change into Master Data. Best effort - never fails the change."""
    try:
        _, master = get_sheets()
        for i, row in enumerate(master.get_all_values()[1:], start=2):
            if len(row) <= MASTER_EMAIL:
                continue
            if str(row[MASTER_ID]).strip() != want_id:
                continue
            if want_email and str(row[MASTER_EMAIL]).strip().lower() != want_email:
                continue
            updates = [
                {'range': f'C{i}', 'values': [[new_date]]},
                {'range': f'D{i}', 'values': [[new_time]]},
            ]
            if new_people is not None:
                updates.append({'range': f'E{i}', 'values': [[new_people]]})
            if new_phone is not None:
                updates.append({'range': f'G{i}', 'values': [[new_phone]]})
            master.batch_update(updates)
            return True
    except Exception:
        log_failure('sheets', "manage link: Master Data update failed", exc_info=True)
    return False


def move_booking_row(spreadsheet, old_sheet, row_number, row, new_date, new_time, note):
    """Copy the booking to new_date's tab and mark the old row Modified.

    New row is written first so a failure never loses the booking.
    """
    moved = list(row) + [''] * max(0, len(DATE_TAB_HEADERS) - len(row))
    moved[COL_TIME] = new_time
    moved[COL_DATE] = new_date
    moved[COL_METHOD] = note
    # SMS history belongs to the old date
    moved[COL_SMS] = ''

    target = get_or_create_date_sheet(spreadsheet, new_date)
    target.append_row(moved[:len(DATE_TAB_HEADERS)])

    old_sheet.batch_update([
        {'range': f'I{row_number}', 'values': [[MODIFIED_STATUS]]},
        {'range': f'L{row_number}', 'values': [[note]]},
    ])
    return target


def booking_view(row):
    def cell(index):
        return row[index] if len(row) > index else ''

    return {
        'name': cell(COL_NAME),
        'time': cell(COL_TIME),
        'people': cell(COL_PEOPLE),
        'phone': format_phone_display(cell(COL_PHONE)),
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
        # (value, en label, zh label)
        dates=[(d, describe_date(d), describe_date_zh(d)) for d in (dates or [])],
        selected_date=selected_date,
        today=str(datetime.now(sydney_tz).date()),
        notice=notice,
        restaurant_phone=RESTAURANT_PHONE,
    ), status_code


def render_active(token, booking, notice=None, on_date=None, status_code=200):
    dates = reschedule_dates(booking['date_raw'])
    shown = on_date or booking['date_raw']

    # No slots left today -> show the next date that has some.
    if not available_times_for(shown) and dates:
        shown = dates[0]

    return render_manage('active', token=token, booking=booking,
                         dates=dates,
                         times=available_times_for(shown),
                         selected_date=shown,
                         notice=notice, status_code=status_code)


def load_managed_booking(token):
    """Returns (payload, date_sheet, row_number, row, error_response)."""
    payload, error = read_manage_token(token)
    if error == 'expired':
        return None, None, None, None, render_manage('expired', status_code=410)
    if error:
        return None, None, None, None, render_manage('invalid', status_code=400)

    try:
        date_sheet, row_number, row = find_booking(payload)
    except Exception:
        log_failure('sheets', "manage link: sheet lookup failed", exc_info=True)
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

    notice = None
    if request.args.get('moved'):
        notice = ('success',
                  f"Your booking has been moved to {booking['date']} at {booking['time']}.",
                  f"您的预订已改为 {booking['date_raw']} {booking['time']}。")

    # ?on=<date> shows that day's slots
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
        log_failure('booking-change', f"customer cancel of {booking['reservation_id']} failed to save", exc_info=True)
        return render_active(token, booking, status_code=503,
                             notice=('error', "Sorry, something went wrong. "
                                              f"Please call us on {RESTAURANT_PHONE}.",
                                      f"抱歉，出了一点问题，请致电我们 {RESTAURANT_PHONE}。"))

    logger.info(f"Booking {booking['reservation_id']} cancelled by customer")

    guest_email = str(payload.get('e') or '')
    if guest_email:
        threading.Thread(target=send_email_async, args=(
            guest_email, booking['name'],
            {'date': booking['date_raw'], 'time': booking['time'],
             'people': booking['people'], 'dish_type': booking['dish_type'],
             'phone': booking['phone'], 'email': guest_email,
             'reservation_id': booking['reservation_id']},
            None, 'cancelled',
        )).start()

    # Not Pending anymore, so no reminder text.
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
    # older cached forms have no date field
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

    # Moving into today has to go through the phone.
    if target == today and current != today:
        return reject('To move a booking to today, please call us on '
                      f'{RESTAURANT_PHONE} so we can check we have room.',
                      f'如需改到今天，请致电我们 {RESTAURANT_PHONE}。')

    if new_date == old_date and new_time == old_time:
        return reject('That is already your booking time.', '这已经是您当前的预订时间。')

    times = available_times_for(new_date)
    if new_time not in times:
        if is_dinner_only(new_date) and new_time in LUNCH_TIMES:
            return reject(DINNER_ONLY_MESSAGE, DINNER_ONLY_MESSAGE_ZH, on_date=new_date)
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

    # Moved bookings go back to Pending so the reminder asks again.
    reconfirm = booking['status'].strip().lower() in ('confirmed', 'yes')

    try:
        if moving_day:
            spreadsheet, _ = get_sheets()
            moved = list(row) + [''] * max(0, len(DATE_TAB_HEADERS) - len(row))
            if reconfirm:
                moved[COL_CONFIRMED] = 'Pending'
            move_booking_row(spreadsheet, date_sheet, row_number, moved,
                             new_date, new_time, note)
        else:
            updates = [
                {'range': f'B{row_number}', 'values': [[new_time]]},
                {'range': f'L{row_number}', 'values': [[note]]},
            ]
            if reconfirm:
                updates.append({'range': f'I{row_number}', 'values': [['Pending']]})
            date_sheet.batch_update(updates)
    except Exception:
        log_failure('booking-change', "customer reschedule failed to save", exc_info=True)
        return reject('Sorry, something went wrong. '
                      f'Please call us on {RESTAURANT_PHONE}.',
                      f'抱歉，出了一点问题，请致电我们 {RESTAURANT_PHONE}。')

    logger.info(f"Booking {booking['reservation_id']}: {note}")
    update_master_booking(str(booking['reservation_id']),
                          str(payload.get('e') or ''), new_date, new_time)

    guest_email = str(payload.get('e') or '')
    if guest_email:
        threading.Thread(target=send_email_async, args=(
            guest_email, booking['name'],
            {'date': new_date, 'time': new_time, 'people': booking['people'],
             'dish_type': booking['dish_type'], 'phone': booking['phone'],
             'email': guest_email, 'reservation_id': booking['reservation_id']},
            {'date': old_date, 'time': old_time},
        )).start()

    if moving_day:
        # redirect so the URL has a token for the new date
        return redirect(url_for('manage_booking',
                                token=make_manage_token(booking['reservation_id'],
                                                        new_date, payload.get('e')),
                                moved=1))

    booking['time'] = new_time
    if reconfirm:
        booking['status'] = 'Pending'
    return render_active(token, booking,
                         notice=('success', f'Your booking has been moved to {new_time}.',
                                 f'您的预订时间已改为 {new_time}。'))

# --- Staff ---

def require_staff_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('staff_authenticated'):
            return redirect('/staff')
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
    # Server runs in UTC, so use Sydney time.
    return render_template('dashboard.html',
                           default_date=datetime.now(sydney_tz).strftime('%Y-%m-%d'))

# --- Staff: upcoming days ---
# One batch read for all upcoming tabs to stay under the Sheets rate limit.

UPCOMING_CACHE_SECONDS = 60
UPCOMING_MAX_DATES = 40  # keeps the batch request URL a sane length

DATE_TAB_RE = re.compile(r'\d{4}-\d{2}-\d{2}')

# Party size is a bucket ('3-4', '10+'), so covers are a range.
PARTY_SIZE_RE = re.compile(r'(\d+)(?:\s*-\s*(\d+))?\s*(\+?)')


def _covers_range(people):
    """'3-4' -> (3, 4, False). '10+' -> (10, 10, True). Unreadable -> zeroes."""
    match = PARTY_SIZE_RE.fullmatch(str(people or '').strip())
    if not match:
        return 0, 0, False
    low = int(match.group(1))
    high = int(match.group(2) or low)
    return low, max(low, high), bool(match.group(3))


def _summarise_date_tab(rows):
    """Counts for one date, or None if there are no live bookings."""
    total = confirmed = 0
    covers_low = covers_high = 0
    covers_open = False

    for row in rows:
        if len(row) <= COL_CONFIRMED:
            continue
        status = str(row[COL_CONFIRMED]).strip().lower()
        if status in FINISHED_STATUSES:
            continue
        total += 1
        if status in ('confirmed', 'yes'):
            confirmed += 1
        low, high, is_open = _covers_range(
            row[COL_PEOPLE] if len(row) > COL_PEOPLE else '')
        covers_low += low
        covers_high += high
        covers_open = covers_open or is_open

    if not total:
        return None
    return {
        'bookings': total,
        'confirmed': confirmed,
        'pending': total - confirmed,
        'covers_low': covers_low,
        'covers_high': covers_high,
        'covers_open': covers_open,
    }


def _date_labels(date_str, today):
    day = datetime.strptime(date_str, '%Y-%m-%d').date()
    delta = (day - today).days
    return {
        'date': date_str,
        'weekday': day.strftime('%a'),
        'day_month': day.strftime('%d/%m'),
        'relative': 'Today' if delta == 0 else 'Tomorrow' if delta == 1 else '',
    }


def _read_upcoming(today):
    """Every date from today with bookings. Two API reads."""
    spreadsheet, _ = get_sheets()

    dates = []
    for worksheet in spreadsheet.worksheets():
        title = worksheet.title
        if not DATE_TAB_RE.fullmatch(title):
            continue
        try:
            day = datetime.strptime(title, '%Y-%m-%d').date()
        except ValueError:
            continue
        if day >= today:
            dates.append(title)

    if not dates:
        return []
    dates = sorted(dates)[:UPCOMING_MAX_DATES]

    # A2:L skips the header
    response = spreadsheet.values_batch_get([f"'{name}'!A2:L" for name in dates])

    # match by name, not position, in case a range is missing
    returned = {}
    for entry in response.get('valueRanges') or []:
        name = str(entry.get('range') or '').split('!')[0].strip("'")
        returned[name] = entry.get('values') or []

    days = []
    for name in dates:
        summary = _summarise_date_tab(returned.get(name, []))
        if summary:
            days.append(dict(summary, **_date_labels(name, today)))
    return days


_upcoming_lock = threading.Lock()
_upcoming_cache = {}


def upcoming_days(force=False):
    """Upcoming days, cached for a minute."""
    today = datetime.now(sydney_tz).date()
    now = datetime.now().timestamp()

    with _upcoming_lock:
        # also keyed on date so it resets at midnight
        if (not force
                and _upcoming_cache.get('date') == today
                and now - _upcoming_cache.get('at', 0) < UPCOMING_CACHE_SECONDS):
            return _upcoming_cache['days']

    days = _read_upcoming(today)

    with _upcoming_lock:
        _upcoming_cache.update({'days': days, 'date': today, 'at': now})
    return days


@app.route("/staff/api/upcoming")
@require_staff_auth
def get_upcoming():
    try:
        days = upcoming_days(force=request.args.get('refresh') == '1')
    except Exception:
        log_failure('staff', "dashboard could not build the upcoming view", exc_info=True)
        return jsonify({'success': False, 'days': [],
                        'message': 'Could not load upcoming bookings'}), 503

    return jsonify({'success': True, 'days': days,
                    'today': datetime.now(sydney_tz).strftime('%Y-%m-%d')})


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
                    'reservation_id': row[9] if len(row) > 9 else '',
                    'textable': bool(mobile_number(row[3] if len(row) > 3 else '')),
                })

        def parse_time(time_str):
            for fmt in ('%H:%M', '%I:%M %p'):
                try:
                    return datetime.strptime(time_str, fmt).time()
                except ValueError:
                    continue
            return datetime.strptime('12:00', '%H:%M').time()

        # Live bookings by time, finished ones at the bottom.
        reservations.sort(key=lambda x: (
            x['confirmed'].strip().lower() in FINISHED_STATUSES,
            parse_time(x['time']),
        ))

        live = [r for r in reservations
                if r['confirmed'].strip().lower() not in FINISHED_STATUSES]
        covers = [_covers_range(r['people']) for r in live]

        return jsonify({
            'success': True,
            'message': f'Found {len(reservations)} reservations for {date}',
            'reservations': reservations,
            'total_confirmed': len([r for r in reservations if r['confirmed'].lower() in ['confirmed', 'yes']]),
            'total_pending': len([r for r in reservations if r['confirmed'].lower() in ['pending', 'no', '']]),
            'covers_low': sum(low for low, _, _ in covers),
            'covers_high': sum(high for _, high, _ in covers),
            'covers_open': any(is_open for _, _, is_open in covers),
        })

    except Exception as e:
        return jsonify({'success': False, 'message': f'Error loading reservations: {str(e)}', 'reservations': []})


# --- Staff: editing bookings ---
# The sheet gets edited by hand, so writes check the reservation id is still
# on the row before touching it.

def locate_booking_row(date_sheet, row_number, reservation_id):
    """Returns (row_number, row, error). Follows the id if the row has moved."""
    rows = date_sheet.get_all_values()

    if not reservation_id:
        # old rows have no id, trust the row number
        if row_number > len(rows):
            return None, None, 'That booking is no longer on this date. Reloading.'
        return row_number, rows[row_number - 1], None

    wanted = str(reservation_id).strip()

    def id_at(index):
        row = rows[index - 1] if 0 < index <= len(rows) else []
        return str(row[COL_RES_ID]).strip() if len(row) > COL_RES_ID else ''

    if id_at(row_number) == wanted:
        return row_number, rows[row_number - 1], None

    logger.warning(f"Staff edit: booking {wanted} is not at row {row_number}; searching")
    for i in range(2, len(rows) + 1):
        if id_at(i) == wanted:
            return i, rows[i - 1], None

    return None, None, 'That booking is no longer on this date. Reloading.'


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
        row_number, _, error = locate_booking_row(
            date_sheet, row_number, data.get('reservation_id'))
        if error:
            return jsonify({'success': False, 'message': error, 'stale': True}), 409

        date_sheet.update_cell(row_number, 9, status)
        return jsonify({'success': True, 'message': f"Reservation updated to {status}"})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error updating reservation: {str(e)}'})


# Email isn't editable - manage links are keyed on it.
STAFF_EDITABLE = ('time', 'date', 'people', 'phone')


def _staff_edit_changes(data, row, current_date):
    """Returns (changes, warnings, error). Only fields that actually changed.

    Staff skip the 2-hour notice and moving-into-today rules on purpose.
    """
    def cell(index):
        return str(row[index]).strip() if len(row) > index else ''

    changes = {}
    warnings = []

    if 'time' in data:
        new_time = str(data.get('time') or '').strip()
        if new_time not in VALID_TIMES:
            return None, None, 'That is not one of our service times.'
        if new_time != cell(COL_TIME):
            changes['time'] = new_time

    if 'people' in data:
        new_people = str(data.get('people') or '').strip()
        if new_people not in VALID_PARTY_SIZES:
            return None, None, 'Please choose a party size.'
        if new_people != cell(COL_PEOPLE):
            changes['people'] = new_people

    if 'phone' in data:
        new_phone, is_mobile, error = normalise_staff_phone(data.get('phone'))
        if error:
            return None, None, error
        if new_phone != cell(COL_PHONE):
            changes['phone'] = new_phone
        if not is_mobile:
            warnings.append('Saved, but this number cannot receive the reminder '
                            'text — this booking will need confirming by phone.')

    if 'date' in data:
        new_date = str(data.get('date') or '').strip()
        try:
            target = datetime.strptime(new_date, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return None, None, 'Please choose a valid date.'
        today = datetime.now(sydney_tz).date()
        if target < today:
            return None, None, 'That date has already passed.'
        if target > max_booking_date(today):
            return None, None, ('Bookings can only be moved up to one month '
                                'ahead.')
        # compare to the tab, not the Date cell (can be blank on old rows)
        if new_date != current_date:
            changes['date'] = new_date

    return changes, warnings, None


@app.route("/staff/api/update_booking", methods=['POST'])
@require_staff_auth
def update_booking():
    """Change time, date, party size or phone. A new date moves the row."""
    if rate_limited('staff_edit', limit=60, window_seconds=300):
        logger.warning(f"Staff edit rate limit hit from {client_ip()}")
        return jsonify({'success': False,
                        'message': 'Too many changes at once. Please wait a moment.'}), 429

    data = request.get_json(silent=True) or {}
    sheet_name = str(data.get('date_tab') or '').replace('/', '-')
    row_number = data.get('row_number')

    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', sheet_name):
        return jsonify({'success': False, 'message': 'Invalid date'}), 400
    if not isinstance(row_number, int) or row_number < 2:
        return jsonify({'success': False, 'message': 'Invalid row'}), 400

    try:
        spreadsheet, _ = get_sheets()
        try:
            date_sheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            return jsonify({'success': False, 'stale': True,
                            'message': 'That date has no bookings. Reloading.'}), 409

        row_number, row, error = locate_booking_row(
            date_sheet, row_number, data.get('reservation_id'))
        if error:
            return jsonify({'success': False, 'message': error, 'stale': True}), 409

        def cell(index):
            return str(row[index]).strip() if len(row) > index else ''

        status = cell(COL_CONFIRMED)
        if status.strip().lower() in FINISHED_STATUSES:
            return jsonify({
                'success': False, 'stale': True,
                'message': f'This booking is {status.lower()}. '
                           'Confirm it first if the table is going ahead.'}), 409

        # Someone else changed it since the dashboard loaded.
        expect = data.get('expect') or {}
        for field, index in (('time', COL_TIME), ('people', COL_PEOPLE),
                             ('phone', COL_PHONE)):
            if field in expect and str(expect[field]).strip() != cell(index):
                return jsonify({
                    'success': False, 'stale': True,
                    'message': 'Someone else changed this booking. Reloading.'}), 409

        changes, warnings, error = _staff_edit_changes(data, row, sheet_name)
        if error:
            return jsonify({'success': False, 'message': error}), 400
        if not changes:
            return jsonify({'success': False, 'message': 'Nothing was changed.'}), 400

        old = {'date': sheet_name, 'time': cell(COL_TIME),
               'people': cell(COL_PEOPLE), 'phone': cell(COL_PHONE)}
        new = dict(old, **changes)
        moving_day = 'date' in changes

        # Moved bookings go back to Pending so the reminder asks again.
        reconfirm = (status.strip().lower() in ('confirmed', 'yes')
                     and ('time' in changes or 'date' in changes))

        stamp = datetime.now(sydney_tz).strftime('%d/%m/%y %H:%M')
        described = ', '.join(f'{field} {old[field]}->{new[field]}'
                              for field in STAFF_EDITABLE if field in changes)
        note = f'Edited by staff {described} {stamp}'

        if moving_day:
            moved = list(row) + [''] * max(0, len(DATE_TAB_HEADERS) - len(row))
            moved[COL_PEOPLE] = new['people']
            moved[COL_PHONE] = new['phone']
            if reconfirm:
                moved[COL_CONFIRMED] = 'Pending'
            move_booking_row(spreadsheet, date_sheet, row_number, moved,
                             new['date'], new['time'], note)
        else:
            updates = [{'range': f'L{row_number}', 'values': [[note]]}]
            for field, column in (('time', 'B'), ('people', 'C'), ('phone', 'D')):
                if field in changes:
                    updates.append({'range': f'{column}{row_number}',
                                    'values': [[new[field]]]})
            if reconfirm:
                updates.append({'range': f'I{row_number}', 'values': [['Pending']]})
            date_sheet.batch_update(updates)

        logger.info(f"Booking {cell(COL_RES_ID) or '?'}: {note}")

        update_master_booking(cell(COL_RES_ID), '', new['date'], new['time'],
                              new_people=new['people'], new_phone=new['phone'])

        if reconfirm:
            warnings.append('Moved back to Pending, so the reminder text asks '
                            'the customer to confirm the new time.')

        # Email is opt-in - staff are often on the phone with the customer.
        guest_email = cell(COL_EMAIL)
        notified = bool(data.get('notify')) and bool(guest_email)
        if notified:
            threading.Thread(target=send_email_async, args=(
                guest_email, cell(COL_NAME),
                {'date': new['date'], 'time': new['time'], 'people': new['people'],
                 'dish_type': cell(COL_DISH), 'phone': new['phone'],
                 'email': guest_email, 'reservation_id': cell(COL_RES_ID)},
                old,
            )).start()
        elif data.get('notify'):
            warnings.append('No email address on this booking, so nothing was sent.')

        return jsonify({
            'success': True,
            'moved': moving_day,
            'new_date': new['date'],
            'notified': notified,
            'warnings': warnings,
            'message': (f"Moved to {describe_date(new['date'])} at {new['time']}"
                        if moving_day else 'Booking updated'),
        })

    except Exception:
        log_failure('staff', "staff edit failed to save", exc_info=True)
        return jsonify({'success': False,
                        'message': 'Could not save that change. Please try again.'}), 503

# --- Admin ---

@app.route("/admin")
@require_staff_auth
def admin_panel():
    today = datetime.now(sydney_tz).strftime('%Y-%m-%d')
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

# --- SMS webhook ---

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
        log_failure('sms-reply', f"webhook crashed: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


def get_reservation_date_from_sms(received_at):
    """Date tab for a reply. Converts the provider's UTC timestamp to Sydney."""
    if not received_at:
        return None
    try:
        moment = datetime.fromisoformat(str(received_at).replace('Z', '+00:00'))
    except Exception as e:
        logger.warning(f"Error parsing received_at: {e}")
        return None

    if moment.tzinfo is None:
        # no offset = already local
        moment = sydney_tz.localize(moment)
    return moment.astimezone(sydney_tz).strftime('%Y-%m-%d')


def _find_sms_row(date_sheet, phone_number):
    """Row a reply is about. Matches normalised numbers and prefers Pending rows."""
    wanted = mobile_number(phone_number)
    if not wanted:
        return None, None

    answered = (None, None)
    for i, row in enumerate(date_sheet.get_all_values()[1:], start=2):
        if len(row) <= COL_CONFIRMED:
            continue
        if mobile_number(row[COL_PHONE]) != wanted:
            continue
        status = str(row[COL_CONFIRMED]).strip().lower()
        if status in FINISHED_STATUSES:
            continue
        if status in ('pending', ''):
            return i, row
        if answered == (None, None):
            answered = (i, row)
    return answered


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
            row_number, row = _find_sms_row(date_sheet, phone_number)

            if row_number:
                logger.info(f"Found reservation in {parsed_date}, row {row_number}")
                name = row[COL_NAME] if row else "Unknown"

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
                    logger.warning(f"SMS reply for {name} needs manual review (text is in the sheet)")

                date_sheet.batch_update([
                    {'range': f'I{row_number}', 'values': [[status]]},
                    {'range': f'K{row_number}', 'values': [[full_reply]]},
                    {'range': f'L{row_number}', 'values': [[method]]}
                ])
                logger.info(f"Updated reservation for {name}")
                return True

        except gspread.WorksheetNotFound:
            logger.warning(f"Sheet not found: {parsed_date}")
        except Exception as e:
            log_failure('sms-reply', f"could not check the {parsed_date} tab: {e}", exc_info=True)

        log_unknown_reply(phone_number, message, received_at)
        return False

    except Exception:
        log_failure('sms-reply', "could not process a reply", exc_info=True)
        return False


def log_unknown_reply(phone_number, message, received_at):
    spreadsheet, _ = get_sheets()
    try:
        try:
            unknown_sheet = spreadsheet.worksheet("Unknown Replies")
        except gspread.WorksheetNotFound:
            unknown_sheet = spreadsheet.add_worksheet("Unknown Replies", rows=100, cols=5)
            # gspread 6: values first, then range
            unknown_sheet.update(
                [['Timestamp', 'Phone Number', 'Message', 'Received At', 'Status']],
                'A1:E1')
        unknown_sheet.append_row([
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            phone_number, message, received_at, "Needs manual review"
        ])
        logger.info("Logged unknown SMS reply")
    except Exception as e:
        log_failure('sms-reply', f"could not save an unmatched reply for review: {e}", exc_info=True)

# --- Health ---

@app.route("/health")
def health():
    return "OK", 200


if __name__ == "__main__":
    # Local dev only. Prod runs gunicorn (see Dockerfile). 5000 clashes with AirPlay.
    port = int(os.environ.get('PORT', 5001))
    # Keep debug off - the Werkzeug debugger allows code execution.
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=USE_RELOADER)
