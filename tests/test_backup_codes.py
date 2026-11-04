"""Authenticator app without email: backup codes, the downloadable recovery kit, forced enrolment, fail-closed sign-in."""
import html as htmllib
import re
from urllib.parse import unquote

import pytest

import app as app_module
import twofactor
from app import Admin, db, utcnow
from conftest import PASSWORD, PASSWORD_HASH, enable_totp, login_as, make_app, make_user, sign_in, totp_now

CODE_RE = re.compile(r'\b[0-9a-f]{5}-[0-9a-f]{5}\b')
RECOVERY_RE = re.compile(r'\b[0-9a-f]{4}(?:-[0-9a-f]{4}){5}\b')


def kit_from(page):
    m = re.search(r'<pre class="kit-text">(.*?)</pre>', page, re.S)
    assert m, 'no recovery kit on the page'
    return htmllib.unescape(m.group(1))


def row(app, username='admin'):
    with app.app_context():
        a = Admin.query.filter_by(username=username).one()
        return {c.name: getattr(a, c.name) for c in Admin.__table__.columns}


def set_admin(app, username='admin', **fields):
    with app.app_context():
        a = Admin.query.filter_by(username=username).one()
        for k, v in fields.items():
            setattr(a, k, v)
        db.session.commit()


def two_step(c, code):
    return c.post('/admin/2fa', data={'code': code})


def at_code_page(app, username='admin'):
    c, r = login_as(app, username)
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/2fa'), r.get_data(as_text=True)[:300]
    return c


def signed_in(app, secret, username='admin'):
    """A browser signed in the full way: password, then a fresh app code."""
    set_admin(app, username, totp_last_step=0)
    c = at_code_page(app, username)
    assert two_step(c, totp_now(secret)).status_code == 302
    return c


@pytest.fixture
def with_app_on(app):
    """The seeded owner (no email, no mail server needed) with the authenticator app on. -> (codes, kit text, secret)."""
    secret, page = enable_totp(app, sign_in(app, 'admin'))
    kit = kit_from(page)
    return CODE_RE.findall(kit), kit, secret


# ---- the app no longer needs an email ---------------------------------------------------------------------------------------

def test_the_app_can_be_turned_on_with_no_email_and_no_mail_server(tmp_path):
    app = make_app(tmp_path, MAIL_BACKEND='')
    c = sign_in(app, 'admin')
    assert c.get('/admin/account/totp').status_code == 200
    enable_totp(app, c)
    a = row(app)
    assert a['email'] is None and a['totp_enabled_at'] and a['totp_secret']


def test_the_account_page_offers_the_app_when_there_is_no_email(app):
    page = sign_in(app, 'admin').get('/admin/account').get_data(as_text=True)
    assert '/admin/account/totp' in page and 'Set up an authenticator app' in page


# ---- backup codes and the recovery kit --------------------------------------------------------------------------------------

def test_turning_the_app_on_shows_ten_different_backup_codes_once(app, with_app_on):
    codes, kit, _ = with_app_on
    assert len(codes) == 10 and len(set(codes)) == 10
    c = at_code_page(app)  # the kit page was consumed by the redirect; a second look must show nothing
    two_step(c, codes[9])
    assert 'kit-text' not in c.get('/admin/account').get_data(as_text=True)


def test_only_keyed_hashes_of_the_backup_codes_are_stored(app, with_app_on):
    codes, kit, _ = with_app_on
    stored = row(app)['backup_hashes']
    hashes = stored.split(',')
    assert len(hashes) == 10 and all(re.fullmatch(r'[0-9a-f]{64}', h) for h in hashes)
    assert not any(code.replace('-', '') in stored for code in codes)
    with app.app_context():
        admin = Admin.query.filter_by(username='admin').one()
        assert app_module.otp_digest(admin, 'backup', codes[0].replace('-', '')) in hashes


def test_the_kit_is_a_download_and_tells_the_admin_to_keep_it_on_their_computer(app):
    c = sign_in(app, 'admin')
    _, page = enable_totp(app, c)
    kit = kit_from(page)
    link = re.search(r'<a [^>]*download="ors-srs-recovery-kit-admin\.txt"[^>]*href="data:text/plain;charset=utf-8,([^"]+)"', page) \
        or re.search(r'<a [^>]*href="data:text/plain;charset=utf-8,([^"]+)"[^>]*download="ors-srs-recovery-kit-admin\.txt"', page)
    assert link, 'no download button for the kit'
    assert unquote(link.group(1)) == kit  # the file is exactly the text on the page
    for needle in ('on your own computer', 'only once', 'Administrator ID: admin', 'http://localhost/admin/login',
                   'reset-2fa', 'Do not email'):
        assert needle in kit, needle
    assert all(code in kit for code in CODE_RE.findall(kit)) and len(CODE_RE.findall(kit)) == 10
    assert 'Download recovery kit' in page and 'data-copy' in page


def test_the_kit_carries_a_recovery_code_when_the_admin_has_none(app, with_app_on):
    codes, kit, _ = with_app_on  # the seeded owner starts with no recovery code
    (recovery,) = RECOVERY_RE.findall(kit)
    assert row(app)['recovery_hash'] == app_module.recovery_hash(recovery)


def test_the_kit_leaves_an_existing_recovery_code_alone(app):
    c = sign_in(app, 'admin')
    c.post('/admin/account/recovery', data={'current': PASSWORD})
    before = row(app)['recovery_hash']
    _, page = enable_totp(app, c)
    assert before and row(app)['recovery_hash'] == before and not RECOVERY_RE.findall(kit_from(page))


def test_a_backup_code_signs_in_once_and_only_once(app, with_app_on):
    codes, _, _ = with_app_on
    c = at_code_page(app)
    shown = codes[0].upper().replace('-', ' ')  # case, spaces and dashes do not matter
    r = two_step(c, shown)
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin') and c.get('/admin').status_code == 200
    assert len(row(app)['backup_hashes'].split(',')) == 9
    again = at_code_page(app)
    r = two_step(again, codes[0])
    assert r.status_code == 200 and again.get('/admin').status_code == 302  # spent
    assert two_step(again, codes[1]).status_code == 302  # another one works


def test_a_backup_code_still_needs_the_password_first(app, with_app_on):
    codes, _, _ = with_app_on
    c = app.test_client()
    assert two_step(c, codes[0]).status_code == 302 and c.get('/admin').status_code == 302
    assert len(row(app)['backup_hashes'].split(',')) == 10


def test_wrong_backup_codes_count_towards_the_same_limits_as_app_codes(app, with_app_on):
    codes, _, _ = with_app_on
    c = at_code_page(app)
    for _ in range(5):  # five wrong tries end this challenge...
        assert two_step(c, 'abcde-abcde').status_code == 200
    assert two_step(c, codes[0]).status_code == 200 and c.get('/admin').status_code == 302  # ...even for a right code
    assert row(app)['mfa_fails'] == 6 and len(row(app)['backup_hashes'].split(',')) == 10
    set_admin(app, mfa_fails=9, otp_tries=0)
    c = at_code_page(app)
    two_step(c, 'abcde-abcde')
    assert row(app)['locked_until'] is not None  # ten wrong codes lock the second step
    assert two_step(c, codes[0]).status_code == 200 and c.get('/admin').status_code == 302


def test_new_backup_codes_need_the_password_and_cancel_the_old_set(app, with_app_on):
    old, _, secret = with_app_on
    c = signed_in(app, secret)
    before = row(app)['backup_hashes']
    c.post('/admin/account/backup-codes', data={'current': 'wrong password!'})
    assert row(app)['backup_hashes'] == before
    r = c.post('/admin/account/backup-codes', data={'current': PASSWORD}, follow_redirects=True)
    new = CODE_RE.findall(kit_from(r.get_data(as_text=True)))
    assert len(new) == 10 and not set(new) & set(old) and row(app)['backup_hashes'] != before
    assert two_step(at_code_page(app), old[0]).status_code == 200  # the old kit is dead
    assert two_step(at_code_page(app), new[0]).status_code == 302


def test_new_backup_codes_need_the_app_to_be_on(app):
    c = sign_in(app, 'admin')
    r = c.post('/admin/account/backup-codes', data={'current': PASSWORD}, follow_redirects=True)
    assert 'authenticator app first' in r.get_data(as_text=True) and row(app)['backup_hashes'] is None


def test_the_account_page_counts_the_unused_codes_and_repeats_the_advice(app, with_app_on):
    page = signed_in(app, with_app_on[2]).get('/admin/account').get_data(as_text=True)
    assert '10 unused backup codes' in page and 'on your own computer' in page


def test_turning_the_app_off_or_resetting_2fa_deletes_the_backup_codes(app, with_app_on, monkeypatch):
    _, _, secret = with_app_on
    c = signed_in(app, secret)
    c.post('/admin/account/totp/disable', data={'code': totp_now(secret, 1), 'current': PASSWORD})
    assert row(app)['totp_enabled_at'] is None and row(app)['backup_hashes'] is None
    enable_totp(app, c)
    assert row(app)['backup_hashes']
    monkeypatch.setattr(app_module, 'admin_app', lambda: app.app_context())
    app_module.command_reset_2fa(['admin'])
    assert row(app)['backup_hashes'] is None and row(app)['totp_enabled_at'] is None


def test_the_second_step_page_offers_a_backup_code_and_no_email_button_without_an_email(app, with_app_on):
    page = at_code_page(app).get('/admin/2fa').get_data(as_text=True)
    assert 'backup code' in page and 'Email me a code instead' not in page


# ---- enforced enrolment ------------------------------------------------------------------------------------------------------

GUARDED = ('/admin', '/admin/account', '/admin/recent', '/admin/export-all', '/admin/account/totp')


@pytest.fixture
def strict(tmp_path):
    return make_app(tmp_path, REQUIRE_2FA=True)


def test_enforcement_sends_a_password_only_admin_to_set_up_the_app_before_anything_opens(strict):
    c, r = login_as(strict, 'admin')
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/2fa/setup')
    with c.session_transaction() as s:
        assert 'aid' not in s and 'stamp' not in s and s['pre']['purpose'] == 'enroll'
    page = c.get('/admin/2fa/setup').get_data(as_text=True)
    assert 'data:image/svg+xml;base64,' in page and 'name="current"' not in page  # the password was just proven
    assert '<style' not in page and 'style="' not in page
    assert c.get('/api/v1/me').status_code == 401
    for url in GUARDED:  # last: any other admin page clears the half-signed-in state
        r = c.get(url)
        assert r.status_code == 302 and '/admin/login' in r.headers['Location'], url


def test_a_wrong_code_keeps_the_browser_at_the_setup_page_and_a_right_one_signs_in_with_a_kit(strict):
    c, _ = login_as(strict, 'admin')
    c.get('/admin/2fa/setup')
    with strict.app_context():
        secret = twofactor.unseal(Admin.query.filter_by(username='admin').one().totp_secret, strict.config['SECRET_KEY'])
    r = c.post('/admin/2fa/setup', data={'code': '000000'})
    with c.session_transaction() as s:
        assert r.status_code == 200 and 'aid' not in s and s['pre']['purpose'] == 'enroll' and row(strict)['totp_enabled_at'] is None
    r = c.post('/admin/2fa/setup', data={'code': totp_now(secret)}, follow_redirects=True)
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and len(CODE_RE.findall(kit_from(page))) == 10
    assert c.get('/admin').status_code == 200 and row(strict)['totp_enabled_at'] and row(strict)['last_login_at']
    nxt, r2 = login_as(strict, 'admin')
    assert r2.headers['Location'].endswith('/admin/2fa')  # the next sign-in asks for the app


def test_the_setup_page_is_only_for_a_browser_sent_there(strict):
    anon = strict.test_client()
    for method in (anon.get, anon.post):
        r = method('/admin/2fa/setup')
        assert r.status_code == 302 and '/admin/login' in r.headers['Location']
    make_user(strict, 'doc', email='doc@example.test')
    c, _ = login_as(strict, 'doc')  # waiting for an emailed code: a different half-signed-in state
    assert '/admin/login' in c.get('/admin/2fa/setup').headers['Location']
    # and an enrolling browser cannot borrow the sign-in or email-proof pages
    e, _ = login_as(strict, 'admin')
    for path in ('/admin/2fa', '/admin/verify'):
        assert '/admin/login' in e.get(path).headers['Location']


def test_a_disabled_account_cannot_finish_enrolling(strict):
    c, _ = login_as(strict, 'admin')
    c.get('/admin/2fa/setup')
    set_admin(strict, status='disabled')
    assert '/admin/login' in c.post('/admin/2fa/setup', data={'code': '123456'}).headers['Location']


def test_enforcement_leaves_an_admin_who_already_has_the_app_alone(strict):
    strict.config['REQUIRE_2FA'] = False
    enable_totp(strict, sign_in(strict, 'admin'))
    strict.config['REQUIRE_2FA'] = True
    c, r = login_as(strict, 'admin')
    assert r.headers['Location'].endswith('/admin/2fa')


def test_enforcement_defaults_on_and_the_host_can_turn_it_off(tmp_path, monkeypatch):
    def build(name):
        return app_module.create_app({'TESTING': True, 'SECRET_KEY': 'x' * 20, 'ADMIN_USERNAME': 'admin', 'ADMIN_PASSWORD_HASH': PASSWORD_HASH,
                                      'SQLALCHEMY_DATABASE_URI': f'sqlite:///{tmp_path / name}', 'MAIL_BACKEND': ''})
    monkeypatch.delenv('ORS_SRS_REQUIRE_2FA', raising=False)
    assert build('a.db').config['REQUIRE_2FA'] is True
    monkeypatch.setenv('ORS_SRS_REQUIRE_2FA', 'off')
    assert build('b.db').config['REQUIRE_2FA'] is False


# ---- a missing second step must never be a way in ---------------------------------------------------------------------------

@pytest.mark.parametrize('enforced', [False, True])
def test_an_app_secret_that_can_no_longer_be_read_and_no_email_is_refused_not_waved_through(tmp_path, enforced):
    app = make_app(tmp_path, REQUIRE_2FA=enforced)
    set_admin(app, totp_enabled_at=utcnow(), totp_secret='sealed-with-another-secret-key')  # e.g. SECRET_KEY was changed
    c, r = login_as(app, 'admin')
    assert r.status_code == 200 and 'reset-2fa' in r.get_data(as_text=True)
    with c.session_transaction() as s:
        assert 'aid' not in s and 'pre' not in s
    assert c.get('/admin').status_code == 302


def test_an_unreadable_app_secret_still_falls_back_to_the_email_code(app):
    make_user(app, 'doc', email='doc@example.test')
    set_admin(app, 'doc', totp_enabled_at=utcnow(), totp_secret='sealed-with-another-secret-key')
    c, r = login_as(app, 'doc')
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/2fa')
    with c.session_transaction() as s:
        assert s['pre']['method'] == 'email'
