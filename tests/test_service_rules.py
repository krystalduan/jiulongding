"""
When the restaurant is open, and when the day-of texts go out.

Covers five rules that were previously wrong or missing:
  * Tuesday and Wednesday are dinner-only — no lunch sitting to book
  * a customer moving a confirmed booking drops it back to Pending
  * the reminder history does not follow a booking to a new date
  * the manage link does not read all of Master Data to find a booking
    that is exactly where its token says it is
  * the day-of texts go out at 08:30 Sydney, not whenever a UTC cron fires
"""
from datetime import datetime, timedelta

import pytest

from conftest import (FakeWorksheet, days_from_now, dinner_only_day_from_now,
                      lunch_day_from_now, get_form_token, valid_booking)
from test_manage_booking import make_booking, row_of, STATUS_COL, DATE_COL

SMS_COL = 10          # column K — 'SMS Reply'
LUNCH = '12:00'
DINNER = '19:00'


# ---------------------------------------------------------------------------
# Tuesday and Wednesday: dinner only
# ---------------------------------------------------------------------------

class TestDinnerOnlyDays:

    def test_the_two_dinner_only_days_are_tuesday_and_wednesday(self, app_module):
        assert app_module.DINNER_ONLY_WEEKDAYS == {1, 2}
        for date_str in (dinner_only_day_from_now(1), dinner_only_day_from_now(8)):
            day = datetime.strptime(date_str, '%Y-%m-%d').date()
            assert day.strftime('%A') in ('Tuesday', 'Wednesday')

    def test_lunch_is_not_offered_on_a_dinner_only_day(self, app_module):
        times = app_module.available_times_for(dinner_only_day_from_now(1))
        assert LUNCH not in times
        assert not any(t in app_module.LUNCH_TIMES for t in times)

    def test_dinner_is_still_offered_on_a_dinner_only_day(self, app_module):
        times = app_module.available_times_for(dinner_only_day_from_now(1))
        assert DINNER in times
        assert times == [t for t in app_module.ORDERED_TIMES
                         if t not in app_module.LUNCH_TIMES]

    def test_lunch_is_offered_on_an_ordinary_day(self, app_module):
        assert LUNCH in app_module.available_times_for(lunch_day_from_now(2))


class TestBookingFormRejectsDinnerOnlyLunch:

    def submit(self, client, date, time):
        form = valid_booking(date=date, time=time)
        form['form_token'] = get_form_token(client)
        form['form_source'] = 'index'
        return client.post('/submit_reservation', data=form)

    def test_a_lunch_booking_on_a_dinner_only_day_is_refused(self, client, sheets):
        response = self.submit(client, dinner_only_day_from_now(1), LUNCH)
        assert response.status_code == 400
        assert len(sheets.master.rows) == 1, "the booking must not be written"

    def test_it_says_why(self, client, sheets):
        html = self.submit(client, dinner_only_day_from_now(1), LUNCH).get_data(as_text=True)
        assert 'dinner only' in html.lower()
        assert '5:00 PM' in html

    def test_the_customer_keeps_their_answers(self, client, sheets):
        html = self.submit(client, dinner_only_day_from_now(1), LUNCH).get_data(as_text=True)
        assert 'value="jane.smith@gmail.com"' in html

    def test_dinner_on_the_same_day_is_accepted(self, client, sheets):
        response = self.submit(client, dinner_only_day_from_now(1), DINNER)
        assert response.status_code == 302
        assert len(sheets.master.rows) == 2

    def test_lunch_on_an_ordinary_day_is_still_accepted(self, client, sheets):
        response = self.submit(client, lunch_day_from_now(2), LUNCH)
        assert response.status_code == 302

    @pytest.mark.parametrize('time', ['12:00', '12:30', '13:00', '13:30'])
    def test_every_lunch_slot_is_refused(self, client, sheets, time):
        assert self.submit(client, dinner_only_day_from_now(1), time).status_code == 400
        assert len(sheets.master.rows) == 1

    def test_the_validator_refuses_it_directly(self, app_module):
        _, error = app_module.validate_reservation(
            valid_booking(date=dinner_only_day_from_now(1), time=LUNCH))
        assert error is not None and 'dinner only' in error.lower()


class TestReschedulingOntoADinnerOnlyDay:

    def test_moving_to_lunch_on_a_dinner_only_day_is_refused(self, client, sheets, app_module):
        token, _ = make_booking(sheets, app_module, date=lunch_day_from_now(2))
        target = dinner_only_day_from_now(3)
        response = client.post(f'/manage/{token}/reschedule',
                               data={'date': target, 'time': LUNCH})
        assert response.status_code == 400
        assert target not in sheets.date_sheets, "the booking must not have moved"

    def test_the_reason_given_is_the_closed_day_not_the_notice_rule(
            self, client, sheets, app_module):
        token, _ = make_booking(sheets, app_module, date=lunch_day_from_now(2))
        html = client.post(f'/manage/{token}/reschedule',
                           data={'date': dinner_only_day_from_now(3),
                                 'time': LUNCH}).get_data(as_text=True)
        assert 'dinner only' in html.lower()
        assert "need at least 2 hours' notice" not in html, \
            "a closed day was explained as insufficient notice"

    def test_moving_to_dinner_on_a_dinner_only_day_works(self, client, sheets, app_module):
        token, _ = make_booking(sheets, app_module, date=lunch_day_from_now(2))
        target = dinner_only_day_from_now(3)
        client.post(f'/manage/{token}/reschedule',
                    data={'date': target, 'time': DINNER}, follow_redirects=True)
        assert row_of(sheets, target)[DATE_COL] == target


# ---------------------------------------------------------------------------
# A moved booking is no longer a confirmed booking
# ---------------------------------------------------------------------------

class TestMovingDropsConfirmedBackToPending:

    def test_moving_to_another_day_resets_the_status(self, client, sheets, app_module):
        token, old = make_booking(sheets, app_module, date=lunch_day_from_now(2),
                                  status='Confirmed')
        new = lunch_day_from_now(6)
        client.post(f'/manage/{token}/reschedule',
                    data={'date': new, 'time': DINNER}, follow_redirects=True)
        assert row_of(sheets, new)[STATUS_COL] == 'Pending', \
            "a booking the customer moved kept a tick it was never given for that date"

    def test_changing_only_the_time_resets_the_status(self, client, sheets, app_module):
        token, date = make_booking(sheets, app_module, date=lunch_day_from_now(2),
                                   time='19:00', status='Confirmed')
        client.post(f'/manage/{token}/reschedule', data={'time': '20:00'})
        assert row_of(sheets, date)[STATUS_COL] == 'Pending'

    def test_a_pending_booking_stays_pending(self, client, sheets, app_module):
        token, _ = make_booking(sheets, app_module, date=lunch_day_from_now(2),
                                status='Pending')
        new = lunch_day_from_now(6)
        client.post(f'/manage/{token}/reschedule',
                    data={'date': new, 'time': DINNER}, follow_redirects=True)
        assert row_of(sheets, new)[STATUS_COL] == 'Pending'

    def test_the_reminder_would_now_reach_the_moved_booking(self, sheets, app_module):
        """send_sms_on_date only texts Pending rows — that is why this matters."""
        token, _ = make_booking(sheets, app_module, date=lunch_day_from_now(2),
                                status='Confirmed')
        new = lunch_day_from_now(6)
        app_module.move_booking_row(
            sheets, sheets.date_sheets[lunch_day_from_now(2)], 2,
            ['Jane', '19:00', '3-4', '61412345678', 'j@x.com', lunch_day_from_now(2),
             '大火锅', '', 'Pending', '7', '', ''],
            new, DINNER, 'moved')
        assert row_of(sheets, new)[STATUS_COL] == 'Pending'


# ---------------------------------------------------------------------------
# The reminder history belongs to a date, not to a booking
# ---------------------------------------------------------------------------

class TestMovingClearsTheSmsHistory:

    def test_column_k_does_not_follow_the_booking(self, sheets, app_module):
        old = lunch_day_from_now(2)
        new = lunch_day_from_now(6)
        row = ['Jane', '19:00', '3-4', '61412345678', 'j@x.com', old, '大火锅', '',
               'Pending', '7', 'day_of sent 01/08/26 08:30', 'Confirmed by SMS']
        sheets.date_sheets[old] = FakeWorksheet(old, [['h'] * 12, list(row)])

        app_module.move_booking_row(sheets, sheets.date_sheets[old], 2, row,
                                    new, DINNER, 'moved by customer')

        assert row_of(sheets, new)[SMS_COL] == '', \
            "the new date inherited the old date's reminder history"

    def test_the_old_row_keeps_its_own_history(self, sheets, app_module):
        old = lunch_day_from_now(2)
        row = ['Jane', '19:00', '3-4', '61412345678', 'j@x.com', old, '大火锅', '',
               'Pending', '7', 'day_of sent 01/08/26 08:30', '']
        sheets.date_sheets[old] = FakeWorksheet(old, [['h'] * 12, list(row)])
        app_module.move_booking_row(sheets, sheets.date_sheets[old], 2, row,
                                    lunch_day_from_now(6), DINNER, 'moved')
        assert sheets.date_sheets[old].rows[1][SMS_COL] == 'day_of sent 01/08/26 08:30'


# ---------------------------------------------------------------------------
# The manage link should not read the whole booking history
# ---------------------------------------------------------------------------

class TestManageLinkReads:

    @staticmethod
    def count_master_reads(sheets):
        sheets.master.reads = 0
        original = sheets.master.get_all_values

        def counted():
            sheets.master.reads += 1
            return original()

        sheets.master.get_all_values = counted
        return lambda: sheets.master.reads

    def test_a_booking_where_the_token_says_costs_no_master_read(
            self, client, sheets, app_module):
        token, _ = make_booking(sheets, app_module, date=lunch_day_from_now(2),
                                in_master=True)
        reads = self.count_master_reads(sheets)
        assert client.get(f'/manage/{token}').status_code == 200
        assert reads() == 0, \
            "Master Data grows forever; the common case must not read it"

    def test_a_moved_booking_still_falls_back_to_master(self, client, sheets, app_module):
        """The token names the old date, so master is the only way to find it."""
        old = lunch_day_from_now(2)
        new = lunch_day_from_now(6)
        token, _ = make_booking(sheets, app_module, date=old, in_master=True)

        # Move it: old row marked Modified, live row in the new tab.
        app_module.move_booking_row(sheets, sheets.date_sheets[old], 2,
                                    list(row_of(sheets, old)), new, DINNER, 'moved')
        sheets.master.rows[1][2] = new

        response = client.get(f'/manage/{token}')
        assert response.status_code == 200
        assert new in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# The day-of texts go out at 08:30 Sydney
# ---------------------------------------------------------------------------

class TestSmsSendWindow:

    @staticmethod
    def at(app_module, hour, minute):
        """sms_window_state as of a given Sydney wall-clock time."""
        now = app_module.datetime.now(app_module.sydney_tz).replace(
            hour=hour, minute=minute)
        return app_module.sms_window_state(now)

    def test_the_target_is_half_past_eight(self, app_module):
        assert (app_module.SMS_SEND_HOUR, app_module.SMS_SEND_MINUTE) == (8, 30)

    @pytest.mark.parametrize('hour,minute', [(7, 30), (6, 0), (8, 29), (0, 0)])
    def test_it_will_not_send_early(self, app_module, hour, minute):
        is_open, _ = self.at(app_module, hour, minute)
        assert not is_open, f"{hour:02d}:{minute:02d} Sydney is before the send time"

    @pytest.mark.parametrize('hour,minute', [(8, 30), (9, 0), (9, 30), (9, 59)])
    def test_it_sends_at_or_shortly_after_the_target(self, app_module, hour, minute):
        is_open, _ = self.at(app_module, hour, minute)
        assert is_open

    @pytest.mark.parametrize('hour,minute', [(10, 30), (14, 0), (23, 0)])
    def test_it_will_not_send_long_afterwards(self, app_module, hour, minute):
        is_open, _ = self.at(app_module, hour, minute)
        assert not is_open

    def test_the_aest_early_cron_is_a_no_op(self, app_module):
        """21:30 UTC is 07:30 in Sydney on AEST — this is the call that used to
        send the texts an hour early."""
        assert not self.at(app_module, 7, 30)[0]
        assert self.at(app_module, 8, 30)[0], "the 22:30 UTC call must still send"


class TestSmsCronEndpoint:

    def test_it_still_needs_the_secret(self, client):
        assert client.get('/api/send-sms-cron').status_code == 401
        assert client.get('/api/send-sms-cron?secret=wrong').status_code == 401

    def test_outside_the_window_it_skips_without_sending(
            self, client, sheets, app_module, monkeypatch):
        sent = []
        monkeypatch.setattr(app_module, 'send_sms_on_date',
                            lambda *a, **k: sent.append(a) or 'sent')
        monkeypatch.setattr(app_module, 'sms_window_state', lambda now=None: (False, -60))

        response = client.get('/api/send-sms-cron?secret=test-cron-secret')
        assert response.status_code == 200, "a skip must not fail the workflow"
        assert response.get_json()['status'] == 'skipped'
        assert sent == [], "nothing should have been texted"

    def test_inside_the_window_it_sends(self, client, sheets, app_module, monkeypatch):
        sent = []
        monkeypatch.setattr(app_module, 'send_sms_on_date',
                            lambda *a, **k: sent.append(a) or 'ok')
        monkeypatch.setattr(app_module, 'sms_window_state', lambda now=None: (True, 0))

        response = client.get('/api/send-sms-cron?secret=test-cron-secret')
        assert response.get_json()['status'] == 'ok'
        assert len(sent) == 1

    def test_force_overrides_the_window(self, client, sheets, app_module, monkeypatch):
        """The manual 'Run workflow' button is a deliberate send."""
        sent = []
        monkeypatch.setattr(app_module, 'send_sms_on_date',
                            lambda *a, **k: sent.append(a) or 'ok')
        monkeypatch.setattr(app_module, 'sms_window_state', lambda now=None: (False, -300))

        response = client.get('/api/send-sms-cron?secret=test-cron-secret&force=1')
        assert response.get_json()['status'] == 'ok'
        assert len(sent) == 1

    def test_it_sends_for_todays_date_in_sydney(self, client, sheets, app_module, monkeypatch):
        sent = []
        monkeypatch.setattr(app_module, 'send_sms_on_date',
                            lambda date, **k: sent.append(date) or 'ok')
        monkeypatch.setattr(app_module, 'sms_window_state', lambda now=None: (True, 0))

        client.get('/api/send-sms-cron?secret=test-cron-secret')
        assert sent == [days_from_now(0)]
