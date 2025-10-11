from dotenv import load_dotenv

from flask import Flask, render_template, request, redirect, url_for, jsonify, session
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
import threading
import atexit
import gspread
import re
from oauth2client.service_account import ServiceAccountCredentials
import requests
import base64
import json
import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

load_dotenv()

# Create Flask app

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY')


SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587


# Google Sheets Setup
SCOPE = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/drive"]

# Handle credentials from environment or file
google_creds = os.environ.get('GOOGLE_CREDENTIALS')
if google_creds:
    # Production: use environment variable
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f:
        f.write(google_creds)
        creds_file = f.name
    CREDENTIALS = ServiceAccountCredentials.from_json_keyfile_name(
        creds_file, SCOPE)
else:
    # Development: use local file
    CREDENTIALS = ServiceAccountCredentials.from_json_keyfile_name(
        "jiulongding-9e2cffe41bca.json", SCOPE)

gc = gspread.authorize(CREDENTIALS)

# SMS API setup
API_URL = "https://api.mobilemessage.com.au/v1/messages"

# Email and MM (SMS) API setup
EMAIL_ADDRESS = os.environ.get('EMAIL_ADDRESS')
EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD')
API_USERNAME = os.environ.get('API_USERNAME')
API_PASSWORD = os.environ.get('API_PASSWORD')


# Encode credentials in base64
auth_string = f"{API_USERNAME}:{API_PASSWORD}"
AUTH_HEADER = base64.b64encode(auth_string.encode()).decode()

# Connect to Google Sheets
try:
    spreadsheet = gc.open("Restaurant Reservations")
    sheet = spreadsheet.worksheet('Master Data')
    print(f"Successfully connected to sheet: {sheet.title}")
except gspread.exceptions.WorksheetNotFound:
    print("Error: 'Master Data' worksheet not found")
    spreadsheet = gc.open("Restaurant Reservations")
    for ws in spreadsheet.worksheets():
        print(f"  - {ws.title}")
    sheet = spreadsheet.get_worksheet(0)
    print(f"Using first sheet as fallback: {sheet.title}")
except Exception as e:
    print(f"Error connecting to Google Sheets: {e}")

# =============================================================================
# BACKGROUND FUNCTIONS FOR SCHEDULER
# =============================================================================


def send_today_confirmations_background():
    """Background job for day-of SMS reminders"""
    with app.app_context():
        today = datetime.now().strftime('%Y-%m-%d')
        result = send_sms_on_date(today, message_type="day_of")
        print(f"Automatic day-of SMS job completed: {result}")


def send_tomorrow_confirmations_background():
    """Background job for day-before SMS reminders"""
    with app.app_context():
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        result = send_sms_on_date(tomorrow, message_type="day_before")
        print(f"Automatic day-before SMS job completed: {result}")

def keep_alive_ping():
    """Ping self every 10 minutes to prevent spin-down"""
    try:
        render_url = os.environ.get('https://jiulongding.onrender.com/', 'http://localhost:5000')
        requests.get(f'{render_url}/test', timeout=5)
        print("✓ Keep-alive ping sent")
    except Exception as e:
        print(f"Keep-alive ping failed: {e}")

# =============================================================================
# SCHEDULER SETUP
# =============================================================================


scheduler = BackgroundScheduler()

# Day-BEFORE reminders at 10 AM (sends to tomorrow's customers)
# scheduler.add_job(
#     func=send_tomorrow_confirmations_background,
#     trigger="cron",
#     hour=17,  # 10 AM
#     minute=0,
#     id='day_before_sms'
# )›››

# Day-of reminders at 5:30PM
scheduler.add_job(
    func=send_today_confirmations_background,
    trigger="cron",
    hour=8,
    minute=45,
    id='daily_sms'
)
# keep it alive at all times 
scheduler.add_job(
    func=keep_alive_ping,
    trigger="cron",
    minute='*/10',  # Every 10 minutes
    id='keep_alive'
)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def is_logged_in():
    return session.get('staff_authenticated') == True


def generate_reservation_id():
    """Generate sequential ID by counting existing reservations"""
    all_data = sheet.get_all_values()
    existing_reservations = len(all_data) - 1  # Subtract header row

    if existing_reservations < 0:
        existing_reservations = 0

    return existing_reservations + 1


def clean_phone(phone):
    """
    Clean and standardise Australian phone numbers to format: 61423456789
    """
    if not phone:
        return None

    # Remove all non-digit characters (spaces, dashes, parentheses, etc.)
    cleaned = re.sub(r'\D', '', str(phone))

    # Handle different Australian number formats
    if cleaned.startswith('614'):
        # Already has country code: 61412345678
        number = cleaned
    elif cleaned.startswith('04'):
        # Mobile starting with 04: 0412345678 -> 61412345678
        number = '61' + cleaned[1:]
    elif cleaned.startswith('4') and len(cleaned) == 9:
        # Mobile without leading 0: 412345678 -> 61412345678
        number = '61' + cleaned
    elif cleaned.startswith('0') and len(cleaned) == 10:
        # Landline with leading 0: 0212345678 -> 61212345678
        number = '61' + cleaned[1:]
    else:
        # Invalid format
        print(f"Warning: Invalid Australian phone format: {phone}")
        return phone
    return number


def send_confirmation_email(customer_email, customer_name, reservation_details):
    """Send enhanced confirmation email with reservation summary"""
    try:
        print(f"Attempting to send email to {customer_email}...")

        try:
            date_obj = datetime.strptime(
                reservation_details['date'], '%Y-%m-%d')
            formatted_date = date_obj.strftime('%A, %B %d, %Y')
        except:
            formatted_date = reservation_details['date']

        msg = MIMEMultipart()
        msg['From'] = EMAIL_ADDRESS
        msg['To'] = customer_email
        msg['Subject'] = f"Booking Summary - {formatted_date} at {reservation_details['time']}"

        text_body = f"""Dear {customer_name},

Thank you for choosing JiuLongDing Chongqing Hotpot!

RESERVATION SUMMARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📅 Date: {formatted_date}
🕐 Time: {reservation_details['time']}
👥 People: {reservation_details['people']} people
🍲 Dish Type: {reservation_details.get('dish_type', 'Not specified')}
📞 Contact: {reservation_details['phone']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🏢 RESTAURANT LOCATION
JiuLongDing Chongqing Hotpot (九龙鼎重庆火锅)
📍 71 Dixon Street (up the stairs)
    Haymarket, Sydney NSW 2000
📞 Phone: +61 423 987 048


⚠️ IMPORTANT REMINDERS
• Please arrive on time - we hold tables for 15 minutes
• To cancel or make changes, please call us at +61 423 987 048 with your name and date of reservation

Warm regards,
The JiuLongDing Team
九龙鼎重庆火锅

---
This is an automated reservation summary."""

        msg.attach(MIMEText(text_body, 'plain'))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.send_message(msg)

        print(
            f"Confirmation email sent to {customer_email} )")
        return True

    except Exception as e:
        print(f"Error sending email: {e}")
        return False


def send_email_async(email, name, reservation_data):
    """Send email in background - separate function"""
    try:
        email_sent = send_confirmation_email(email, name, reservation_data)
        if email_sent:
            print(f"✅ Background email sent successfully to {email}")
        else:
            print(f"❌ Background email failed for {email}")
    except Exception as e:
        print(f"❌ Background email error: {str(e)}")


def create_date_sheet(name, phone, email, people, date, time, dish_type, notes, reservation_id):
    """Create a new sheet for the date and add booking details"""
    try:
        sheet_name = str(date).replace('/', '-')

        date_sheet = None
        try:
            date_sheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            date_sheet = spreadsheet.add_worksheet(
                title=sheet_name, rows="100", cols="11")
            headers = ["Name", "Time", "People", "Phone", "Email",  "Date",
                       "Dish Type", "Notes", "Confirmed", "Reservation ID", "SMS Reply", "Confirmation Method"]
            date_sheet.append_row(headers)
            date_sheet.format("A1:L1", {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 0.2, "green": 0.6, "blue": 0.9}
            })

        date_sheet.append_row([name, time, people, phone, email, date,
                              dish_type, notes, "Pending", reservation_id or ""])

    except Exception as e:
        print(f"Error creating/updating date sheet: {e}")


def send_sms(to_number, message_text, custom_ref=None):
    """Send SMS using Mobile Message API"""
    sender = "61485900180"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {AUTH_HEADER}"
    }
    payload = {
        "messages": [
            {
                "to": to_number,
                "message": message_text,
                "sender": sender
            }
        ]
    }
    if custom_ref:
        payload["messages"][0]["custom_ref"] = custom_ref
    print("DEBUG Payload:", json.dumps(payload, indent=2))

    try:
        response = requests.post(
            API_URL, headers=headers, json=payload, timeout=10)

        if response.status_code != 200:
            print(f"Error {response.status_code}: {response.text}")
            return None

        response_data = response.json()
        print("SMS API Response:", json.dumps(response_data, indent=2))
        return response_data
    except Exception as e:
        print(f"Error sending SMS: {e}")
        return None


def send_sms_on_date(target_date, message_type="day_of"):
    """Helper function to send SMS for any date"""
    try:
        sheet_name = target_date.replace('/', '-')

        try:
            date_sheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            return f"No reservations found for {target_date}"

        all_data = date_sheet.get_all_values()
        sent_count = 0
        failed_count = 0
        batch_updates = []

        for i, row in enumerate(all_data[1:], start=2):
            if len(row) < 9:
                continue
            name = row[0]
            time = row[1]
            people = row[2]
            phone = row[3]
            email = row[4]
            date = row[5]
            dish = row[6]
            notes = row[7]
            confirmed = row[8]
            reservation_id = row[9]

            if confirmed == "Pending" and phone:

                sms_message = f"""Hi {name}!
                This is a reminder of your reservation tomorrow on {date} at {time} for {people} people.
                Please reply Y to confirm or N to cancel.
                Location: 71 Dixon Street (up the stairs), Haymarket
                 - JLD hotpot restaurant"""

                result = send_sms(phone, sms_message,
                                  custom_ref=f"{message_type}_{datetime.now().timestamp()}")

                print(f"sms sent: {name}")
                timestamp = datetime.now().strftime('%H:%M')
                if result:
                    sent_count += 1
                    
                    batch_updates.append({
                        'range': f'K{i}',
                        'values': [[f"{message_type} SMS sent {timestamp}"]]
                    })

                else:
                    failed_count += 1
                    batch_updates.append({
                        'range': f'K{i}',
                        'values': [[f"{message_type} SMS failed {timestamp}"]]
                    })
                   
        if batch_updates:
            date_sheet.batch_update(batch_updates)
        return f"SMS Summary for {target_date}: {sent_count} sent successfully, {failed_count} failed"

    except Exception as e:
        return f"Error sending SMS for {target_date}: {e}"


def require_staff_auth():
    """Simple staff authentication"""
    auth = request.authorization
    staff_password = os.environ.get('STAFF_PASSWORD', 'jld2024')

    print(f"Auth received: {auth}")  # debug
    print(f"Expected password: {staff_password}")  # debug

    if not auth:
        print("No authorization header")
        return False

    if auth.password != staff_password:
        print(
            f"Password mismatch: got '{auth.password}', expected '{staff_password}'")
        return False

    print("Authentication successful")
    return True

# =============================================================================
# CUSTOMER-FACING ROUTES
# =============================================================================


@app.route("/")
def home():
    """Customer reservation form"""
    print("HOME PAGE LOADED")
    return render_template("index.html")


@app.route("/submit_reservation", methods=["POST"])
def submit_reservation_route():
    """Handle customer reservation submission"""
    print("=== FORM SUBMITTED TO /submit_reservation ===")
    print(f"Form data received: {dict(request.form)}")

    # Get form data
    name = request.form.get("name")
    email = request.form.get("email")
    phone = clean_phone(request.form.get("phone"))
    people = request.form.get("people")
    date = request.form.get("date")
    time = request.form.get("time")
    dish_type = request.form.get('dish-type')
    notes = request.form.get('notes', "")

    # Validate required fields
    if not name or not email or not phone or not people or not date or not time:
        error = "All fields are required. Please fill out the entire form."
        print("VALIDATION FAILED - Missing fields")
        return render_template("index.html", error=error)

   
    reservation_id = generate_reservation_id()
    # save to master data sheet
    sheet.append_row(
        [reservation_id, name, date, time,  people, dish_type, phone, email, notes])

    reservation_data = {
        'name': name, 'phone': phone, 'email': email, 'people': people,
        'date': date, 'time': time, 'dish_type': dish_type, 'notes': notes, 'reservation_id': reservation_id
    }

    return redirect(url_for('reservation_success', **reservation_data))


@app.route("/reservation_success")
def reservation_success():
    """Customer reservation confirmation page"""
    print("SUCCESS PAGE LOADED")

    # Get data from URL parameters
    name = request.args.get('name', 'N/A')
    email = request.args.get('email', 'N/A')
    phone = request.args.get('phone', 'N/A')
    people = request.args.get('people', 'N/A')
    date = request.args.get('date', 'N/A')
    time = request.args.get('time', 'N/A')
    dish_type = request.args.get('dish_type', 'N/A')
    notes = request.args.get('notes', 'N/A')
    reservation_id = request.args.get('reservation_id', 'N/A')

    # Create date-specific sheet
    create_date_sheet(name, phone, email, people, date,
                      time, dish_type, notes, reservation_id)
    reservation_data = {
        'name': name, 'phone': phone, 'email': email, 'people': people,
        'date': date, 'time': time, 'dish_type': dish_type, 'notes': notes, 'reservation_id': reservation_id
    }

    email_thread = threading.Thread(
        target=send_email_async,
        args=(email, name, reservation_data)
    )
    email_thread.start()

    
    return render_template('reservation_success.html',
                           name=name, email=email, phone=phone, people=people,
                           date=date, time=time, dish_type=dish_type,
                           notes=notes, reservation_id=reservation_id)

# =============================================================================
# STAFF DASHBOARD ROUTES
# =============================================================================


def require_staff_auth(f):
    from functools import wraps

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('staff_authenticated'):
            return redirect('/staff/login')
        return f(*args, **kwargs)

    return decorated_function


@app.route("/staff")
def staff_login():
    """Staff login page"""
    return render_template('staff_login.html')


@app.route("/staff/login", methods=["POST"])
def staff_login_post():
    password = request.form.get('password')
    staff_password = os.environ.get('STAFF_PASSWORD', '123')
    print(password)
    print(staff_dashboard)
    if password == staff_password:
        # Store authentication in session
        session['staff_authenticated'] = True
        session.permanent = True
        return redirect('/staff/dashboard')
    else:
        return render_template('staff_login.html', error="Invalid password"), 401


@app.route("/staff/dashboard")
@require_staff_auth
def staff_dashboard():
    today = datetime.now().strftime('%Y-%m-%d')
    return render_template('dashboard.html', default_date=today)


@app.route("/staff/api/reservations/<date>")
@require_staff_auth
def get_reservations(date):
    try:
        sheet_name = date.replace('/', '-')

        try:
            date_sheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            return jsonify({
                'success': False,
                'message': f'No reservations found for {date}',
                'reservations': []
            })

        all_data = date_sheet.get_all_values()

        if len(all_data) <= 1:
            return jsonify({
                'success': False,
                'message': f'No reservations found for {date}',
                'reservations': []
            })

        reservations = []
        for i, row in enumerate(all_data[1:], start=2):
            if len(row) >= 9:
                reservation = {
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
                }
                reservations.append(reservation)

        # Sort by time
        def parse_time(time_str):
            try:
                return datetime.strptime(time_str, '%H:%M').time()
            except:
                try:
                    return datetime.strptime(time_str, '%I:%M %p').time()
                except:
                    return datetime.strptime('12:00', '%H:%M').time()

        reservations.sort(key=lambda x: parse_time(x['time']))

        return jsonify({
            'success': True,
            'message': f'Found {len(reservations)} reservations for {date}',
            'reservations': reservations,
            'total_confirmed': len([r for r in reservations if r['confirmed'].lower() in ['confirmed', 'yes']]),
            'total_pending': len([r for r in reservations if r['confirmed'].lower() in ['pending', 'no', '']]),
            'total_people': sum([int(r['people']) for r in reservations if r['people'].isdigit()])
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error loading reservations: {str(e)}',
            'reservations': []
        })

# API to update status


@app.route("/staff/api/update_status", methods=['POST'])
@require_staff_auth
def update_reservation_status():
    try:
        data = request.get_json()
        date = data.get('date')
        row_number = data.get('row_number')
        new_status = data.get('status')

        sheet_name = date.replace('/', '-')
        date_sheet = spreadsheet.worksheet(sheet_name)

        # Update the confirmed status (column I = 9)
        date_sheet.update_cell(row_number, 9, new_status)

        return jsonify({
            'success': True,
            'message': f'Reservation updated to {new_status}'
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error updating reservation: {str(e)}'
        })

# =============================================================================
# ADMIN/SMS ROUTES
# =============================================================================


@app.route("/admin")
def admin_panel():
    """Admin panel for manual SMS and dashboard access"""
    if not require_staff_auth():
        return render_template('staff_login.html'), 401

    today = datetime.now().strftime('%Y-%m-%d')
    return f"""
    <html>
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
            <h2>🍲 JLD Restaurant Admin Panel</h2>
            <p style="text-align: center;"><strong>Today: {today}</strong></p>
            
            <div style="text-align: center;">
                <a href="/staff/dashboard" class="btn btn-primary">📊 Staff Dashboard</a><br>
                <a href="/send_today_confirmations" class="btn btn-success">📱 Send Today's SMS</a><br>
                <a href="/send_tomorrow_confirmations" class="btn btn-warning">📅 Send Tomorrow's SMS</a>
            </div>
        </div>
    </body>
    </html>
    """


@app.route("/send_today_confirmations")
def send_today_confirmations():
    """Manual trigger for day-of SMS"""
    if not require_staff_auth():
        return "Unauthorized", 401

    today = datetime.now().strftime('%Y-%m-%d')
    result = send_sms_on_date(today, message_type="day_of")
    return f"<h2>SMS Results for {today}</h2><p>{result}</p><a href='/admin'>← Back to Admin</a>"


@app.route("/send_tomorrow_confirmations")
def send_tomorrow_confirmations():
    """Manual trigger for day-before SMS"""
    if not require_staff_auth():
        return "Unauthorized", 401

    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
    result = send_sms_on_date(tomorrow, message_type="day_before")
    return f"<h2>SMS Results for {tomorrow}</h2><p>{result}</p><a href='/admin'>← Back to Admin</a>"

# =============================================================================
# SMS REPLY ROUTES (WebHook)
# =============================================================================


@app.route('/sms-webhook', methods=['POST'])
def receive_sms():
    """Webhook endpoint to receive inbound SMS"""
    try:
        data = request.get_json()
        print("Received webhook data:", json.dumps(data, indent=2))

        sender = data.get('sender')
        message_text = data.get('message')
        received_at = data.get('received_at')
        original_custom_ref = data.get('original_custom_ref')

        # Process the reply with date detection
        success = process_sms_reply_smart(sender, message_text, received_at)

        if success:
            return jsonify({"status": "success"}), 200
        else:
            return jsonify({"status": "warning", "message": "No matching reservation"}), 200

    except Exception as e:
        print(f"Error processing webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


def get_reservation_date_from_sms(received_at):
    """
    Determine which date sheet to check based on SMS data
    Returns a list of possible sheet names to check
    """

    # Method 2: Use received_at as fallback
    if received_at:
        try:
            # Parse ISO format: "2024-09-30T14:35:00Z"
            received_datetime = datetime.fromisoformat(
                received_at.replace('Z', '+00:00'))

            # Check same day (for day_of messages)
            same_day = received_datetime.strftime('%Y-%m-%d')
            return same_day


        except Exception as e:
            print(f"Error parsing received_at: {e}")

    return None


def process_sms_reply_smart(phone_number, message, received_at):
    """
    Smart SMS reply processing - goes directly to the correct date sheet
    No phone index needed!
    """
    try:

        print(f"Looking for reservation with phone: {phone_number}")

        # Get possible date sheets to check
        parsed_date = get_reservation_date_from_sms(received_at)
        print(f"the parsed date is {parsed_date}")
        if not parsed_date:
            print("⚠ Could not determine reservation date")
            log_unknown_reply(phone_number, message, received_at)
            return False

        try:
            date_sheet = spreadsheet.worksheet(parsed_date)
            cell = date_sheet.find(phone_number, in_column=4)

            if cell:
                print(f"✓ Found reservation in {date_sheet}, row {cell.row}")

                # Get the row data
                row_data = date_sheet.row_values(cell.row)
                name = row_data[0] if len(row_data) > 0 else "Unknown"

                # Format reply timestamp
                reply_timestamp = datetime.fromisoformat(
                    received_at.replace('Z', '+00:00')
                ).strftime('%Y-%m-%d %H:%M')
                full_reply = f"{reply_timestamp}: {message}"
                print(full_reply + "full reply")

                # Determine status based on message
                message_upper = message.strip().upper()

                if message_upper in ['Y', 'YES', 'YEP', 'YUP', 'CONFIRM', 'CONFIRMED']:
                    status = "Confirmed"
                    method = "Confirmed by SMS"
                    print(f"✓ Reservation CONFIRMED for {name}")
                elif message_upper in ['N', 'NO', 'NOPE', 'CANCEL', 'CANCELLED']:
                    status = "Cancelled"
                    method = "Cancelled by SMS"
                    print(f"✗ Reservation CANCELLED for {name}")
                else:
                    status = f"Reply needs review: {message}"
                    method = "SMS"
                    print(f"⚠ Reply needs manual review: {message}")
                # Batch update both columns 
                date_sheet.batch_update([
                    {
                        'range': f'I{cell.row}',  # Column I: Confirmed status
                        'values': [[status]]
                    },
                    {
                        'range': f'K{cell.row}',  # Column L: SMS Reply
                        'values': [[full_reply]]
                    },
                    {
                        'range': f'L{cell.row}',
                        'values': [[method]]
                    }
                ])

                print(f"✓ Updated reservation for {name}")
                return True

        except gspread.WorksheetNotFound:
            print(f"Sheet not found: {date_sheet}")
        except Exception as e:
            print(f"Error checking sheet {date_sheet}: {e}")

        log_unknown_reply(phone_number, message, received_at)
        return False
    except Exception as e:
        print(f"Error processing SMS reply: {e}")
        import traceback
        traceback.print_exc()
        return False


def log_unknown_reply(phone_number, message, received_at):
    """Log replies that couldn't be matched to a reservation"""
    try:
        try:
            unknown_sheet = spreadsheet.worksheet("Unknown Replies")
        except gspread.WorksheetNotFound:
            unknown_sheet = spreadsheet.add_worksheet(
                "Unknown Replies", rows=100, cols=5)
            unknown_sheet.update(
                'A1:E1', [['Timestamp', 'Phone Number', 'Message', 'Received At', 'Status']])

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        unknown_sheet.append_row([
            timestamp,
            phone_number,
            message,
            received_at,
            "Needs manual review"
        ])
        print(f"Logged unknown reply to 'Unknown Replies' sheet")

    except Exception as e:
        print(f"Error logging unknown reply: {e}")


# =============================================================================
# TEST ROUTES
# =============================================================================


@app.route("/test")
def test():
    print("TEST ROUTE ACCESSED!")
    return "Test page works!"


@app.route("/test-auth")
def test_auth():
    """Test authentication without redirects"""
    auth = request.authorization
    staff_password = os.environ.get('STAFF_PASSWORD', 'jld2024')

    if not auth:
        return f"No auth provided. Expected password: {staff_password}", 401

    if auth.password == staff_password:
        return f"✅ Authentication successful! Username: {auth.username}, Password: {auth.password}"
    else:
        return f"❌ Wrong password. Got: {auth.password}, Expected: {staff_password}", 401


@app.route("/test-scheduler")
def test_scheduler():
    """Test scheduler status"""
    jobs = scheduler.get_jobs()
    job_info = []
    for job in jobs:
        job_info.append({
            'id': job.id,
            'next_run': str(job.next_run_time),
            'function': job.func.__name__
        })

    return jsonify({
        'scheduler_running': scheduler.running,
        'jobs': job_info,
        'current_time': datetime.now().isoformat()
    })


@app.route("/test-api")
def test_api():
    """Test if API routing works at all"""
    print("🔍 test-api route called!")
    return jsonify({
        'success': True,
        'message': 'API routing works!',
        'timestamp': datetime.now().isoformat()
    })


@app.route("/staff/api/test")
def test_staff_api():
    """Test if staff API routing works"""
    print("🔍 staff API test route called!")
    return jsonify({
        'success': True,
        'message': 'Staff API routing works!',
        'auth_header': request.headers.get('Authorization', 'None')
    })


@app.route("/test-env")
def test_env():
    return f"""
    EMAIL_ADDRESS: {os.environ.get('EMAIL_ADDRESS', 'NOT FOUND')}
    EMAIL_PASSWORD: {'***' if os.environ.get('EMAIL_PASSWORD') else 'NOT FOUND'}
    API_USERNAME: {os.environ.get('API_USERNAME', 'NOT FOUND')}
    """


if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
