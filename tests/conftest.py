"""
Test fixtures for the JLD booking app.

These tests run completely offline: Google Sheets, email and SMS are all
replaced with fakes, so nothing touches the real spreadsheet or sends
anything to a customer.
"""
import os
import sys
import types
from datetime import datetime, timedelta

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# Fake credentials must be in place BEFORE app.py is imported, otherwise it
# tries to read the real .env / service-account file.
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
os.environ.setdefault('STAFF_PASSWORD', 'test-staff-password')
os.environ.setdefault('CRON_SECRET', 'test-cron-secret')
os.environ.setdefault('SMS_WEBHOOK_SECRET', 'test-webhook-secret')
os.environ.pop('GOOGLE_CREDENTIALS', None)

# Stop oauth2client from validating the service-account JSON.
_fake_oauth = types.ModuleType('oauth2client.service_account')


class _FakeCredentials:
    @staticmethod
    def from_json_keyfile_name(*args, **kwargs):
        return object()


_fake_oauth.ServiceAccountCredentials = _FakeCredentials
sys.modules.setdefault('oauth2client', types.ModuleType('oauth2client'))
sys.modules['oauth2client.service_account'] = _fake_oauth

import gspread  # noqa: E402

gspread.authorize = lambda creds: types.SimpleNamespace(open=lambda name: None)

import app as flask_app  # noqa: E402


class FakeWorksheet:
    """Minimal stand-in for a gspread worksheet."""

    def __init__(self, title, rows=None):
        self.title = title
        self.rows = rows if rows is not None else []

    def get_all_values(self):
        return self.rows

    def append_row(self, row):
        self.rows.append(row)

    def format(self, *args, **kwargs):
        pass

    def update_cell(self, row, col, value):
        while len(self.rows) < row:
            self.rows.append([''] * 12)
        target = self.rows[row - 1]
        while len(target) < col:
            target.append('')
        target[col - 1] = value

    def batch_update(self, updates):
        pass

    def find(self, value, in_column=None):
        return None


class FakeSpreadsheet:
    def __init__(self):
        self.master = FakeWorksheet('Master Data', [['id', 'name', 'date']])
        self.date_sheets = {}

    def worksheet(self, name):
        if name in self.date_sheets:
            return self.date_sheets[name]
        raise gspread.WorksheetNotFound(name)

    def add_worksheet(self, title=None, rows=None, cols=None):
        sheet = FakeWorksheet(title)
        self.date_sheets[title] = sheet
        return sheet


@pytest.fixture
def sheets(monkeypatch):
    """A fake spreadsheet; inspect `sheets.master.rows` to see what got saved."""
    fake = FakeSpreadsheet()
    monkeypatch.setattr(flask_app, 'get_sheets', lambda: (fake, fake.master))
    # never send real email or SMS during tests
    monkeypatch.setattr(flask_app, 'send_email_async', lambda *a, **k: None)
    monkeypatch.setattr(flask_app, 'send_sms', lambda *a, **k: None)
    monkeypatch.setattr(flask_app.threading, 'Thread',
                        lambda *a, **k: types.SimpleNamespace(start=lambda: None))
    return fake


@pytest.fixture(autouse=True)
def clean_rate_limits():
    """Each test starts with an empty rate-limit table."""
    flask_app._rate_hits.clear()
    yield
    flask_app._rate_hits.clear()


@pytest.fixture(autouse=True)
def instant_submissions(monkeypatch):
    """Disable the 'form filled too fast' check; individual tests re-enable it."""
    monkeypatch.setattr(flask_app, 'MIN_FILL_SECONDS', 0)


@pytest.fixture
def client():
    flask_app.app.config['TESTING'] = True
    with flask_app.app.test_client() as c:
        yield c


@pytest.fixture
def app_module():
    return flask_app


@pytest.fixture(scope='session', autouse=True)
def stop_scheduler():
    """Shut the scheduler down before pytest closes its log streams."""
    yield
    try:
        flask_app.scheduler.shutdown(wait=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sydney_now():
    return datetime.now(flask_app.sydney_tz)


def days_from_now(n):
    return (sydney_now() + timedelta(days=n)).strftime('%Y-%m-%d')


def get_form_token(client, path='/'):
    """Load the booking page and pull the CSRF/one-time token out of the HTML."""
    import re
    html = client.get(path).get_data(as_text=True)
    match = re.search(r'name="form_token" value="([^"]+)"', html)
    assert match, f"no form_token found on {path}"
    return match.group(1)


def valid_booking(**overrides):
    """A booking that should always be accepted, with optional field overrides."""
    booking = {
        'name': 'Jane Smith',
        'email': 'jane.smith@gmail.com',
        'phone': '0412345678',
        'people': '3-4',
        'date': days_from_now(3),
        'time': '19:00',
        'dish-type': '大火锅',
        'notes': '',
    }
    booking.update(overrides)
    return booking


def submit(client, **overrides):
    """Submit a booking with a freshly issued token."""
    form = valid_booking(**overrides)
    form['form_token'] = get_form_token(client)
    return client.post('/submit_reservation', data=form)
