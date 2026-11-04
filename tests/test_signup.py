"""One-time email codes for proving an address under Account, and the owner's Admins page.

How a new administrator signs up (Google proves the email, no owner approval) is in test_google_signup.py."""
import re
from datetime import timedelta

import pytest

import app as app_module
from app import Admin, AdminIdentity, Client, db, utcnow
from conftest import PASSWORD, last_code, login_as, make_user, outbox, sign_in

NEW_MAIL = 'new@example.test'


def page_of(r):
    return re.sub(r'name="csrf_token" value="[^"]*"', '', r.get_data(as_text=True))  # that value changes every second


def row(app, username='dr-new'):
    with app.app_context():
        a = Admin.query.filter_by(username=username).first()
        return a and {c.name: getattr(a, c.name) for c in Admin.__table__.columns}


def settled(c):
    """Show the dashboard once, so the 'no recovery code yet' notice from signing in is used up and cannot hide a later message."""
    assert c.get('/admin').status_code == 200
    return c


def verify(c, app, code=None, to=NEW_MAIL):
    return c.post('/admin/verify', data={'code': code or last_code(app, to)})


@pytest.fixture
def member(app):
    """Signed-in administrator 'dr-new' who has asked to confirm new@example.test: the code was mailed, nothing confirmed yet."""
    make_user(app, 'dr-new')
    c = settled(sign_in(app, 'dr-new'))
    r = c.post('/admin/account/email', data={'email': NEW_MAIL, 'current': PASSWORD})
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/verify')
    return c


# ---- asking for a code ------------------------------------------------------------------------------------------------------

def test_asking_to_confirm_an_address_mails_a_code_and_changes_nothing_yet(app, member):
    a = row(app)
    assert a['email'] is None and a['pending_email'] == NEW_MAIL and a['status'] == 'active'
    (mail,) = outbox(app)
    assert mail['to'] == NEW_MAIL and re.fullmatch(r'Your ORS/SRS portal code: \d{6}', mail['subject'])
    assert 'http' not in mail['body'] and last_code(app, NEW_MAIL) in mail['subject']
    assert 'Confirm your email' in member.get('/admin/verify').get_data(as_text=True)


def test_the_code_is_stored_as_a_keyed_hash_never_in_clear(app, member):
    code = last_code(app, NEW_MAIL)
    a = row(app)
    assert re.fullmatch(r'[0-9a-f]{64}', a['otp_hash']) and code not in a['otp_hash'] and a['otp_purpose'] == 'verify'
    with app.app_context():  # the same code under another admin id or purpose hashes differently
        admin = Admin.query.filter_by(username='dr-new').one()
        assert app_module.otp_digest(admin, 'verify', code) == a['otp_hash']
        assert app_module.otp_digest(admin, 'login', code) != a['otp_hash']


def test_a_mail_failure_leaves_no_live_code_and_says_so(app, monkeypatch):
    make_user(app, 'dr-new')
    c = settled(sign_in(app, 'dr-new'))

    def boom(*a, **k):
        raise app_module.mailer.MailError('smtp: down')
    monkeypatch.setattr(app_module.mailer, 'send', boom)
    r = c.post('/admin/account/email', data={'email': NEW_MAIL, 'current': PASSWORD}, follow_redirects=True)
    assert 'could not email' in r.get_data(as_text=True).lower()
    a = row(app)
    assert a['otp_hash'] is None and a['otp_expires_at'] is None


# ---- proving the address ---------------------------------------------------------------------------------------------------------

def test_the_right_code_confirms_the_address_and_keeps_the_account_active(app, member):
    r = verify(member, app)
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/account')
    a = row(app)
    assert a['email'] == NEW_MAIL and a['pending_email'] is None and a['otp_hash'] is None and a['status'] == 'active'
    assert 'confirmed' in member.get('/admin/account').get_data(as_text=True)


def test_a_code_works_once(app, member):
    code = last_code(app, NEW_MAIL)
    assert verify(member, app, code).status_code == 302
    assert verify(member, app, code).status_code == 302 and row(app)['email'] == NEW_MAIL  # nothing pending any more: sent away, no change


def test_wrong_code_is_refused_and_five_wrong_tries_kill_the_code(app, member):
    good = last_code(app, NEW_MAIL)
    bad = '000000' if good != '000000' else '111111'
    for _ in range(5):
        r = verify(member, app, bad)
        assert r.status_code == 200 and 'not right or has expired' in r.get_data(as_text=True)
    assert 'not right or has expired' in verify(member, app, good).get_data(as_text=True)  # the right code is now dead too
    assert row(app)['email'] is None


def test_expired_code_is_refused(app, member):
    with app.app_context():
        Admin.query.filter_by(username='dr-new').one().otp_expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    assert 'not right or has expired' in verify(member, app).get_data(as_text=True)
    assert row(app)['email'] is None


def test_code_is_tied_to_its_purpose(app, member):
    with app.app_context():
        admin = Admin.query.filter_by(username='dr-new').one()
        code = last_code(app, NEW_MAIL)
        assert app_module.check_otp(admin, 'login', code) is False  # a confirmation code cannot sign anyone in
        assert admin.otp_hash is not None  # and the mismatch did not spend it
        assert app_module.check_otp(admin, 'verify', code) is True and admin.otp_hash is None


def test_resend_waits_a_minute_then_replaces_the_old_code(app, member):
    old = last_code(app, NEW_MAIL)
    r = member.post('/admin/verify/resend', follow_redirects=True)
    assert 'moment ago' in r.get_data(as_text=True) and len(outbox(app)) == 1  # too soon: nothing new went out
    with app.app_context():  # pretend the first code went out two minutes ago
        Admin.query.filter_by(username='dr-new').one().otp_expires_at = utcnow() + timedelta(minutes=8)
        db.session.commit()
    member.post('/admin/verify/resend')
    new = last_code(app, NEW_MAIL)
    assert len(outbox(app)) == 2
    if new != old:  # one in a million that the two codes match
        assert verify(member, app, old).status_code == 200 and row(app)['email'] is None
    assert verify(member, app, new).status_code == 302 and row(app)['email'] == NEW_MAIL


def test_the_verify_pages_need_a_signed_in_administrator(anon):
    assert anon.get('/admin/verify').status_code == 302 and anon.post('/admin/verify', data={'code': '123456'}).status_code == 302
    assert anon.post('/admin/verify/resend').status_code == 302


def test_the_page_masks_the_address(app, member):
    html = member.get('/admin/verify').get_data(as_text=True)
    assert 'n***@example.test' in html and NEW_MAIL not in html


def test_two_admins_asking_for_one_address_only_the_first_to_prove_it_wins(app, member):
    make_user(app, 'second')
    other = settled(sign_in(app, 'second'))
    other.post('/admin/account/email', data={'email': NEW_MAIL, 'current': PASSWORD})
    codes = [m for m in outbox(app)]
    assert verify(member, app, re.search(r'code: (\d{6})', codes[0]['subject']).group(1)).status_code == 302
    r = other.post('/admin/verify', data={'code': re.search(r'code: (\d{6})', codes[1]['subject']).group(1)}, follow_redirects=True)
    assert 'already has an account' in r.get_data(as_text=True)
    assert row(app)['email'] == NEW_MAIL and row(app, 'second')['email'] is None and row(app, 'second')['pending_email'] is None


def test_an_address_someone_already_confirmed_is_refused_up_front(app):
    make_user(app, 'taken', email='used@example.test')
    make_user(app, 'dr-new')
    c = settled(sign_in(app, 'dr-new'))
    r = c.post('/admin/account/email', data={'email': 'used@example.test', 'current': PASSWORD}, follow_redirects=True)
    assert 'already has an account' in r.get_data(as_text=True) and outbox(app) == []


# ---- accounts that can no longer sign in ----------------------------------------------------------------------------------------

@pytest.mark.parametrize('status', ['disabled', 'pending', 'unverified'])
def test_only_an_active_account_gets_past_the_password_step(app, status):
    make_user(app, 'dr-old', status=status)
    c, r = login_as(app, 'dr-old')
    assert r.status_code == 200 and 'disabled' in r.get_data(as_text=True)
    with c.session_transaction() as s:
        assert 'aid' not in s and 'pre' not in s
    same = app.test_client()  # one browser, so the page's CSRF token is the same in both
    wrong = same.post('/admin/login', data={'username': 'dr-old', 'password': 'wrong password!'})
    nobody = same.post('/admin/login', data={'username': 'nobody-here', 'password': 'wrong password!'})
    assert page_of(wrong) == page_of(nobody) and 'Incorrect administrator ID or password' in wrong.get_data(as_text=True)


# ---- the owner's page -----------------------------------------------------------------------------------------------------------

@pytest.fixture
def owned(app):
    """The seeded owner 'admin' signed in, and a second administrator 'dr-new' (active, no clients)."""
    make_user(app, 'dr-new')
    return sign_in(app, 'admin')


def member_id(app):
    return row(app)['id']


def test_the_owner_sees_every_admin_and_how_many_clients_each_has_but_never_who(app, owned):
    with app.app_context():
        db.session.add(Client(admin_id=member_id(app), slug='secret-person', first_name='Secret', last_name='Person'))
        db.session.commit()
    page = owned.get('/admin/admins').get_data(as_text=True)
    assert 'dr-new' in page and 'Owner' in page and 'Active' in page
    assert 'secret-person' not in page and 'Secret' not in page
    assert re.search(r'dr-new.*?<td class="num">1</td>', page, re.S)
    assert '<span class="count">' not in owned.get('/admin').get_data(as_text=True)  # no approval queue any more


def test_disable_signs_the_admin_out_at_once_and_enable_restores(app, owned):
    other = sign_in(app, 'dr-new')
    assert other.get('/admin').status_code == 200
    owned.post(f'/admin/admins/{member_id(app)}/disable')
    assert other.get('/admin').status_code == 302  # the cookie still carries a valid stamp, but the account is not active
    _, r = login_as(app, 'dr-new')
    assert 'disabled' in r.get_data(as_text=True)
    owned.post(f'/admin/admins/{member_id(app)}/enable')
    assert other.get('/admin').status_code == 302  # that browser was signed out for good: sign in again
    assert sign_in(app, 'dr-new').get('/admin').status_code == 200


def test_owner_cannot_act_on_themselves_or_on_a_state_that_does_not_fit(app, owned):
    owner_id = row(app, 'admin')['id']
    for action in ('disable', 'remove'):
        r = owned.post(f'/admin/admins/{owner_id}/{action}', follow_redirects=True)
        assert 'not possible' in r.get_data(as_text=True)
    assert row(app, 'admin')['status'] == 'active'
    r = owned.post(f'/admin/admins/{member_id(app)}/enable', follow_redirects=True)  # active, not disabled
    assert 'not possible' in r.get_data(as_text=True) and row(app)['status'] == 'active'


def test_remove_deletes_an_admin_without_clients_and_their_google_link_with_them(app, owned):
    with app.app_context():
        app_module.create_admin('gone', PASSWORD, with_code=False, identity={'provider': 'google', 'subject': 's-1', 'email': 'g@example.test'})
        assert AdminIdentity.query.count() == 1
    r = owned.post(f'/admin/admins/{row(app, "gone")["id"]}/remove', follow_redirects=True)
    assert 'gone: removed' in r.get_data(as_text=True) and row(app, 'gone') is None
    with app.app_context():
        assert AdminIdentity.query.count() == 0


def test_remove_never_deletes_an_admin_who_has_clients(app, owned):
    with app.app_context():
        db.session.add(Client(admin_id=member_id(app), slug='kept', first_name='K', last_name='P'))
        db.session.commit()
    r = owned.post(f'/admin/admins/{member_id(app)}/remove', follow_redirects=True)
    assert 'has clients' in r.get_data(as_text=True) and row(app) is not None
    with app.app_context():
        assert Client.query.filter_by(slug='kept').count() == 1


def test_removing_mails_nobody(app, owned):
    owned.post(f'/admin/admins/{member_id(app)}/remove')
    assert row(app) is None and outbox(app) == []


def test_the_retired_approve_and_reject_actions_and_unknown_ones_are_404(owned):
    for action in ('approve', 'reject', 'explode'):
        assert owned.post(f'/admin/admins/1/{action}').status_code == 404
    assert owned.post('/admin/admins/999999/disable').status_code == 404


def test_only_the_owner_has_the_admins_page(app, owned):
    make_user(app, 'plain')
    c = sign_in(app, 'plain')
    assert c.get('/admin/admins').status_code == 404
    for action in ('disable', 'enable', 'remove'):
        assert c.post(f'/admin/admins/{row(app, "admin")["id"]}/{action}').status_code == 404
    assert 'Admins' not in c.get('/admin').get_data(as_text=True).split('<main')[0]
    assert owned.get('/admin/admins').status_code == 200


def test_signed_out_visitors_are_sent_to_sign_in_from_the_admins_page(anon):
    assert anon.get('/admin/admins').status_code == 302
    assert anon.post('/admin/admins/1/disable').status_code == 302


def test_only_the_first_administrator_is_the_owner(app):
    with app.app_context():
        assert [a.is_owner for a in Admin.query.order_by(Admin.id)] == [True]
        a, code = app_module.create_admin('second', PASSWORD)
        assert a.is_owner is False and a.status == 'active' and code
        b, none = app_module.create_admin('third', PASSWORD, with_code=False)
        assert b.is_owner is False and b.recovery_hash is None and none is None
