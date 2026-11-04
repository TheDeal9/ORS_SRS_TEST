"""Administrator sign-up with a Google-proven email: no mail server, no owner approval, then forced authenticator set-up.

Google is a stub server on this machine. The browser steps are real requests: start, callback (with the state and nonce the
start handed out), finish (ID and password), then the existing forced two-step set-up and its one-time recovery kit."""
import base64
import html as htmllib
import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

import twofactor
from app import Admin, AdminIdentity, create_admin, db
from conftest import PASSWORD, add_client, login_as, make_app, totp_now

from test_concurrency import race

CID, SECRET, OK = 'client-id.apps.example', 'client-secret-value', 'ok@example.test'
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
FORM = dict(username='dr-new', password=PASSWORD, confirm=PASSWORD)
CODE_RE = re.compile(r'\b[0-9a-f]{5}-[0-9a-f]{5}\b')
RECOVERY_RE = re.compile(r'\b[0-9a-f]{4}(?:-[0-9a-f]{4}){5}\b')
GUARDED = ('/admin', '/admin/account', '/admin/recent', '/admin/export-all', '/admin/account/totp')


def b64(data):
    return base64.urlsafe_b64encode(data if isinstance(data, bytes) else json.dumps(data).encode()).rstrip(b'=').decode()


def good_claims(n, **over):
    now = int(time.time())
    return {'iss': 'https://accounts.google.com', 'aud': CID, 'exp': now + 3600, 'iat': now, 'sub': 'g-1', 'email': OK,
            'email_verified': True, 'nonce': n, **over}


class GoogleStub:
    """Answers the token request. `claims` is what the id token says; `status` and `raw` let a test break the reply."""

    def __init__(self):
        self.claims, self.status, self.raw, self.delay, self.seen = {}, 200, None, 0, []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                form = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
                stub.seen.append({'path': self.path, 'form': {k: v[0] for k, v in form.items()}, 'type': self.headers.get('Content-Type')})
                time.sleep(stub.delay)
                body = stub.raw if stub.raw is not None else json.dumps(
                    {'access_token': 'at-secret', 'id_token': f'{b64({"alg": "RS256"})}.{b64(stub.claims)}.{b64(b"sig")}'}).encode()
                self.send_response(stub.status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        class Quiet(HTTPServer):
            def handle_error(self, request, client_address):  # a client that gave up (timeout test) is not an error here
                pass

        self.server = Quiet(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}/token'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def google():
    stub = GoogleStub()
    yield stub
    stub.close()


@pytest.fixture
def gapp(tmp_path, google):
    app = make_app(tmp_path, GOOGLE_CLIENT_ID=CID, GOOGLE_CLIENT_SECRET=SECRET, GOOGLE_ALLOWED=frozenset({OK}),
                   GOOGLE_TOKEN_URL=google.url, GOOGLE_TIMEOUT=2)
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


def admins(app):
    with app.app_context():
        return Admin.query.count()


def begin(c):
    """Click 'Continue with Google'. Returns the query Google would receive."""
    r = c.get('/admin/google/start')
    assert r.status_code == 302 and r.headers['Location'].startswith(AUTH_URL), r.headers.get('Location')
    return {k: v[0] for k, v in parse_qs(urlparse(r.headers['Location']).query).items()}


def come_back(c, google, query, state=None, **claims):
    google.claims = good_claims(query['nonce'], **claims)
    return c.get('/admin/google/callback', query_string={'code': 'the-code', 'state': state or query['state']})


def proven(app, google, **claims):
    """A browser that came back from Google with an allowed, verified email and now sits at the finish page."""
    c = app.test_client()
    r = come_back(c, google, begin(c), **claims)
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/signup/finish'), r.headers.get('Location')
    return c


def register(app, google, **form):
    """Whole sign-up up to the forced set-up page. Returns the browser, which holds only the half-signed-in state."""
    c = proven(app, google)
    r = c.post('/admin/signup/finish', data={**FORM, **form})
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/2fa/setup'), r.get_data(as_text=True)[:300]
    return c


def secret_of(app, username='dr-new'):
    with app.app_context():
        return twofactor.unseal(Admin.query.filter_by(username=username).one().totp_secret, app.config['SECRET_KEY'])


def kit_of(page):
    m = re.search(r'<pre class="kit-text">(.*?)</pre>', page, re.S)
    assert m, 'no recovery kit on the page'
    return htmllib.unescape(m.group(1))


def flashes(c):
    with c.session_transaction() as s:
        return [m for _, m in s.get('_flashes', [])]


# ---- present at boot -----------------------------------------------------------------------------------------------------

def test_sign_up_is_on_offer_at_boot_with_no_mail_and_no_google_settings(tmp_path):
    app = make_app(tmp_path, MAIL_BACKEND='')
    c = app.test_client()
    assert '/admin/signup' in c.get('/admin/login').get_data(as_text=True)
    assert '/admin/signup' in c.get('/').get_data(as_text=True)
    r = c.get('/admin/signup')
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and 'not set up' in page and '/admin/google/start' not in page


def test_the_owner_can_switch_sign_up_off(tmp_path):
    app = make_app(tmp_path, SIGNUP_ENABLED=False)
    c = app.test_client()
    assert '/admin/signup' not in c.get('/admin/login').get_data(as_text=True)
    r = c.get('/admin/signup')
    assert r.status_code == 404 and 'Sign-up is closed' in r.get_data(as_text=True)
    r = c.get('/admin/google/start')
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/signup')  # never on to Google
    with c.session_transaction() as s:
        assert 'gstate' not in s
    assert c.get('/admin/signup/finish').status_code == 302 and admins(app) == 1


def test_the_page_offers_google_once_it_is_set_up(gapp):
    page = gapp.test_client().get('/admin/signup').get_data(as_text=True)
    assert '/admin/google/start' in page and 'not set up' not in page


@pytest.mark.parametrize('missing', ['GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_ALLOWED'])
def test_a_half_set_up_server_still_boots_names_what_is_missing_and_offers_no_button(tmp_path, caplog, missing):
    cfg = dict(GOOGLE_CLIENT_ID=CID, GOOGLE_CLIENT_SECRET=SECRET, GOOGLE_ALLOWED=frozenset({OK}))
    cfg[missing] = frozenset() if missing == 'GOOGLE_ALLOWED' else ''
    with caplog.at_level(logging.WARNING):
        app = make_app(tmp_path, **cfg)
    names = {'GOOGLE_CLIENT_ID': 'ORS_SRS_GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET': 'ORS_SRS_GOOGLE_CLIENT_SECRET',
             'GOOGLE_ALLOWED': 'ORS_SRS_GOOGLE_ALLOWED_EMAILS'}
    assert names[missing] in caplog.text and CID not in caplog.text and SECRET not in caplog.text and OK not in caplog.text
    page = app.test_client().get('/admin/signup').get_data(as_text=True)
    assert 'not set up' in page and '/admin/google/start' not in page


# ---- start ---------------------------------------------------------------------------------------------------------------

def test_start_sends_the_browser_to_google_with_a_fresh_state_and_nonce(gapp):
    c = gapp.test_client()
    q = begin(c)
    assert (q['client_id'], q['response_type'], q['scope'], q['prompt']) == (CID, 'code', 'openid email', 'select_account')
    assert q['redirect_uri'] == 'http://localhost/admin/google/callback'
    assert len(q['state']) >= 32 and len(q['nonce']) >= 32 and q['state'] != q['nonce']
    assert SECRET not in c.get('/admin/google/start').headers['Location']
    q2 = begin(gapp.test_client())
    assert q2['state'] != q['state'] and q2['nonce'] != q['nonce']


def test_start_does_nothing_when_google_is_not_set_up(app):
    c = app.test_client()
    r = c.get('/admin/google/start')
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/signup')
    with c.session_transaction() as s:
        assert 'gstate' not in s


# ---- callback: every way it must refuse ---------------------------------------------------------------------------------

def refused(app, c, r, where='/admin/signup', count=1):
    assert r.status_code == 302 and r.headers['Location'].endswith(where), r.headers.get('Location')
    with c.session_transaction() as s:
        assert 'gsignup' not in s
    assert admins(app) == count


def test_a_callback_without_a_start_is_refused(gapp):
    c = gapp.test_client()
    refused(gapp, c, c.get('/admin/google/callback', query_string={'code': 'x', 'state': 'y'}))


def test_a_wrong_state_is_refused_and_so_is_a_replayed_one(gapp, google):
    c = gapp.test_client()
    q = begin(c)
    refused(gapp, c, come_back(c, google, q, state='not-the-state'))
    c = gapp.test_client()
    q = begin(c)
    assert come_back(c, google, q).headers['Location'].endswith('/admin/signup/finish')
    c2 = gapp.test_client()  # a second browser replaying the first browser's state has none of its own
    refused(gapp, c2, c2.get('/admin/google/callback', query_string={'code': 'x', 'state': q['state']}))
    with c.session_transaction() as s:
        s.pop('gsignup')
    refused(gapp, c, come_back(c, google, q))  # the same state a second time: it was used up


def test_a_sign_up_started_before_the_owner_closed_sign_up_is_refused_on_return(gapp, google):
    c = gapp.test_client()
    q = begin(c)
    gapp.config['SIGNUP_ENABLED'] = False
    refused(gapp, c, come_back(c, google, q))


def test_an_expired_state_is_refused(gapp, google):
    c = gapp.test_client()
    q = begin(c)
    with c.session_transaction() as s:
        s['gstate'] = {**s['gstate'], 'exp': int(time.time()) - 1}
    refused(gapp, c, come_back(c, google, q))


def test_cancelling_at_google_says_so_and_stores_nothing(gapp):
    c = gapp.test_client()
    begin(c)
    r = c.get('/admin/google/callback', query_string={'error': 'access_denied', 'state': 'x'})
    refused(gapp, c, r)
    assert 'cancelled' in ' '.join(flashes(c))


BAD_CLAIMS = [
    ('wrong issuer', {'iss': 'https://evil.example'}),
    ('wrong audience', {'aud': 'someone-elses-client'}),
    ('expired', {'exp': 1}),
    ('wrong nonce', {'nonce': 'not-the-nonce'}),
    ('email not verified', {'email_verified': False}),
    ('email_verified as the string false', {'email_verified': 'false'}),
    ('no email_verified', {'email_verified': None}),
    ('no subject', {'sub': ''}),
    ('no email', {'email': ''}),
]


@pytest.mark.parametrize('why,claims', BAD_CLAIMS, ids=[b[0] for b in BAD_CLAIMS])
def test_an_id_token_that_fails_any_check_is_refused(gapp, google, why, claims):
    c = gapp.test_client()
    q = begin(c)
    google.claims = good_claims(q['nonce'], **claims)
    google.claims = {k: v for k, v in google.claims.items() if v is not None}
    r = c.get('/admin/google/callback', query_string={'code': 'the-code', 'state': q['state']})
    refused(gapp, c, r)
    assert 'did not work' in ' '.join(flashes(c))


@pytest.mark.parametrize('iss', ['https://accounts.google.com', 'accounts.google.com'])
def test_both_google_issuer_spellings_and_a_true_string_are_accepted(gapp, google, iss):
    c = gapp.test_client()
    r = come_back(c, google, begin(c), iss=iss, email_verified='true')
    assert r.headers['Location'].endswith('/admin/signup/finish')


def test_an_email_that_is_not_on_the_list_is_refused_with_a_message_that_says_to_ask_the_owner(gapp, google):
    c = gapp.test_client()
    r = come_back(c, google, begin(c), email='stranger@example.test')
    refused(gapp, c, r)
    assert 'not on the list' in ' '.join(flashes(c))


def test_the_list_is_not_case_sensitive(gapp, google):
    c = gapp.test_client()
    assert come_back(c, google, begin(c), email='OK@Example.Test').headers['Location'].endswith('/admin/signup/finish')


@pytest.mark.parametrize('how', ['server error', 'not json', 'no id token', 'garbage id token', 'slow', 'nothing listening'])
def test_a_broken_token_exchange_is_refused_and_leaks_nothing(gapp, google, caplog, how):
    if how == 'server error':
        google.status = 500
    elif how == 'not json':
        google.raw = b'<html>nope</html>'
    elif how == 'no id token':
        google.raw = json.dumps({'access_token': 'at-secret'}).encode()
    elif how == 'garbage id token':
        google.raw = json.dumps({'id_token': 'a.b'}).encode()
    elif how == 'slow':
        gapp.config['GOOGLE_TIMEOUT'], google.delay = 0.3, 1.5
    else:
        gapp.config['GOOGLE_TOKEN_URL'] = 'http://127.0.0.1:1/token'
    c = gapp.test_client()
    q = begin(c)
    google.claims = good_claims(q['nonce'])
    with caplog.at_level(logging.DEBUG):
        r = c.get('/admin/google/callback', query_string={'code': 'the-code', 'state': q['state']})
    refused(gapp, c, r)
    assert 'did not work' in ' '.join(flashes(c))
    for secret in (SECRET, 'at-secret', 'the-code', OK, q['state'], q['nonce']):
        assert secret not in caplog.text


def test_the_token_request_is_a_form_post_with_the_code_secret_and_redirect(gapp, google):
    c = gapp.test_client()
    q = begin(c)
    assert come_back(c, google, q).status_code == 302
    seen = google.seen[0]
    assert seen['type'].startswith('application/x-www-form-urlencoded')
    assert seen['form'] == {'code': 'the-code', 'client_id': CID, 'client_secret': SECRET,
                            'redirect_uri': 'http://localhost/admin/google/callback', 'grant_type': 'authorization_code'}


def test_a_google_account_that_already_has_an_administrator_is_sent_to_sign_in(gapp, google):
    register(gapp, google)
    c = gapp.test_client()
    r = come_back(c, google, begin(c))
    refused(gapp, c, r, where='/admin/login', count=2)  # the owner and the first sign-up
    assert 'already has an account' in ' '.join(flashes(c))


# ---- the finish page ----------------------------------------------------------------------------------------------------

def test_a_proven_browser_sees_the_finish_page_with_the_email_masked_and_no_account_yet(gapp, google):
    c = proven(gapp, google)
    page = c.get('/admin/signup/finish').get_data(as_text=True)
    assert 'o***@example.test' in page and OK not in page and 'name="password"' in page
    assert admins(gapp) == 1


def test_the_finish_page_needs_the_proof_and_the_proof_expires(gapp, google):
    anon = gapp.test_client()
    for method in (anon.get, anon.post):
        r = method('/admin/signup/finish', data=FORM)
        assert r.status_code == 302 and r.headers['Location'].endswith('/admin/signup')
    c = proven(gapp, google)
    with c.session_transaction() as s:
        s['gsignup'] = {**s['gsignup'], 'exp': int(time.time()) - 1}
    r = c.post('/admin/signup/finish', data=FORM)
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/signup') and admins(gapp) == 1


@pytest.mark.parametrize('over,message', [
    (dict(password='short', confirm='short'), 'at least'),
    (dict(confirm='something else entirely'), 'differ'),
    (dict(username='x'), 'ID must be'),
    (dict(username='bad id!'), 'ID must be'),
    (dict(username='admin'), 'already exists'),
    (dict(username=' ADMIN '), 'already exists'),
])
def test_a_bad_finish_keeps_the_proof_and_the_typed_id_but_never_the_password(gapp, google, over, message):
    c = proven(gapp, google)
    r = c.post('/admin/signup/finish', data={**FORM, **over})
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and message in page and admins(gapp) == 1
    assert PASSWORD not in page and over.get('confirm', 'zzz-none') not in page.replace('differ', '')
    if 'username' in over:
        assert htmllib.escape(over['username'].strip()) in page
    assert c.post('/admin/signup/finish', data=FORM).status_code == 302  # still proven: a retry works


def test_the_id_is_case_and_space_insensitive(gapp, google):
    register(gapp, google, username='  Dr-New ')
    with gapp.app_context():
        assert Admin.query.filter_by(username='dr-new').count() == 1


# ---- what the new administrator is, and the forced two-step set-up -----------------------------------------------------

def test_finish_creates_an_active_non_owner_linked_to_google_with_nothing_else_set(gapp, google):
    register(gapp, google)
    with gapp.app_context():
        a = Admin.query.filter_by(username='dr-new').one()
        assert (a.status, a.is_owner, a.email, a.pending_email) == ('active', False, None, None)
        assert a.recovery_hash is None and a.totp_enabled_at is None and a.backup_hashes is None
        ident = a.identities[0]
        assert (ident.provider, ident.subject, ident.email) == ('google', 'g-1', OK) and len(a.identities) == 1
        assert a.password_hash != PASSWORD and a.password_hash


def test_nothing_opens_until_the_authenticator_app_is_set_up(gapp, google):
    c = register(gapp, google)
    with c.session_transaction() as s:
        assert 'aid' not in s and 'stamp' not in s and s['pre']['purpose'] == 'enroll' and 'gsignup' not in s
    page = c.get('/admin/2fa/setup').get_data(as_text=True)
    assert 'data:image/svg+xml;base64,' in page and '<style' not in page and 'style="' not in page
    assert c.get('/api/v1/me').status_code == 401
    for url in GUARDED:  # last: any other admin page clears the half-signed-in state
        r = c.get(url)
        assert r.status_code == 302 and '/admin/login' in r.headers['Location'], url


def test_the_app_code_turns_on_two_step_and_shows_ten_backup_codes_and_a_recovery_code_once(gapp, google):
    c = register(gapp, google)
    c.get('/admin/2fa/setup')
    r = c.post('/admin/2fa/setup', data={'code': '000000'})
    with c.session_transaction() as s:
        assert r.status_code == 200 and 'aid' not in s
    page = c.post('/admin/2fa/setup', data={'code': totp_now(secret_of(gapp))}, follow_redirects=True).get_data(as_text=True)
    kit = kit_of(page)
    assert len(set(CODE_RE.findall(kit))) == 10 and len(RECOVERY_RE.findall(kit)) == 1 and 'dr-new' in kit
    assert 'data:text/plain' in page or 'download=' in page  # the downloadable file
    assert c.get('/admin').status_code == 200
    assert 'kit-text' not in c.get('/admin').get_data(as_text=True)  # shown once
    with gapp.app_context():
        a = Admin.query.filter_by(username='dr-new').one()
        assert a.totp_enabled_at and a.recovery_hash and len([h for h in a.backup_hashes.split(',') if h]) == 10


def test_afterwards_sign_in_is_id_password_and_a_code_and_a_backup_code_works_once(gapp, google):
    c = register(gapp, google)
    c.get('/admin/2fa/setup')
    kit = kit_of(c.post('/admin/2fa/setup', data={'code': totp_now(secret_of(gapp))}, follow_redirects=True).get_data(as_text=True))
    backup = CODE_RE.findall(kit)[0]
    n, r = login_as(gapp, 'dr-new')
    assert r.headers['Location'].endswith('/admin/2fa')
    assert n.post('/admin/2fa', data={'code': backup}).status_code == 302 and n.get('/admin').status_code == 200
    n2, _ = login_as(gapp, 'dr-new')
    assert n2.post('/admin/2fa', data={'code': backup}).status_code == 200  # spent: the code page again
    with n2.session_transaction() as s:
        assert 'aid' not in s


def test_leaving_the_set_up_unfinished_forces_it_again_at_the_next_sign_in(tmp_path, google):
    app = make_app(tmp_path, GOOGLE_CLIENT_ID=CID, GOOGLE_CLIENT_SECRET=SECRET, GOOGLE_ALLOWED=frozenset({OK}),
                   GOOGLE_TOKEN_URL=google.url, REQUIRE_2FA=True)
    register(app, google)  # closes the tab here
    c, r = login_as(app, 'dr-new')
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/2fa/setup')
    with c.session_transaction() as s:
        assert 'aid' not in s


def test_set_up_is_forced_even_where_the_host_turned_enforcement_off(gapp, google):
    gapp.config['REQUIRE_2FA'] = False  # a development setting; a sign-up still goes through set-up
    c = proven(gapp, google)
    assert c.post('/admin/signup/finish', data=FORM).headers['Location'].endswith('/admin/2fa/setup')


def test_the_new_administrator_sees_none_of_the_owners_clients(gapp, google, admin):
    cid = add_client(admin)
    c = register(gapp, google)
    c.get('/admin/2fa/setup')
    c.post('/admin/2fa/setup', data={'code': totp_now(secret_of(gapp))})
    page = c.get('/admin')
    assert page.status_code == 200 and 'Patel' not in page.get_data(as_text=True)
    assert c.get(f'/admin/client/{cid}').status_code == 404


# ---- simultaneous clicks and the schema -----------------------------------------------------------------------------------

def stamp_proof(c, sub='g-1'):
    with c.session_transaction() as s:
        s['gsignup'] = {'sub': sub, 'email': OK, 'exp': int(time.time()) + 600}


def test_two_finishes_with_one_google_account_make_one_administrator(gapp):
    browsers = [gapp.test_client() for _ in range(2)]
    for b in browsers:
        stamp_proof(b)
    res = race(2, lambda i: browsers[i].post('/admin/signup/finish', data={**FORM, 'username': f'dr-{i}'}))
    assert sorted(r.status_code for r in res) == [200, 302]  # one goes on to set-up, the other is told the account exists
    with gapp.app_context():
        assert AdminIdentity.query.count() == 1 and Admin.query.count() == 2  # the owner and exactly one new one


def test_two_finishes_with_one_id_make_one_administrator(gapp):
    browsers = [gapp.test_client() for _ in range(2)]
    for i, b in enumerate(browsers):
        stamp_proof(b, sub=f'g-{i}')
    res = race(2, lambda i: browsers[i].post('/admin/signup/finish', data=FORM))
    assert all(r.status_code in (200, 302) for r in res)
    with gapp.app_context():
        assert Admin.query.filter_by(username='dr-new').count() == 1 and Admin.query.count() == 2


def test_create_admin_links_the_identity_in_the_same_commit_or_not_at_all(app):
    with app.app_context():
        ident = {'provider': 'google', 'subject': 's-1', 'email': OK}
        a, code = create_admin('first', PASSWORD, status='active', with_code=False, identity=ident)
        assert code is None and a.recovery_hash is None and a.identities[0].subject == 's-1'
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            create_admin('second', PASSWORD, status='active', with_code=False, identity=ident)
        db.session.rollback()
        assert Admin.query.filter_by(username='second').count() == 0  # no half-made administrator
        b, code = create_admin('third', PASSWORD)  # the old behaviour is untouched
        assert code and b.recovery_hash and b.identities == []


def test_deleting_an_administrator_removes_the_google_link(app):
    with app.app_context():
        a, _ = create_admin('gone', PASSWORD, with_code=False, identity={'provider': 'google', 'subject': 's-9', 'email': OK})
        db.session.delete(a)
        db.session.commit()
        assert AdminIdentity.query.count() == 0


def test_the_new_pages_use_no_inline_style_script_or_handler(gapp, google):
    pages = [gapp.test_client().get('/admin/signup').get_data(as_text=True),
             proven(gapp, google).get('/admin/signup/finish').get_data(as_text=True)]
    for page in pages:
        assert '<style' not in page and 'style="' not in page and not re.search(r'\bon[a-z]+=', page)
        assert not re.search(r'<script(?![^>]*\bsrc=)', page)
