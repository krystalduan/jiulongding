# JiuLongDing Hotpot

Website and booking system for JiuLongDing Chongqing Hotpot (九龙鼎重庆火锅), Haymarket, Sydney. Live at [jiulongding.au](https://jiulongding.au).

- Customers book online, and can change or cancel their booking through a link in the confirmation email
- Bookings are stored in Google Sheets, with one tab per date plus a Master Data tab
- Guests get a reminder SMS at 8:30am on the day and can reply Y/N
- Staff dashboard at `/staff` for viewing and editing bookings

Built with Flask, gspread, Resend (email) and Mobile Message (SMS). Hosted on Fly.io.

## Running locally

```sh
pip install -r requirements.txt
python3 app.py          # http://127.0.0.1:5001
```

Needs a `.env` with at least `SECRET_KEY`, `STAFF_PASSWORD` and `SPREADSHEET_KEY`, plus a Google service account JSON (or `GOOGLE_CREDENTIALS`). Email and SMS need `RESEND_API_KEY`, `API_USERNAME` and `API_PASSWORD`. For the rest, grep `os.environ` in `app.py`.

## Tests

```sh
python3 -m pytest tests/
```

## Deploying

Pushing to `main` deploys to Fly via GitHub Actions. The daily SMS job is in `.github/workflows/send-daily-sms.yml` and needs the `CRON_SECRET` repo secret.
