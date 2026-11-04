"""Phase 0: secrets, auth, CSRF, rate limiting, cookie flags, headers, token storage."""
import hashlib
import os
import re
from pathlib import Path

import pytest
from werkzeug.security import check_password_hash, generate_password_hash

import app as app_module
from app import Link, create_app, db
from conftest import PASSWORD, PASSWORD_HASH, add_client, make_app, new_link

GOOD = dict(SECRET_KEY='x' * 32, ADMIN_PASSWORD_HASH=PASSWORD_HASH, SQLALCHEMY_DATABASE_URI='sqlite://')


# ---- G1: no default or weak secrets ---------------------------------------------------------

def test_app_refuses_to_start_without_secret_key(monkeypatch):
    monkeypatch.delenv('ORS_SRS_SECRET_KEY', raising=False)
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        create_app({k: v for k, v in GOOD.items() if k != 'SECRET_KEY'})


def test_app_refuses_short_secret_key():
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        create_app({**GOOD, 'SECRET_KEY': 'short'})


@pytest.mark.parametrize('bad', [None, '', 'plaintext-password', 'old-prototype-default-value'])
def test_app_refuses_missing_or_plaintext_password_hash(bad, monkeypatch):
    monkeypatch.delenv('ORS_SRS_ADMIN_PASSWORD_HASH', raising=False)
    with pytest.raises(RuntimeError, match='PASSWORD_HASH'):
        create_app({**GOOD, 'ADMIN_PASSWORD_HASH': bad})


# Only the SHA-256 of the first prototype's default password is kept here, so this public file does not publish it.
OLD_DEFAULT_SHA256 = '456e50e731787eaf3c759141176fe1184b6b0984a40805f08c8b5e55817ad492'


def test_old_hardcoded_admin_password_is_gone_from_source():
    tokens = re.findall(r"[^\s'\"]+", Path(app_module.__file__).read_text())
    assert OLD_DEFAULT_SHA256 not in {hashlib.sha256(t.encode()).hexdigest() for t in tokens}


def test_env_vars_configure_the_app(monkeypatch, tmp_path):
    monkeypatch.setenv('ORS_SRS_SECRET_KEY', 'e' * 20)
    monkeypatch.setenv('ORS_SRS_ADMIN_PASSWORD_HASH', generate_password_hash('pw-from-env'))
    monkeypatch.setenv('ORS_SRS_ADMIN_USERNAME', 'dr-demo')
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path / "e.db"}')
    c = create_app({'RATELIMIT_ENABLED': False, 'WTF_CSRF_ENABLED': False}).test_client()
    assert c.post('/admin/login', data={'username': 'dr-demo', 'password': 'pw-from-env'}).status_code == 302
    assert c.post('/admin/login', data={'username': 'admin', 'password': 'pw-from-env'}).status_code == 200


def test_the_direct_neon_string_wins_when_both_are_set(monkeypatch, tmp_path):
    """`neon env pull` writes a pooled DATABASE_URL and a direct DATABASE_URL_UNPOOLED; the app wants the direct one."""
    monkeypatch.setenv('ORS_SRS_SECRET_KEY', 'e' * 20)
    monkeypatch.setenv('ORS_SRS_ADMIN_PASSWORD_HASH', generate_password_hash('pw-from-env'))
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path / "pooled.db"}')
    assert create_app().config['SQLALCHEMY_DATABASE_URI'].endswith('pooled.db')  # only the one string: used as before
    monkeypatch.setenv('DATABASE_URL_UNPOOLED', f'sqlite:///{tmp_path / "direct.db"}')
    assert create_app().config['SQLALCHEMY_DATABASE_URI'].endswith('direct.db')


@pytest.mark.parametrize('given, expected', [
    ('postgres://u:p@h/db', 'postgresql+psycopg://u:p@h/db'),
    ('postgresql://u:p@h/db?sslmode=require', 'postgresql+psycopg://u:p@h/db?sslmode=require'),
    ('sqlite:///x.db', 'sqlite:///x.db'),
])
def test_database_url_is_normalised_for_the_psycopg_driver(given, expected):
    assert app_module.database_url(given) == expected


# ---- every admin surface is behind login ------------------------------------------------------

ADMIN_GET = ['/admin', '/admin/recent', '/admin/caseload', '/admin/admins', '/admin/account/totp', '/admin/account', '/admin/client/some-id', '/admin/client/some-id/report', '/admin/assessment/1', '/admin/client/some-id/export', '/admin/export-all']
ADMIN_POST = ['/admin/clients', '/admin/account/password', '/admin/account/recovery', '/admin/account/keys', '/admin/account/keys/1/revoke', '/admin/client/some-id/links', '/admin/client/some-id/links/revoke',
              '/admin/client/some-id/delete', '/admin/assessment/1/delete', '/admin/account/email', '/admin/account/totp', '/admin/account/backup-codes',
              '/admin/account/totp/disable', '/admin/admins/1/approve', '/admin/admins/1/reject', '/admin/admins/1/disable', '/admin/admins/1/enable']


@pytest.mark.parametrize('path', ADMIN_GET)
def test_admin_get_routes_redirect_to_login_when_signed_out(anon, path):
    r = anon.get(path)
    assert r.status_code == 302 and '/admin/login' in r.headers['Location']


@pytest.mark.parametrize('path', ADMIN_POST)
def test_admin_post_routes_redirect_to_login_when_signed_out(anon, path):
    r = anon.post(path, data={'first_name': 'A', 'last_name': 'B'})
    assert r.status_code == 302 and '/admin/login' in r.headers['Location']


def test_activity_endpoint_returns_401_not_a_login_page(anon):
    assert anon.get('/admin/client/some-id/activity.json').status_code == 401


def test_signed_out_requests_never_change_data(anon, app):
    anon.post('/admin/clients', data={'first_name': 'Eve', 'last_name': 'Intruder'})
    assert db_count(app, app_module.Client) == 0


def db_count(app, model):
    with app.app_context():
        return model.query.count()


# ---- login ----------------------------------------------------------------------------------------

def test_wrong_password_and_wrong_username_fail(anon):
    for creds in ({'username': 'admin', 'password': 'nope'}, {'username': 'root', 'password': PASSWORD}, {'username': '', 'password': ''}):
        r = anon.post('/admin/login', data=creds)
        assert r.status_code == 200 and b'Incorrect' in r.data
    assert anon.get('/admin').status_code == 302


def test_correct_login_opens_dashboard(admin):
    r = admin.get('/admin')
    assert r.status_code == 200 and b'Add client' in r.data


def test_logout_is_post_only_and_ends_the_session(admin):
    assert admin.get('/admin/logout').status_code in (404, 405)
    assert admin.get('/admin').status_code == 200
    assert admin.post('/admin/logout').status_code == 302
    assert admin.get('/admin').status_code == 302


# ---- G3: rate limit ---------------------------------------------------------------------------------

def test_login_is_rate_limited_after_five_attempts_per_minute(tmp_path):
    app = make_app(tmp_path, RATELIMIT_ENABLED=True)
    app_module.limiter.reset()
    c = app.test_client()
    codes = [c.post('/admin/login', data={'username': 'admin', 'password': 'bad'}).status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]
    # even the right password is refused while locked out
    assert c.post('/admin/login', data={'username': 'admin', 'password': PASSWORD}).status_code == 429
    app_module.limiter.reset()


# ---- G2: CSRF -----------------------------------------------------------------------------------------

def csrf_token(client, path='/admin/login'):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


def test_login_requires_csrf_token(tmp_path):
    c = make_app(tmp_path, WTF_CSRF_ENABLED=True).test_client()
    assert c.post('/admin/login', data={'username': 'admin', 'password': PASSWORD}).status_code == 400
    ok = c.post('/admin/login', data={'username': 'admin', 'password': PASSWORD, 'csrf_token': csrf_token(c)})
    assert ok.status_code == 302


def test_csrf_still_works_over_https_when_browsers_send_no_referer(tmp_path):
    """Regression: with Referrer-Policy no-referrer, browsers send no Referer; a strict Referer check would 400 every HTTPS login."""
    c = make_app(tmp_path, WTF_CSRF_ENABLED=True).test_client()
    https = dict(base_url='https://localhost')
    page = c.get('/admin/login', **https)
    assert page.headers['Referrer-Policy'] == 'no-referrer'
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
    ok = c.post('/admin/login', data={'username': 'admin', 'password': PASSWORD, 'csrf_token': token}, **https)   # no Referer header
    assert ok.status_code == 302
    assert c.post('/admin/login', data={'username': 'admin', 'password': PASSWORD}, **https).status_code == 400    # no token: still refused


def test_every_admin_post_rejects_missing_csrf_token(tmp_path):
    app = make_app(tmp_path, WTF_CSRF_ENABLED=True)
    c = app.test_client()
    c.post('/admin/login', data={'username': 'admin', 'password': PASSWORD, 'csrf_token': csrf_token(c)})
    token = csrf_token(c, '/admin')
    c.post('/admin/clients', data={'first_name': 'Jane', 'last_name': 'Patel', 'csrf_token': token})
    for path in ['/admin/clients', '/admin/client/jane-patel/links', '/admin/client/jane-patel/links/revoke',
                 '/admin/client/jane-patel/delete', '/admin/assessment/1/delete', '/admin/logout',
                 '/admin/account/password', '/admin/account/recovery', '/admin/account/keys', '/admin/account/keys/1/revoke',
                 '/admin/account/email', '/admin/account/totp', '/admin/account/totp/disable', '/admin/account/backup-codes',
                 '/admin/admins/1/approve', '/admin/signup', '/admin/verify', '/admin/verify/resend', '/admin/2fa', '/admin/2fa/resend',
                 '/admin/2fa/setup']:
        assert c.post(path, data={'first_name': 'x', 'last_name': 'y'}).status_code == 400, path
    assert db_count(app, app_module.Client) == 1  # the forged deletes did nothing
    assert c.post('/admin/client/jane-patel/delete', data={'csrf_token': token}).status_code == 302
    assert db_count(app, app_module.Client) == 0


def test_client_save_endpoint_works_without_csrf_because_it_uses_the_one_time_token(tmp_path):
    app = make_app(tmp_path, WTF_CSRF_ENABLED=True)
    admin = app.test_client()
    admin.post('/admin/login', data={'username': 'admin', 'password': PASSWORD, 'csrf_token': csrf_token(admin)})
    t = csrf_token(admin, '/admin')
    admin.post('/admin/clients', data={'first_name': 'Jane', 'last_name': 'Patel', 'csrf_token': t})
    r = admin.post('/admin/client/jane-patel/links', data={'kinds': 'ORS', 'csrf_token': t}, follow_redirects=True)
    token = re.search(r'http://localhost/client/([^"]+)"', r.get_data(as_text=True)).group(1)
    assert app.test_client().post('/api/save', json={'token': token, 'type': 'ORS', 'positions': [.5] * 4}).status_code == 200


# ---- G4: cookies, headers ----------------------------------------------------------------------------------

def test_session_cookie_is_secure_httponly_samesite_by_default(tmp_path):
    app = make_app(tmp_path, SESSION_COOKIE_SECURE=True)
    r = app.test_client().post('/admin/login', data={'username': 'admin', 'password': PASSWORD})
    cookie = r.headers['Set-Cookie']
    assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=Lax' in cookie and 'Expires=' in cookie


def test_cookie_secure_defaults_on_and_dev_switch_turns_it_off(monkeypatch):
    monkeypatch.delenv('ORS_SRS_ENV', raising=False)
    assert create_app(GOOD).config['SESSION_COOKIE_SECURE'] is True
    monkeypatch.setenv('ORS_SRS_ENV', 'dev')
    assert create_app(GOOD).config['SESSION_COOKIE_SECURE'] is False


def test_admin_session_lasts_eight_hours_not_thirty_one_days(app):
    assert app.config['PERMANENT_SESSION_LIFETIME'].total_seconds() == 8 * 3600


@pytest.mark.parametrize('path', ['/', '/health', '/admin/login', '/client/nope'])
def test_privacy_headers_on_every_response(anon, path):
    h = anon.get(path).headers
    assert h['Referrer-Policy'] == 'no-referrer'
    assert 'noindex' in h['X-Robots-Tag'] and h['X-Content-Type-Options'] == 'nosniff'


def test_content_security_policy_forbids_inline_and_third_party_code(anon):
    csp = anon.get('/').headers['Content-Security-Policy']
    directives = dict(d.strip().split(' ', 1) for d in csp.split(';') if d.strip())
    assert directives['script-src'] == "'self'" and directives['style-src'] == "'self'"   # no 'unsafe-inline', no hosts
    assert directives['default-src'] == "'self'" and directives['object-src'] == "'none'"
    assert directives['frame-ancestors'] == "'none'" and directives['base-uri'] == "'none'" and directives['form-action'] == "'self'"
    assert 'unsafe' not in csp and 'http' not in csp


@pytest.mark.parametrize('path', ['/', '/client/nope', '/static/app.css', '/health'])
def test_hardening_headers_everywhere(anon, path):
    h = anon.get(path).headers
    assert h['X-Frame-Options'] == 'DENY' and 'camera=()' in h['Permissions-Policy'] and 'Content-Security-Policy' in h


def test_hsts_only_over_https(anon):
    assert 'Strict-Transport-Security' not in anon.get('/').headers
    h = anon.get('/', base_url='https://localhost').headers
    assert h['Strict-Transport-Security'].startswith('max-age=31536000')


@pytest.mark.parametrize('path', ['/admin/login', '/client/nope'])
def test_sensitive_pages_are_not_cacheable(anon, path):
    assert anon.get(path).headers['Cache-Control'] == 'no-store'


# ---- G5: link tokens are stored hashed ---------------------------------------------------------------------------

def test_token_is_never_stored_only_its_hash(app, admin):
    cid = add_client(admin)
    token = new_link(admin, cid)
    with app.app_context():
        link = Link.query.one()
        assert link.token_hash != token and len(link.token_hash) == 64
        columns = [str(getattr(link, c.name)) for c in Link.__table__.columns]
        assert not any(token in v for v in columns)
    assert len(token) >= 32  # 24 random bytes, urlsafe


def test_one_time_link_is_shown_once_only(admin):
    cid = add_client(admin)
    new_link(admin, cid)
    again = admin.get(f'/admin/client/{cid}').get_data(as_text=True)
    assert 'http://localhost/client/' not in again  # a reload must not reveal the link again


# ---- G9: server-side input limits --------------------------------------------------------------------------------

def test_name_length_is_capped_on_the_server(app, admin):
    r = admin.post('/admin/clients', data={'first_name': 'a' * 81, 'last_name': 'b', 'age_band': 'adult'}, follow_redirects=True)
    assert b'80 characters' in r.data and db_count(app, app_module.Client) == 0


def test_eighty_char_names_are_accepted_and_fit_the_id_column(app, admin):
    cid = add_client(admin, 'a' * 80, 'b' * 80)
    assert len(cid) == 161 and db_count(app, app_module.Client) == 1


def test_blank_and_whitespace_names_are_rejected(app, admin):
    for first, last in [('', 'x'), ('x', ''), ('   ', '  ')]:
        admin.post('/admin/clients', data={'first_name': first, 'last_name': last, 'age_band': 'adult'})
    assert db_count(app, app_module.Client) == 0


def test_unknown_age_band_is_rejected(app, admin):
    admin.post('/admin/clients', data={'first_name': 'A', 'last_name': 'B', 'age_band': 'senior'})
    assert db_count(app, app_module.Client) == 0


def test_oversized_request_body_is_refused(anon):
    assert anon.post('/api/save', data=b'x' * (70 * 1024), content_type='application/json').status_code == 413


def test_old_database_schema_stops_the_app_with_a_clear_message(tmp_path):
    import sqlite3
    path = tmp_path / 'old.db'
    sqlite3.connect(path).executescript('CREATE TABLE client (id INTEGER PRIMARY KEY, client_id VARCHAR(200), first_name TEXT, last_name TEXT);')
    with pytest.raises(RuntimeError, match=r"lacks columns .*'client': \[.*'slug'.*\]"):
        create_app({**GOOD, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{path}'})
    assert create_app({**GOOD, 'SQLALCHEMY_DATABASE_URI': f'sqlite:///{tmp_path / "fresh.db"}'})   # a new database is fine


# ---- local .env: login hash and secret live in a git-ignored file ---------------------------------

def test_read_env_handles_quotes_comments_and_dollar_signs(tmp_path):
    env = tmp_path / '.env'
    env.write_text('# note\n\nA=plain\nB="double"\nC=\'scrypt:32768:8:1$salt$abc\'\n  D = spaced \nE=a=b\nno-equals-line\n')
    assert app_module.read_env(env) == {'A': 'plain', 'B': 'double', 'C': 'scrypt:32768:8:1$salt$abc', 'D': 'spaced', 'E': 'a=b'}
    assert app_module.read_env(tmp_path / 'missing') == {}


def test_a_real_environment_variable_beats_the_env_file(monkeypatch, tmp_path):
    monkeypatch.setattr(os, 'environ', {'FROM_HOST': 'host value'})  # a throwaway environment, restored after the test
    env = tmp_path / '.env'
    env.write_text("FROM_HOST='file value'\nONLY_IN_FILE='file value'\n")
    app_module.load_env(env)
    assert os.environ['FROM_HOST'] == 'host value' and os.environ['ONLY_IN_FILE'] == 'file value'


def test_create_app_never_loads_the_env_file(monkeypatch):
    """The file is loaded by the command line only, so tests and hosts cannot pick up a developer's .env by accident."""
    monkeypatch.setattr(os, 'environ', {})
    monkeypatch.setattr(app_module, 'load_env', lambda *a, **k: pytest.fail('create_app must not read .env'))
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        create_app({'SQLALCHEMY_DATABASE_URI': 'sqlite://'})


def test_the_env_file_is_git_ignored():
    assert '.env' in (Path(app_module.__file__).parent / '.gitignore').read_text().split()


def test_requirements_list_everything_the_host_must_install():
    """Render installs only requirements.txt; a missing line (gunicorn, pyexcel and its xlsx plugin) breaks the build."""
    names = {re.split(r'[=\[<>]', line)[0].lower() for line in (Path(app_module.__file__).parent / 'requirements.txt').read_text().split()}
    assert {'flask', 'flask-sqlalchemy', 'flask-wtf', 'flask-limiter', 'psycopg', 'openpyxl', 'gunicorn',
            'pyexcel', 'pyexcel-io', 'pyexcel-xlsx', 'pyotp', 'qrcode', 'cryptography'} <= names


def test_render_blueprint_keeps_secrets_out_of_the_repo_and_matches_the_guide():
    text = (Path(app_module.__file__).parent / 'render.yaml').read_text()
    blocks = {m.group(1): m.group(2) for m in re.finditer(r'- key: (\w+)([^\n]*(?:\n(?!      - key:)[^\n]*)*)', text)}
    for secret in ('DATABASE_URL', 'ORS_SRS_ADMIN_USERNAME', 'ORS_SRS_ADMIN_PASSWORD_HASH'):
        assert 'sync: false' in blocks[secret] and 'value:' not in blocks[secret], secret  # asked for in the dashboard, never stored here
    # The Blueprint leaves mail off: ORS_SRS_MAIL=brevo with an empty sender stops the app at start (the first 0.1.1 deploy failed
    # that way). The owner sets ORS_SRS_MAIL in the dashboard once the sender and key below are filled in.
    assert 'ORS_SRS_MAIL' not in blocks
    for secret in ('ORS_SRS_MAIL_FROM', 'ORS_SRS_BREVO_API_KEY'):
        assert 'sync: false' in blocks[secret] and 'value:' not in blocks[secret], secret
    assert 'generateValue: true' in blocks['ORS_SRS_SECRET_KEY'] and 'value:' not in blocks['ORS_SRS_SECRET_KEY']
    assert 'value: "1"' in blocks['ORS_SRS_PROXY_HOPS']  # one trusted proxy: more would let a visitor spoof their address
    assert 'ORS_SRS_ENV' not in text  # dev mode would drop the Secure cookie flag
    assert not re.search(r'scrypt:|pbkdf2:|postgres(ql)?://', text)
    assert re.search(r'^    plan: free$', text, re.M) and re.search(r'^    region: singapore$', text, re.M)
    assert 'healthCheckPath: /health' in text
    assert 'app:create_app()' in text and '--workers 1' in text and 'access-logfile' not in text.replace('# one-time', '').split('startCommand:')[1].split('\n')[0]
    assert f'PYTHON_VERSION' in blocks and (Path(app_module.__file__).parent / '.python-version').read_text().strip() in blocks['PYTHON_VERSION']
