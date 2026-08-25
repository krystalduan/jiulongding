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
        '/staff/api/upcoming',
        '/send_today_confirmations',
        '/send_tomorrow_confirmations',
    ])
    def test_logged_out_users_are_sent_to_login(self, client, path):
        response = client.get(path)
        assert response.status_code == 302
        assert response.headers['Location'].endswith('/staff')

    def test_update_status_requires_login(self, client):
        response = client.post('/staff/api/update_status',
                               json={'date': '2026-01-01', 'row_number': 2, 'status': 'Confirmed'})
        assert response.status_code == 302

    def test_following_that_redirect_reaches_the_login_form(self, client, sheets):
        """It used to point at /staff/login, which is POST-only, so a staff
        member opening a bookmarked dashboard landed on a 405."""
        response = client.get('/staff/dashboard', follow_redirects=True)
        assert response.status_code == 200
        assert 'name="password"' in response.get_data(as_text=True)

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


class TestCoversForTheDay:
    """This used to be reported as total_people, summed with int() behind an
    isdigit() guard — and party size is a bucket, so the guard was never true
    and the figure was always 0."""

    def _day(self, sheets, *sizes_and_statuses):
        from conftest import FakeWorksheet
        rows = [['Name', 'Time', 'People', 'Phone', 'Email', 'Date', 'Dish',
                 'Notes', 'Confirmed', 'ID']]
        for n, (people, status) in enumerate(sizes_and_statuses, start=1):
            rows.append([f'Guest{n}', '19:00', people, '61412345678', 'g@x.com',
                         '2026-01-01', '大火锅', '', status, str(n)])
        sheets.date_sheets['2026-01-01'] = FakeWorksheet('2026-01-01', rows)

    def _covers(self, client):
        login(client)
        data = client.get('/staff/api/reservations/2026-01-01').get_json()
        return data['covers_low'], data['covers_high'], data['covers_open']

    def test_buckets_add_up_to_a_range(self, client, sheets):
        self._day(sheets, ('3-4', 'Pending'), ('1-2', 'Confirmed'))
        assert self._covers(client) == (4, 6, False)

    def test_an_open_ended_bucket_is_flagged(self, client, sheets):
        self._day(sheets, ('10+', 'Pending'))
        assert self._covers(client) == (10, 10, True)

    def test_cancelled_tables_are_not_cooked_for(self, client, sheets):
        self._day(sheets, ('3-4', 'Pending'), ('7-10', 'Cancelled'),
                  ('5-6', 'Modified'))
        assert self._covers(client) == (3, 4, False)


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


class TestTheDaysAhead:
    """The dashboard's landing view: every date from today on that still has
    tables to lay, counted from the same tabs the day view reads."""

    def _tab(self, sheets, date, *bookings):
        """bookings are (status, people) pairs."""
        from conftest import FakeWorksheet
        rows = [['Name', 'Time', 'People', 'Phone', 'Email', 'Date', 'Dish',
                 'Notes', 'Confirmed', 'ID']]
        for n, (status, people) in enumerate(bookings, start=1):
            rows.append([f'Guest{n}', '19:00', people, '61412345678', 'g@x.com',
                         date, '大火锅', '', status, str(n)])
        sheets.date_sheets[date] = FakeWorksheet(date, rows)

    def _days(self, client, query=''):
        login(client)
        response = client.get('/staff/api/upcoming' + query)
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        return data['days']

    def test_an_empty_spreadsheet_is_not_an_error(self, client, sheets):
        assert self._days(client) == []

    def test_upcoming_dates_are_listed_in_order(self, client, sheets):
        from conftest import days_from_now
        for offset in (5, 1, 2):
            self._tab(sheets, days_from_now(offset), ('Pending', '3-4'))
        dates = [day['date'] for day in self._days(client)]
        assert dates == [days_from_now(1), days_from_now(2), days_from_now(5)]

    def test_today_is_included_and_the_past_is_not(self, client, sheets):
        from conftest import days_from_now
        self._tab(sheets, days_from_now(0), ('Pending', '3-4'))
        self._tab(sheets, days_from_now(-1), ('Pending', '3-4'))
        self._tab(sheets, days_from_now(-30), ('Confirmed', '3-4'))
        assert [d['date'] for d in self._days(client)] == [days_from_now(0)]

    def test_today_and_tomorrow_are_named(self, client, sheets):
        from conftest import days_from_now
        for offset in (0, 1, 2):
            self._tab(sheets, days_from_now(offset), ('Pending', '3-4'))
        assert [d['relative'] for d in self._days(client)] == ['Today', 'Tomorrow', '']

    def test_tabs_that_are_not_dates_are_ignored(self, client, sheets):
        """Master Data and Unknown Replies are tabs in the same spreadsheet."""
        from conftest import FakeWorksheet, days_from_now
        sheets.date_sheets['Unknown Replies'] = FakeWorksheet(
            'Unknown Replies', [['Timestamp'], ['2026-01-01 10:00']])
        self._tab(sheets, days_from_now(1), ('Pending', '3-4'))
        assert [d['date'] for d in self._days(client)] == [days_from_now(1)]

    def test_a_day_of_only_cancellations_is_left_off(self, client, sheets):
        from conftest import days_from_now
        self._tab(sheets, days_from_now(1), ('Cancelled', '3-4'), ('Modified', '1-2'))
        assert self._days(client) == [], "a day with no live tables is not upcoming work"

    def test_cancelled_bookings_are_not_counted(self, client, sheets):
        from conftest import days_from_now
        self._tab(sheets, days_from_now(1),
                  ('Pending', '3-4'), ('Cancelled', '7-10'), ('Modified', '10+'))
        day = self._days(client)[0]
        assert day['bookings'] == 1
        assert day['covers_high'] == 4, "a cancelled table is not covers to cook for"

    def test_confirmed_and_pending_are_split(self, client, sheets):
        from conftest import days_from_now
        self._tab(sheets, days_from_now(1), ('Confirmed', '3-4'), ('Pending', '1-2'),
                  ('', '1-2'), ('Reply needs review: maybe', '1-2'))
        day = self._days(client)[0]
        assert (day['bookings'], day['confirmed'], day['pending']) == (4, 1, 3)

    def test_covers_are_a_range_because_party_size_is_a_bucket(self, client, sheets):
        from conftest import days_from_now
        self._tab(sheets, days_from_now(1), ('Pending', '3-4'), ('Confirmed', '1-2'))
        day = self._days(client)[0]
        assert (day['covers_low'], day['covers_high']) == (4, 6)
        assert day['covers_open'] is False

    def test_an_open_ended_party_size_is_flagged(self, client, sheets):
        from conftest import days_from_now
        self._tab(sheets, days_from_now(1), ('Pending', '10+'))
        day = self._days(client)[0]
        assert (day['covers_low'], day['covers_high']) == (10, 10)
        assert day['covers_open'] is True, "'10+' could be twenty; say so"

    def test_an_unreadable_party_size_still_counts_as_a_booking(self, client, sheets):
        from conftest import days_from_now
        self._tab(sheets, days_from_now(1), ('Pending', ''), ('Pending', 'a party'))
        day = self._days(client)[0]
        assert day['bookings'] == 2
        assert day['covers_high'] == 0

    def test_the_whole_view_costs_two_api_reads(self, client, sheets):
        """One for the tab list, one for every tab's contents. Reading the tabs
        in a loop instead would be twenty round trips against a ceiling of 60
        per minute."""
        from conftest import days_from_now
        for offset in range(1, 15):
            self._tab(sheets, days_from_now(offset), ('Pending', '3-4'))
        sheets.reads.clear()
        self._days(client)
        assert sheets.reads == ['worksheets', 'values_batch_get'], \
            f"14 dates cost {len(sheets.reads)} reads"

    def test_a_second_load_is_served_from_cache(self, client, sheets):
        from conftest import days_from_now
        self._tab(sheets, days_from_now(1), ('Pending', '3-4'))
        self._days(client)
        sheets.reads.clear()
        self._days(client)
        assert sheets.reads == [], "the overview should not re-read within the TTL"

    def test_refresh_bypasses_the_cache(self, client, sheets):
        """What a staff member gets after confirming a booking and tapping back:
        the count they just changed must not come from cache."""
        from conftest import days_from_now
        self._tab(sheets, days_from_now(1), ('Pending', '3-4'))
        self._days(client)
        sheets.reads.clear()
        assert self._days(client, '?refresh=1')[0]['pending'] == 1
        assert sheets.reads == ['worksheets', 'values_batch_get']

    def test_a_change_shows_up_on_a_forced_refresh(self, client, sheets):
        from conftest import days_from_now
        date = days_from_now(1)
        self._tab(sheets, date, ('Pending', '3-4'))
        assert self._days(client)[0]['pending'] == 1

        client.post('/staff/api/update_status',
                    json={'date': date, 'row_number': 2, 'status': 'Confirmed'})
        day = self._days(client, '?refresh=1')[0]
        assert (day['pending'], day['confirmed']) == (0, 1)

    def test_a_broken_spreadsheet_reports_failure_rather_than_crashing(
            self, client, sheets, monkeypatch, app_module):
        def boom():
            raise RuntimeError('Sheets is down')

        monkeypatch.setattr(sheets, 'worksheets', boom)
        login(client)
        response = client.get('/staff/api/upcoming')
        assert response.status_code == 503
        assert response.get_json()['days'] == []


class TestTheDashboardPage:

    def test_it_defaults_to_today_in_sydney(self, client, sheets, app_module):
        """Fly runs in UTC, so datetime.now() is yesterday from 10am Sydney on —
        the dashboard used to open on the wrong day for all of service."""
        from datetime import datetime
        login(client)
        html = client.get('/staff/dashboard').get_data(as_text=True)
        today = datetime.now(app_module.sydney_tz).strftime('%Y-%m-%d')
        assert f'value="{today}"' in html


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

    def test_the_summary_counts_untextable_bookings_apart(self, sheets, app_module,
                                                          monkeypatch):
        """"Already sent" is the job working; "no mobile" is a table that will
        sit at Pending until somebody rings it. One number for both hid that."""
        from conftest import FakeWorksheet
        from datetime import datetime
        today = datetime.now(app_module.sydney_tz).strftime('%Y-%m-%d')
        sheets.date_sheets[today] = FakeWorksheet(today, [
            ['Name', 'Time', 'People', 'Phone', 'Email', 'Date', 'Dish', 'Notes',
             'Confirmed', 'ID', 'SMS Reply', 'Method'],
            ['Landline', '19:00', '2', '0298765432', 'a@x.com', today, '炒菜', '',
             'Pending', '1', '', ''],
            ['Mobile', '19:00', '2', '61412345678', 'b@x.com', today, '大火锅', '',
             'Pending', '2', '', ''],
        ])
        self._count_sends(app_module, monkeypatch)
        result = app_module.send_sms_on_date(today)
        assert '1 sent' in result
        assert 'no mobile number' in result

    def test_yesterdays_marker_does_not_block_today(self, sheets, app_module, monkeypatch):
        today = self._sheet_with_pending_bookings(sheets, app_module, n=1)
        sheets.date_sheets[today].rows[1][10] = 'day_of sent 01/01/20 08:30'
        sent = self._count_sends(app_module, monkeypatch)
        app_module.send_sms_on_date(today)
        assert len(sent) == 1, "a stale marker from another day must not block sending"


class TestWhichDayAReplyIsAbout:
    """The provider timestamps in UTC and Sydney is ten hours ahead of it, so
    formatting that instant directly named the wrong day for any reply before
    mid-morning — and the reminder goes out at 8:30, so that was all of them."""

    def test_an_early_morning_reply_belongs_to_the_sydney_day(self, app_module):
        # 8:35am Sydney on the 13th is 22:35 UTC on the 12th.
        assert app_module.get_reservation_date_from_sms(
            '2026-08-12T22:35:00Z') == '2026-08-13'

    def test_an_evening_reply_is_unaffected(self, app_module):
        assert app_module.get_reservation_date_from_sms(
            '2026-08-13T09:00:00Z') == '2026-08-13'

    def test_a_timestamp_with_an_offset_is_honoured(self, app_module):
        assert app_module.get_reservation_date_from_sms(
            '2026-08-13T08:35:00+10:00') == '2026-08-13'

    def test_a_timestamp_with_no_offset_is_read_as_local(self, app_module):
        assert app_module.get_reservation_date_from_sms(
            '2026-08-13T08:35:00') == '2026-08-13'

    @pytest.mark.parametrize('bad', [None, '', 'yesterday', '13/08/2026'])
    def test_an_unusable_timestamp_gives_nothing(self, app_module, bad):
        assert app_module.get_reservation_date_from_sms(bad) is None


class TestWhichBookingAReplyIsAbout:

    def _day(self, sheets, app_module, *bookings):
        """bookings are (name, phone, status) triples, on today in Sydney."""
        from conftest import FakeWorksheet
        from datetime import datetime
        today = datetime.now(app_module.sydney_tz).strftime('%Y-%m-%d')
        rows = [['Name', 'Time', 'People', 'Phone', 'Email', 'Date', 'Dish',
                 'Notes', 'Confirmed', 'ID', 'SMS Reply', 'Method']]
        for n, (name, phone, status) in enumerate(bookings, start=1):
            rows.append([name, '19:00', '3-4', phone, 'g@x.com', today,
                         '大火锅', '', status, str(n), '', ''])
        sheets.date_sheets[today] = FakeWorksheet(today, rows)
        return sheets.date_sheets[today]

    def _reply(self, app_module, phone='61412345678', message='Y'):
        from datetime import datetime
        now = datetime.now(app_module.sydney_tz).isoformat()
        return app_module.process_sms_reply_smart(phone, message, now)

    def test_a_reply_confirms_the_live_booking_not_a_cancelled_one(
            self, sheets, app_module):
        """find() took the first phone match in the tab, so a customer who had
        already cancelled once that day confirmed the cancelled row and left the
        real table sitting at Pending."""
        sheet = self._day(sheets, app_module,
                          ('Jane', '61412345678', 'Cancelled'),
                          ('Jane', '61412345678', 'Pending'))
        assert self._reply(app_module) is True
        assert sheet.rows[1][8] == 'Cancelled', 'the cancelled row was reopened'
        assert sheet.rows[2][8] == 'Confirmed'

    def test_a_row_that_moved_to_another_date_is_skipped(self, sheets, app_module):
        sheet = self._day(sheets, app_module,
                          ('Jane', '61412345678', 'Modified'),
                          ('Jane', '61412345678', 'Pending'))
        self._reply(app_module)
        assert sheet.rows[1][8] == 'Modified'
        assert sheet.rows[2][8] == 'Confirmed'

    def test_a_number_stored_in_local_form_still_matches(self, sheets, app_module):
        """The sheet holds what was typed at booking time; the provider reports
        the sender in international form. A literal search finds neither."""
        sheet = self._day(sheets, app_module, ('Jane', '0412345678', 'Pending'))
        assert self._reply(app_module, phone='61412345678') is True
        assert sheet.rows[1][8] == 'Confirmed'

    def test_a_cancellation_reply_cancels(self, sheets, app_module):
        sheet = self._day(sheets, app_module, ('Jane', '61412345678', 'Pending'))
        self._reply(app_module, message='N')
        assert sheet.rows[1][8] == 'Cancelled'

    def test_anything_else_is_flagged_for_review(self, sheets, app_module):
        sheet = self._day(sheets, app_module, ('Jane', '61412345678', 'Pending'))
        self._reply(app_module, message='maybe?')
        assert 'needs review' in sheet.rows[1][8].lower()

    def test_a_stranger_is_filed_as_unknown(self, sheets, app_module):
        self._day(sheets, app_module, ('Jane', '61412345678', 'Pending'))
        assert self._reply(app_module, phone='61499000111') is False
        assert 'Unknown Replies' in sheets.date_sheets

    def test_filing_an_unknown_reply_labels_the_new_tab(self, sheets, app_module):
        """The header write used the old gspread argument order, so the one run
        that had to create this tab was the run that raised instead of filing
        the reply."""
        self._reply(app_module, phone='61499000111')
        unknown = sheets.date_sheets['Unknown Replies']
        assert unknown.rows[0][:2] == ['Timestamp', 'Phone Number']
        assert unknown.rows[1][1] == '61499000111'


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
