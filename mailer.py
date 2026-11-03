"""Outgoing mail for one-time codes and account notices. Pure standard library, no Flask.

Two real backends because Render's free web service blocks the SMTP ports (25, 465, 587): `smtp` for a local relay or a
paid host, `brevo` (an HTTPS API call on port 443) for the free one. `console` prints the message for local development
and `memory` collects it for tests. Messages carry codes and plain text only: no links, nothing to phish.
"""
import html
import json
import smtplib
import ssl
import sys
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import parseaddr

BACKENDS = ('smtp', 'brevo', 'console', 'memory')
BREVO_URL = 'https://api.brevo.com/v3/smtp/email'
TIMEOUT = 10  # seconds; the send happens inside the request, so a hung relay must not hold a worker thread long
LOCAL_URLS = ('http://127.0.0.1', 'http://localhost')  # plain http is allowed only for a stub server on this machine


class MailError(Exception):
    """A send failed. The message names the cause by class or status only, so it is safe to log (never a body or key)."""


def check_config(cfg, dev=False):
    """Raise RuntimeError with a message fit to show if the mail settings cannot work. Unset backend is allowed."""
    backend = cfg.get('MAIL_BACKEND') or ''
    if backend and backend not in BACKENDS:
        raise RuntimeError(f'ORS_SRS_MAIL must be one of {", ".join(BACKENDS)} (or unset), not "{backend}".')
    if backend == 'console' and not dev:
        raise RuntimeError('ORS_SRS_MAIL=console prints one-time codes to the log and is for ORS_SRS_ENV=dev only.')
    if backend == 'memory' and not (dev or cfg.get('TESTING')):
        raise RuntimeError('ORS_SRS_MAIL=memory keeps mail in RAM and exists only for tests.')
    if backend in ('smtp', 'brevo') and not address(cfg.get('MAIL_FROM'))[1]:
        raise RuntimeError('ORS_SRS_MAIL_FROM must be a sender address such as "ORS/SRS Portal <you@example.com>".')
    if backend == 'smtp':
        if not cfg.get('SMTP_HOST'):
            raise RuntimeError('ORS_SRS_SMTP_HOST is required when ORS_SRS_MAIL=smtp.')
        if cfg.get('SMTP_SECURITY', 'starttls') not in ('starttls', 'ssl', 'none'):
            raise RuntimeError('ORS_SRS_SMTP_SECURITY must be starttls, ssl or none.')
        if cfg.get('SMTP_SECURITY') == 'none' and cfg.get('SMTP_USER'):
            raise RuntimeError('Refusing to send an SMTP password without TLS: set ORS_SRS_SMTP_SECURITY to starttls or ssl.')
    if backend == 'brevo':
        if not cfg.get('BREVO_API_KEY'):
            raise RuntimeError('ORS_SRS_BREVO_API_KEY is required when ORS_SRS_MAIL=brevo.')
        url = cfg.get('BREVO_URL') or BREVO_URL
        if not url.startswith('https://') and not url.startswith(LOCAL_URLS):
            raise RuntimeError('The Brevo URL must use https.')


def address(value):
    """(display name, address) from "Name <a@b.c>" or "a@b.c"; the address is '' unless it looks like one."""
    name, addr = parseaddr(str(value or ''))
    ok = addr.count('@') == 1 and ' ' not in addr and '\r' not in addr and '\n' not in addr and len(addr) <= 254
    return name, addr if ok else ''


def send(cfg, to, subject, body):
    """Deliver one plain-text message. Raises MailError; callers decide what the person sees."""
    backend = cfg.get('MAIL_BACKEND')
    if backend not in BACKENDS:
        raise MailError('no mail backend configured')
    to = address(to)[1]
    sender_name, sender = address(cfg.get('MAIL_FROM') or 'portal@localhost')
    if not to or not sender:
        raise MailError('bad address')
    if any(c in subject for c in '\r\n'):
        raise MailError('bad subject')
    if backend == 'memory':
        cfg['MAIL_OUTBOX'].append({'to': to, 'subject': subject, 'body': body})
    elif backend == 'console':
        print(f'--- mail to {to}: {subject}\n{body}\n---', file=sys.stderr)
    elif backend == 'smtp':
        _smtp(cfg, sender_name, sender, to, subject, body)
    else:
        _brevo(cfg, sender_name, sender, to, subject, body)


def _smtp(cfg, sender_name, sender, to, subject, body):
    msg = EmailMessage()
    msg['From'] = f'{sender_name} <{sender}>' if sender_name else sender
    msg['To'] = to
    msg['Subject'] = subject
    msg.set_content(body)
    mode = cfg.get('SMTP_SECURITY', 'starttls')
    context = ssl.create_default_context()
    try:
        port = int(cfg.get('SMTP_PORT') or (465 if mode == 'ssl' else 587))
        smtp = (smtplib.SMTP_SSL(cfg['SMTP_HOST'], port, timeout=TIMEOUT, context=context) if mode == 'ssl'
                else smtplib.SMTP(cfg['SMTP_HOST'], port, timeout=TIMEOUT))
        with smtp:
            if mode == 'starttls':
                smtp.starttls(context=context)
            if cfg.get('SMTP_USER'):
                smtp.login(cfg['SMTP_USER'], cfg.get('SMTP_PASSWORD') or '')
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        raise MailError(f'smtp: {type(exc).__name__}') from None  # not the text: it can echo the address or server banner


def _brevo(cfg, sender_name, sender, to, subject, body):
    payload = {'sender': {'email': sender, **({'name': sender_name} if sender_name else {})}, 'to': [{'email': to}],
               'subject': subject, 'textContent': body,
               'htmlContent': '<html><body><p>' + html.escape(body).replace('\n', '<br>') + '</p></body></html>'}
    url = cfg.get('BREVO_URL') or BREVO_URL
    if not url.startswith('https://') and not url.startswith(LOCAL_URLS):
        raise MailError('brevo: insecure url')
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method='POST', headers={
        'api-key': cfg['BREVO_API_KEY'], 'content-type': 'application/json', 'accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # nosec B310 - scheme checked just above
            if resp.status not in (200, 201, 202):
                raise MailError(f'brevo: status {resp.status}')
    except urllib.error.HTTPError as exc:
        raise MailError(f'brevo: status {exc.code}') from None
    except (OSError, ValueError) as exc:  # URLError is an OSError
        raise MailError(f'brevo: {type(exc).__name__}') from None
