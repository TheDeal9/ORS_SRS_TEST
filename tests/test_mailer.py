"""mailer.py: settings checks and the real SMTP and Brevo code paths, against stub servers on this machine."""
import json
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import mailer

SMTP = {'MAIL_BACKEND': 'smtp', 'MAIL_FROM': 'Portal <portal@example.test>', 'SMTP_HOST': '127.0.0.1', 'SMTP_SECURITY': 'none'}


# ---- stub servers -------------------------------------------------------------------------------------------------

class _SmtpHandler(socketserver.StreamRequestHandler):
    def handle(self):
        def say(text):
            self.wfile.write((text + '\r\n').encode())
        say('220 stub')
        in_data, body = False, []
        for raw in self.rfile:
            line = raw.decode().rstrip('\r\n')
            if in_data:
                if line == '.':
                    self.server.messages.append('\n'.join(body))
                    in_data, body = False, []
                    say('250 queued')
                else:
                    body.append(line)
            elif line.upper().startswith(('EHLO', 'HELO')):
                say('250 stub')  # no STARTTLS offered
            elif line.upper() == 'DATA':
                say('354 go')
                in_data = True
            elif line.upper() == 'QUIT':
                say('221 bye')
                return
            else:
                say('250 ok')


@pytest.fixture
def smtp_server():
    server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), _SmtpHandler)
    server.daemon_threads, server.messages = True, []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def make_brevo(status=201):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append({'path': self.path, 'headers': {k.lower(): v for k, v in self.headers.items()}, 'json': json.loads(self.rfile.read(int(self.headers['Content-Length'])))})
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b'{"messageId": "x"}')

        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


# ---- settings -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('cfg, dev, message', [
    ({'MAIL_BACKEND': 'carrier-pigeon'}, False, 'must be one of'),
    ({'MAIL_BACKEND': 'console'}, False, 'dev only'),
    ({'MAIL_BACKEND': 'memory'}, False, 'only for tests'),
    ({'MAIL_BACKEND': 'smtp', 'SMTP_HOST': 'h'}, False, 'MAIL_FROM'),
    ({'MAIL_BACKEND': 'smtp', 'MAIL_FROM': 'a@b.co'}, False, 'SMTP_HOST'),
    ({'MAIL_BACKEND': 'smtp', 'MAIL_FROM': 'a@b.co', 'SMTP_HOST': 'h', 'SMTP_SECURITY': 'plain'}, False, 'starttls, ssl or none'),
    ({'MAIL_BACKEND': 'smtp', 'MAIL_FROM': 'a@b.co', 'SMTP_HOST': 'h', 'SMTP_SECURITY': 'none', 'SMTP_USER': 'u'}, False, 'without TLS'),
    ({'MAIL_BACKEND': 'brevo', 'MAIL_FROM': 'a@b.co'}, False, 'BREVO_API_KEY'),
    ({'MAIL_BACKEND': 'brevo', 'BREVO_API_KEY': 'k'}, False, 'MAIL_FROM'),
    ({'MAIL_BACKEND': 'brevo', 'MAIL_FROM': 'a@b.co', 'BREVO_API_KEY': 'k', 'BREVO_URL': 'http://example.com/x'}, False, 'https'),
])
def test_bad_mail_settings_stop_the_app_with_a_clear_message(cfg, dev, message):
    with pytest.raises(RuntimeError, match=message):
        mailer.check_config(cfg, dev=dev)


@pytest.mark.parametrize('cfg, dev', [
    ({}, False), ({'MAIL_BACKEND': ''}, False), ({'MAIL_BACKEND': 'console'}, True), ({'MAIL_BACKEND': 'memory'}, True),
    ({'MAIL_BACKEND': 'memory', 'TESTING': True}, False),
    (SMTP, False),
    ({'MAIL_BACKEND': 'smtp', 'MAIL_FROM': 'a@b.co', 'SMTP_HOST': 'h', 'SMTP_USER': 'u'}, False),
    ({'MAIL_BACKEND': 'brevo', 'MAIL_FROM': 'a@b.co', 'BREVO_API_KEY': 'k'}, False),
    ({'MAIL_BACKEND': 'brevo', 'MAIL_FROM': 'a@b.co', 'BREVO_API_KEY': 'k', 'BREVO_URL': 'http://127.0.0.1:9/x'}, False),
])
def test_good_mail_settings_and_unset_mail_are_accepted(cfg, dev):
    mailer.check_config(cfg, dev=dev)


def test_the_app_refuses_to_start_with_bad_mail_settings(tmp_path):
    from conftest import make_app
    with pytest.raises(RuntimeError, match='must be one of'):
        make_app(tmp_path, MAIL_BACKEND='nope')


@pytest.mark.parametrize('value, expected', [
    ('Portal <p@x.co>', ('Portal', 'p@x.co')), ('p@x.co', ('', 'p@x.co')), ('no-at-sign', ('', '')),
    ('a@b@c.co', ('', '')), ('a b@c.co', ('', '')), ('', ('', '')), (None, ('', '')),
    ('x' * 250 + '@y.co', ('', '')),
])
def test_address_parsing(value, expected):
    assert mailer.address(value) == expected


# ---- memory, console -------------------------------------------------------------------------------------------------

def test_memory_backend_collects_the_message():
    cfg = {'MAIL_BACKEND': 'memory', 'MAIL_OUTBOX': [], 'MAIL_FROM': 'p@x.co'}
    mailer.send(cfg, 'a@b.co', 'Subject', 'Body')
    assert cfg['MAIL_OUTBOX'] == [{'to': 'a@b.co', 'subject': 'Subject', 'body': 'Body'}]


def test_console_backend_prints_to_stderr(capsys):
    mailer.send({'MAIL_BACKEND': 'console', 'MAIL_FROM': 'p@x.co'}, 'a@b.co', 'S', 'the body')
    assert 'the body' in capsys.readouterr().err


def test_unset_backend_and_bad_addresses_or_subjects_are_refused():
    with pytest.raises(mailer.MailError):
        mailer.send({}, 'a@b.co', 'S', 'B')
    cfg = {'MAIL_BACKEND': 'memory', 'MAIL_OUTBOX': [], 'MAIL_FROM': 'p@x.co'}
    for to, subject in [('nope', 'S'), ('a@b.co\r\nBcc: x@y.co', 'S'), ('a@b.co', 'S\r\nBcc: x@y.co'), ('a@b.co', 'S\nX')]:
        with pytest.raises(mailer.MailError):
            mailer.send(cfg, to, subject, 'B')
    assert cfg['MAIL_OUTBOX'] == []


# ---- smtp ---------------------------------------------------------------------------------------------------------------

def test_smtp_delivers_a_plain_text_message(smtp_server):
    mailer.send({**SMTP, 'SMTP_PORT': smtp_server.server_address[1]}, 'to@example.test', 'Code 123456', 'Hello there')
    assert len(smtp_server.messages) == 1
    msg = smtp_server.messages[0]
    assert 'Subject: Code 123456' in msg and 'To: to@example.test' in msg and 'From: Portal <portal@example.test>' in msg
    assert 'Hello there' in msg and 'text/plain' in msg


def test_smtp_connection_refused_is_a_safe_error():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    with pytest.raises(mailer.MailError) as err:
        mailer.send({**SMTP, 'SMTP_PORT': port}, 'to@example.test', 'S', 'SECRET BODY 424242')
    assert 'smtp:' in str(err.value) and '424242' not in str(err.value) and 'to@example.test' not in str(err.value)


def test_smtp_that_never_answers_times_out(monkeypatch):
    silent = socket.socket()
    silent.bind(('127.0.0.1', 0))
    silent.listen(1)
    monkeypatch.setattr(mailer, 'TIMEOUT', 0.3)
    try:
        with pytest.raises(mailer.MailError, match='smtp'):
            mailer.send({**SMTP, 'SMTP_PORT': silent.getsockname()[1]}, 'to@example.test', 'S', 'B')
    finally:
        silent.close()


# ---- brevo ----------------------------------------------------------------------------------------------------------------

def brevo_cfg(server):
    return {'MAIL_BACKEND': 'brevo', 'MAIL_FROM': 'Portal <portal@example.test>', 'BREVO_API_KEY': 'key-123',
            'BREVO_URL': f'http://127.0.0.1:{server.server_address[1]}/v3/smtp/email'}


def test_brevo_sends_the_documented_request():
    server, seen = make_brevo(201)
    mailer.send(brevo_cfg(server), 'to@example.test', 'Code 123456', 'Line one\n<b>two</b>')
    server.shutdown()
    request = seen[0]
    assert request['path'] == '/v3/smtp/email' and request['headers']['api-key'] == 'key-123'
    assert request['headers']['content-type'] == 'application/json'
    body = request['json']
    assert body['sender'] == {'email': 'portal@example.test', 'name': 'Portal'} and body['to'] == [{'email': 'to@example.test'}]
    assert body['subject'] == 'Code 123456' and body['textContent'] == 'Line one\n<b>two</b>'
    assert '&lt;b&gt;two&lt;/b&gt;' in body['htmlContent'] and '<br>' in body['htmlContent']  # the text is escaped in the html part


@pytest.mark.parametrize('status', [400, 401, 429, 500])
def test_brevo_error_statuses_become_a_safe_error(status):
    server, _ = make_brevo(status)
    with pytest.raises(mailer.MailError) as err:
        mailer.send(brevo_cfg(server), 'to@example.test', 'S', 'SECRET BODY 424242')
    server.shutdown()
    assert str(err.value) == f'brevo: status {status}' and 'key-123' not in str(err.value) and '424242' not in str(err.value)


def test_brevo_unreachable_is_a_safe_error():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    cfg = {'MAIL_BACKEND': 'brevo', 'MAIL_FROM': 'p@x.co', 'BREVO_API_KEY': 'k', 'BREVO_URL': f'http://127.0.0.1:{port}/x'}
    with pytest.raises(mailer.MailError, match='brevo'):
        mailer.send(cfg, 'to@example.test', 'S', 'B')


def test_brevo_never_posts_over_plain_http_to_another_host():
    cfg = {'MAIL_BACKEND': 'brevo', 'MAIL_FROM': 'p@x.co', 'BREVO_API_KEY': 'k', 'BREVO_URL': 'http://evil.example/x'}
    with pytest.raises(mailer.MailError, match='insecure'):
        mailer.send(cfg, 'to@example.test', 'S', 'B')
