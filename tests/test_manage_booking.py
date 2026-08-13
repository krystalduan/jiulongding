"""
Customer self-service: cancel or move a booking from the emailed link.

Run with:  python3 -m pytest tests/test_manage_booking.py -v

The token in the URL is the entire credential, so the security tests here
matter as much as the behavioural ones.
"""
import re
import types
from datetime import datetime, timedelta

import pytest

from conftest import FakeWorksheet, days_from_now


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_booking(sheets, app_module, date=None, time='19:00', email='jane@gmail.com',
                 status='Pending', reservation_id='7', name='Jane Smith',
                 in_master=False):
    """Put one booking in its date tab and return (token, date).

    in_master also writes the Master Data row, which is what lets a link sent
    before a date change still find the booking afterwards.
    """
    date = date or days_from_now(3)
    header = ['Name', 'Time', 'People', 'Phone', 'Email', 'Date', 'Dish Type',
              'Notes', 'Confirmed', 'Reservation ID', 'SMS Reply', 'Confirmation Method']
    row = [name, time, '3-4', '61412345678', email, date, '大火锅', '',
           status, reservation_id, '', '']
    sheets.date_sheets[date] = FakeWorksheet(date, [header, list(row)])

    if in_master:
        # Master Data column order, per submit_reservation_route().
        sheets.master.rows.append([reservation_id, name, date, time, '3-4',
                                   '大火锅', '61412345678', email, '', ''])

    token = app_module.make_manage_token(reservation_id, date, email)
    return token, date


def row_of(sheets, date, index=1):
    return sheets.date_sheets[date].rows[index]


STATUS_COL, TIME_COL, METHOD_COL = 8, 1, 11
DATE_COL = 5


# ---------------------------------------------------------------------------
# Token security — the token is the only credential
# ---------------------------------------------------------------------------

class TestTokenSecurity:

    def test_a_valid_token_round_trips(self, app_module):
        token = app_module.make_manage_token('7', days_from_now(2), 'a@b.com')
        payload, error = app_module.read_manage_token(token)
        assert error is None
        assert payload['r'] == '7'

    def test_a_tampered_token_is_refused(self, app_module):
        token = app_module.make_manage_token('7', days_from_now(2), 'a@b.com')
        # flip a character in the payload half
        broken = ('X' if token[0] != 'X' else 'Y') + token[1:]
        _, error = app_module.read_manage_token(broken)
        assert error == 'invalid'

    def test_a_token_signed_with_another_key_is_refused(self, app_module):
        from itsdangerous import URLSafeSerializer
        forged = URLSafeSerializer('not-the-real-secret',
                                   salt=app_module.MANAGE_TOKEN_SALT).dumps(
            {'r': '7', 'd': days_from_now(2), 'e': 'a@b.com'})
        _, error = app_module.read_manage_token(forged)
        assert error == 'invalid', "a token signed with the wrong key must not validate"

    def test_a_token_with_the_wrong_salt_is_refused(self, app_module):
        """Stops a token minted for some other purpose being replayed here."""
        from itsdangerous import URLSafeSerializer
        other = URLSafeSerializer(app_module.app.secret_key, salt='some-other-feature')
        _, error = app_module.read_manage_token(
            other.dumps({'r': '7', 'd': days_from_now(2), 'e': 'a@b.com'}))
        assert error == 'invalid'

    @pytest.mark.parametrize('junk', ['', 'abc', 'a.b.c', '....', 'null', '{}'])
    def test_rubbish_is_refused_without_raising(self, app_module, junk):
        _, error = app_module.read_manage_token(junk)
        assert error == 'invalid'

    def test_a_past_booking_link_is_expired(self, app_module):
        token = app_module.make_manage_token('7', days_from_now(-1), 'a@b.com')
        _, error = app_module.read_manage_token(token)
        assert error == 'expired'

    def test_todays_link_still_works(self, app_module):
        token = app_module.make_manage_token('7', days_from_now(0), 'a@b.com')
        _, error = app_module.read_manage_token(token)
        assert error is None, "a link must work on the day of the booking"

    def test_the_id_alone_is_not_enough(self, client, sheets, app_module):
        """Guessing a reservation id must not open someone else's booking."""
        make_booking(sheets, app_module, reservation_id='7')
        for guess in ['7', '1', '007']:
            assert client.get(f'/manage/{guess}').status_code == 400


# ---------------------------------------------------------------------------
# Viewing
# ---------------------------------------------------------------------------

class TestViewingABooking:

    def test_the_page_shows_the_booking(self, client, sheets, app_module):
        token, _ = make_booking(sheets, app_module, time='19:00', name='Jane Smith')
        html = client.get(f'/manage/{token}').get_data(as_text=True)
        assert 'Jane Smith' in html
        assert '19:00' in html

    def test_viewing_changes_nothing(self, client, sheets, app_module):
        """A mail scanner pre-fetching the link must not cancel anything."""
        token, date = make_booking(sheets, app_module)
        for _ in range(3):
            client.get(f'/manage/{token}')
        assert row_of(sheets, date)[STATUS_COL] == 'Pending'

    def test_an_unknown_booking_gives_404(self, client, sheets, app_module):
        token = app_module.make_manage_token('999', days_from_now(3), 'nobody@x.com')
        assert client.get(f'/manage/{token}').status_code == 404

    def test_a_wrong_email_in_the_token_does_not_match(self, client, sheets, app_module):
        """Id plus email must both line up."""
        make_booking(sheets, app_module, reservation_id='7', email='jane@gmail.com')
        token = app_module.make_manage_token('7', days_from_now(3), 'attacker@evil.com')
        assert client.get(f'/manage/{token}').status_code == 404

    def test_an_already_cancelled_booking_says_so(self, client, sheets, app_module):
        token, _ = make_booking(sheets, app_module, status='Cancelled')
        html = client.get(f'/manage/{token}').get_data(as_text=True)
        assert 'cancelled' in html.lower()
        assert 'Cancel this booking' not in html

    def test_an_expired_link_returns_410(self, client, sheets, app_module):
        token = app_module.make_manage_token('7', days_from_now(-2), 'a@b.com')
        assert client.get(f'/manage/{token}').status_code == 410


# ---------------------------------------------------------------------------
# Cancelling
# ---------------------------------------------------------------------------

class TestCancelling:

    def test_cancel_marks_the_sheet(self, client, sheets, app_module):
        token, date = make_booking(sheets, app_module)
        response = client.post(f'/manage/{token}/cancel')
        assert response.status_code == 200
        assert row_of(sheets, date)[STATUS_COL] == 'Cancelled'

    def test_cancel_records_who_did_it(self, client, sheets, app_module):
        token, date = make_booking(sheets, app_module)
        client.post(f'/manage/{token}/cancel')
        assert 'customer' in row_of(sheets, date)[METHOD_COL].lower()

    def test_cancel_is_not_reachable_by_GET(self, client, sheets, app_module):
        """Mail scanners follow links. A GET must never cancel."""
        token, date = make_booking(sheets, app_module)
        response = client.get(f'/manage/{token}/cancel')
        assert response.status_code == 405
        assert row_of(sheets, date)[STATUS_COL] == 'Pending'

    def test_cancelling_twice_is_harmless(self, client, sheets, app_module):
        token, date = make_booking(sheets, app_module)
        client.post(f'/manage/{token}/cancel')
        response = client.post(f'/manage/{token}/cancel')
        assert response.status_code == 200
        assert row_of(sheets, date)[STATUS_COL] == 'Cancelled'

    def test_cancel_needs_a_valid_token(self, client, sheets, app_module):
        make_booking(sheets, app_module)
        assert client.post('/manage/forged-token/cancel').status_code == 400

    def test_a_cancelled_booking_gets_no_reminder_sms(self, client, sheets, app_module,
                                                      monkeypatch):
        """The whole point of writing to column I."""
        today = days_from_now(0)
        token, _ = make_booking(sheets, app_module, date=today)
        sent = []
        monkeypatch.setattr(app_module, 'send_sms',
                            lambda to, msg, custom_ref=None: sent.append(to) or {'ok': 1})

        client.post(f'/manage/{token}/cancel')
        app_module.send_sms_on_date(today)
        assert sent == [], "a cancelled guest must not be texted"


# ---------------------------------------------------------------------------
# Rescheduling
# ---------------------------------------------------------------------------

class TestRescheduling:

    def test_moving_to_another_slot_works(self, client, sheets, app_module):
        token, date = make_booking(sheets, app_module, date=days_from_now(3), time='19:00')
        response = client.post(f'/manage/{token}/reschedule', data={'time': '20:00'})
        assert response.status_code == 200
        assert row_of(sheets, date)[TIME_COL] == '20:00'

    def test_the_change_is_recorded(self, client, sheets, app_module):
        token, date = make_booking(sheets, app_module, date=days_from_now(3), time='19:00')
        client.post(f'/manage/{token}/reschedule', data={'time': '20:00'})
        note = row_of(sheets, date)[METHOD_COL]
        assert '19:00' in note and '20:00' in note

    def test_an_invalid_time_is_refused(self, client, sheets, app_module):
        token, date = make_booking(sheets, app_module, date=days_from_now(3), time='19:00')
        response = client.post(f'/manage/{token}/reschedule', data={'time': '03:00'})
        assert response.status_code == 400
        assert row_of(sheets, date)[TIME_COL] == '19:00'

    def test_a_missing_time_is_refused(self, client, sheets, app_module):
        token, date = make_booking(sheets, app_module, date=days_from_now(3))
        assert client.post(f'/manage/{token}/reschedule', data={}).status_code == 400

    def test_you_cannot_reschedule_a_cancelled_booking(self, client, sheets, app_module):
        token, date = make_booking(sheets, app_module, date=days_from_now(3),
                                   time='19:00', status='Cancelled')
        client.post(f'/manage/{token}/reschedule', data={'time': '20:00'})
        assert row_of(sheets, date)[TIME_COL] == '19:00'

    def test_reschedule_is_not_reachable_by_GET(self, client, sheets, app_module):
        token, date = make_booking(sheets, app_module, date=days_from_now(3))
        assert client.get(f'/manage/{token}/reschedule').status_code == 405

    def test_a_future_booking_offers_every_slot(self, app_module):
        assert app_module.available_times_for(days_from_now(5)) == app_module.ORDERED_TIMES

    def test_a_past_booking_offers_nothing(self, app_module):
        assert app_module.available_times_for(days_from_now(-1)) == []


# ---------------------------------------------------------------------------
# Changing the date
# ---------------------------------------------------------------------------

class TestBothLanguages:
    """The sheet stores Chinese; an English reader must not be shown it raw."""

    @pytest.mark.parametrize('stored,english', [
        ('大火锅', 'Shared Hotpot'),
        ('小火锅', 'Individual Hotpot'),
        ('炒菜', 'Stir-fry'),
    ])
    def test_dish_types_translate(self, app_module, stored, english):
        assert app_module.dish_in_english(stored) == english

    def test_an_unknown_dish_type_passes_through(self, app_module):
        """A value typed straight into the sheet must not vanish."""
        assert app_module.dish_in_english('麻辣香锅') == '麻辣香锅'

    def test_the_gloss_keeps_the_chinese(self, app_module):
        """Email and confirmation page have no switch, so they carry both."""
        assert app_module.dish_bilingual('大火锅') == '大火锅 · Shared Hotpot'

    def test_the_gloss_does_not_repeat_an_untranslated_dish(self, app_module):
        assert app_module.dish_bilingual('麻辣香锅') == '麻辣香锅'

    def test_the_gloss_survives_a_missing_dish(self, app_module):
        assert app_module.dish_bilingual('') == ''
        assert app_module.dish_bilingual(None) == ''

    def test_the_email_shows_the_dish_in_both(self, app_module):
        _, html, text = app_module.build_confirmation_email('Jane Smith', {
            'date': days_from_now(3), 'time': '19:00', 'people': '3-4',
            'dish_type': '大火锅', 'phone': '61412345678',
            'email': 'jane@gmail.com', 'reservation_id': 7})
        for body in (html, text):
            assert '大火锅' in body and 'Shared Hotpot' in body

    def test_dates_render_in_chinese(self, app_module):
        assert app_module.describe_date_zh('2026-08-13') == '2026年8月13日 星期四'

    def test_a_malformed_date_survives_both_formatters(self, app_module):
        assert app_module.describe_date_zh('nonsense') == 'nonsense'
        assert app_module.describe_date('nonsense') == 'nonsense'

    def test_the_page_carries_both_halves_of_every_value(self, client, sheets, app_module):
        token, date = make_booking(sheets, app_module)
        html = client.get(f'/manage/{token}').get_data(as_text=True)

        assert 'Shared Hotpot' in html and '大火锅' in html
        assert app_module.describe_date(date) in html
        assert app_module.describe_date_zh(date) in html

    def test_the_date_picker_offers_labels_in_both(self, client, sheets, app_module):
        token, _ = make_booking(sheets, app_module)
        html = client.get(f'/manage/{token}').get_data(as_text=True)
        tomorrow = days_from_now(1)
        assert f'data-en="{app_module.describe_date(tomorrow)}"' in html
        assert f'data-zh="{app_module.describe_date_zh(tomorrow)}"' in html


class TestTheChangeEmail:
    """A guest who moves a booking gets it in writing, not just on screen."""

    def _email(self, app_module, new=None, old=None):
        booking = {'date': days_from_now(6), 'time': '20:00', 'people': '3-4',
                   'dish_type': '大火锅', 'phone': '61412345678',
                   'email': 'jane@gmail.com', 'reservation_id': 7}
        booking.update(new or {})
        previous = {'date': days_from_now(3), 'time': '19:00'}
        previous.update(old or {})
        return app_module.build_change_email('Jane Smith', booking, previous)

    def test_it_says_what_it_is(self, app_module):
        subject, html, _ = self._email(app_module)
        assert 'updated' in subject.lower()
        assert 'has been updated' in html

    def test_the_old_values_are_struck_through(self, app_module):
        _, html, _ = self._email(app_module)
        struck = [line for line in html.splitlines() if 'line-through' in line]
        assert struck, "the replaced values need to be visibly replaced"

    def test_only_what_changed_is_struck_through(self, app_module):
        """A time-only change must not strike out the date as well."""
        same = days_from_now(3)
        _, html, _ = self._email(app_module, new={'date': same}, old={'date': same})
        before, _, after = html.partition('line-through')
        assert 'line-through' in html, "the time did change"
        assert app_module.describe_date(same) not in before.split('<tr>')[-1]

    def test_unchanged_details_carry_over(self, app_module):
        _, html, _ = self._email(app_module)
        assert 'Jane Smith' in html
        assert '大火锅' in html and 'Shared Hotpot' in html

    def test_the_plain_text_half_shows_the_change(self, app_module):
        _, _, text = self._email(app_module)
        assert '19:00' in text and '20:00' in text
        assert 'was' in text.lower()

    def test_it_carries_a_working_manage_link(self, client, sheets, app_module):
        new_date = days_from_now(6)
        make_booking(sheets, app_module, date=new_date, time='20:00')
        _, html, _ = self._email(app_module, new={'date': new_date})

        token = re.search(r'/manage/([A-Za-z0-9_\-\.]+)"', html).group(1)
        assert client.get(f'/manage/{token}').status_code == 200

    def test_it_looks_like_the_confirmation(self, app_module):
        """Both are built from the same card, so the chrome must match."""
        _, changed, _ = self._email(app_module)
        _, confirmation, _ = app_module.build_confirmation_email('Jane Smith', {
            'date': days_from_now(6), 'time': '20:00', 'people': '3-4',
            'dish_type': '大火锅', 'phone': '61412345678',
            'email': 'jane@gmail.com', 'reservation_id': 7})

        for shared in ('九龙鼎', 'Chongqing Hotpot', 'Finding us',
                       '71 Dixon Street', 'We hold tables for'):
            assert shared in changed and shared in confirmation

    def test_a_change_actually_sends_one(self, client, sheets, app_module, monkeypatch):
        sent = []
        monkeypatch.setattr(app_module, 'send_email_async',
                            lambda *a, **k: sent.append(a))
        # conftest stubs out Thread; call the target inline instead.
        monkeypatch.setattr(app_module.threading, 'Thread',
                            lambda target, args=(), **k: types.SimpleNamespace(
                                start=lambda: target(*args)))

        token, _ = make_booking(sheets, app_module, date=days_from_now(3), time='19:00')
        client.post(f'/manage/{token}/reschedule',
                    data={'date': days_from_now(6), 'time': '20:00'},
                    follow_redirects=True)

        assert len(sent) == 1, "exactly one email per change"
        email, name, new, previous = sent[0]
        assert email == 'jane@gmail.com'
        assert new['time'] == '20:00' and previous['time'] == '19:00'

    def test_no_email_when_the_change_is_refused(self, client, sheets, app_module,
                                                 monkeypatch):
        sent = []
        monkeypatch.setattr(app_module, 'send_email_async',
                            lambda *a, **k: sent.append(a))
        monkeypatch.setattr(app_module.threading, 'Thread',
                            lambda target, args=(), **k: types.SimpleNamespace(
                                start=lambda: target(*args)))

        token, _ = make_booking(sheets, app_module, date=days_from_now(3))
        client.post(f'/manage/{token}/reschedule',
                    data={'date': days_from_now(0), 'time': '20:00'})
        assert sent == [], "a rejected change must not be confirmed by email"


class TestTheCancellationEmail:

    def _email(self, app_module, **over):
        booking = {'date': days_from_now(3), 'time': '19:00', 'people': '3-4',
                   'dish_type': '大火锅', 'phone': '61412345678',
                   'email': 'jane@gmail.com', 'reservation_id': 7}
        booking.update(over)
        return app_module.build_cancellation_email('Jane Smith', booking)

    def test_it_says_what_happened(self, app_module):
        subject, html, text = self._email(app_module)
        assert 'cancelled' in subject.lower()
        assert 'has been cancelled' in html
        assert 'cancelled' in text.lower()

    def test_it_names_the_booking_that_went(self, app_module):
        date = days_from_now(3)
        _, html, _ = self._email(app_module, date=date, time='19:00')
        assert app_module.describe_date(date) in html
        assert '19:00' in html

    def test_it_offers_a_way_back(self, app_module):
        _, html, text = self._email(app_module)
        assert '/book' in html and '/book' in text

    def test_it_does_not_offer_a_dead_manage_link(self, app_module):
        """The booking is gone, so a manage link would lead nowhere useful."""
        _, html, _ = self._email(app_module)
        assert '/manage/' not in html

    def test_nothing_is_struck_through(self, app_module):
        """Strike-through means 'replaced by' in the change email."""
        _, html, _ = self._email(app_module)
        assert 'line-through' not in html

    def test_it_looks_like_the_others(self, app_module):
        _, html, _ = self._email(app_module)
        for shared in ('九龙鼎', 'Chongqing Hotpot', 'Finding us', '71 Dixon Street'):
            assert shared in html

    def test_cancelling_sends_it(self, client, sheets, app_module, monkeypatch):
        sent = []
        monkeypatch.setattr(app_module, 'send_email_async',
                            lambda *a, **k: sent.append(a))
        monkeypatch.setattr(app_module.threading, 'Thread',
                            lambda target, args=(), **k: types.SimpleNamespace(
                                start=lambda: target(*args)))

        token, _ = make_booking(sheets, app_module)
        client.post(f'/manage/{token}/cancel')

        assert len(sent) == 1
        email, name, data, previous, kind = sent[0]
        assert email == 'jane@gmail.com'
        assert kind == 'cancelled'
        assert previous is None

    def test_cancelling_twice_only_emails_once(self, client, sheets, app_module,
                                               monkeypatch):
        sent = []
        monkeypatch.setattr(app_module, 'send_email_async',
                            lambda *a, **k: sent.append(a))
        monkeypatch.setattr(app_module.threading, 'Thread',
                            lambda target, args=(), **k: types.SimpleNamespace(
                                start=lambda: target(*args)))

        token, _ = make_booking(sheets, app_module)
        client.post(f'/manage/{token}/cancel')
        client.post(f'/manage/{token}/cancel')
        assert len(sent) == 1, "the second cancel is a no-op and must stay silent"


class TestTheLinkOnTheSuccessPage:
    """The confirmation page offers the same self-service link as the email."""

    def _book(self, client, sheets, **over):
        from conftest import submit
        submit(client, **over)
        return client.get('/reservation_success')

    def test_the_button_is_there(self, client, sheets, app_module):
        html = self._book(client, sheets).get_data(as_text=True)
        assert 'Modify booking' in html
        assert '/manage/' in html

    def test_the_link_opens_that_booking(self, client, sheets, app_module):
        date = days_from_now(3)
        html = self._book(client, sheets, date=date).get_data(as_text=True)
        token = re.search(r'/manage/([A-Za-z0-9_\-\.]+)"', html).group(1)

        payload, error = app_module.read_manage_token(token)
        assert error is None
        assert payload['e'] == 'jane.smith@gmail.com'
        assert payload['d'] == date

    def test_the_page_is_not_indexable(self, client, sheets):
        """It carries a credential and a customer's contact details."""
        html = self._book(client, sheets).get_data(as_text=True)
        assert 'noindex' in html
        assert 'no-referrer' in html

    def test_no_link_without_a_session(self, client, sheets):
        """Someone opening the URL cold gets nothing, not a stranger's link."""
        response = client.get('/reservation_success')
        assert response.status_code == 302
        assert '/manage/' not in response.get_data(as_text=True)


class TestWhichDatesAreOffered:

    def test_a_future_booking_is_not_offered_today(self, app_module):
        offered = app_module.reschedule_dates(days_from_now(3))
        assert days_from_now(0) not in offered, "moving into today must go through the phone"
        assert days_from_now(1) in offered

    def test_a_future_booking_can_move_further_out(self, app_module):
        offered = app_module.reschedule_dates(days_from_now(3))
        assert days_from_now(10) in offered

    def test_a_booking_today_keeps_today(self, app_module):
        offered = app_module.reschedule_dates(days_from_now(0))
        if app_module.available_times_for(days_from_now(0)):
            assert offered[0] == days_from_now(0)
        else:
            pytest.skip("no slot left today at this time of day")

    def test_a_booking_today_can_move_to_a_later_day(self, app_module):
        """Moving out of today frees a table, so it is always allowed."""
        assert days_from_now(4) in app_module.reschedule_dates(days_from_now(0))

    def test_nothing_beyond_thirty_days(self, app_module):
        offered = app_module.reschedule_dates(days_from_now(3))
        assert days_from_now(app_module.MAX_RESCHEDULE_DAYS) in offered
        assert days_from_now(app_module.MAX_RESCHEDULE_DAYS + 1) not in offered

    def test_the_horizon_is_measured_from_today(self, app_module):
        """Not from the booking's own date, or a booking could walk itself
        forward 30 days at a time."""
        horizon = app_module.MAX_RESCHEDULE_DAYS
        for booking_in in (1, 10, horizon):
            offered = app_module.reschedule_dates(days_from_now(booking_in))
            assert offered[-1] == days_from_now(horizon), \
                f"a booking {booking_in} days out must still stop at today+{horizon}"

    def test_a_past_booking_offers_no_dates(self, app_module):
        assert app_module.reschedule_dates(days_from_now(-1)) == []


class TestChangingTheDate:

    def test_moving_to_another_day_writes_the_new_tab(self, client, sheets, app_module):
        token, old = make_booking(sheets, app_module, date=days_from_now(3))
        new = days_from_now(6)

        client.post(f'/manage/{token}/reschedule',
                    data={'date': new, 'time': '20:00'}, follow_redirects=True)

        moved = row_of(sheets, new)
        assert moved[DATE_COL] == new
        assert moved[TIME_COL] == '20:00'
        assert moved[STATUS_COL] == 'Pending', "the moved booking is still live"

    def test_the_old_date_keeps_a_modified_row(self, client, sheets, app_module):
        """Staff opening the old day should see the table moved, not a gap."""
        token, old = make_booking(sheets, app_module, date=days_from_now(3))
        new = days_from_now(6)

        client.post(f'/manage/{token}/reschedule',
                    data={'date': new, 'time': '20:00'}, follow_redirects=True)

        assert len(sheets.date_sheets[old].rows) == 2, "the old row must not be deleted"
        left_behind = row_of(sheets, old)
        assert left_behind[STATUS_COL] == 'Modified'
        assert new in left_behind[METHOD_COL], "the note should say where it went"

    def test_a_modified_row_is_not_the_live_booking(self, client, sheets, app_module):
        """The tombstone shares its id, so lookups must not settle on it."""
        token, old = make_booking(sheets, app_module, date=days_from_now(3),
                                  in_master=True)
        new = days_from_now(6)
        client.post(f'/manage/{token}/reschedule',
                    data={'date': new, 'time': '20:00'}, follow_redirects=True)

        html = client.get(f'/manage/{token}').get_data(as_text=True)
        assert '20:00' in html
        assert 'Booking Cancelled' not in html
        assert app_module.describe_date(new) in html, "must show the new date, not the old"

    def test_a_modified_booking_gets_no_reminder_sms(self, client, sheets, app_module,
                                                     monkeypatch):
        """The row stays on the old date, so it must not be texted that morning."""
        today = days_from_now(0)
        token, _ = make_booking(sheets, app_module, date=today, time='20:30')
        sent = []
        monkeypatch.setattr(app_module, 'send_sms',
                            lambda to, msg, custom_ref=None: sent.append(to) or {'ok': 1})

        client.post(f'/manage/{token}/reschedule',
                    data={'date': days_from_now(4), 'time': '19:00'},
                    follow_redirects=True)
        app_module.send_sms_on_date(today)
        assert sent == [], "a guest who moved to another day must not be texted"

    def test_the_moved_row_keeps_its_details(self, client, sheets, app_module):
        token, _ = make_booking(sheets, app_module, date=days_from_now(3),
                                name='Jane Smith')
        new = days_from_now(6)
        client.post(f'/manage/{token}/reschedule',
                    data={'date': new, 'time': '20:00'}, follow_redirects=True)

        moved = row_of(sheets, new)
        assert moved[0] == 'Jane Smith'
        assert moved[9] == '7', "the reservation id must survive the move"
        assert moved[4] == 'jane@gmail.com'

    def test_a_day_with_no_tab_yet_gets_one(self, client, sheets, app_module):
        token, _ = make_booking(sheets, app_module, date=days_from_now(3))
        new = days_from_now(9)
        assert new not in sheets.date_sheets

        client.post(f'/manage/{token}/reschedule',
                    data={'date': new, 'time': '18:00'}, follow_redirects=True)
        assert sheets.date_sheets[new].rows[0][0] == 'Name', "a new tab needs its header"

    def test_the_move_is_recorded(self, client, sheets, app_module):
        token, _ = make_booking(sheets, app_module, date=days_from_now(3))
        new = days_from_now(6)
        client.post(f'/manage/{token}/reschedule',
                    data={'date': new, 'time': '20:00'}, follow_redirects=True)
        assert 'customer' in row_of(sheets, new)[METHOD_COL].lower()

    def test_master_data_follows_the_move(self, client, sheets, app_module):
        token, _ = make_booking(sheets, app_module, date=days_from_now(3),
                                in_master=True)
        new = days_from_now(6)
        client.post(f'/manage/{token}/reschedule',
                    data={'date': new, 'time': '20:00'}, follow_redirects=True)

        master_row = sheets.master.rows[1]
        assert master_row[2] == new, "Master Data must not keep the old date"
        assert master_row[3] == '20:00'

    def test_the_emailed_link_still_works_after_a_move(self, client, sheets, app_module):
        """The token carries the old date; the booking is no longer in that tab."""
        token, _ = make_booking(sheets, app_module, date=days_from_now(3),
                                in_master=True)
        new = days_from_now(6)
        client.post(f'/manage/{token}/reschedule',
                    data={'date': new, 'time': '20:00'}, follow_redirects=True)

        response = client.get(f'/manage/{token}')
        assert response.status_code == 200, "the original emailed link must not break"
        assert '20:00' in response.get_data(as_text=True)

    def test_a_move_redirects_to_a_working_link(self, client, sheets, app_module):
        token, _ = make_booking(sheets, app_module, date=days_from_now(3))
        new = days_from_now(6)
        response = client.post(f'/manage/{token}/reschedule',
                               data={'date': new, 'time': '20:00'})

        assert response.status_code == 302
        assert client.get(response.headers['Location']).status_code == 200

    def test_you_cannot_pull_a_booking_into_today(self, client, sheets, app_module):
        token, date = make_booking(sheets, app_module, date=days_from_now(3))
        response = client.post(f'/manage/{token}/reschedule',
                               data={'date': days_from_now(0), 'time': '20:00'})

        assert response.status_code == 400
        assert 'call us' in response.get_data(as_text=True).lower()
        assert row_of(sheets, date)[DATE_COL] == date, "the booking must not move"

    def test_you_cannot_move_into_the_past(self, client, sheets, app_module):
        token, date = make_booking(sheets, app_module, date=days_from_now(3))
        response = client.post(f'/manage/{token}/reschedule',
                               data={'date': days_from_now(-1), 'time': '20:00'})
        assert response.status_code == 400
        assert row_of(sheets, date)[DATE_COL] == date

    def test_you_cannot_move_beyond_thirty_days(self, client, sheets, app_module):
        token, date = make_booking(sheets, app_module, date=days_from_now(3))
        too_far = days_from_now(app_module.MAX_RESCHEDULE_DAYS + 5)
        response = client.post(f'/manage/{token}/reschedule',
                               data={'date': too_far, 'time': '20:00'})
        assert response.status_code == 400
        assert row_of(sheets, date)[DATE_COL] == date

    def test_a_malformed_date_is_refused(self, client, sheets, app_module):
        token, date = make_booking(sheets, app_module, date=days_from_now(3))
        response = client.post(f'/manage/{token}/reschedule',
                               data={'date': 'next tuesday', 'time': '20:00'})
        assert response.status_code == 400
        assert row_of(sheets, date)[DATE_COL] == date

    def test_a_form_without_a_date_still_changes_the_time(self, client, sheets, app_module):
        """An older cached copy of the page posts no date field."""
        token, date = make_booking(sheets, app_module, date=days_from_now(3),
                                   time='19:00')
        client.post(f'/manage/{token}/reschedule', data={'time': '20:00'})
        assert row_of(sheets, date)[TIME_COL] == '20:00'

    def test_moving_a_booking_out_of_today_is_allowed(self, client, sheets, app_module):
        token, today = make_booking(sheets, app_module, date=days_from_now(0),
                                    time='20:30')
        new = days_from_now(5)
        client.post(f'/manage/{token}/reschedule',
                    data={'date': new, 'time': '12:00'}, follow_redirects=True)
        assert row_of(sheets, new)[DATE_COL] == new


class TestTheTwoHourRule:
    """Same-day changes need at least MIN_RESCHEDULE_MINUTES notice."""

    def _slot_offset_minutes(self, app_module, slot):
        now = datetime.now(app_module.sydney_tz)
        hour, minute = (int(p) for p in slot.split(':'))
        return (hour * 60 + minute) - (now.hour * 60 + now.minute)

    def test_only_slots_an_hour_out_are_offered(self, app_module):
        offered = app_module.available_times_for(days_from_now(0))
        for slot in app_module.ORDERED_TIMES:
            gap = self._slot_offset_minutes(app_module, slot)
            if gap >= app_module.MIN_RESCHEDULE_MINUTES:
                assert slot in offered, f"{slot} is {gap}min away and should be offered"
            else:
                assert slot not in offered, f"{slot} is only {gap}min away"

    def test_a_too_soon_slot_is_rejected_by_the_route(self, client, sheets, app_module):
        today = days_from_now(0)
        soon = [s for s in app_module.ORDERED_TIMES
                if 0 <= self._slot_offset_minutes(app_module, s)
                < app_module.MIN_RESCHEDULE_MINUTES]
        if not soon:
            pytest.skip("no slot inside the 1-hour window at this time of day")

        token, date = make_booking(sheets, app_module, date=today, time='20:30')
        response = client.post(f'/manage/{token}/reschedule', data={'time': soon[0]})
        assert response.status_code == 400
        assert row_of(sheets, date)[TIME_COL] == '20:30'

    def test_a_far_enough_slot_is_accepted_by_the_route(self, client, sheets, app_module):
        today = days_from_now(0)
        ok = [s for s in app_module.ORDERED_TIMES
              if self._slot_offset_minutes(app_module, s)
              >= app_module.MIN_RESCHEDULE_MINUTES]
        if len(ok) < 2:
            pytest.skip("not enough remaining slots today")

        token, date = make_booking(sheets, app_module, date=today, time=ok[0])
        client.post(f'/manage/{token}/reschedule', data={'time': ok[1]})
        assert row_of(sheets, date)[TIME_COL] == ok[1]


# ---------------------------------------------------------------------------
# The link in the confirmation email
# ---------------------------------------------------------------------------

class TestTheEmailLink:

    def _email(self, app_module, **over):
        d = {'date': days_from_now(3), 'time': '19:00', 'people': '3-4',
             'dish_type': '大火锅', 'phone': '61412345678',
             'email': 'jane@gmail.com', 'reservation_id': 7}
        d.update(over)
        return app_module.build_confirmation_email('Jane Smith', d)

    def test_the_email_contains_a_manage_link(self, app_module):
        _, html, text = self._email(app_module)
        assert '/manage/' in html
        assert '/manage/' in text, "the plain-text version needs the link too"

    def test_the_link_opens_the_right_booking(self, client, sheets, app_module):
        import re
        date = days_from_now(3)
        make_booking(sheets, app_module, date=date, reservation_id='7',
                     email='jane@gmail.com')
        _, html, _ = self._email(app_module, date=date)
        token = re.search(r'/manage/([A-Za-z0-9_\-\.]+)"', html).group(1)
        assert client.get(f'/manage/{token}').status_code == 200

    def test_no_link_when_there_is_no_reservation_id(self, app_module):
        _, html, text = self._email(app_module, reservation_id=None)
        assert '/manage/' not in html
        assert 'call us' in text.lower()

    def test_two_bookings_get_different_links(self, app_module):
        _, a, _ = self._email(app_module, reservation_id=7)
        _, b, _ = self._email(app_module, reservation_id=8)
        assert a != b
