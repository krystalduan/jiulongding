from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json

# Create separate Flask app for dashboard
dashboard_app = Flask(__name__, template_folder='dashboard_templates')

# Google Sheets Setup (same as main app)
SCOPE = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/drive"]
CREDENTIALS = ServiceAccountCredentials.from_json_keyfile_name(
    "jiulongding-9e2cffe41bca.json", SCOPE)
gc = gspread.authorize(CREDENTIALS)

# Connect to Google Sheets
try:
    spreadsheet = gc.open("Restaurant Reservations")
    print("Connected to Restaurant Reservations spreadsheet")
except Exception as e:
    print(f"Error connecting to Google Sheets: {e}")


@dashboard_app.route("/")
def dashboard_home():
    """Main dashboard page"""
    today = datetime.now().strftime('%Y-%m-%d')
    return render_template('dashboard.html', default_date=today)


@dashboard_app.route("/api/reservations/<date>")
def get_reservations(date):
    """API endpoint to get reservations for a specific date"""
    try:
        # Convert date format to match sheet names (YYYY-MM-DD)
        sheet_name = date.replace('/', '-')

        try:
            date_sheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            return jsonify({
                'success': False,
                'message': f'No reservations found for {date}',
                'reservations': []
            })

        # Get all data from the sheet
        all_data = date_sheet.get_all_values()

        if len(all_data) <= 1:  # Only header or empty
            return jsonify({
                'success': False,
                'message': f'No reservations found for {date}',
                'reservations': []
            })

        # Parse reservations (skip header row)
        reservations = []
        headers = all_data[0]  # Get headers to ensure proper mapping

        # Start from row 2 for row numbers
        for i, row in enumerate(all_data[1:], start=2):
            if len(row) >= 9:  # Ensure we have enough columns
                reservation = {
                    'row_number': i,
                    'name': row[0] if len(row) > 0 else '',
                    'phone': row[1] if len(row) > 1 else '',
                    'email': row[2] if len(row) > 2 else '',
                    'people': row[3] if len(row) > 3 else '1',
                    'date': row[4] if len(row) > 4 else date,
                    'time': row[5] if len(row) > 5 else '',
                    'dish_type': row[6] if len(row) > 6 else '',
                    'notes': row[7] if len(row) > 7 else '',
                    'confirmed': row[8] if len(row) > 8 else 'Pending',
                    'status_notes': row[9] if len(row) > 9 else '',
                    'reservation_id': row[10] if len(row) > 10 else ''
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
                    # Default
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


@dashboard_app.route("/api/update_status", methods=['POST'])
def update_reservation_status():
    """API endpoint to update reservation confirmation status"""
    try:
        data = request.get_json()
        date = data.get('date')
        row_number = data.get('row_number')
        new_status = data.get('status')

        sheet_name = date.replace('/', '-')
        date_sheet = spreadsheet.worksheet(sheet_name)

        # Update the confirmed status (column I = 9)
        date_sheet.update_cell(row_number, 9, new_status)

        # Add timestamp to status notes (column J = 10)
        timestamp = datetime.now().strftime('%H:%M')
        status_note = f"Updated to {new_status} at {timestamp}"
        date_sheet.update_cell(row_number, 10, status_note)

        return jsonify({
            'success': True,
            'message': f'Reservation updated to {new_status}'
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error updating reservation: {str(e)}'
        })


if __name__ == "__main__":
    dashboard_app.run(debug=True, port=5001)  # Run on different port
