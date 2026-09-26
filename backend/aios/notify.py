"""Alerts sent outside the portal: a webhook, an e-mail, or both.

An alert is sent once, when it opens (or opens again), if its severity reaches
the configured level. Alerts are raised by several services, so none of them
sends anything: the control plane looks for alerts not yet sent once a minute
and delivers them. A destination that does not answer is tried again at the
next pass, for an hour.

The webhook receives JSON; with a secret, the body is signed with HMAC-SHA256
in the X-AIOS-Signature header (sha256=<hex>) so the receiver can check it came
from this appliance. The SMTP password is kept in /etc/aios/secrets, never
returned to the browser.
"""
import hashlib
import hmac
import ipaddress
import json
import smtplib
import socket
import ssl
from email.message import EmailMessage
from urllib.parse import urlparse

import httpx

from .core import ETC, atomic_write, execute, now, rows, setting, set_setting
from .alerts import SEVERITIES

GIVE_UP = 3600
DEFAULTS = {'min_severity': 'WARNING', 'webhook_url': '', 'email_to': [], 'smtp_host': '', 'smtp_port': 587,
            'smtp_security': 'starttls', 'smtp_username': '', 'email_from': ''}


def password_path():
    return ETC / 'secrets' / 'smtp-password'


def secret_path():
    return ETC / 'secrets' / 'webhook-secret'


def read(path):
    try:
        return path.read_text()
    except OSError:
        return ''


def store(path, value):
    """Keep a secret, or remove it when set to an empty string; None leaves it."""
    if value is None:
        return
    if value:
        atomic_write(path, value, 0o600)
    else:
        path.unlink(missing_ok=True)


def config():
    return {**DEFAULTS, **(setting('notifications') or {})}


def public_config():
    """What the portal shows: the settings, and whether each secret is set."""
    return {**config(), 'smtp_password_set': bool(read(password_path())), 'webhook_secret_set': bool(read(secret_path())),
            'status': setting('notifications_status') or {}}


def channels(settings):
    found = []
    if settings.get('webhook_url'):
        found.append('webhook')
    if settings.get('smtp_host') and settings.get('email_to') and settings.get('email_from'):
        found.append('email')
    return found


def event(alert):
    return {'event': 'alert', 'id': alert['id'], 'severity': alert['severity'], 'message': alert['message'],
            'created_at': alert['created_at'], 'appliance': socket.gethostname()}


def forbidden_address(url, resolve=True):
    """Why a webhook may not be called at this address, or None. The appliance's
    own services listen on loopback: a webhook must not reach them. Addresses on
    the local network are allowed, since that is where tools such as n8n run."""
    host = urlparse(url).hostname
    if not host:
        return 'The webhook needs a host name'
    if host.lower() in ('localhost', 'localhost.localdomain') or host.lower().endswith('.localhost'):
        return 'The webhook may not point at this appliance'
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        if not resolve:
            return None
        try:
            addresses = [ipaddress.ip_address(info[4][0].split('%')[0]) for info in socket.getaddrinfo(host, None)]
        except (OSError, ValueError):
            return None  # an unknown name fails when the message is sent
    for address in addresses:
        if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
            return f'The webhook may not point at {address}: that is this appliance or a link-local address'
    return None


def send_webhook(settings, payload):
    refused = forbidden_address(settings['webhook_url'])
    if refused:
        raise OSError(refused)
    body = json.dumps(payload, sort_keys=True).encode()
    headers = {'Content-Type': 'application/json', 'User-Agent': 'AIOS'}
    secret = read(secret_path())
    if secret:
        headers['X-AIOS-Signature'] = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    proxy = (setting('system_config') or {}).get('proxy') or None
    with httpx.Client(timeout=15, follow_redirects=False, trust_env=False, proxy=proxy) as client:
        response = client.post(settings['webhook_url'], content=body, headers=headers)
    if response.status_code >= 300:
        raise OSError(f'Webhook answered HTTP {response.status_code}')


def send_email(settings, payload):
    message = EmailMessage()
    message['Subject'] = f"[AIOS {payload['appliance']}] {payload['severity']}: {payload['message'][:80]}"
    message['From'] = settings['email_from']
    message['To'] = ', '.join(settings['email_to'])
    message.set_content(f"{payload['message']}\n\nAppliance: {payload['appliance']}\nSeverity: {payload['severity']}\n"
                        f"Alert: {payload['id']}\n\nOpen the portal's Dashboard to see every alert.")
    security, host, port = settings['smtp_security'], settings['smtp_host'], int(settings['smtp_port'])
    context = ssl.create_default_context()
    connection = smtplib.SMTP_SSL(host, port, timeout=20, context=context) if security == 'tls' else smtplib.SMTP(host, port, timeout=20)
    with connection as smtp:
        if security == 'starttls':
            smtp.starttls(context=context)
        password = read(password_path())
        if settings.get('smtp_username') and password:
            smtp.login(settings['smtp_username'], password)
        smtp.send_message(message)


SENDERS = {'webhook': send_webhook, 'email': send_email}


def deliver(payload, settings=None, only=None):
    """Send one event on every configured channel. Returns {channel: error or ''}."""
    settings = settings or config()
    outcome = {}
    for channel in only or channels(settings):
        try:
            SENDERS[channel](settings, payload)
            outcome[channel] = ''
        except (OSError, httpx.HTTPError, smtplib.SMTPException, ssl.SSLError, ValueError) as exc:
            outcome[channel] = str(exc) or type(exc).__name__
    return outcome


def record(outcome):
    status = setting('notifications_status') or {}
    for channel, error in outcome.items():
        status[channel] = {'at': now(), 'ok': not error, 'error': error}
    set_setting('notifications_status', status)


def deliver_pending(current=None):
    """Send the alerts opened since the last pass; called once a minute."""
    current = current or now()
    settings = config()
    pending = rows('SELECT * FROM alerts WHERE notified=0 AND resolved=0 ORDER BY created_at')
    if not pending:
        return 0
    wanted = channels(settings)
    level = SEVERITIES.index(settings['min_severity']) if settings['min_severity'] in SEVERITIES else 1
    sent = 0
    for alert in pending:
        if not wanted or SEVERITIES.index(alert['severity']) < level:
            # Nothing to send it to, or below the level: it stays on the dashboard only.
            execute('UPDATE alerts SET notified=1 WHERE id=?', (alert['id'],))
            continue
        outcome = deliver(event(alert), settings)
        record(outcome)
        if not any(outcome.values()):
            execute('UPDATE alerts SET notified=1 WHERE id=?', (alert['id'],))
            sent += 1
        elif current - alert['created_at'] > GIVE_UP:
            execute('UPDATE alerts SET notified=2 WHERE id=?', (alert['id'],))
    return sent
