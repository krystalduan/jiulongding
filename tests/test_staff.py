"""
Staff dashboard, admin and webhook tests.

Run with:  python3 -m pytest tests/test_staff.py -v
"""
import pytest

STAFF_PASSWORD = 'test-staff-password'
CRON_SECRET = 'test-cron-secret'
WEBHOOK_SECRET = 'test-webhook-secret'


def login(client):
    return client.post('/staff/login', data={'password': STAFF_PASSWORD})


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

class TestStaffLogin:

    def test_correct_password_logs_you_in(self, client):
        response = login(client)
        assert response.status_code == 302
        assert '/staff/dashboard' in response.headers['Location']

    def test_wrong_password_is_refused(self, client):
        response = client.post('/staff/login', data={'password': 'wrong'})
        assert response.status_code == 401
        assert 'invalid password' in response.get_data(as_text=True).lower()

    def test_empty_password_is_refused(self, client):
        assert client.post('/staff/login', data={'password': ''}).status_code == 401

    def test_missing_password_field_is_refused(self, client):
        assert client.post('/staff/login', data={}).status_code == 401

    def test_repeated_guessing_gets_rate_limited(self, client):
        statuses = [client.post('/staff/login', data={'password': f'guess{i}'}).status_code
                    for i in range(12)]
        assert 429 in statuses, "brute-force attempts were never rate limited"

    def test_rate_limit_does_not_leak_the_password(self, client):
        for i in range(12):
            client.post('/staff/login', data={'password': f'guess{i}'})
        body = client.post('/staff/login', data={'password': 'guess'}).get_data(as_text=True)
        assert STAFF_PASSWORD not in body


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

class TestStaffAccessControl:

    @pytest.mark.parametrize('path', [
        '/staff/dashboard',
        '/admin',
        '/staff/api/reservations/2026-01-01',
        '/send_today_confirmations',
        '/send_tomorrow_confirmations',
    ])
    def test_logged_out_users_are_sent_to_login(self, client, path):
        response = client.get(path)
        assert response.status_code == 302
        assert '/staff/login' in response.headers['Location']

    def test_update_status_requires_login(self, client):
        response = client.post('/staff/api/update_status',
                               json={'date': '2026-01-01', 'row_number': 2, 'status': 'Confirmed'})
        assert response.status_code == 302

    def test_dashboard_opens_once_logged_in(self, client, sheets):
        login(client)
        assert client.get('/staff/dashboard').status_code == 200

    def test_admin_panel_opens_once_logged_in(self, client, sheets):
        login(client)
        assert client.get('/admin').status_code == 200


# ---------------------------------------------------------------------------
# Reservations API
# ---------------------------------------------------------------------------

class TestReservationsApi:

    def test_reading_a_day_with_no_bookings(self, client, sheets):
        login(client)
        data = client.get('/staff/api/reservations/2026-01-01').get_json()
        assert data['success'] is False
        assert data['reservations'] == []

    def test_reading_a_day_with_bookings(self, client, sheets):
        from conftest import FakeWorksheet
        sheets.date_sheets['2026-01-01'] = FakeWorksheet('2026-01-01', [
            ['Name', 'Time', 'People', 'Phone', 'Email', 'Date', 'Dish', 'Notes', 'Confirmed', 'ID'],
            ['Jane', '19:00', '3-4', '61412345678', 'j@x.com', '2026-01-01', '大火锅', '', 'Pending', '1'],
        ])
        login(client)
        data = client.get('/staff/api/reservations/2026-01-01').get_json()
        assert data['success'] is True
        assert len(data['reservations']) == 1
        assert data['reservations'][0]['name'] == 'Jane'

    @pytest.mark.parametrize('bad_date', ['not-a-date', 'Master%20Data', '../secrets', '2026-1-1'])
    def test_malformed_dates_are_refused(self, client, sheets, bad_date):
        login(client)
        response = client.get(f'/staff/api/reservations/{bad_date}')
        assert response.status_code in (400, 404)


class TestTheOrderOfTheDay:
    """Tables still to serve first, in service order; the rest below them."""

    def _day(self, sheets, *statuses_and_times):
        from conftest import FakeWorksheet
        rows = [['Name', 'Time', 'People', 'Phone', 'Email', 'Date', 'Dish',
                 'Notes', 'Confirmed', 'ID']]
        for n, (status, time) in enumerate(statuses_and_times, start=1):
            rows.append([f'Guest{n}', time, '3-4', '61412345678', 'g@x.com',
                         '2026-01-01', '大火锅', '', status, str(n)])
        sheets.date_sheets['2026-01-01'] = FakeWorksheet('2026-01-01', rows)

    def _names(self, client):
        login(client)
        data = client.get('/staff/api/reservations/2026-01-01').get_json()
        return [(r['name'], r['confirmed'], r['time']) for r in data['reservations']]

    def test_live_bookings_come_first_in_time_order(self, client, sheets):
        self._day(sheets, ('Pending', '20:00'), ('Confirmed', '18:00'),
                  ('Pending', '19:00'))
        assert [t for _, _, t in self._names(client)] == ['18:00', '19:00', '20:00']

    def test_cancelled_and_modified_sink_to_the_bottom(self, client, sheets):
        self._day(sheets, ('Cancelled', '12:00'), ('Modified', '12:30'),
                  ('Pending', '20:30'), ('Confirmed', '20:00'))
        statuses = [s for _, s, _ in self._names(client)]
        assert statuses[:2] == ['Confirmed', 'Pending'], "live tables lead"
        assert set(statuses[2:]) == {'Cancelled', 'Modified'}

    def test_the_finished_ones_keep_time_order_among_themselves(self, client, sheets):
        self._day(sheets, ('Modified', '20:00'), ('Cancelled', '12:00'),
                  ('Pending', '13:00'))
        assert [t for _, _, t in self._names(client)] == ['13:00', '12:00', '20:00']

    def test_a_modified_booking_is_not_counted_as_pending(self, client, sheets):
        self._day(sheets, ('Modified', '19:00'), ('Pending', '20:00'))
        login(client)
        data = client.get('/staff/api/reservations/2026-01-01').get_json()
        assert data['total_pending'] == 1, "a table that moved is not still expected"


class TestUpdateStatus:

    def _sheet_with_one_booking(self, sheets):
        from conftest import FakeWorksheet
        sheets.date_sheets['2026-01-01'] = FakeWorksheet('2026-01-01', [
            ['Name', 'Time', 'People', 'Phone', 'Email', 'Date', 'Dish', 'Notes', 'Confirmed', 'ID'],
            ['Jane', '19:00', '3-4', '61412345678', 'j@x.com', '2026-01-01', '大火锅', '', 'Pending', '1'],
        ])

    def test_confirming_a_booking_works(self, client, sheets):
        self._sheet_with_one_booking(sheets)
        login(client)
        response = client.post('/staff/api/update_status',
                               json={'date': '2026-01-01', 'row_number': 2, 'status': 'Confirmed'})
        assert response.get_json()['success'] is True
        assert sheets.date_sheets['2026-01-01'].rows[1][8] == 'Confirmed'

    def test_cancelling_a_booking_works(self, client, sheets):
        self._sheet_with_one_booking(sheets)
        login(client)
        response = client.post('/staff/api/update_status',
                               json={'date': '2026-01-01', 'row_number': 2, 'status': 'Cancelled'})
        assert response.get_json()['success'] is True
        assert sheets.date_sheets['2026-01-01'].rows[1][8] == 'Cancelled'

    def test_a_made_up_status_is_refused(self, client, sheets):
        self._sheet_with_one_booking(sheets)
        login(client)
        response = client.post('/staff/api/update_status',
                               json={'date': '2026-01-01', 'row_number': 2, 'status': 'Whatever'})
        assert response.status_code == 400

    def test_a_bad_row_number_is_refused(self, client, sheets):
        login(client)
        for row in ['abc', -1, 0, None]:
            response = client.post('/staff/api/update_status',
                                   json={'date': '2026-01-01', 'row_number': row, 'status': 'Confirmed'})
            assert response.status_code == 400

    def test_a_bad_date_is_refused(self, client, sheets):
        login(client)
        response = client.post('/staff/api/update_status',
                               json={'date': 'Master Data', 'row_number': 2, 'status': 'Confirmed'})
        assert response.status_code == 400

    def test_empty_body_does_not_crash(self, client, sheets):
        login(client)
        assert client.post('/staff/api/update_status', json={}).status_code == 400


# ---------------------------------------------------------------------------
# Cron + webhook endpoints
# ---------------------------------------------------------------------------

class TestCronEndpoint:

    def test_correct_secret_is_accepted(self, client, sheets):
        response = client.get(f'/api/send-sms-cron?secret={CRON_SECRET}')
        assert response.status_code == 200

    @pytest.mark.parametrize('secret', ['', 'wrong', 'test-cron-secre', 'test-cron-secretX'])
    def test_wrong_secret_is_refused(self, client, secret):
        response = client.get(f'/api/send-sms-cron?secret={secret}')
        assert response.status_code == 401

    def test_no_secret_at_all_is_refused(self, client):
        assert client.get('/api/send-sms-cron').status_code == 401


class TestSmsIsSentOnce:
    """The daily job is triggered twice (once per Sydney DST offset), and can
    also be re-run by hand, so sending must be idempotent per day."""

    def _sheet_with_pending_bookings(self, sheets, app_module, n=3):
        from conftest import FakeWorksheet
        from datetime import datetime
        today = datetime.now(app_module.sydney_tz).strftime('%Y-%m-%d')
        rows = [['Name', 'Time', 'People', 'Phone', 'Email', 'Date', 'Dish',
                 'Notes', 'Confirmed', 'ID', 'SMS Reply', 'Method']]
        for i in range(n):
            rows.append([f'Guest{i}', '19:00', '2', f'6141234567{i}', 'g@x.com',
                         today, '大火锅', '', 'Pending', str(i + 1), '', ''])
        sheets.date_sheets[today] = FakeWorksheet(today, rows)
        return today

    def _count_sends(self, app_module, monkeypatch):
        sent = []
        monkeypatch.setattr(app_module, 'send_sms',
                            lambda to, msg, custom_ref=None: sent.append(to) or {'ok': True})
        return sent

    def test_first_run_texts_everyone(self, sheets, app_module, monkeypatch):
        self._sheet_with_pending_bookings(sheets, app_module, n=3)
        sent = self._count_sends(app_module, monkeypatch)
        today = __import__('datetime').datetime.now(app_module.sydney_tz).strftime('%Y-%m-%d')
        app_module.send_sms_on_date(today)
        assert len(sent) == 3, f"expected 3 texts, got {len(sent)}"

    def test_second_run_same_day_sends_nothing(self, sheets, app_module, monkeypatch):
        """This is the duplicate-SMS bug: it must send 3, then 0."""
        today = self._sheet_with_pending_bookings(sheets, app_module, n=3)
        sent = self._count_sends(app_module, monkeypatch)

        app_module.send_sms_on_date(today)
        assert len(sent) == 3

        result = app_module.send_sms_on_date(today)
        assert len(sent) == 3, f"second run re-sent {len(sent) - 3} duplicate texts"
        assert 'already sent today' in result

    def test_a_third_run_still_sends_nothing(self, sheets, app_module, monkeypatch):
        today = self._sheet_with_pending_bookings(sheets, app_module, n=2)
        sent = self._count_sends(app_module, monkeypatch)
        for _ in range(3):
            app_module.send_sms_on_date(today)
        assert len(sent) == 2

    def test_a_failed_send_is_retried(self, sheets, app_module, monkeypatch):
        """A failure must not be treated as 'already sent'."""
        today = self._sheet_with_pending_bookings(sheets, app_module, n=1)
        attempts = []

        def failing(to, msg, custom_ref=None):
            attempts.append(to)
            return None

        monkeypatch.setattr(app_module, 'send_sms', failing)
        app_module.send_sms_on_date(today)
        app_module.send_sms_on_date(today)
        assert len(attempts) == 2, "a failed send should be retried, not skipped"

    def test_confirmed_bookings_are_never_texted(self, sheets, app_module, monkeypatch):
        today = self._sheet_with_pending_bookings(sheets, app_module, n=2)
        sheets.date_sheets[today].rows[1][8] = 'Confirmed'
        sent = self._count_sends(app_module, monkeypatch)
        app_module.send_sms_on_date(today)
        assert len(sent) == 1

    def test_marker_records_the_send_date(self, sheets, app_module, monkeypatch):
        """The marker must carry the date, or tomorrow's run would think it
        had already sent and skip everyone."""
        import re
        from datetime import datetime
        today = self._sheet_with_pending_bookings(sheets, app_module, n=1)
        self._count_sends(app_module, monkeypatch)
        app_module.send_sms_on_date(today)

        marker = sheets.date_sheets[today].rows[1][10]
        assert re.search(r'\d{2}/\d{2}/\d{2}', marker), \
            f"marker has no date, so it cannot expire: {marker!r}"
        assert datetime.now(app_module.sydney_tz).strftime('%d/%m/%y') in marker

    def test_yesterdays_marker_does_not_block_today(self, sheets, app_module, monkeypatch):
        today = self._sheet_with_pending_bookings(sheets, app_module, n=1)
        sheets.date_sheets[today].rows[1][10] = 'day_of sent 01/01/20 08:30'
        sent = self._count_sends(app_module, monkeypatch)
        app_module.send_sms_on_date(today)
        assert len(sent) == 1, "a stale marker from another day must not block sending"


class TestSmsWebhook:

    def test_correct_secret_is_accepted(self, client, sheets):
        response = client.post(f'/sms-webhook?secret={WEBHOOK_SECRET}',
                               json={'sender': '61412345678', 'message': 'Y',
                                     'received_at': '2026-01-01T10:00:00Z'})
        assert response.status_code == 200

    def test_wrong_secret_is_refused(self, client):
        response = client.post('/sms-webhook?secret=wrong',
                               json={'sender': '61412345678', 'message': 'Y'})
        assert response.status_code == 401

    def test_no_secret_is_refused(self, client):
        response = client.post('/sms-webhook', json={'sender': '61412345678', 'message': 'Y'})
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

class TestPublicPages:

    @pytest.mark.parametrize('path', ['/', '/book', '/health', '/robots.txt', '/sitemap.xml'])
    def test_page_loads(self, client, sheets, path):
        assert client.get(path).status_code == 200

    @pytest.mark.parametrize('path', ['/', '/book'])
    def test_booking_pages_include_a_form_token(self, client, sheets, path):
        assert 'name="form_token"' in client.get(path).get_data(as_text=True)

    @pytest.mark.parametrize('path', ['/', '/book'])
    def test_booking_pages_include_the_honeypot(self, client, sheets, path):
        assert 'name="website"' in client.get(path).get_data(as_text=True)

    def test_each_page_load_issues_a_fresh_token(self, client, sheets):
        from conftest import get_form_token
        assert get_form_token(client) != get_form_token(client)

    def test_success_page_redirects_if_you_have_not_booked(self, client):
        response = client.get('/reservation_success')
        assert response.status_code == 302
