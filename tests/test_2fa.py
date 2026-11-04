"""Second step at sign-in: emailed codes, the authenticator app, lockout, email changes."""
import re
import time
from datetime import timedelta

import pytest

import app as app_module
import twofactor
from app import Admin, db, utcnow
from conftest import PASSWORD, last_code, login_as, make_app, make_user, outbox, sign_in, totp_now

EMAIL = 'doc@example.test'


@pytest.fixture
def doc(app):
    """An active administrator with a confirmed email (so sign-in asks for a code)."""
    make_user(app, 'doc', email=EMAIL)


def admin_row(app, username='doc'):
    with app.app_context():
        a = Admin.query.filter_by(username=username).one()
        return {c.name: getattr(a, c.name) for c in Admin.__table__.columns}


def set_admin(app, username='doc', **fields):
    with app.app_context():
        a = Admin.query.filter_by(username=username).one()
        for k, v in fields.items():
            setattr(a, k, v)
        db.session.commit()


def first_step(app, username='doc'):
    c, r = login_as(app, username)
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/2fa'), r.get_data(as_text=True)[:200]
    return c


def second_step(c, code):
    return c.post('/admin/2fa', data={'code': code})


def full_login(app, username='doc', email=EMAIL):
    c = first_step(app, username)
    r = second_step(c, last_code(app, email))
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin')
    return c


GUARDED = ('/admin', '/admin/account', '/admin/recent', '/admin/export-all', '/admin/admins', '/admin/account/totp')


# ---- password alone is not enough --------------------------------------------------------------------------------------------

def test_admin_without_an_email_still_signs_in_with_the_password_alone(app):
    c = sign_in(app, 'admin')  # the seeded owner has no email yet
    with c.session_transaction() as s:
        assert set(s) - {'_flashes'} == {'aid', 'stamp', '_permanent'}


def test_after_the_password_the_browser_holds_no_session_and_nothing_opens(app, doc):
    c = first_step(app)
    with c.session_transaction() as s:
        assert 'aid' not in s and 'stamp' not in s and set(s['pre']) == {'aid', 'stamp', 'purpose', 'method', 'exp'}
    for url in GUARDED:
        r = c.get(url)
        assert r.status_code == 302 and '/admin/login' in r.headers['Location'], url
    assert c.post('/admin/clients', data={'first_name': 'A', 'last_name': 'B', 'age_band': 'adult'}).status_code == 302
    with app.app_context():
        assert app_module.Client.query.count() == 0
    assert c.get('/api/v1/me').status_code == 401  # a cookie never opens the API


def test_login_mails_a_six_digit_code_and_shows_the_code_page(app, doc):
    c = first_step(app)
    (mail,) = outbox(app)
    assert mail['to'] == EMAIL and re.fullmatch(r'Your ORS/SRS portal code: \d{6}', mail['subject']) and 'http' not in mail['body']
    page = c.get('/admin/2fa').get_data(as_text=True)
    assert 'd***@example.test' in page and 'one-time-code' in page and EMAIL not in page
    assert admin_row(app)['otp_purpose'] == 'login' and len(admin_row(app)['otp_hash']) == 64


def test_the_right_code_signs_in_and_is_spent(app, doc):
    c = first_step(app)
    code = last_code(app, EMAIL)
    r = second_step(c, code)
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin')
    with c.session_transaction() as s:
        assert set(s) - {'_flashes'} == {'aid', 'stamp', '_permanent'}
    assert c.get('/admin').status_code == 200
    a = admin_row(app)
    assert a['otp_hash'] is None and a['mfa_fails'] == 0 and a['last_login_at'] is not None
    replay = first_step(app)
    assert second_step(replay, code).status_code == 200 and replay.get('/admin').status_code == 302  # old code, new sign-in


def test_spaces_in_the_code_are_fine(app, doc):
    c = first_step(app)
    code = last_code(app, EMAIL)
    assert second_step(c, f'{code[:3]} {code[3:]}').status_code == 302


def test_wrong_code_five_times_kills_the_code(app, doc):
    c = first_step(app)
    good = last_code(app, EMAIL)
    bad = '000000' if good != '000000' else '111111'
    for _ in range(5):
        r = second_step(c, bad)
        assert r.status_code == 200 and 'not right or has expired' in r.get_data(as_text=True)
    assert second_step(c, good).status_code == 200 and c.get('/admin').status_code == 302
    assert admin_row(app)['mfa_fails'] == 6


def test_expired_code_is_refused(app, doc):
    c = first_step(app)
    set_admin(app, otp_expires_at=utcnow() - timedelta(seconds=1))
    assert second_step(c, last_code(app, EMAIL)).status_code == 200 and c.get('/admin').status_code == 302


def test_ten_wrong_codes_lock_the_second_step_even_for_the_right_one(app, doc):
    c = first_step(app)
    good = last_code(app, EMAIL)
    bad = '000000' if good != '000000' else '111111'
    for _ in range(10):
        second_step(c, bad)
    a = admin_row(app)
    assert a['locked_until'] and a['locked_until'] > utcnow() and a['mfa_fails'] == 0
    r = second_step(c, good)
    assert 'locked' in r.get_data(as_text=True) and c.get('/admin').status_code == 302
    set_admin(app, locked_until=utcnow() - timedelta(seconds=1), otp_tries=0)  # time passes
    assert second_step(c, good).status_code == 302


def test_resend_during_a_lock_sends_nothing(app, doc):
    c = first_step(app)
    set_admin(app, locked_until=utcnow() + timedelta(minutes=5))
    n = len(outbox(app))
    r = c.post('/admin/2fa/resend', follow_redirects=True)
    assert 'cannot be sent' in r.get_data(as_text=True) and len(outbox(app)) == n


def test_the_half_signed_in_state_dies_with_the_time_limit_and_with_a_password_change(app, doc):
    c = first_step(app)
    with c.session_transaction() as s:
        s['pre'] = {**s['pre'], 'exp': int(time.time()) - 1}
    assert c.get('/admin/2fa').status_code == 302
    c = first_step(app)
    set_admin(app, password_hash=app_module.generate_password_hash('a brand new password', method='pbkdf2:sha256:1000'))
    assert c.get('/admin/2fa').status_code == 302 and second_step(c, last_code(app, EMAIL)).status_code == 302
    with c.session_transaction() as s:
        assert 'aid' not in s


def test_a_forged_or_foreign_pre_cannot_finish_someone_elses_sign_in(app, doc):
    make_user(app, 'other', email='other@example.test')
    set_admin(app, 'other', password_hash=app_module.generate_password_hash('different password 1', method='pbkdf2:sha256:1000'))  # real hashes are salted, so stamps differ
    c = first_step(app)
    with c.session_transaction() as s:
        s['pre'] = {**s['pre'], 'aid': admin_row(app, 'other')['id']}  # stamp no longer matches that admin
    assert c.get('/admin/2fa').status_code == 302


@pytest.mark.parametrize('purpose', ['verify', 'enroll', 'anything'])
def test_a_half_signed_in_state_made_for_another_purpose_cannot_be_used_at_the_login_step(app, purpose):
    c = app.test_client()
    with app.app_context():
        a = Admin.query.filter_by(username='admin').one()
        pre = {'aid': a.id, 'stamp': a.stamp, 'purpose': purpose, 'method': 'email', 'exp': int(time.time()) + 600}
    with c.session_transaction() as s:
        s['pre'] = pre
    assert c.get('/admin/2fa').status_code == 302
    assert c.post('/admin/2fa', data={'code': '123456'}).status_code == 302
    assert c.get('/admin').status_code == 302


def test_a_second_step_page_without_a_password_step_goes_to_sign_in(anon):
    assert anon.get('/admin/2fa').status_code == 302
    assert anon.post('/admin/2fa', data={'code': '123456'}).status_code == 302
    assert anon.post('/admin/2fa/resend').status_code == 302


def test_resend_waits_then_replaces_the_code(app, doc):
    c = first_step(app)
    old = last_code(app, EMAIL)
    c.post('/admin/2fa/resend')
    assert len(outbox(app)) == 1  # a code went out seconds ago
    set_admin(app, otp_expires_at=utcnow() + timedelta(minutes=8))
    c.post('/admin/2fa/resend')
    new = last_code(app, EMAIL)
    assert len(outbox(app)) == 2
    if new != old:
        assert second_step(c, old).status_code == 200
    assert second_step(c, new).status_code == 302


def test_if_mail_is_down_sign_in_is_refused_not_waved_through(app, doc, monkeypatch):
    def boom(*a, **k):
        raise app_module.mailer.MailError('smtp: down')
    monkeypatch.setattr(app_module.mailer, 'send', boom)
    c, r = login_as(app, 'doc')
    assert r.status_code == 200 and 'could not email' in r.get_data(as_text=True).lower()
    with c.session_transaction() as s:
        assert 'aid' not in s and 'pre' not in s
    assert admin_row(app)['otp_hash'] is None


def test_if_the_server_has_no_mail_an_admin_with_an_email_cannot_sign_in_with_a_password_alone(tmp_path):
    app = make_app(tmp_path, MAIL_BACKEND='')
    make_user(app, 'doc', email=EMAIL)
    c, r = login_as(app, 'doc')
    assert r.status_code == 200 and 'sign-in is paused' in r.get_data(as_text=True)
    with c.session_transaction() as s:
        assert 'aid' not in s and 'pre' not in s


def test_codes_never_reach_the_log(app, doc, caplog):
    first_step(app)
    assert last_code(app, EMAIL) not in caplog.text


def test_changing_the_password_voids_a_waiting_code(app, doc):
    set_admin(app, otp_hash='a' * 64, otp_purpose='login', otp_expires_at=utcnow() + timedelta(minutes=5))
    with app.app_context():
        app_module.reset_password(Admin.query.filter_by(username='doc').one(), 'another password 123')
    assert admin_row(app)['otp_hash'] is None


# ---- email under Account ----------------------------------------------------------------------------------------------------------

def test_owner_adds_and_proves_an_email_then_sign_in_asks_for_a_code(app):
    c = sign_in(app, 'admin')
    page = c.get('/admin/account').get_data(as_text=True)
    assert 'Two-step sign-in is off' in page
    r = c.post('/admin/account/email', data={'email': 'Owner@Example.test', 'current': PASSWORD})
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/verify')
    assert admin_row(app, 'admin')['email'] is None and admin_row(app, 'admin')['pending_email'] == 'owner@example.test'
    done = c.post('/admin/verify', data={'code': last_code(app, 'owner@example.test')}, follow_redirects=True)
    assert 'confirmed' in done.get_data(as_text=True) and admin_row(app, 'admin')['email'] == 'owner@example.test'
    again, r = login_as(app, 'admin')
    assert r.headers['Location'].endswith('/admin/2fa')


def test_email_change_needs_the_password_and_a_new_unique_address(app, doc):
    make_user(app, 'rival', email='rival@example.test')
    c = full_login(app)
    n = len(outbox(app))
    wrong = c.post('/admin/account/email', data={'email': 'new@example.test', 'current': 'nope nope nope'}, follow_redirects=True)
    assert 'current password was not correct' in wrong.get_data(as_text=True) and len(outbox(app)) == n
    for email, message in [('bad', 'valid email'), (EMAIL, 'already your email'), ('rival@example.test', 'already has an account')]:
        r = c.post('/admin/account/email', data={'email': email, 'current': PASSWORD}, follow_redirects=True)
        assert message in r.get_data(as_text=True)
    assert len(outbox(app)) == n and admin_row(app)['pending_email'] is None


def test_changing_the_email_keeps_the_old_one_until_the_new_is_proven_and_warns_the_old(app, doc):
    c = full_login(app)
    c.post('/admin/account/email', data={'email': 'new@example.test', 'current': PASSWORD})
    assert admin_row(app)['email'] == EMAIL  # still the old one
    other = first_step(app)  # sign-in still uses the old address meanwhile
    assert last_code(app, EMAIL) and other
    c.post('/admin/verify', data={'code': last_code(app, 'new@example.test')})
    assert admin_row(app)['email'] == 'new@example.test'
    warn = [m for m in outbox(app) if m['to'] == EMAIL and 'changed' in m['subject']]
    assert len(warn) == 1 and 'http' not in warn[0]['body']


def test_no_email_change_when_the_server_cannot_send_mail(tmp_path):
    app = make_app(tmp_path, MAIL_BACKEND='')
    c = sign_in(app, 'admin')
    r = c.post('/admin/account/email', data={'email': 'a@example.test', 'current': PASSWORD}, follow_redirects=True)
    assert 'cannot send email' in r.get_data(as_text=True) and admin_row(app, 'admin')['pending_email'] is None


# ---- authenticator app -------------------------------------------------------------------------------------------------------------

def enrol(app, c, username='doc'):
    """Walk through the setup page; returns the plain secret."""
    page = c.get('/admin/account/totp').get_data(as_text=True)
    assert 'data:image/svg+xml;base64,' in page
    with app.app_context():
        secret = twofactor.unseal(Admin.query.filter_by(username=username).one().totp_secret, app.config['SECRET_KEY'])
    r = c.post('/admin/account/totp', data={'code': totp_now(secret), 'current': PASSWORD})
    assert r.status_code == 302 and admin_row(app, username)['totp_enabled_at']
    return secret


def test_setup_page_shows_a_qr_and_key_and_stores_the_secret_sealed(app, doc):
    c = full_login(app)
    page = c.get('/admin/account/totp').get_data(as_text=True)
    assert 'data:image/svg+xml;base64,' in page and '<style' not in page and 'style="' not in page
    stored = admin_row(app)['totp_secret']
    with app.app_context():
        secret = twofactor.unseal(stored, app.config['SECRET_KEY'])
    assert secret and secret not in stored and ' '.join(secret[i:i + 4] for i in range(0, len(secret), 4)) in page
    assert admin_row(app)['totp_enabled_at'] is None  # not on until a code proves the phone has it
    assert c.get('/admin/account/totp').get_data(as_text=True).count(stored) == 0  # the page never shows the sealed form
    assert admin_row(app)['totp_secret'] == stored  # viewing again keeps the same secret


def test_enrolling_needs_the_password_and_a_right_code(app, doc):
    c = full_login(app)
    c.get('/admin/account/totp')
    with app.app_context():
        secret = twofactor.unseal(Admin.query.filter_by(username='doc').one().totp_secret, app.config['SECRET_KEY'])
    for data in ({'code': totp_now(secret), 'current': 'wrong password!'}, {'code': '000000', 'current': PASSWORD}):
        c.post('/admin/account/totp', data=data)
        assert admin_row(app)['totp_enabled_at'] is None
    assert c.post('/admin/account/totp', data={'code': totp_now(secret), 'current': PASSWORD}).status_code == 302
    assert admin_row(app)['totp_enabled_at'] and admin_row(app)['totp_last_step'] > 0


def test_sign_in_with_the_app_code_and_no_email_is_sent(app, doc):
    secret = enrol(app, full_login(app))
    n = len(outbox(app))
    c, r = login_as(app, 'doc')
    assert r.headers['Location'].endswith('/admin/2fa') and len(outbox(app)) == n
    with c.session_transaction() as s:
        assert s['pre']['method'] == 'totp' and 'aid' not in s
    assert 'authenticator app' in c.get('/admin/2fa').get_data(as_text=True)
    # the step used to enrol is spent, so wait for the next one
    set_admin(app, totp_last_step=admin_row(app)['totp_last_step'] - 1)
    assert second_step(c, totp_now(secret)).status_code == 302 and c.get('/admin').status_code == 200


def test_an_app_code_works_once(app, doc):
    secret = enrol(app, full_login(app))
    code = totp_now(secret)
    set_admin(app, totp_last_step=0)
    c, _ = login_as(app, 'doc')
    assert second_step(c, code).status_code == 302
    d, _ = login_as(app, 'doc')
    r = second_step(d, code)
    assert r.status_code == 200 and d.get('/admin').status_code == 302  # same code again: replay refused


def test_app_codes_accept_one_step_of_drift_but_not_two(app, doc, monkeypatch):
    secret = enrol(app, full_login(app))
    results = {}
    for offset in (-2, -1, 1, 2):
        set_admin(app, totp_last_step=0)
        c, _ = login_as(app, 'doc')
        results[offset] = second_step(c, totp_now(secret, offset)).status_code == 302
    assert results == {-2: False, -1: True, 1: True, 2: False}


def test_wrong_app_codes_are_counted_and_kill_the_challenge_after_five(app, doc):
    secret = enrol(app, full_login(app))
    set_admin(app, totp_last_step=0)
    c, _ = login_as(app, 'doc')
    for _ in range(5):
        assert second_step(c, '000000').status_code == 200
    assert second_step(c, totp_now(secret)).status_code == 200 and c.get('/admin').status_code == 302  # needs the password again
    d, _ = login_as(app, 'doc')
    assert second_step(d, totp_now(secret)).status_code == 302


def test_lost_phone_falls_back_to_an_emailed_code(app, doc):
    enrol(app, full_login(app))
    c, _ = login_as(app, 'doc')
    assert 'Email me a code instead' in c.get('/admin/2fa').get_data(as_text=True)
    c.post('/admin/2fa/resend')
    with c.session_transaction() as s:
        assert s['pre']['method'] == 'email'
    assert second_step(c, last_code(app, EMAIL)).status_code == 302 and c.get('/admin').status_code == 200


def test_turning_the_app_off_needs_password_and_code(app, doc):
    secret = enrol(app, full_login(app))
    c = full_login_after_totp(app, secret)
    assert 'Authenticator app: on' in c.get('/admin/account').get_data(as_text=True)
    c.post('/admin/account/totp/disable', data={'code': '000000', 'current': PASSWORD})
    c.post('/admin/account/totp/disable', data={'code': totp_now(secret, 1), 'current': 'wrong password!'})
    assert admin_row(app)['totp_enabled_at']
    c.post('/admin/account/totp/disable', data={'code': totp_now(secret, 1), 'current': PASSWORD})
    a = admin_row(app)
    assert a['totp_enabled_at'] is None and a['totp_secret'] is None and a['totp_last_step'] == 0


def full_login_after_totp(app, secret):
    set_admin(app, totp_last_step=0)
    c, _ = login_as(app, 'doc')
    assert second_step(c, totp_now(secret)).status_code == 302
    return c


def test_if_secret_key_changes_the_sealed_secret_is_unreadable_and_email_takes_over(tmp_path):
    app = make_app(tmp_path)
    make_user(app, 'doc', email=EMAIL)
    enrol(app, full_login(app))
    app.config['SECRET_KEY'] = 'a-different-secret-key-entirely'
    c, r = login_as(app, 'doc')
    assert r.headers['Location'].endswith('/admin/2fa')
    with c.session_transaction() as s:
        assert s['pre']['method'] == 'email'
    assert second_step(c, last_code(app, EMAIL)).status_code == 302


def test_sealed_secrets_round_trip_and_are_tied_to_the_key():
    secret = twofactor.new_secret()
    sealed = twofactor.seal(secret, 'k' * 20)
    assert twofactor.unseal(sealed, 'k' * 20) == secret and twofactor.unseal(sealed, 'j' * 20) is None
    assert twofactor.unseal(None, 'k' * 20) is None and twofactor.unseal('garbage', 'k' * 20) is None
    assert secret not in sealed


def test_provisioning_uri_and_qr_are_well_formed():
    uri = twofactor.provisioning_uri('JBSWY3DPEHPK3PXP', 'doc')
    assert uri.startswith('otpauth://totp/') and 'secret=JBSWY3DPEHPK3PXP' in uri and 'issuer=ORS%20SRS%20Portal' in uri and '/ORS%20SRS' in uri
    import base64
    img = twofactor.qr_data_uri(uri)
    svg = base64.b64decode(img.split(',', 1)[1]).decode()
    assert img.startswith('data:image/svg+xml;base64,') and '<svg' in svg and 'style=' not in svg and '<script' not in svg


@pytest.mark.parametrize('code, ok', [('123456', False), ('12345', False), ('', False), ('abcdef', False), ('1234567', False)])
def test_malformed_codes_never_match(code, ok):
    assert (twofactor.verify('JBSWY3DPEHPK3PXP', code) is not None) is ok


def test_verify_returns_the_step_and_refuses_it_again(monkeypatch):
    import pyotp
    secret, now = 'JBSWY3DPEHPK3PXP', 1_700_000_000
    code = pyotp.TOTP(secret).at(now)
    step = twofactor.verify(secret, code, 0, now)
    assert step == now // 30 and twofactor.verify(secret, code, step, now) is None
    assert twofactor.verify(secret, code, step - 1, now) == step


# ---- CLI escape hatch -----------------------------------------------------------------------------------------------------------------

def test_reset_2fa_removes_the_email_and_the_app(app, doc, monkeypatch, tmp_path):
    enrol(app, full_login(app))
    set_admin(app, locked_until=utcnow() + timedelta(minutes=5), mfa_fails=4)
    monkeypatch.setattr(app_module, 'admin_app', lambda: app.app_context())
    app_module.command_reset_2fa(['DOC'])
    a = admin_row(app)
    assert a['email'] is None and a['pending_email'] is None and a['totp_secret'] is None and a['totp_enabled_at'] is None
    assert a['locked_until'] is None and a['mfa_fails'] == 0 and a['otp_hash'] is None
    sign_in(app, 'doc')  # password alone works again
    with pytest.raises(SystemExit):
        app_module.command_reset_2fa([])
    with pytest.raises(SystemExit), monkeypatch.context() as m:
        m.setattr(app_module, 'admin_app', lambda: app.app_context())
        app_module.command_reset_2fa(['nobody'])


# ---- a half-signed-in browser may only do the one thing it was sent to do ---------------------------------------------------------

def test_a_password_only_browser_cannot_use_the_email_proof_pages(app, doc):
    c = full_login(app)
    c.post('/admin/account/email', data={'email': 'new@example.test', 'current': PASSWORD})  # a proof is now pending...
    code = last_code(app, 'new@example.test')
    other = first_step(app)  # ...and someone with only the password arrives at the sign-in step
    assert other.get('/admin/verify').status_code == 302 and '/admin/login' in other.get('/admin/verify').headers['Location']
    assert other.post('/admin/verify', data={'code': code}).status_code == 302
    assert admin_row(app)['email'] == EMAIL and admin_row(app)['pending_email'] == 'new@example.test'  # nothing was confirmed from there


def test_an_app_code_is_not_accepted_while_the_waiting_challenge_is_for_something_else(app, doc):
    secret = enrol(app, full_login(app))
    set_admin(app, totp_last_step=0)
    c, _ = login_as(app, 'doc')
    set_admin(app, otp_purpose='verify')  # e.g. an email proof was started meanwhile
    assert second_step(c, totp_now(secret)).status_code == 200 and c.get('/admin').status_code == 302
    set_admin(app, otp_purpose='login')
    assert second_step(c, totp_now(secret)).status_code == 302
