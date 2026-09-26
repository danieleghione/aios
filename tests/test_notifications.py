"""Alerts sent by webhook and e-mail: once, at the chosen level, signed, retried."""
import hashlib
import hmac
import json

import httpx
import pytest


@pytest.fixture
def webhook(monkeypatch):
    from aios import notify
    received, answer = [], [200]
    original = httpx.Client

    def handler(request):
        received.append(request)
        return httpx.Response(answer[0])
    monkeypatch.setattr(notify.httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != 'proxy'}))
    return received, answer


def configure(environment, **values):
    environment.set_setting('notifications', {'webhook_url': 'https://hooks.example.org/aios', 'min_severity': 'WARNING', **values})


def test_an_alert_is_sent_once_and_signed(environment, webhook):
    from aios import alerts, notify
    received, _ = webhook
    configure(environment)
    notify.store(notify.secret_path(), 'shared secret')
    alerts.raise_alert('disk', 'WARNING', 'Model storage is almost full')
    assert notify.deliver_pending() == 1
    body = received[0].content
    assert json.loads(body)['message'] == 'Model storage is almost full' and json.loads(body)['severity'] == 'WARNING'
    assert received[0].headers['X-AIOS-Signature'] == 'sha256=' + hmac.new(b'shared secret', body, hashlib.sha256).hexdigest()
    alerts.raise_alert('disk', 'ERROR', 'worse')  # still open: not a new alert
    assert notify.deliver_pending() == 0 and len(received) == 1
    alerts.resolve('disk')
    alerts.raise_alert('disk', 'WARNING', 'again')  # opened again: sent again
    assert notify.deliver_pending() == 1 and len(received) == 2


def test_below_the_level_or_without_a_channel_nothing_is_sent(environment, webhook):
    from aios import alerts, notify
    received, _ = webhook
    alerts.raise_alert('updates', 'WARNING', 'restart pending')
    assert notify.deliver_pending() == 0  # nothing configured
    configure(environment)
    assert notify.deliver_pending() == 0 and not received  # already settled
    alerts.raise_alert('other', 'INFO', 'two security updates')
    assert notify.deliver_pending() == 0 and not received
    assert environment.one("SELECT notified FROM alerts WHERE id='other'")['notified'] == 1


def test_a_destination_that_fails_is_tried_again_then_given_up(environment, webhook):
    from aios import alerts, notify
    received, answer = webhook
    configure(environment)
    answer[0] = 500
    alerts.raise_alert('disk', 'ERROR', 'full')
    created = environment.one("SELECT created_at FROM alerts WHERE id='disk'")['created_at']
    assert notify.deliver_pending(created + 60) == 0
    assert environment.one("SELECT notified FROM alerts WHERE id='disk'")['notified'] == 0
    assert environment.setting('notifications_status')['webhook']['error'] == 'Webhook answered HTTP 500'
    answer[0] = 204
    assert notify.deliver_pending(created + 120) == 1 and len(received) == 2
    answer[0] = 500
    alerts.resolve('disk')
    alerts.raise_alert('disk', 'ERROR', 'full again')
    created = environment.one("SELECT created_at FROM alerts WHERE id='disk'")['created_at']
    notify.deliver_pending(created + notify.GIVE_UP + 1)
    assert environment.one("SELECT notified FROM alerts WHERE id='disk'")['notified'] == 2


def test_alerts_from_before_the_upgrade_are_not_sent(environment):
    environment.execute("INSERT INTO alerts(id,severity,message,created_at,resolved) VALUES ('old','ERROR','x',0,0)")
    assert environment.one("SELECT notified FROM alerts WHERE id='old'")['notified'] == 1


def test_an_email_is_composed_and_sent(environment, monkeypatch):
    from aios import notify
    sent = []

    class Server:
        def __init__(self, host, port, timeout=None, context=None):
            sent.append(('connect', host, port))
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def starttls(self, context=None):
            sent.append(('starttls',))
        def login(self, user, password):
            sent.append(('login', user, password))
        def send_message(self, message):
            sent.append(('message', message['To'], message['Subject'], message.get_content()))
    monkeypatch.setattr(notify.smtplib, 'SMTP', Server)
    settings = {**notify.DEFAULTS, 'smtp_host': 'mail.example.org', 'email_from': 'aios@example.org',
                'email_to': ['ops@example.org', 'lead@example.org'], 'smtp_username': 'aios'}
    notify.store(notify.password_path(), 'smtp secret')
    outcome = notify.deliver({'id': 'disk', 'severity': 'ERROR', 'message': 'Model storage is almost full', 'appliance': 'lab', 'created_at': 0}, settings)
    assert outcome == {'email': ''}
    assert sent[0] == ('connect', 'mail.example.org', 587) and sent[1] == ('starttls',) and sent[2] == ('login', 'aios', 'smtp secret')
    assert sent[3][1] == 'ops@example.org, lead@example.org' and 'ERROR' in sent[3][2] and 'almost full' in sent[3][3]


def test_the_settings_routes_keep_secrets_out(admin, environment, webhook):
    received, _ = webhook
    assert admin.post('/api/v1/aios/system/notifications/test').status_code == 409
    saved = admin.put('/api/v1/aios/system/notifications', json={
        'webhook_url': 'https://hooks.example.org/aios', 'webhook_secret': 'shared secret', 'min_severity': 'ERROR',
        'smtp_host': 'mail.example.org', 'smtp_password': 'smtp secret', 'email_to': ['ops@example.org']})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body['webhook_secret_set'] and body['smtp_password_set'] and 'smtp secret' not in saved.text and 'shared secret' not in saved.text
    # Saving again without the secrets keeps them; an empty string removes one.
    admin.put('/api/v1/aios/system/notifications', json={'webhook_url': 'https://hooks.example.org/aios', 'webhook_secret': ''})
    again = admin.get('/api/v1/aios/system/notifications').json()
    assert again['smtp_password_set'] and not again['webhook_secret_set'] and again['min_severity'] == 'WARNING'
    tested = admin.post('/api/v1/aios/system/notifications/test').json()
    assert tested == {'results': {'webhook': {'ok': True, 'error': ''}}} and json.loads(received[0].content)['event'] == 'test'
    assert admin.put('/api/v1/aios/system/notifications', json={'email_to': ['not an address']}).status_code == 422
    assert admin.put('/api/v1/aios/system/notifications', json={'webhook_url': 'ftp://x'}).status_code == 422


def test_the_webhook_cannot_reach_the_appliance_itself(admin, environment, monkeypatch):
    from aios import notify
    for url in ('http://127.0.0.1:8090/v1', 'http://localhost/x', 'http://[::1]:8081/', 'http://169.254.169.254/latest', 'http://0.0.0.0/'):
        assert notify.forbidden_address(url), url
        assert admin.put('/api/v1/aios/system/notifications', json={'webhook_url': url}).status_code == 422, url
    assert notify.forbidden_address('http://192.168.1.20:5678/webhook/aios') is None
    # A name that resolves to loopback is refused when the message is sent.
    monkeypatch.setattr(notify.socket, 'getaddrinfo', lambda host, port: [(2, 1, 6, '', ('127.0.0.1', 0))])
    outcome = notify.deliver({'id': 'x'}, {**notify.DEFAULTS, 'webhook_url': 'https://sneaky.example.org/hook'})
    assert 'may not point at 127.0.0.1' in outcome['webhook']
