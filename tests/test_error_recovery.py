"""
What a customer sees when a booking is rejected.

The rules these lock in:
  * a rejected submit comes back with every answer still filled in
  * the reason is on the page, marked up so it can be found and announced
  * an expired or unrecognised token is recoverable, not a silent bounce home
  * several booking pages open at once do not cancel each other out
  * resubmitting a saved booking shows the confirmation instead of double-booking
"""
import re

import pytest

from conftest import days_from_now, get_form_token, valid_booking


def post(client, source='index', **overrides):
    """Submit with a freshly issued token and a known originating page."""
    form = valid_booking(**overrides)
    form['form_token'] = get_form_token(client, '/book' if source == 'book' else '/')
    form['form_source'] = source
    return client.post('/submit_reservation', data=form)


def rows(sheets):
    return len(sheets.master.rows) - 1


# ---------------------------------------------------------------------------
# Nothing the customer typed should be lost
# ---------------------------------------------------------------------------

class TestRejectedSubmitKeepsTheAnswers:

    @pytest.fixture
    def rejected(self, client, sheets):
        """One booking rejected for a bad phone number, with everything else valid."""
        response = post(client,
                        name="Jane O'Brien",
                        email='jane.smith@gmail.com',
                        phone='+1-800-555-0100',   # US number — will be rejected
                        people='7-10',
                        time='19:30',
                        notes='2 high chairs',
                        date=days_from_now(6))
        assert response.status_code == 400
        assert rows(sheets) == 0, "a rejected booking must not be written"
        return response.get_data(as_text=True)

    def test_the_name_comes_back(self, rejected):
        assert 'value="Jane O&#39;Brien"' in rejected

    def test_the_email_comes_back(self, rejected):
        assert 'value="jane.smith@gmail.com"' in rejected

    def test_the_bad_phone_comes_back_so_it_can_be_corrected(self, rejected):
        assert 'value="+1-800-555-0100"' in rejected

    def test_the_notes_come_back(self, rejected):
        assert 'value="2 high chairs"' in rejected

    def test_the_date_comes_back(self, rejected):
        assert f'value="{days_from_now(6)}"' in rejected

    def test_the_dropdowns_come_back_selected(self, rejected):
        assert '<option value="7-10" selected>' in rejected
        assert '<option value="19:30" selected>' in rejected
        assert '<option value="大火锅" selected>' in rejected

    def test_the_honeypot_is_never_echoed_back(self, client, sheets):
        html = post(client, phone='bad', website='http://spam.example.com').get_data(as_text=True)
        assert 'spam.example.com' not in html

    def test_the_reason_is_on_the_page_and_findable(self, rejected):
        assert 'id="booking-error"' in rejected
        assert 'role="alert"' in rejected
        assert 'mobile' in rejected.lower()

    def test_a_fresh_token_is_issued_so_the_retry_works(self, client, sheets):
        html = post(client, phone='bad').get_data(as_text=True)
        retry = re.search(r'name="form_token" value="([^"]+)"', html).group(1)
        form = valid_booking()
        form['form_token'] = retry
        form['form_source'] = 'index'
        assert client.post('/submit_reservation', data=form).status_code == 302
        assert rows(sheets) == 1, "the corrected retry should save"


# ---------------------------------------------------------------------------
# The customer is sent back to the page they were actually on
# ---------------------------------------------------------------------------

class TestErrorGoesBackToTheRightPage:

    def test_an_error_on_the_booking_page_re_renders_the_booking_page(self, client, sheets):
        html = post(client, source='book', phone='bad').get_data(as_text=True)
        assert 'Book a Table' in html

    def test_an_error_on_the_homepage_re_renders_the_homepage(self, client, sheets):
        html = post(client, source='index', phone='bad').get_data(as_text=True)
        assert 'About Us' in html


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

class TestSeveralPagesOpenAtOnce:

    def test_opening_a_second_page_does_not_break_the_first(self, client, sheets):
        """Used to fail: the newest page load overwrote the only stored token."""
        first = get_form_token(client, '/')
        get_form_token(client, '/book')        # second tab

        form = valid_booking()
        form['form_token'] = first
        form['form_source'] = 'index'
        response = client.post('/submit_reservation', data=form)

        assert response.status_code == 302
        assert '/reservation_success' in response.headers['Location']
        assert rows(sheets) == 1

    def test_both_open_pages_can_book(self, client, sheets):
        tokens = [get_form_token(client, '/'), get_form_token(client, '/book')]
        for token in tokens:
            form = valid_booking()
            form['form_token'] = token
            client.post('/submit_reservation', data=form)
        assert rows(sheets) == 2


class TestExpiredOrUnknownToken:

    @pytest.fixture
    def stale(self, client, sheets):
        form = valid_booking()
        form['form_token'] = 'deadbeef' * 4      # never issued to this session
        form['form_source'] = 'book'
        response = client.post('/submit_reservation', data=form)
        assert rows(sheets) == 0
        return response

    def test_it_is_not_a_silent_bounce_to_the_homepage(self, stale):
        assert stale.status_code == 400, "should re-render the form, not redirect away"

    def test_it_explains_what_to_do(self, stale):
        assert 'Submit once more' in stale.get_data(as_text=True)

    def test_it_keeps_the_answers(self, stale):
        assert 'value="jane.smith@gmail.com"' in stale.get_data(as_text=True)

    def test_the_retry_actually_saves(self, client, sheets, stale):
        html = stale.get_data(as_text=True)
        retry = re.search(r'name="form_token" value="([^"]+)"', html).group(1)
        form = valid_booking()
        form['form_token'] = retry
        client.post('/submit_reservation', data=form)
        assert rows(sheets) == 1


class TestResubmittingASavedBooking:

    def test_it_does_not_book_twice(self, client, sheets):
        form = valid_booking()
        form['form_token'] = get_form_token(client)
        client.post('/submit_reservation', data=form)
        assert rows(sheets) == 1

        again = client.post('/submit_reservation', data=form)
        assert rows(sheets) == 1, "refresh or back-button created a second booking"
        assert '/reservation_success' in again.headers.get('Location', ''), \
            "should land on the confirmation, not the homepage"

    def test_the_confirmation_survives_a_refresh(self, client, sheets):
        form = valid_booking(name='Jane Smith')
        form['form_token'] = get_form_token(client)
        client.post('/submit_reservation', data=form)

        first = client.get('/reservation_success')
        second = client.get('/reservation_success')
        assert first.status_code == 200
        assert second.status_code == 200, "refreshing the confirmation bounced the customer home"
        assert 'Jane Smith' in second.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Bot defences still reject, but a false positive can now recover
# ---------------------------------------------------------------------------

class TestBotDefencesStayIntact:

    def test_honeypot_still_blocks_the_booking(self, client, sheets):
        response = post(client, website='http://spam.example.com')
        assert rows(sheets) == 0
        assert response.status_code == 400

    def test_a_too_fast_submit_still_blocks_the_booking(self, client, sheets, app_module, monkeypatch):
        monkeypatch.setattr(app_module, 'MIN_FILL_SECONDS', 30)
        response = post(client)
        assert rows(sheets) == 0
        assert 'Submit once more' in response.get_data(as_text=True)

    def test_a_human_caught_by_the_speed_check_can_just_resubmit(self, client, sheets, app_module, monkeypatch):
        monkeypatch.setattr(app_module, 'MIN_FILL_SECONDS', 30)
        html = post(client).get_data(as_text=True)

        monkeypatch.setattr(app_module, 'MIN_FILL_SECONDS', 0)
        retry = re.search(r'name="form_token" value="([^"]+)"', html).group(1)
        form = valid_booking()
        form['form_token'] = retry
        client.post('/submit_reservation', data=form)
        assert rows(sheets) == 1
