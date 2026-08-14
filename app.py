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


def mobile_number(phone):
    """Normalise to 61XXXXXXXXX, or None if this is not an AU mobile.

    Silent, unlike clean_phone(), because most callers are only asking a
    question. Deciding whether the dashboard should mark a booking as textable
    happens for every row of every day staff open, and a landline answering that
    question with "no" is not a rejection of anything.
    """
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
    return None


def clean_phone(phone):
    """mobile_number(), for the places where a number that is not a mobile means
    a booking that cannot be taken. Logs, because there it is a refusal."""
    normalised = mobile_number(phone)
    if normalised:
        return normalised
    logger.warning("Rejected invalid Australian mobile number")
    return None


def normalise_staff_phone(raw):
    """A phone number as staff typed it, ready for the sheet.

    clean_phone() takes AU mobiles only, because that is all the day-of SMS can
    reach. Staff take bookings from landlines and overseas numbers as well, and
    refusing those would mean staff could not correct a number that is simply
    true — so they are kept, and reported back as not textable rather than
    silently stored as if a reminder were coming.

    Returns (stored_value, is_mobile, error). error is None when usable.
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

# The kitchen opens at 5pm on Tuesday and Wednesday, so there is no lunch
# sitting on those days. date.weekday(): Monday=0 ... Sunday=6.
DINNER_ONLY_WEEKDAYS = {1, 2}
LUNCH_TIMES = {"12:00", "12:30", "13:00", "13:30"}
DINNER_ONLY_MESSAGE = ("We serve dinner only on Tuesdays and Wednesdays. "
                       "Please choose a time from 5:00 PM.")
DINNER_ONLY_MESSAGE_ZH = "周二、周三仅供应晚市，请选择下午 5:00 之后的时间。"


def times_on_date(booking_date):
    """Service times that exist on a given date, before any notice rules.

    Lunch is dropped on the two dinner-only days. Everything downstream — the
    booking form, the customer's reschedule picker and the server-side checks
    behind both — asks this one question, so the days the kitchen is shut are
    described in a single place.
    """
    if booking_date.weekday() in DINNER_ONLY_WEEKDAYS:
        return [t for t in ORDERED_TIMES if t not in LUNCH_TIMES]
    return list(ORDERED_TIMES)


def is_dinner_only(date_str):
    """True when date_str falls on a day with no lunch sitting."""
    try:
        return (datetime.strptime(date_str, '%Y-%m-%d').date().weekday()
                in DINNER_ONLY_WEEKDAYS)
    except (ValueError, TypeError):
        return False

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
    if target_date < now.date():
        return []

    # Days the kitchen is shut for lunch come out first, so the notice rule
    # below is only ever applied to slots that exist in the first place.
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

    if time not in times_on_date(booking_date):
        return None, DINNER_ONLY_MESSAGE

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

# Where the email should fetch fonts and other assets from. Emails are read
# long after they are sent and from anywhere, so this is the live site even
# when the mail was generated on a laptop.
ASSET_BASE_URL = os.environ.get('ASSET_BASE_URL', 'https://jiulongding.au').rstrip('/')

# The same two stacks the site uses, so a booking looks the same in the inbox
# as on the page. Both are old-style serifs for a 复古 feel: Garamond for
# English, and a Song/Ming face for Chinese — the traditional typeface of
# printed Chinese text.
#
# The named webfonts come first and are declared in EMAIL_FONT_FACES below.
# Gmail and Outlook ignore @font-face, so everything after them is a stack of
# faces already installed on the reader's machine: those clients simply land
# one step down rather than on a default sans.
EMAIL_FONT = ("'EB Garamond', Garamond, 'Hoefler Text', 'Palatino Linotype', "
              "Palatino, 'Book Antiqua', Georgia, 'Times New Roman', serif")
EMAIL_FONT_CN = ("'Songti SC', STSong, 'Noto Serif SC', 'Source Han Serif SC', "
                 "SimSun, 'Songti TC', STKaiti, KaiTi, 'PingFang SC', serif")
# The masthead face, matching the page. Its unicode-range covers exactly
# 九龙鼎重庆火锅, so any other character falls through to the Song stack on its
# own — the same behaviour as the stylesheet.
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
    """The detail table shared by both emails.

    Each row is (english label, chinese label, value) or a 4th entry with the
    value it is replacing. A replaced value is struck through in grey above the
    new one, so a guest can see at a glance what actually moved.
    """
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
    """The one button style these emails use. Empty href renders nothing.

    Table-wrapped anchor rather than a padded <a>: Outlook renders mail with
    Word, which drops padding on inline elements, so a plain styled link
    collapses to bare text there.
    """
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
    """The self-service button carried by the booking and change emails."""
    return _email_button_html(
        manage_link, 'Change or cancel your booking', '更改或取消预订',
        "Same-day changes need at least 2 hours' notice.",
        '当天更改需提前至少 2 小时。')


def _email_document(subject, preheader, intro_html, row_html,
                    manage_button_html, note_html):
    """The card every email from us is built in: masthead, details, address.

    Both builders go through here so the two mails cannot drift apart.
    """
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
          <div style="font-family:{EMAIL_FONT_BRAND};font-size:34px;color:#ffffff;
                      letter-spacing:.02em;line-height:1.25;">九龙鼎重庆火锅</div>
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
    """The values both emails present, formatted once."""
    try:
        date_obj = datetime.strptime(d['date'], '%Y-%m-%d')
        formatted_date = date_obj.strftime('%A, %d %B %Y')   # in the email body
        subject_date = date_obj.strftime('%d/%m/%Y')         # in the subject line
    except Exception:
        formatted_date = subject_date = d['date']

    # Self-service link. Only produced when we know which booking this is —
    # a resend without a reservation id simply omits the button.
    manage_link = ''
    if d.get('reservation_id'):
        try:
            manage_link = manage_url(d['reservation_id'], d['date'], d.get('email'))
        except Exception:
            logger.exception("Could not build manage link; sending email without it")

    return {
        'formatted_date': formatted_date,
        'subject_date': subject_date,
        'phone': format_phone_display(d.get('phone')),
        # Chinese as chosen, with the English gloss beside it — an email has no
        # language switch, so it has to carry both.
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
    """The self-service block for the plain-text half of either email."""
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
    """Returns (subject, html_body, text_body) for a reservation confirmation."""
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
    """Confirmation that a booking moved. `previous` holds what it moved from.

    Same card as the original confirmation, with the replaced date or time
    struck through above the new one so the change is legible at a glance.
    """
    parts = _booking_email_parts(customer_name, d)
    manage_link = parts['manage_link']
    subject = f"Your reservation has been updated | {parts['subject_date']}"

    def replaced(key, now_value, render=lambda v: v):
        """What this field used to be, or None if it did not change.

        Staff can change the party size and the phone number as well as the
        date, so every field the card shows has to be able to carry its own
        strike-through. Showing a new party size with nothing struck out beside
        it reads as though we had it wrong all along.
        """
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
    """Returns (subject, html_body, text_body) confirming a cancellation.

    No strike-through here, unlike the change email: there is no replacement
    value, and reusing that styling for "gone" rather than "replaced" would
    read as a second meaning for the same mark. The booking is stated plainly
    under a heading that says it is cancelled.
    """
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
        # The manage link is useless now, so the way back is a fresh booking.
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
    """Send a booking email.

    kind='cancelled' sends the cancellation; otherwise `previous` selects the
    change confirmation and its absence the original booking confirmation.
    """
    try:
        logger.info(f"Sending email to {customer_email}")
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


def send_email_async(email, name, reservation_data, previous=None, kind=None):
    try:
        if send_confirmation_email(email, name, reservation_data, previous, kind):
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
        no_mobile_count = 0
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

            if confirmed != "Pending":
                continue

            # Staff can save a landline or an overseas number for a booking taken
            # over the phone, so "has a number" is not the same as "can be
            # texted". Handing one of those to the SMS API spends a message on a
            # send that cannot arrive, so the check is what the number actually
            # is, not whether the cell is filled.
            mobile = mobile_number(phone)
            if not mobile:
                no_mobile_count += 1
                logger.info(f"Row {i}: no mobile number, cannot send {message_type}")
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
        # The two kinds of skip are counted apart: "already texted" is the job
        # working, while a booking with no mobile is one that will sit at Pending
        # until somebody rings it, and reporting them as one number hid that.
        summary = (f"SMS Summary for {target_date}: {sent_count} sent, "
                   f"{failed_count} failed, {skipped_count} already sent today")
        if no_mobile_count:
            summary += f", {no_mobile_count} with no mobile number (needs a call)"
        return summary

    except Exception as e:
        return f"Error sending SMS for {target_date}: {e}"

# =============================================================================
# CRON ENDPOINT
# =============================================================================

# The day-of texts go out at 8:30 AM Sydney.
#
# GitHub Actions cron is UTC only and has no daylight-saving awareness, so the
# workflow fires at both 21:30 and 22:30 UTC. Exactly one of those is 08:30 in
# Sydney and which one it is changes with DST. Nothing previously chose between
# them: whichever came first simply sent, so for the half of the year Sydney is
# on AEST the texts went out at 07:30.
#
# The window runs forward from the target rather than either side of it, so the
# early call is always refused while the later one still lands. Its length
# leaves room for a retry after a failed first attempt — safe because
# send_sms_on_date() will not text the same booking twice in one day.
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

    # force=1 is the manual "send now": the workflow_dispatch button, or staff
    # running it by hand. Those are deliberate, so they are not held to the
    # schedule — only the unattended cron is.
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
    # Same self-service link the confirmation email carries. Safe to put here
    # because this page is only ever rendered for the session that made the
    # booking — see the redirect above — so it is shown to the person who
    # already knows every detail on it. The page is noindex/no-referrer for
    # the same reason the manage page is.
    manage_token = None
    if reservation_data.get('reservation_id'):
        try:
            manage_token = make_manage_token(reservation_data['reservation_id'],
                                             reservation_data['date'],
                                             reservation_data.get('email'))
        except Exception:
            logger.exception("Could not build manage link for the success page")

    # The session holds the raw submitted values: an ISO date and a Chinese
    # dish type. Both need a readable form in each language.
    return render_template(
        'reservation_success.html',
        date_en=describe_date(reservation_data.get('date')),
        date_zh=describe_date_zh(reservation_data.get('date')),
        dish_type_en=dish_in_english(reservation_data.get('dish_type')),
        dish_type_both=dish_bilingual(reservation_data.get('dish_type')),
        manage_token=manage_token,
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
COL_SMS = 10                          # column K
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
MASTER_PEOPLE, MASTER_PHONE = 4, 6


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

    # The date in the token is right for every booking that has not moved, which
    # is nearly all of them. Master Data is the whole booking history and grows
    # forever, so reading it up front made the common case pay for the rare one
    # on a page the customer is waiting on.
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
    """Keep Master Data in step with a change.

    Date and time always; party size and phone only when given, which is how a
    staff edit keeps the master list from drifting out of agreement with the
    date tabs it indexes.

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
    # Column K is the reminder history for the date this booking used to be on:
    # which text went out that morning, and what the customer replied to it.
    # None of that is true of the new date, and carrying it across left staff
    # reading a booking weeks out as though it had already been texted.
    moved[COL_SMS] = ''

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

    # In writing, so the guest has a record that it actually went through.
    # Background, for the same reason as the other two: the page they are about
    # to see must not wait on Resend, or fail with it.
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
        # Checked before the notice rules so a lunch slot on a Tuesday is
        # explained as a day we are shut, not as insufficient notice.
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

    # A confirmed table that has moved is no longer a confirmed table: the
    # customer agreed to a time that no longer exists. Dropping back to Pending
    # is also what puts the booking back into the day-of reminder, so they get
    # asked to confirm the new one. The staff edit path has always done this;
    # a change the customer makes themselves is no different, and without it a
    # moved booking kept a tick it had never been given for that date and was
    # skipped by the reminder, which only ever texts Pending rows.
    reconfirm = booking['status'].strip().lower() in ('confirmed', 'yes')

    try:
        if moving_day:
            # The row lives in the tab named after its date, so a new date means
            # a different tab.
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
        logger.exception("Manage link: reschedule write failed")
        return reject('Sorry, something went wrong. '
                      f'Please call us on {RESTAURANT_PHONE}.',
                      f'抱歉，出了一点问题，请致电我们 {RESTAURANT_PHONE}。')

    logger.info(f"Booking {booking['reservation_id']}: {note}")
    # Keeps the staff's master list truthful, and is what lets the link already
    # sitting in the customer's inbox find the booking in its new tab.
    update_master_booking(str(booking['reservation_id']),
                          str(payload.get('e') or ''), new_date, new_time)

    # A written record of the change, so the guest is not relying on a page
    # they may have already closed. Sent in the background: the confirmation
    # they are about to see must not wait on Resend, and must not fail with it.
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
        # Redirect so the address bar ends up holding a token that points at
        # the new tab, and so the page shown is a fresh read of the moved row.
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

# =============================================================================
# STAFF ROUTES
# =============================================================================

def require_staff_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('staff_authenticated'):
            # /staff, not /staff/login: the latter only accepts POST, so a staff
            # member opening a bookmarked dashboard was redirected to a 405
            # instead of to the login form.
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
    # Sydney, not the server's clock. Fly runs in UTC, so from 10am Sydney
    # onwards datetime.now() is still yesterday — the dashboard used to open on
    # the wrong day for the whole of service.
    return render_template('dashboard.html',
                           default_date=datetime.now(sydney_tz).strftime('%Y-%m-%d'))

# =============================================================================
# STAFF — THE DAYS AHEAD
# =============================================================================
#
# What the dashboard opens on: one line per date that still has tables to lay,
# so staff see the book rather than an empty date picker.
#
# Bookings live one worksheet tab per date, so the obvious implementation reads
# every upcoming tab in a loop — twenty-odd round trips against a ceiling of 60
# reads per minute. values_batch_get() asks for all of them in a single request
# instead, which holds this whole view to two reads: one for the list of tabs,
# one for their contents.
#
# The counts come from those same tabs, under the same status rules as the day
# view below, so a number on a line and the list behind it cannot disagree.

UPCOMING_CACHE_SECONDS = 60
# A booking can be made 32 days out and moved 30 days from the day of the
# change, so the number of future tabs is bounded by how the app works. This cap
# only stops a spreadsheet full of hand-made tabs from building a request URL
# long enough to be rejected.
UPCOMING_MAX_DATES = 40

DATE_TAB_RE = re.compile(r'\d{4}-\d{2}-\d{2}')

# Party size is stored as the bucket the customer picked ('3-4', '10+'), not a
# number, so a day's covers can only ever be a range. Summing these as integers
# is what the old total_people did, and it scored 0 for every real booking.
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
    """Counts for one date's rows, or None if nothing is still expected there.

    Cancelled and moved rows are skipped rather than counted: a day whose only
    rows are cancellations is not upcoming work, and returning None is what
    keeps it off the list entirely.
    """
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
    """Everything the line needs to name its date, worked out in Sydney time."""
    day = datetime.strptime(date_str, '%Y-%m-%d').date()
    delta = (day - today).days
    return {
        'date': date_str,
        'weekday': day.strftime('%a'),
        'day_month': day.strftime('%d/%m'),
        'relative': 'Today' if delta == 0 else 'Tomorrow' if delta == 1 else '',
    }


def _read_upcoming(today):
    """Every date from today on that still has bookings. Two API reads."""
    spreadsheet, _ = get_sheets()

    dates = []
    for worksheet in spreadsheet.worksheets():
        title = worksheet.title
        if not DATE_TAB_RE.fullmatch(title):
            continue            # Master Data, Unknown Replies, anything by hand
        try:
            day = datetime.strptime(title, '%Y-%m-%d').date()
        except ValueError:
            continue            # tab named like a date but isn't one
        if day >= today:
            dates.append(title)

    if not dates:
        return []
    dates = sorted(dates)[:UPCOMING_MAX_DATES]

    # A2:L skips the header row. One request, every tab.
    response = spreadsheet.values_batch_get([f"'{name}'!A2:L" for name in dates])

    # Paired by the tab name the API echoes back rather than by position: if a
    # range ever came back missing, zipping on order would shift every date
    # after it onto another day's bookings.
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
    """The days ahead, briefly cached.

    Cheap enough to call on every dashboard load: inside the TTL it costs no
    API calls at all. The cache is per worker process and holds nothing but
    counts — the day view is always read live, so it stays the truth, and a
    change made there forces its way back through here.
    """
    today = datetime.now(sydney_tz).date()
    now = datetime.now().timestamp()

    with _upcoming_lock:
        # Keyed on the date as well as the clock, so it turns over at midnight
        # however recently it was filled.
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
        logger.exception("Staff dashboard: could not build the upcoming view")
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
                    # Whether the day-of reminder can reach this booking at all.
                    # Derived rather than stored: it is only ever a fact about
                    # the number in the cell, so there is nothing to keep in
                    # step and nothing that can go stale.
                    'textable': bool(mobile_number(row[3] if len(row) > 3 else '')),
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

        # Covers, from the live bookings only. This used to sum the party size
        # with int() behind an isdigit() guard, and party size is a bucket
        # ('3-4', '10+') — so the guard was never true and the figure it
        # reported was always 0.
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


# =============================================================================
# STAFF — WRITING TO A BOOKING
# =============================================================================
#
# Every write below addresses a booking by row number, which is a position in a
# sheet that people also edit by hand. A row number on its own is therefore a
# guess: insert a row above, and it points at somebody else's table. So each
# write carries the reservation id the dashboard was showing at that position,
# and locate_booking_row() refuses to write anywhere that id is not still
# sitting.

def locate_booking_row(date_sheet, row_number, reservation_id):
    """Confirm a row still holds the booking the dashboard was showing.

    Returns (row_number, row, error). The row number can come back different
    from the one asked for: if the booking has shifted position, following the
    id to where it is now is better than refusing a change staff are watching a
    customer wait for. It only fails when the id is nowhere in the tab.
    """
    rows = date_sheet.get_all_values()

    if not reservation_id:
        # Older rows predate reservation ids, and there is nothing else to
        # identify them by. Trust the row number, which is all the dashboard
        # has ever had.
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


# The four things staff are asked to change over the phone. Name, email and dish
# type are deliberately not here: an email address is what the manage link and
# the master index are keyed on, so changing it is a different job with its own
# consequences, not a field on this form.
STAFF_EDITABLE = ('time', 'date', 'people', 'phone')


def _staff_edit_changes(data, row, current_date):
    """Read the requested new values off the request.

    Returns (changes, warnings, error); error is None when the values are usable.

    Only fields that are actually different from the sheet come back, so a form
    submitted with one box touched writes one cell, and a form submitted with
    nothing touched is caught as a no-op rather than costing a write and an
    email to the customer.

    Staff rules are looser than the customer's on purpose: the whole reason
    somebody phones the restaurant is to do what the website would not allow, so
    the two-hour notice and the no-moving-into-today rules are not applied here.
    What stays is what keeps the sheet readable by everything downstream.
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
        if target > today + timedelta(days=MAX_ADVANCE_DAYS):
            return None, None, ('Bookings can only be moved up to '
                                f'{MAX_ADVANCE_DAYS} days ahead.')
        # Against the tab the row lives in, not its Date cell. The tab is what
        # decides which day a booking is on, and the two can disagree: an older
        # row with that cell left blank would otherwise read as a move to the
        # date it is already on, which appends a second copy to the same tab and
        # marks the original Modified.
        if new_date != current_date:
            changes['date'] = new_date

    return changes, warnings, None


@app.route("/staff/api/update_booking", methods=['POST'])
@require_staff_auth
def update_booking():
    """Change a booking's time, date, party size or phone number.

    A change of date is a move between worksheet tabs, not a cell write, so it
    goes through the same move_booking_row() a customer's own reschedule uses:
    the old row stays visible on its original date marked Modified, and the live
    row is the new one.
    """
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

        # Optimistic concurrency: the dashboard sends back what it was showing,
        # and a mismatch means somebody else — another phone, the customer's own
        # manage link — changed this booking since. Saving over that silently
        # would lose their change with no trace.
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

        # A confirmed table that has been moved is no longer a confirmed table:
        # the customer agreed to a time that no longer exists. Dropping it back
        # to Pending is also what puts it back in the day-of reminder, so they
        # get asked to confirm the new one.
        reconfirm = (status.strip().lower() in ('confirmed', 'yes')
                     and ('time' in changes or 'date' in changes))

        stamp = datetime.now(sydney_tz).strftime('%d/%m/%y %H:%M')
        described = ', '.join(f'{field} {old[field]}->{new[field]}'
                              for field in STAFF_EDITABLE if field in changes)
        note = f'Edited by staff {described} {stamp}'

        if moving_day:
            # move_booking_row() sets the time, date and note on the copy it
            # writes; everything else has to be right on the row handed to it.
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

        # Master Data is the only index from reservation id to date, so it is
        # what lets a manage link already sitting in the customer's inbox find
        # the booking after staff have moved it.
        update_master_booking(cell(COL_RES_ID), '', new['date'], new['time'],
                              new_people=new['people'], new_phone=new['phone'])

        if reconfirm:
            warnings.append('Moved back to Pending, so the reminder text asks '
                            'the customer to confirm the new time.')

        # Notifying is the staff member's call, not ours: most of these edits are
        # made while the customer is on the phone being told the new time, and an
        # email that arrives mid-conversation is noise. Sent in the background so
        # the dashboard never waits on Resend, or fails with it.
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
        logger.exception("Staff edit: write failed")
        return jsonify({'success': False,
                        'message': 'Could not save that change. Please try again.'}), 503

# =============================================================================
# ADMIN ROUTES
# =============================================================================

@app.route("/admin")
@require_staff_auth
def admin_panel():
    # Sydney, like everything else that names a service day. On UTC this read as
    # yesterday from mid-morning, next to two buttons that send today's texts.
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
    """Which date tab a reply belongs to, read in Sydney time.

    This used to format the provider's timestamp as it arrived, which is UTC.
    Sydney is ten or eleven hours ahead, so for any reply before mid-morning the
    UTC date is still yesterday — and the reminder goes out at 8:30, so that was
    every reply to it. The booking being confirmed sits on today's tab; the
    lookup was asking for the day before, which either found nothing and filed
    the reply for manual review, or found a repeat customer's booking from
    yesterday and confirmed that one instead.
    """
    if not received_at:
        return None
    try:
        moment = datetime.fromisoformat(str(received_at).replace('Z', '+00:00'))
    except Exception as e:
        logger.warning(f"Error parsing received_at: {e}")
        return None

    if moment.tzinfo is None:
        # No offset given, so it is already local time as the provider sees it.
        moment = sydney_tz.localize(moment)
    return moment.astimezone(sydney_tz).strftime('%Y-%m-%d')


def _find_sms_row(date_sheet, phone_number):
    """The row a text reply is about: (row_number, row), or (None, None).

    Matched on the number rather than on the text in the cell. The sheet holds
    whatever was typed at booking time ('0412345678'), while the provider reports
    the sender in international form ('61412345678') — the same phone, two
    spellings, and a literal search finds neither from the other.

    Cancelled rows and rows that have moved to another date are skipped, and a
    still-Pending row wins over one that has already been answered, so a repeat
    customer's "Y" lands on the table they are actually being asked about.
    """
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
                    logger.warning(f"SMS reply needs manual review: {message}")

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
            # gspread 6 takes the values first and the range second. With the old
            # order this raised, so the very run that had to create the tab was
            # the one that failed to label it and lost the reply it was filing.
            unknown_sheet.update(
                [['Timestamp', 'Phone Number', 'Message', 'Received At', 'Status']],
                'A1:E1')
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
