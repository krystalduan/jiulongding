"""Failure logging and alert emails."""
import logging

import pytest

import app as flask_app


@pytest.fixture
def alerts(monkeypatch):
    """Alerts go to a list instead of Resend; threads run inline."""
    sent = []
    monkeypatch.setattr(flask_app, 'ALERT_EMAIL', 'owner@example.com')
    monkeypatch.setattr(flask_app, 'send_alert_email',
                        lambda kind, message, count: sent.append((kind, message, count)))

    class InlineThread:
        def __init__(self, target, args=(), **kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(flask_app.threading, 'Thread', InlineThread)
    return sent


class TestMasking:

    def test_email_keeps_first_two_letters_and_domain(self):
        assert flask_app.mask_email('krystal@gmail.com') == 'kr***@gmail.com'

    def test_not_an_email(self):
        assert flask_app.mask_email('') == '***'
        assert flask_app.mask_email(None) == '***'

    def test_phone_keeps_last_three_digits(self):
        assert flask_app.mask_phone('+61 412 345 678') == '***678'
        assert flask_app.mask_phone('') == '***'


class TestFailureLine:

    def test_tagged_so_one_search_finds_it(self, caplog, alerts):
        with caplog.at_level(logging.ERROR):
            flask_app.log_failure('email', 'Resend 500')
        assert 'FAILED email: Resend 500' in caplog.text


class TestAlertThreshold:

    def test_one_off_failure_does_not_alert(self, alerts):
        flask_app.log_failure('email', 'blip')
        flask_app.log_failure('email', 'blip')
        assert alerts == []

    def test_repeated_failures_alert_once(self, alerts):
        for _ in range(5):
            flask_app.log_failure('email', 'Resend 500')
        assert alerts == [('email', 'Resend 500', 3)]

    def test_kinds_are_counted_separately(self, alerts):
        flask_app.log_failure('email', 'x')
        flask_app.log_failure('email', 'x')
        flask_app.log_failure('sheets', 'y')
        assert alerts == []

    def test_old_failures_drop_out_of_the_window(self, alerts):
        hour = flask_app.ALERT_WINDOW_SECONDS
        flask_app.record_failure('email', 'x', now=0)
        flask_app.record_failure('email', 'x', now=10)
        flask_app.record_failure('email', 'x', now=hour + 20)
        assert alerts == []

    def test_alerts_again_after_cooldown(self, alerts):
        start = 1000
        for t in (start, start + 1, start + 2):
            flask_app.record_failure('email', 'x', now=t)
        later = start + flask_app.ALERT_COOLDOWN_SECONDS + 5
        for t in (later, later + 1, later + 2):
            flask_app.record_failure('email', 'x', now=t)
        assert len(alerts) == 2

    def test_serious_kinds_alert_immediately(self, alerts):
        flask_app.log_failure('sms-job', 'day_of run crashed')
        assert alerts == [('sms-job', 'day_of run crashed', 1)]

    def test_no_alert_email_configured_still_logs(self, alerts, monkeypatch, caplog):
        monkeypatch.setattr(flask_app, 'ALERT_EMAIL', '')
        flask_app.log_failure('sms-job', 'crashed')
        assert alerts == []
        assert 'ALERT_EMAIL is not set' in caplog.text


class TestSendAlertEmail:

    def test_resend_failure_is_logged_not_raised(self, monkeypatch, caplog):
        def boom(*a, **k):
            raise ConnectionError('down')
        monkeypatch.setattr(flask_app.requests, 'post', boom)
        with caplog.at_level(logging.WARNING):
            flask_app.send_alert_email('email', 'x', 3)
        assert 'Alert email not sent' in caplog.text
        assert 'FAILED' not in caplog.text


class TestWiredIntoTheApp:

    def test_crashed_sms_job_alerts(self, alerts, monkeypatch):
        class BrokenSpreadsheet:
            def worksheet(self, name):
                raise RuntimeError('Sheets is down')
        monkeypatch.setattr(flask_app, 'get_sheets', lambda: (BrokenSpreadsheet(), None))
        result = flask_app.send_sms_on_date('2026-09-28')
        assert result.startswith('Error sending SMS')
        assert alerts and alerts[0][0] == 'sms-job'

    def test_unhandled_crash_is_tagged_and_alerts(self, client, alerts, monkeypatch):
        def crash():
            raise RuntimeError('boom')
        monkeypatch.setattr(flask_app, 'get_sheets', crash)
        form = {'form_token': 'x'}
        monkeypatch.setattr(flask_app, 'consume_form_token', lambda t: ('ok', None))
        monkeypatch.setattr(flask_app, 'validate_reservation', lambda f: ({}, None))
        with pytest.raises(RuntimeError):
            client.post('/submit_reservation', data=form)
        assert alerts and alerts[0][0] == 'unhandled'
        assert 'POST /submit_reservation' in alerts[0][1]
