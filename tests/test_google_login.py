"""Signing in with Google for an administrator who signed up with it. Google replaces only the password: the authenticator
app (or a backup code) is still asked for, a disabled account is still refused, and this route never creates an account."""
import logging
import time
from urllib.parse import parse_qs, urlparse

import pytest

from app import Admin, db
from conftest import make_app, totp_now

from test_google_signup import (AUTH_URL, BAD_CLAIMS, CID, CODE_RE, OK, SECRET, admins, begin, come_back, flashes,  # noqa: F401
                                gapp, good_claims, google, kit_of, register, secret_of)


def begin_login(c):
    """Click 'Sign in with Google'. Returns the query Google would receive."""
    r = c.get('/admin/google/login')
    assert r.status_code == 302 and r.headers['Location'].startswith(AUTH_URL), r.headers.get('Location')
    return {k: v[0] for k, v in parse_qs(urlparse(r.headers['Location']).query).items()}


def signed_up(app, google):
    """'dr-new', signed up with Google and with the authenticator app on. Returns (app secret, backup codes)."""
    c = register(app, google)
    c.get('/admin/2fa/setup')
    page = c.post('/admin/2fa/setup', data={'code': totp_now(secret_of(app))}, follow_redirects=True).get_data(as_text=True)
    with app.app_context():  # the set-up code used this 30 s step; a sign-in right after needs the step to be free again
        Admin.query.filter_by(username='dr-new').one().totp_last_step = 0
        db.session.commit()
    return secret_of(app), CODE_RE.findall(kit_of(page))


def google_login(app, google, **claims):
    c = app.test_client()
    return c, come_back(c, google, begin_login(c), **claims)


# ---- the button and the start -----------------------------------------------------------------------------------------------

def test_the_sign_in_page_offers_google_only_when_it_is_set_up(app, gapp):
    assert 'Sign in with Google' not in app.test_client().get('/admin/login').get_data(as_text=True)
    page = gapp.test_client().get('/admin/login').get_data(as_text=True)
    assert 'Sign in with Google' in page and '/admin/google/login' in page
    assert '<style' not in page and 'style="' not in page


@pytest.mark.parametrize('over', [{'SIGNUP_ENABLED': False}, {'GOOGLE_ALLOWED': frozenset()}], ids=['sign-up closed', 'empty list'])
def test_signing_in_needs_only_the_client_id_and_secret_not_the_sign_up_settings(tmp_path, google, over):
    app = make_app(tmp_path, GOOGLE_CLIENT_ID=CID, GOOGLE_CLIENT_SECRET=SECRET, GOOGLE_TOKEN_URL=google.url,
                   **{'GOOGLE_ALLOWED': frozenset({OK}), **over})
    c = app.test_client()
    assert 'Sign in with Google' in c.get('/admin/login').get_data(as_text=True)
    assert begin_login(c)['client_id'] == CID


def test_a_half_set_up_server_offers_no_google_sign_in(tmp_path):
    app = make_app(tmp_path, GOOGLE_CLIENT_ID=CID, GOOGLE_CLIENT_SECRET='', GOOGLE_ALLOWED=frozenset({OK}))
    c = app.test_client()
    assert 'Sign in with Google' not in c.get('/admin/login').get_data(as_text=True)
    r = c.get('/admin/google/login')
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/login')
    with c.session_transaction() as s:
        assert 'gstate' not in s


def test_the_start_goes_to_google_and_remembers_in_the_session_that_this_is_a_sign_in(gapp):
    c = gapp.test_client()
    q = begin_login(c)
    assert (q['client_id'], q['response_type'], q['scope'], q['prompt']) == (CID, 'code', 'openid email', 'select_account')
    assert q['redirect_uri'] == 'http://localhost/admin/google/callback' and 'purpose' not in q and SECRET not in str(q)
    with c.session_transaction() as s:
        assert s['gstate']['purpose'] == 'login' and s['gstate']['state'] == q['state']
    begin(c)  # the sign-up button
    with c.session_transaction() as s:
        assert s['gstate']['purpose'] == 'signup'


# ---- who gets in ---------------------------------------------------------------------------------------------------------------

def test_a_returning_admin_passes_the_first_step_with_google_but_still_needs_the_app_code(gapp, google):
    secret, _ = signed_up(gapp, google)
    c, r = google_login(gapp, google)
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/2fa')
    with c.session_transaction() as s:
        assert 'aid' not in s and s['pre']['purpose'] == 'login' and s['pre']['method'] == 'totp' and 'gsignup' not in s
    assert c.post('/admin/2fa', data={'code': '000000'}).status_code == 200  # a wrong code opens nothing
    r = c.post('/admin/2fa', data={'code': totp_now(secret)})
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin') and c.get('/admin').status_code == 200
    with gapp.app_context():
        assert Admin.query.filter_by(username='dr-new').one().last_login_at is not None


def test_a_backup_code_works_as_the_second_step_after_google_and_only_once(gapp, google):
    _, backups = signed_up(gapp, google)
    c, r = google_login(gapp, google)
    assert c.post('/admin/2fa', data={'code': backups[0]}).headers['Location'].endswith('/admin')
    c2, _ = google_login(gapp, google)
    assert c2.post('/admin/2fa', data={'code': backups[0]}).status_code == 200  # spent
    with c2.session_transaction() as s:
        assert 'aid' not in s


def test_nothing_opens_between_google_and_the_app_code(gapp, google):
    signed_up(gapp, google)
    c, _ = google_login(gapp, google)
    assert c.get('/api/v1/me').status_code == 401
    for url in ('/admin/account', '/admin/recent', '/admin/export-all', '/admin/account/totp'):
        r = c.get(url)
        assert r.status_code == 302 and '/admin/login' in r.headers['Location'], url


def test_an_admin_who_never_finished_set_up_is_sent_back_to_it_after_google(tmp_path, google):
    app = make_app(tmp_path, GOOGLE_CLIENT_ID=CID, GOOGLE_CLIENT_SECRET=SECRET, GOOGLE_ALLOWED=frozenset({OK}),
                   GOOGLE_TOKEN_URL=google.url, REQUIRE_2FA=True)
    register(app, google)  # closes the tab at the set-up page
    c, r = google_login(app, google)
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/2fa/setup')
    with c.session_transaction() as s:
        assert 'aid' not in s and s['pre']['purpose'] == 'enroll'


def test_a_disabled_admin_is_refused_with_no_session_and_no_half_signed_in_state(gapp, google):
    signed_up(gapp, google)
    with gapp.app_context():
        Admin.query.filter_by(username='dr-new').one().status = 'disabled'
        db.session.commit()
    c, r = google_login(gapp, google)
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/login')
    with c.session_transaction() as s:
        assert 'aid' not in s and 'pre' not in s
    assert 'disabled' in c.get('/admin/login').get_data(as_text=True)


def test_a_google_account_with_no_administrator_creates_nothing_even_when_it_is_on_the_list(gapp, google):
    c, r = google_login(gapp, google)  # OK is on the allow-list, but nobody signed up with it
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/login')
    assert 'No portal account is linked' in ' '.join(flashes(c)) and admins(gapp) == 1
    with c.session_transaction() as s:
        assert 'aid' not in s and 'pre' not in s and 'gsignup' not in s


def test_the_allow_list_and_the_sign_up_switch_gate_sign_up_only_not_an_existing_admin(gapp, google):
    signed_up(gapp, google)
    gapp.config.update(GOOGLE_ALLOWED=frozenset({'someone-else@example.test'}), SIGNUP_ENABLED=False)
    c, r = google_login(gapp, google)
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/2fa')


def test_a_state_started_for_sign_in_cannot_be_turned_into_a_sign_up(gapp, google):
    c = gapp.test_client()
    q = begin_login(c)
    r = come_back(c, google, q)  # an allowed, verified Google account with no administrator
    assert r.headers['Location'].endswith('/admin/login') and admins(gapp) == 1
    with c.session_transaction() as s:
        assert 'gsignup' not in s
    assert c.get('/admin/signup/finish').headers['Location'].endswith('/admin/signup')


# ---- every way the return from Google can be refused ----------------------------------------------------------------------------

def refused_login(app, c, r):
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/login'), r.headers.get('Location')
    with c.session_transaction() as s:
        assert 'aid' not in s and 'pre' not in s and 'gsignup' not in s


@pytest.mark.parametrize('why,claims', BAD_CLAIMS, ids=[b[0] for b in BAD_CLAIMS])
def test_an_id_token_that_fails_any_check_does_not_sign_anyone_in(gapp, google, why, claims):
    signed_up(gapp, google)
    c = gapp.test_client()
    q = begin_login(c)
    google.claims = {k: v for k, v in good_claims(q['nonce'], **claims).items() if v is not None}
    r = c.get('/admin/google/callback', query_string={'code': 'the-code', 'state': q['state']})
    refused_login(gapp, c, r)
    assert 'did not work' in ' '.join(flashes(c))


def test_a_wrong_replayed_or_expired_state_does_not_sign_anyone_in(gapp, google):
    signed_up(gapp, google)
    c = gapp.test_client()
    refused_login(gapp, c, come_back(c, google, begin_login(c), state='not-the-state'))
    c = gapp.test_client()
    q = begin_login(c)
    assert come_back(c, google, q).headers['Location'].endswith('/admin/2fa')
    d = gapp.test_client()  # another browser replaying that state has none of its own; with no purpose it takes the sign-up path
    r = d.get('/admin/google/callback', query_string={'code': 'x', 'state': q['state']})
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/signup')
    with d.session_transaction() as s:
        assert 'aid' not in s and 'pre' not in s and 'gsignup' not in s
    e = gapp.test_client()
    q = begin_login(e)
    with e.session_transaction() as s:
        s['gstate'] = {**s['gstate'], 'exp': int(time.time()) - 1}
    refused_login(gapp, e, come_back(e, google, q))


def test_cancelling_at_google_returns_to_sign_in_with_a_message(gapp):
    c = gapp.test_client()
    begin_login(c)
    r = c.get('/admin/google/callback', query_string={'error': 'access_denied', 'state': 'x'})
    refused_login(gapp, c, r)
    assert 'cancelled' in ' '.join(flashes(c))


@pytest.mark.parametrize('how', ['server error', 'not json', 'nothing listening'])
def test_a_broken_token_exchange_signs_nobody_in_and_leaks_nothing(gapp, google, caplog, how):
    signed_up(gapp, google)
    if how == 'server error':
        google.status = 500
    elif how == 'not json':
        google.raw = b'<html>nope</html>'
    else:
        gapp.config['GOOGLE_TOKEN_URL'] = 'http://127.0.0.1:1/token'
    c = gapp.test_client()
    q = begin_login(c)
    google.claims = good_claims(q['nonce'])
    with caplog.at_level(logging.DEBUG):
        r = c.get('/admin/google/callback', query_string={'code': 'the-code', 'state': q['state']})
    refused_login(gapp, c, r)
    for secret in (SECRET, 'at-secret', 'the-code', OK, q['state'], q['nonce']):
        assert secret not in caplog.text


def test_a_successful_sign_in_logs_nothing_secret_either(gapp, google, caplog):
    signed_up(gapp, google)
    with caplog.at_level(logging.DEBUG):
        c, r = google_login(gapp, google)
    assert r.headers['Location'].endswith('/admin/2fa')
    for secret in (SECRET, 'at-secret', 'the-code', OK):
        assert secret not in caplog.text


def test_password_sign_in_is_unchanged_for_the_same_administrator(gapp, google):
    from conftest import PASSWORD, login_as
    signed_up(gapp, google)
    c, r = login_as(gapp, 'dr-new', 'a password you did not choose')
    assert r.status_code == 200 and 'Incorrect administrator ID or password' in r.get_data(as_text=True)
    c, r = login_as(gapp, 'dr-new', PASSWORD)
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/2fa')
