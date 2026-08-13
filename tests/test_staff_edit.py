"""
Staff editing a booking from the dashboard: time, date, party size, phone.

Run with:  python3 -m pytest tests/test_staff_edit.py -v
"""
import pytest

from conftest import FakeWorksheet, days_from_now

STAFF_PASSWORD = 'test-staff-password'

# Column letters in a date tab, for reading assertions:
#   A name  B time  C people  D phone  E email  F date
#   G dish  H notes  I confirmed  J id  K sms reply  L method
NAME, TIME, PEOPLE, PHONE, EMAIL, DATE = 0, 1, 2, 3, 4, 5
DISH, NOTES, CONFIRMED, RES_ID, SMS_REPLY, METHOD = 6, 7, 8, 9, 10, 11


def login(client):
    return client.post('/staff/login', data={'password': STAFF_PASSWORD})


def booking_row(name='Jane', time='19:00', people='3-4', phone='61412345678',
                email='jane@x.com', date=None, status='Pending', res_id='1'):
    return [name, time, people, phone, email, date, '大火锅', '', status, res_id, '', '']


def a_day(sheets, date, *rows):
    header = ['Name', 'Time', 'People', 'Phone', 'Email', 'Date', 'Dish Type',
              'Notes', 'Confirmed', 'Reservation ID', 'SMS Reply', 'Confirmation Method']
    filled = [list(row) for row in rows]
    for row in filled:
        row[DATE] = row[DATE] or date
    sheets.date_sheets[date] = FakeWorksheet(date, [header] + filled)
    return sheets.date_sheets[date]


def a_master_row(sheets, res_id='1', name='Jane', date=None, time='19:00',
                 people='3-4', phone='61412345678', email='jane@x.com'):
    """Master Data is the id -> date index, so an edit has to keep it in step."""
    sheets.master.rows.append([res_id, name, date, time, people, '大火锅',
                               phone, email, '', '01/01/26 10:00'])
    return len(sheets.master.rows)


def edit(client, date_tab, **fields):
    payload = {'date_tab': date_tab, 'row_number': 2, 'reservation_id': '1'}
    payload.update(fields)
    return client.post('/staff/api/update_booking', json=payload)


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

class TestAccessControl:

    def test_editing_requires_login(self, client):
        response = client.post('/staff/api/update_booking',
                               json={'date_tab': '2026-01-01', 'row_number': 2,
                                     'time': '19:00'})
        assert response.status_code == 302
        assert response.headers['Location'].endswith('/staff')


# ---------------------------------------------------------------------------
# Changes that stay on the same date
# ---------------------------------------------------------------------------

class TestEditingInPlace:

    @pytest.fixture
    def day(self, sheets, client):
        date = days_from_now(3)
        sheet = a_day(sheets, date, booking_row())
        a_master_row(sheets, date=date)
        login(client)
        return date, sheet

    def test_changing_the_time(self, client, day):
        date, sheet = day
        response = edit(client, date, time='20:00')
        assert response.get_json()['success'] is True
        assert sheet.rows[1][TIME] == '20:00'

    def test_changing_the_party_size(self, client, day):
        date, sheet = day
        assert edit(client, date, people='7-10').get_json()['success'] is True
        assert sheet.rows[1][PEOPLE] == '7-10'

    def test_changing_the_phone_number_normalises_it(self, client, day):
        date, sheet = day
        assert edit(client, date, phone='0499 888 777').get_json()['success'] is True
        assert sheet.rows[1][PHONE] == '61499888777'

    def test_changing_several_things_at_once(self, client, day):
        date, sheet = day
        assert edit(client, date, time='12:30', people='1-2',
                    phone='0400000000').get_json()['success'] is True
        assert sheet.rows[1][TIME] == '12:30'
        assert sheet.rows[1][PEOPLE] == '1-2'
        assert sheet.rows[1][PHONE] == '61400000000'

    def test_the_change_is_written_into_the_audit_column(self, client, day):
        date, sheet = day
        edit(client, date, time='20:30')
        note = sheet.rows[1][METHOD]
        assert 'Edited by staff' in note
        assert '19:00->20:30' in note, f'the note does not say what changed: {note!r}'

    def test_an_untouched_field_is_not_rewritten(self, client, day):
        """A form submitted with one box changed should write one cell."""
        date, sheet = day
        edit(client, date, time='20:00', people='3-4', phone='61412345678')
        written = [update['range'] for batch in sheet.batches for update in batch]
        assert 'C2' not in written and 'D2' not in written
        assert 'B2' in written

    def test_a_change_that_changes_nothing_is_refused(self, client, day):
        date, sheet = day
        response = edit(client, date, time='19:00', people='3-4', phone='61412345678')
        assert response.status_code == 400
        assert 'nothing' in response.get_json()['message'].lower()
        assert sheet.batches == [], 'a no-op should not cost a write'

    def test_master_data_is_kept_in_step(self, client, sheets, day):
        date, _ = day
        edit(client, date, time='20:00', people='7-10', phone='0499888777')
        master = sheets.master.rows[1]
        assert master[3] == '20:00', 'master time is stale'
        assert master[4] == '7-10', 'master party size is stale'
        assert master[6] == '61499888777', 'master phone is stale'


# ---------------------------------------------------------------------------
# Moving a booking to another date
# ---------------------------------------------------------------------------

class TestMovingToAnotherDate:

    @pytest.fixture
    def day(self, sheets, client):
        date = days_from_now(3)
        sheet = a_day(sheets, date, booking_row())
        a_master_row(sheets, date=date)
        login(client)
        return date, sheet

    def test_the_booking_appears_on_the_new_date(self, client, sheets, day):
        date, _ = day
        new_date = days_from_now(6)
        response = edit(client, date, date=new_date, time='18:00')
        body = response.get_json()

        assert body['success'] is True and body['moved'] is True
        assert body['new_date'] == new_date

        moved = sheets.date_sheets[new_date].rows[-1]
        assert moved[NAME] == 'Jane'
        assert moved[TIME] == '18:00'
        assert moved[DATE] == new_date

    def test_the_old_row_is_kept_and_marked_modified(self, client, day):
        """Staff opening the original date should see that the table moved,
        rather than find a booking that vanished overnight."""
        date, sheet = day
        edit(client, date, date=days_from_now(6))
        assert sheet.rows[1][CONFIRMED] == 'Modified'
        assert 'Edited by staff' in sheet.rows[1][METHOD]

    def test_other_edits_travel_with_the_move(self, client, sheets, day):
        date, _ = day
        new_date = days_from_now(5)
        edit(client, date, date=new_date, people='10+', phone='0433222111')
        moved = sheets.date_sheets[new_date].rows[-1]
        assert moved[PEOPLE] == '10+'
        assert moved[PHONE] == '61433222111'

    def test_a_date_nobody_has_booked_yet_gets_a_tab(self, client, sheets, day):
        date, _ = day
        new_date = days_from_now(11)
        assert new_date not in sheets.date_sheets
        assert edit(client, date, date=new_date).get_json()['success'] is True
        assert new_date in sheets.date_sheets

    def test_master_data_follows_the_move(self, client, sheets, day):
        """This is what lets a manage link already in the customer's inbox find
        the booking in its new tab."""
        date, _ = day
        new_date = days_from_now(6)
        edit(client, date, date=new_date, time='12:00')
        assert sheets.master.rows[1][2] == new_date
        assert sheets.master.rows[1][3] == '12:00'

    def test_moving_to_today_is_allowed_for_staff(self, client, sheets, day):
        """The customer's own reschedule refuses this — a same-day change to the
        kitchen's numbers goes through the phone. This *is* that phone call."""
        date, _ = day
        today = days_from_now(0)
        assert edit(client, date, date=today).get_json()['success'] is True
        assert sheets.date_sheets[today].rows[-1][NAME] == 'Jane'


# ---------------------------------------------------------------------------
# Writing to the right row
# ---------------------------------------------------------------------------

class TestNotWritingToTheWrongTable:
    """A row number is a position in a sheet people also edit by hand, so on its
    own it is a guess about which booking sits there."""

    def test_the_id_is_followed_when_the_row_has_shifted(self, client, sheets):
        date = days_from_now(2)
        sheet = a_day(sheets, date,
                      booking_row(name='Someone Else', res_id='9'),
                      booking_row(name='Jane', res_id='1'))
        login(client)

        # The dashboard was showing Jane at row 2; a row has since been inserted
        # above her.
        response = edit(client, date, time='20:00')
        assert response.get_json()['success'] is True
        assert sheet.rows[1][TIME] == '19:00', "someone else's booking was rewritten"
        assert sheet.rows[2][TIME] == '20:00'

    def test_a_booking_that_is_gone_is_refused(self, client, sheets):
        date = days_from_now(2)
        sheet = a_day(sheets, date, booking_row(res_id='9'))
        login(client)

        response = edit(client, date, time='20:00')
        assert response.status_code == 409
        assert response.get_json()['stale'] is True
        assert sheet.batches == [], 'nothing should have been written'

    def test_a_row_whose_date_cell_is_blank_is_not_moved_onto_itself(self, client, sheets):
        """Which day a booking is on is decided by the tab it lives in, not by
        its Date cell — and older rows have that cell empty. Comparing against
        the cell made 'same date' look like a move, which appended a second copy
        to the same tab and marked the original Modified."""
        date = days_from_now(3)
        row = booking_row()
        row[DATE] = ''
        sheet = a_day(sheets, date, row)
        sheet.rows[1][DATE] = ''          # a_day fills it in; put it back
        login(client)

        response = edit(client, date, date=date, time='20:00')
        body = response.get_json()

        assert body['success'] is True
        assert body['moved'] is False, 'a time change was treated as a move'
        assert len(sheet.rows) == 2, f'the booking was duplicated: {sheet.rows}'
        assert sheet.rows[1][CONFIRMED] == 'Pending'
        assert sheet.rows[1][TIME] == '20:00'

    def test_a_date_with_no_tab_is_refused(self, client, sheets):
        login(client)
        response = edit(client, days_from_now(2), time='20:00')
        assert response.status_code == 409

    def test_status_writes_check_the_id_too(self, client, sheets):
        date = days_from_now(2)
        sheet = a_day(sheets, date, booking_row(res_id='9'))
        login(client)

        response = client.post('/staff/api/update_status',
                               json={'date': date, 'row_number': 2,
                                     'reservation_id': '1', 'status': 'Cancelled'})
        assert response.status_code == 409
        assert sheet.rows[1][CONFIRMED] == 'Pending', 'a stranger was cancelled'

    def test_a_status_write_without_an_id_still_works(self, client, sheets):
        """Rows created before reservation ids existed have nothing else to
        identify them by."""
        date = days_from_now(2)
        sheet = a_day(sheets, date, booking_row(res_id=''))
        login(client)

        response = client.post('/staff/api/update_status',
                               json={'date': date, 'row_number': 2, 'status': 'Confirmed'})
        assert response.get_json()['success'] is True
        assert sheet.rows[1][CONFIRMED] == 'Confirmed'


class TestTwoStaffAtOnce:

    def test_a_booking_changed_underneath_you_is_not_overwritten(self, client, sheets):
        date = days_from_now(3)
        sheet = a_day(sheets, date, booking_row(time='19:00'))
        login(client)

        # Another phone — or the customer's own manage link — moved it to 20:00
        # after this dashboard drew the card.
        sheet.rows[1][TIME] = '20:00'

        response = edit(client, date, time='12:00',
                        expect={'time': '19:00', 'people': '3-4',
                                'phone': '61412345678'})
        assert response.status_code == 409
        assert 'someone else' in response.get_json()['message'].lower()
        assert sheet.rows[1][TIME] == '20:00', "the other change was lost"

    def test_matching_expectations_go_through(self, client, sheets):
        date = days_from_now(3)
        sheet = a_day(sheets, date, booking_row())
        login(client)

        response = edit(client, date, time='12:00',
                        expect={'time': '19:00', 'people': '3-4',
                                'phone': '61412345678'})
        assert response.get_json()['success'] is True
        assert sheet.rows[1][TIME] == '12:00'


class TestFinishedBookings:

    @pytest.mark.parametrize('status', ['Cancelled', 'Modified', 'No'])
    def test_they_cannot_be_edited(self, client, sheets, status):
        date = days_from_now(3)
        sheet = a_day(sheets, date, booking_row(status=status))
        login(client)

        response = edit(client, date, time='20:00')
        assert response.status_code == 409
        assert sheet.rows[1][TIME] == '19:00'


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:

    @pytest.fixture
    def day(self, sheets, client):
        date = days_from_now(3)
        sheet = a_day(sheets, date, booking_row())
        login(client)
        return date, sheet

    @pytest.mark.parametrize('bad_time', ['25:00', '19:15', '', 'lunch', '7pm'])
    def test_a_time_we_do_not_serve_is_refused(self, client, day, bad_time):
        date, sheet = day
        assert edit(client, date, time=bad_time).status_code == 400
        assert sheet.rows[1][TIME] == '19:00'

    @pytest.mark.parametrize('bad_size', ['0', '5-6', 'lots', '', '100'])
    def test_a_party_size_that_is_not_one_of_ours_is_refused(self, client, day, bad_size):
        date, _ = day
        assert edit(client, date, people=bad_size).status_code == 400

    def test_a_date_in_the_past_is_refused(self, client, day):
        date, _ = day
        response = edit(client, date, date=days_from_now(-1))
        assert response.status_code == 400
        assert 'passed' in response.get_json()['message'].lower()

    def test_a_date_beyond_the_booking_window_is_refused(self, client, day):
        date, _ = day
        assert edit(client, date, date=days_from_now(90)).status_code == 400

    @pytest.mark.parametrize('bad_date', ['not-a-date', '2026-13-01', '01/02/2026', ''])
    def test_an_unreadable_date_is_refused(self, client, day, bad_date):
        date, _ = day
        assert edit(client, date, date=bad_date).status_code == 400

    @pytest.mark.parametrize('bad_phone', ['call the office', '123', '', 'oh four one two'])
    def test_something_that_is_not_a_phone_number_is_refused(self, client, day, bad_phone):
        date, _ = day
        assert edit(client, date, phone=bad_phone).status_code == 400

    @pytest.mark.parametrize('bad_tab', ['Master Data', 'not-a-date', '../secrets'])
    def test_a_malformed_date_tab_is_refused(self, client, day, bad_tab):
        response = client.post('/staff/api/update_booking',
                               json={'date_tab': bad_tab, 'row_number': 2,
                                     'reservation_id': '1', 'time': '20:00'})
        assert response.status_code == 400

    @pytest.mark.parametrize('bad_row', ['abc', 0, 1, -5, None])
    def test_a_bad_row_number_is_refused(self, client, day, bad_row):
        date, _ = day
        response = client.post('/staff/api/update_booking',
                               json={'date_tab': date, 'row_number': bad_row,
                                     'time': '20:00'})
        assert response.status_code == 400

    def test_an_empty_body_does_not_crash(self, client, day):
        assert client.post('/staff/api/update_booking', json={}).status_code == 400


# ---------------------------------------------------------------------------
# Numbers that cannot be texted
# ---------------------------------------------------------------------------

class TestNonMobileNumbers:
    """Staff take bookings from landlines and overseas numbers, so those are
    kept — but the day-of reminder can never reach them, and pretending
    otherwise leaves a table nobody ever confirms."""

    @pytest.fixture
    def day(self, sheets, client):
        date = days_from_now(3)
        sheet = a_day(sheets, date, booking_row())
        login(client)
        return date, sheet

    def test_a_landline_is_accepted(self, client, day):
        date, sheet = day
        response = edit(client, date, phone='02 9876 5432')
        assert response.get_json()['success'] is True
        assert sheet.rows[1][PHONE] == '0298765432'

    def test_saving_one_says_it_cannot_be_texted(self, client, day):
        date, _ = day
        warnings = ' '.join(edit(client, date, phone='0298765432').get_json()['warnings'])
        assert 'text' in warnings.lower()

    def test_a_mobile_is_not_flagged(self, client, day):
        date, _ = day
        assert edit(client, date, phone='0499888777').get_json()['warnings'] == []

    def test_the_day_view_marks_it(self, client, sheets):
        date = days_from_now(1)
        a_day(sheets, date, booking_row(phone='0298765432'),
              booking_row(phone='61412345678', res_id='2'))
        login(client)
        rows = client.get(f'/staff/api/reservations/{date}').get_json()['reservations']
        flags = {row['phone']: row['textable'] for row in rows}
        assert flags == {'0298765432': False, '61412345678': True}

    def test_the_reminder_text_skips_it(self, client, sheets, app_module, monkeypatch):
        """The guard has to be what the number is, not whether the cell is
        filled — handing a landline to the SMS API spends a message on a send
        that cannot arrive."""
        today = days_from_now(0)
        a_day(sheets, today,
              booking_row(name='Landline', phone='0298765432'),
              booking_row(name='Mobile', phone='61412345678', res_id='2'))

        sent = []
        monkeypatch.setattr(app_module, 'send_sms',
                            lambda to, msg, custom_ref=None: sent.append(to) or {'ok': True})
        app_module.send_sms_on_date(today)
        assert sent == ['61412345678']


# ---------------------------------------------------------------------------
# What a change means for the booking's status
# ---------------------------------------------------------------------------

class TestReconfirming:

    def _confirmed_day(self, sheets, client):
        date = days_from_now(3)
        sheet = a_day(sheets, date, booking_row(status='Confirmed'))
        login(client)
        return date, sheet

    def test_moving_a_confirmed_booking_puts_it_back_to_pending(self, client, sheets):
        """They agreed to a time that no longer exists, and Pending is also what
        puts them back in the day-of reminder to confirm the new one."""
        date, sheet = self._confirmed_day(sheets, client)
        response = edit(client, date, time='12:00')
        assert response.get_json()['success'] is True
        assert sheet.rows[1][CONFIRMED] == 'Pending'

    def test_and_says_so(self, client, sheets):
        date, _ = self._confirmed_day(sheets, client)
        warnings = ' '.join(edit(client, date, time='12:00').get_json()['warnings'])
        assert 'pending' in warnings.lower()

    def test_a_moved_confirmed_booking_lands_pending_on_the_new_date(self, client, sheets):
        date, _ = self._confirmed_day(sheets, client)
        new_date = days_from_now(7)
        edit(client, date, date=new_date)
        assert sheets.date_sheets[new_date].rows[-1][CONFIRMED] == 'Pending'

    def test_correcting_a_phone_number_leaves_it_confirmed(self, client, sheets):
        """A wrong digit in a phone number is not a reason to make somebody
        confirm their table again."""
        date, sheet = self._confirmed_day(sheets, client)
        edit(client, date, phone='0499888777')
        assert sheet.rows[1][CONFIRMED] == 'Confirmed'

    def test_a_pending_booking_stays_pending(self, client, sheets):
        date = days_from_now(3)
        sheet = a_day(sheets, date, booking_row(status='Pending'))
        login(client)
        edit(client, date, time='12:00')
        assert sheet.rows[1][CONFIRMED] == 'Pending'


# ---------------------------------------------------------------------------
# Telling the customer
# ---------------------------------------------------------------------------

class TestNotifyingTheCustomer:

    def test_nothing_is_sent_unless_staff_ask(self, client, sheets):
        """Most of these edits are made with the customer on the phone being
        told the new time, so an email is the staff member's call."""
        date = days_from_now(3)
        a_day(sheets, date, booking_row())
        login(client)
        assert edit(client, date, time='12:00').get_json()['notified'] is False

    def test_it_is_sent_when_they_do(self, client, sheets):
        date = days_from_now(3)
        a_day(sheets, date, booking_row())
        login(client)
        assert edit(client, date, time='12:00', notify=True).get_json()['notified'] is True

    def test_a_booking_with_no_email_says_so(self, client, sheets):
        date = days_from_now(3)
        a_day(sheets, date, booking_row(email=''))
        login(client)
        body = edit(client, date, time='12:00', notify=True).get_json()
        assert body['notified'] is False
        assert any('email' in warning.lower() for warning in body['warnings'])

    def test_the_email_strikes_through_what_changed(self, app_module):
        """build_change_email only crossed out the date and time. Staff can now
        change the party size and the phone too, and a new party size shown with
        nothing struck out beside it reads as though we had it wrong all along."""
        _, html, text = app_module.build_change_email(
            'Jane',
            {'date': '2026-08-20', 'time': '19:00', 'people': '7-10',
             'dish_type': '大火锅', 'phone': '61499888777', 'email': 'j@x.com',
             'reservation_id': '1'},
            {'date': '2026-08-20', 'time': '19:00', 'people': '3-4',
             'phone': '61412345678'},
        )
        assert 'line-through' in html
        assert '3-4 people' in html and '7-10 people' in html
        assert 'was 3-4 people' in text
        assert 'was +61 412 345 678' in text

    def test_an_unchanged_field_is_not_struck_through(self, app_module):
        _, html, _ = app_module.build_change_email(
            'Jane',
            {'date': '2026-08-20', 'time': '20:00', 'people': '3-4',
             'dish_type': '大火锅', 'phone': '61412345678', 'email': 'j@x.com',
             'reservation_id': '1'},
            {'date': '2026-08-20', 'time': '19:00', 'people': '3-4',
             'phone': '61412345678'},
        )
        assert html.count('line-through') == 1, 'only the time changed'


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:

    def test_a_runaway_client_is_stopped(self, client, sheets):
        date = days_from_now(3)
        a_day(sheets, date, booking_row())
        login(client)
        statuses = [edit(client, date, time='19:30' if i % 2 else '19:00').status_code
                    for i in range(70)]
        assert 429 in statuses
