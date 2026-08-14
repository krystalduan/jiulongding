"""
Test fixtures for the JLD booking app.

These tests run completely offline: Google Sheets, email and SMS are all
replaced with fakes, so nothing touches the real spreadsheet or sends
anything to a customer.
"""
import os
import re
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
        self.batches = []

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

    def update(self, values, range_name=None, **kwargs):
        """gspread 6 takes the values first and the range second.

        Raising on the old gspread 5 order is the point: the app had a call left
        the other way round, and a fake that quietly accepted either would have
        let it keep passing tests while failing in production.
        """
        if not isinstance(values, (list, tuple)):
            raise TypeError('update() takes the values first, then the range')

        row, col = 1, 1
        if range_name:
            match = re.match(r'([A-Z]+)(\d+)', str(range_name).split(':')[0].strip())
            if match:
                col = 0
                for ch in match.group(1):
                    col = col * 26 + (ord(ch) - ord('A') + 1)
                row = int(match.group(2))

        for r_offset, row_values in enumerate(values):
            for c_offset, value in enumerate(row_values):
                self.update_cell(row + r_offset, col + c_offset, value)

    def batch_update(self, updates):
        """Really apply the writes.

        This used to be a no-op, which quietly made the sheet untestable:
        the day-of SMS job records "already sent" markers through this
        method, so a stub here would let duplicate-send tests pass no
        matter what the code did.
        """
        self.batches.append(updates)
        for update in updates:
            for row, col, value in _parse_a1(update['range'], update['values']):
                self.update_cell(row, col, value)

    def delete_rows(self, index):
        """1-based, like gspread. Used when a booking moves to another date."""
        if 1 <= index <= len(self.rows):
            self.rows.pop(index - 1)

    def find(self, value, in_column=None):
        return None


def _parse_a1(cell_range, values):
    """'K5' + [['x']] -> [(5, 11, 'x')]. Single cells only, which is all the
    app writes."""
    match = re.fullmatch(r'([A-Z]+)(\d+)', cell_range.strip())
    if not match:
        return []
    letters, row = match.group(1), int(match.group(2))
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - ord('A') + 1)
    out = []
    for r_offset, row_values in enumerate(values):
        for c_offset, value in enumerate(row_values):
            out.append((row + r_offset, col + c_offset, value))
    return out


class FakeSpreadsheet:
    def __init__(self):
        self.master = FakeWorksheet('Master Data', [['id', 'name', 'date']])
        self.date_sheets = {}
        # Every call that would cost a Google Sheets API read, so tests can
        # assert the upcoming view stays at two of them however many tabs
        # there are.
        self.reads = []

    def worksheet(self, name):
        if name in self.date_sheets:
            return self.date_sheets[name]
        raise gspread.WorksheetNotFound(name)

    def add_worksheet(self, title=None, rows=None, cols=None):
        sheet = FakeWorksheet(title)
        self.date_sheets[title] = sheet
        return sheet

    def worksheets(self):
        """Tab metadata — one API read, and it includes the non-date tabs."""
        self.reads.append('worksheets')
        return [self.master] + list(self.date_sheets.values())

    def values_batch_get(self, ranges, params=None):
        """Many tabs in a single read, like the real batchGet.

        Mirrors two details the app has to cope with: the echoed range is
        resolved ("'2026-08-14'!A2:L100", not the A2:L that was asked for),
        and an empty range comes back with no 'values' key at all.
        """
        self.reads.append('values_batch_get')
        value_ranges = []
        for spec in ranges:
            name = str(spec).split('!')[0].strip("'")
            sheet = self.date_sheets.get(name)
            rows = [list(row) for row in sheet.rows[1:]] if sheet else []
            entry = {'range': f"'{name}'!A2:L1000", 'majorDimension': 'ROWS'}
            if rows:
                entry['values'] = rows
            value_ranges.append(entry)
        return {'spreadsheetId': 'fake', 'valueRanges': value_ranges}


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
def clean_upcoming_cache():
    """The dashboard's upcoming view is cached per process for 60 seconds, so
    without this one test's counts would be served to the next."""
    flask_app._upcoming_cache.clear()
    yield
    flask_app._upcoming_cache.clear()


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


# The in-process APScheduler was removed — day-of SMS is now driven by
# GitHub Actions calling /api/send-sms-cron — so there is no longer a
# background thread for the test session to shut down.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sydney_now():
    return datetime.now(flask_app.sydney_tz)


def days_from_now(n):
    return (sydney_now() + timedelta(days=n)).strftime('%Y-%m-%d')


def lunch_day_from_now(n):
    """days_from_now(n), moved forward to a day that actually serves lunch.

    Tuesday and Wednesday are dinner-only, so a test that just wants "an
    ordinary future date" has to say so — otherwise it passes or fails
    depending on which weekday the suite happens to run on.
    """
    day = (sydney_now() + timedelta(days=n)).date()
    while day.weekday() in flask_app.DINNER_ONLY_WEEKDAYS:
        day += timedelta(days=1)
    return str(day)


def dinner_only_day_from_now(n=1):
    """The first Tuesday or Wednesday on or after n days from now."""
    day = (sydney_now() + timedelta(days=n)).date()
    while day.weekday() not in flask_app.DINNER_ONLY_WEEKDAYS:
        day += timedelta(days=1)
    return str(day)


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
