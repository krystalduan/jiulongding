from flask import Flask, render_template, request, redirect, url_for
import datetime

app = Flask(__name__)

@app.route("/")
def home():
    print("HOME PAGE LOADED")  # Debug line
    return render_template("index.html")

@app.route("/submit_reservation", methods=["POST"])
def submit_reservation_route():
    print("=== FORM SUBMITTED TO /submit_reservation ===")  # Debug line
    print(f"Request method: {request.method}")
    print(f"Form data received: {dict(request.form)}")
    
    # Retrieve form data
    name = request.form.get("name")
    email = request.form.get("email")
    phone = request.form.get("phone")
    people = request.form.get("people")
    date = request.form.get("date")
    time = request.form.get("time")
    
    print(f"Name: '{name}', Email: '{email}', Phone: '{phone}'")
    print(f"People: '{people}', Date: '{date}', Time: '{time}'")
    
    # Validate that all fields are filled
    if not name or not email or not phone or not people or not date or not time:
        error = "All fields are required. Please fill out the entire form."
        print(f"VALIDATION FAILED - Missing fields")
        return render_template("index.html", error=error)

    # Process the data
    print(f"SUCCESS! Reservation Details: {name}, {email}, {phone}, {people}, {date}, {time}")
    
    # Save to file
    try:
        with open('reservations.txt', 'a') as f:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{timestamp}] {name}, {email}, {phone}, {people}, {date}, {time}\n")
        print("Data saved to reservations.txt")
    except Exception as e:
        print(f"Error saving to file: {e}")
    
    # Store reservation data to pass to success page
    reservation_data = {
        'name': name,
        'email': email,
        'phone': phone,
        'people': people,
        'date': date,
        'time': time
    }
    
    # Redirect to success page with data
    return redirect(url_for('reservation_success', **reservation_data))

@app.route("/reservation_success")
def reservation_success():
    print("SUCCESS PAGE LOADED")  # Debug line
    # Get data from URL parameters
    name = request.args.get('name', 'N/A')
    email = request.args.get('email', 'N/A')
    phone = request.args.get('phone', 'N/A')
    people = request.args.get('people', 'N/A')
    date = request.args.get('date', 'N/A')
    time = request.args.get('time', 'N/A')
    
    return f"""
    <h1>Reservation Confirmed!</h1>
    <p>Thank you! Your reservation has been submitted successfully.</p>
    <hr>
    <h2>Reservation Details:</h2>
    <p><strong>Name:</strong> {name}</p>
    <p><strong>Email:</strong> {email}</p>
    <p><strong>Phone:</strong> {phone}</p>
    <p><strong>Number of People:</strong> {people}</p>
    <p><strong>Date:</strong> {date}</p>
    <p><strong>Time:</strong> {time}</p>
    <hr>
    <a href="/">Make Another Reservation</a>
    """

@app.route("/test")
def test():
    print("TEST ROUTE ACCESSED!")
    return "Test page works!"

@app.route("/testform")
def test_form():
    return render_template("test.html")

if __name__ == "__main__":
    app.run(debug=True)
    # app.run(port=5500)