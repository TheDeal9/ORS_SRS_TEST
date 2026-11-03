"""Admin sign-up, email proof, owner approval, and the account states."""
import re
from datetime import timedelta

import pytest

import app as app_module
from app import Admin, Client, db, utcnow
from conftest import PASSWORD, add_client, last_code, login_as, make_app, make_user, outbox, sign_in

NEW = dict(username='dr-new', email='new@example.test', password=PASSWORD, confirm=PASSWORD)


def signup(c, **over):
    return c.post('/admin/signup', data={**NEW, **over})


def page_of(r):
    return re.sub(r'name="csrf_token" value="[^"]*"', '', r.get_data(as_text=True))  # that value changes every second


def row(app, username='dr-new'):
    with app.app_context():
        a = Admin.query.filter_by(username=username).first()
        return a and {c.name: getattr(a, c.name) for c in Admin.__table__.columns}


def verify(c, app, code=None, to='new@example.test'):
    return c.post('/admin/verify', data={'code': code or last_code(app, to)})


def register(app, **over):
    """Sign up and prove the email: leaves a 'pending' administrator. Returns the browser that did it."""
    c = app.test_client()
    assert signup(c, **over).status_code == 302
    r = verify(c, app, to=over.get('email', NEW['email']))
    assert r.status_code == 200 and 'Email confirmed' in r.get_data(as_text=True)
    return c


# ---- the page -----------------------------------------------------------------------------------------------------------

def test_login_and_landing_offer_sign_up_when_mail_works(app, anon):
    assert '/admin/signup' in anon.get('/admin/login').get_data(as_text=True)
    assert '/admin/signup' in anon.get('/').get_data(as_text=True)
    page = anon.get('/admin/signup').get_data(as_text=True)
    assert 'name="username"' in page and 'name="email"' in page and 'name="confirm"' in page and 'csrf_token' in page


@pytest.mark.parametrize('over', [{'MAIL_BACKEND': ''}, {'SIGNUP_ENABLED': False}])
def test_sign_up_is_closed_without_mail_or_when_the_owner_switches_it_off(tmp_path, over):
    app = make_app(tmp_path, **over)
    c = app.test_client()
    assert '/admin/signup' not in c.get('/admin/login').get_data(as_text=True)
    assert '/admin/signup' not in c.get('/').get_data(as_text=True)
    for r in (c.get('/admin/signup'), signup(c)):
        assert r.status_code == 404 and 'closed' in r.get_data(as_text=True)
    assert row(app) is None and outbox(app) == [] if over.get('MAIL_BACKEND') != '' else row(app) is None


# ---- creating the account --------------------------------------------------------------------------------------------------

def test_sign_up_creates_an_unverified_account_and_mails_a_code(app, anon):
    r = signup(anon)
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/verify')
    a = row(app)
    assert a['status'] == 'unverified' and a['email'] is None and a['pending_email'] == 'new@example.test' and not a['is_owner']
    assert a['recovery_hash'] is None  # shown only after the email is proven
    (mail,) = outbox(app)
    assert mail['to'] == 'new@example.test' and re.fullmatch(r'Your ORS/SRS portal code: \d{6}', mail['subject'])
    assert 'http' not in mail['body'] and last_code(app, 'new@example.test') in mail['subject']
    assert 'verify' in anon.get('/admin/verify').get_data(as_text=True).lower()


def test_half_registered_browser_is_not_signed_in(app, anon):
    signup(anon)
    with anon.session_transaction() as s:
        assert 'aid' not in s and 'pre' in s
    for url in ('/admin', '/admin/account', '/admin/recent', '/admin/export-all', '/admin/admins'):
        assert anon.get(url).status_code == 302 and '/admin/login' in anon.get(url).headers['Location']


def test_the_code_is_stored_as_a_keyed_hash_never_in_clear(app, anon):
    signup(anon)
    code = last_code(app, 'new@example.test')
    a = row(app)
    assert re.fullmatch(r'[0-9a-f]{64}', a['otp_hash']) and code not in a['otp_hash'] and a['otp_purpose'] == 'verify'
    with app.app_context():  # the same code under another admin id or purpose hashes differently
        admin = Admin.query.filter_by(username='dr-new').one()
        assert app_module.otp_digest(admin, 'verify', code) == a['otp_hash']
        assert app_module.otp_digest(admin, 'login', code) != a['otp_hash']


@pytest.mark.parametrize('over, message', [
    ({'password': 'short', 'confirm': 'short'}, 'at least 10'),
    ({'confirm': 'something else entirely'}, 'differ'),
    ({'email': 'not-an-email'}, 'valid email'),
    ({'email': 'a b@example.test'}, 'valid email'),
    ({'email': 'a@b@example.test'}, 'valid email'),
    ({'email': ''}, 'email address'),
    ({'username': 'x'}, 'ID must be'),
    ({'username': 'Has Space'}, 'ID must be'),
    ({'username': '-lead'}, 'ID must be'),
])
def test_sign_up_validation(app, anon, over, message):
    r = signup(anon, **over)
    assert r.status_code == 200 and message in r.get_data(as_text=True)
    assert row(app, over.get('username', 'dr-new').strip().lower()) is None and outbox(app) == []
    html = r.get_data(as_text=True)
    assert PASSWORD not in html  # the password is never echoed back


def test_sign_up_keeps_what_was_typed_but_not_the_password(app, anon):
    html = signup(anon, password='short', confirm='short').get_data(as_text=True)
    assert 'value="dr-new"' in html and 'value="new@example.test"' in html


def test_duplicate_id_and_duplicate_confirmed_email_are_refused(app, anon):
    make_user(app, 'taken', email='used@example.test')
    assert 'already exists' in signup(anon, username='TAKEN').get_data(as_text=True)
    assert 'already has an account' in signup(anon, email='used@example.test').get_data(as_text=True)
    assert outbox(app) == []


def test_id_is_case_and_space_insensitive(app, anon):
    assert signup(anon, username='  Dr-New ').status_code == 302 and row(app) is not None


def test_unconfirmed_sign_ups_older_than_a_day_free_their_id(app, anon):
    signup(anon)
    with app.app_context():
        Admin.query.filter_by(username='dr-new').one().created_at = utcnow() - timedelta(hours=25)
        db.session.commit()
    other = app.test_client()
    assert signup(other, email='other@example.test').status_code == 302  # same ID, different mailbox
    assert row(app)['pending_email'] == 'other@example.test'


def test_a_mail_failure_leaves_no_live_code_and_says_so(app, anon, monkeypatch):
    def boom(*a, **k):
        raise app_module.mailer.MailError('smtp: down')
    monkeypatch.setattr(app_module.mailer, 'send', boom)
    r = anon.post('/admin/signup', data=NEW, follow_redirects=True)
    assert 'could not email' in r.get_data(as_text=True).lower()
    a = row(app)
    assert a['otp_hash'] is None and a['otp_expires_at'] is None


# ---- proving the email ---------------------------------------------------------------------------------------------------------

def test_correct_code_confirms_the_email_and_waits_for_the_owner(app, anon):
    make_user(app, 'boss', email='boss@example.test', owner=True)
    signup(anon)
    r = verify(anon, app)
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and 'Email confirmed' in page and 'approve' in page.lower()
    code = re.search(r'value="([0-9a-f]{4}(?:-[0-9a-f]{4}){5})"', page).group(1)
    a = row(app)
    assert a['status'] == 'pending' and a['email'] == 'new@example.test' and a['pending_email'] is None
    assert a['otp_hash'] is None and a['recovery_hash'] == app_module.recovery_hash(code)
    notice = [m for m in outbox(app) if m['to'] == 'boss@example.test']
    assert len(notice) == 1 and 'dr-new' in notice[0]['body'] and 'http' not in notice[0]['body']
    with anon.session_transaction() as s:
        assert 'pre' not in s and 'aid' not in s


def test_a_code_works_once(app, anon):
    signup(anon)
    code = last_code(app, 'new@example.test')
    assert 'Email confirmed' in verify(anon, app, code).get_data(as_text=True)
    again = app.test_client()
    again.post('/admin/login', data={'username': 'dr-new', 'password': PASSWORD})
    assert verify(again, app, code).status_code == 302  # nothing pending any more: back to sign in


def test_wrong_code_is_refused_and_five_wrong_tries_kill_the_code(app, anon):
    signup(anon)
    good = last_code(app, 'new@example.test')
    bad = '000000' if good != '000000' else '111111'
    for _ in range(5):
        r = verify(anon, app, bad)
        assert r.status_code == 200 and 'not right or has expired' in r.get_data(as_text=True)
    assert 'not right or has expired' in verify(anon, app, good).get_data(as_text=True)  # the right code is now dead too
    assert row(app)['status'] == 'unverified'


def test_expired_code_is_refused(app, anon):
    signup(anon)
    with app.app_context():
        Admin.query.filter_by(username='dr-new').one().otp_expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    assert 'not right or has expired' in verify(anon, app).get_data(as_text=True)
    assert row(app)['status'] == 'unverified'


def test_code_is_tied_to_its_purpose(app, anon):
    signup(anon)
    with app.app_context():
        admin = Admin.query.filter_by(username='dr-new').one()
        code = last_code(app, 'new@example.test')
        assert app_module.check_otp(admin, 'login', code) is False  # a sign-up code cannot sign anyone in
        assert admin.otp_hash is not None  # and the mismatch did not spend it
        assert app_module.check_otp(admin, 'verify', code) is True and admin.otp_hash is None


def test_resend_waits_a_minute_then_replaces_the_old_code(app, anon):
    signup(anon)
    old = last_code(app, 'new@example.test')
    r = anon.post('/admin/verify/resend', follow_redirects=True)
    assert 'moment ago' in r.get_data(as_text=True) and len(outbox(app)) == 1  # too soon: nothing new went out
    with app.app_context():  # pretend the first code went out two minutes ago
        Admin.query.filter_by(username='dr-new').one().otp_expires_at = utcnow() + timedelta(minutes=8)
        db.session.commit()
    anon.post('/admin/verify/resend')
    new = last_code(app, 'new@example.test')
    assert len(outbox(app)) == 2
    if new != old:  # one in a million that the two codes match
        assert 'Email confirmed' not in verify(anon, app, old).get_data(as_text=True) and row(app)['status'] == 'unverified'
    assert 'Email confirmed' in verify(anon, app, new).get_data(as_text=True)


def test_verify_page_without_a_pending_browser_goes_to_sign_in(anon):
    assert anon.get('/admin/verify').status_code == 302 and anon.post('/admin/verify', data={'code': '123456'}).status_code == 302
    assert anon.post('/admin/verify/resend').status_code == 302


def test_page_masks_the_address(app, anon):
    signup(anon)
    html = anon.get('/admin/verify').get_data(as_text=True)
    assert 'n***@example.test' in html and 'new@example.test' not in html


def test_two_sign_ups_for_one_address_only_the_first_to_prove_it_wins(app):
    a, b = app.test_client(), app.test_client()
    signup(a, username='first')
    signup(b, username='second')
    codes = [m for m in outbox(app)]
    assert verify(a, app, re.search(r'code: (\d{6})', codes[0]['subject']).group(1)).status_code == 200
    r = b.post('/admin/verify', data={'code': re.search(r'code: (\d{6})', codes[1]['subject']).group(1)}, follow_redirects=True)
    assert 'already has an account' in r.get_data(as_text=True)
    assert row(app, 'first')['status'] == 'pending' and row(app, 'second')['status'] == 'unverified' and row(app, 'second')['email'] is None


# ---- signing in while not active -----------------------------------------------------------------------------------------------

def test_unconfirmed_account_signing_in_is_sent_to_prove_the_email(app, anon):
    signup(anon)
    with app.app_context():
        Admin.query.filter_by(username='dr-new').one().otp_expires_at = utcnow() + timedelta(minutes=8)  # cooldown over
        db.session.commit()
    c, r = login_as(app, 'dr-new')
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/verify') and len(outbox(app)) == 2


def test_pending_account_is_told_so_only_after_the_right_password(app):
    register(app)
    c, r = login_as(app, 'dr-new')
    assert r.status_code == 200 and 'not approved your account yet' in r.get_data(as_text=True)
    with c.session_transaction() as s:
        assert 'aid' not in s and 'pre' not in s
    same = app.test_client()  # one browser, so the page's CSRF token is the same in both
    wrong = same.post('/admin/login', data={'username': 'dr-new', 'password': 'wrong password!'})
    nobody = same.post('/admin/login', data={'username': 'nobody-here', 'password': 'wrong password!'})
    assert page_of(wrong) == page_of(nobody) and 'Incorrect administrator ID or password' in wrong.get_data(as_text=True)


# ---- the owner's page -----------------------------------------------------------------------------------------------------------

@pytest.fixture
def owned(app):
    """Seeded owner 'admin' with a confirmed email, a pending applicant, and a browser signed in as the owner."""
    with app.app_context():
        Admin.query.filter_by(username='admin').one().email = 'owner@example.test'
        db.session.commit()
    register(app)
    c, r = login_as(app, 'admin')
    assert r.headers['Location'].endswith('/admin/2fa')
    c.post('/admin/2fa', data={'code': last_code(app, 'owner@example.test')})
    return c


def applicant_id(app):
    return row(app)['id']


def test_owner_sees_applicants_and_approves(app, owned):
    page = owned.get('/admin/admins').get_data(as_text=True)
    assert 'dr-new' in page and 'Waiting for approval' in page and 'Approve' in page and 'Owner' in page
    assert '<span class="count">1</span>' in owned.get('/admin').get_data(as_text=True)  # nav badge
    before = len(outbox(app))
    r = owned.post(f'/admin/admins/{applicant_id(app)}/approve', follow_redirects=True)
    assert 'dr-new: approved' in r.get_data(as_text=True) and row(app)['status'] == 'active'
    note = outbox(app)[before:]
    assert len(note) == 1 and note[0]['to'] == 'new@example.test' and 'approved' in note[0]['subject'].lower()
    assert '<span class="count">' not in owned.get('/admin').get_data(as_text=True)


def test_approved_admin_signs_in_with_an_emailed_code(app, owned):
    owned.post(f'/admin/admins/{applicant_id(app)}/approve')
    c, r = login_as(app, 'dr-new')
    assert r.status_code == 302 and r.headers['Location'].endswith('/admin/2fa')
    assert c.post('/admin/2fa', data={'code': last_code(app, 'new@example.test')}).headers['Location'].endswith('/admin')
    assert c.get('/admin').status_code == 200


def test_owner_rejects_a_pending_applicant_and_they_are_removed_and_told(app, owned):
    before = len(outbox(app))
    owned.post(f'/admin/admins/{applicant_id(app)}/reject')
    assert row(app) is None
    note = outbox(app)[before:]
    assert len(note) == 1 and 'not approved' in note[0]['subject']


def test_reject_never_deletes_an_admin_who_has_clients(app, owned):
    owned.post(f'/admin/admins/{applicant_id(app)}/approve')
    with app.app_context():
        db.session.add(Client(admin_id=applicant_id(app), slug='kept', first_name='K', last_name='P'))
        db.session.commit()
        Admin.query.filter_by(username='dr-new').one().status = 'pending'  # force the state that reject allows
        db.session.commit()
    r = owned.post(f'/admin/admins/{applicant_id(app)}/reject', follow_redirects=True)
    assert 'has clients' in r.get_data(as_text=True) and row(app) is not None
    with app.app_context():
        assert Client.query.filter_by(slug='kept').count() == 1


def test_disable_signs_the_admin_out_at_once_and_enable_restores(app, owned):
    owned.post(f'/admin/admins/{applicant_id(app)}/approve')
    other = sign_in_with_code(app, 'dr-new', 'new@example.test')
    assert other.get('/admin').status_code == 200
    owned.post(f'/admin/admins/{applicant_id(app)}/disable')
    assert other.get('/admin').status_code == 302  # the cookie still carries a valid stamp, but the account is not active
    _, r = login_as(app, 'dr-new')
    assert 'disabled' in r.get_data(as_text=True)
    owned.post(f'/admin/admins/{applicant_id(app)}/enable')
    assert other.get('/admin').status_code == 302  # that browser was signed out for good: sign in again
    assert sign_in_with_code(app, 'dr-new', 'new@example.test').get('/admin').status_code == 200


def sign_in_with_code(app, username, email):
    c, r = login_as(app, username)
    assert r.headers['Location'].endswith('/admin/2fa')
    assert c.post('/admin/2fa', data={'code': last_code(app, email)}).status_code == 302
    return c


def test_owner_cannot_disable_themselves_or_act_on_a_state_that_does_not_fit(app, owned):
    owner_id = row(app, 'admin')['id']
    for action in ('disable', 'reject', 'approve'):
        r = owned.post(f'/admin/admins/{owner_id}/{action}', follow_redirects=True)
        assert 'not possible' in r.get_data(as_text=True)
    assert row(app, 'admin')['status'] == 'active'
    r = owned.post(f'/admin/admins/{applicant_id(app)}/enable', follow_redirects=True)  # pending, not disabled
    assert 'not possible' in r.get_data(as_text=True) and row(app)['status'] == 'pending'


def test_unknown_action_and_id_are_404(owned):
    assert owned.post('/admin/admins/999999/approve').status_code == 404
    assert owned.post('/admin/admins/1/explode').status_code == 404


def test_only_the_owner_has_the_admins_page(app, owned):
    owned.post(f'/admin/admins/{applicant_id(app)}/approve')
    make_user(app, 'plain')
    c = sign_in(app, 'plain')
    assert c.get('/admin/admins').status_code == 404
    for action in ('approve', 'reject', 'disable', 'enable'):
        assert c.post(f'/admin/admins/{row(app, "admin")["id"]}/{action}').status_code == 404
    assert 'Admins' not in c.get('/admin').get_data(as_text=True).split('<main')[0]
    assert owned.get('/admin/admins').status_code == 200


def test_signed_out_visitors_are_sent_to_sign_in_from_the_admins_page(anon):
    assert anon.get('/admin/admins').status_code == 302
    assert anon.post('/admin/admins/1/approve').status_code == 302


def test_the_owner_sees_how_many_clients_each_admin_has_but_never_who(app, owned):
    owned.post(f'/admin/admins/{applicant_id(app)}/approve')
    with app.app_context():
        db.session.add(Client(admin_id=applicant_id(app), slug='secret-person', first_name='Secret', last_name='Person'))
        db.session.commit()
    page = owned.get('/admin/admins').get_data(as_text=True)
    assert 'secret-person' not in page and 'Secret' not in page
    assert re.search(r'dr-new.*?<td class="num">1</td>', page, re.S)


def test_only_the_first_administrator_is_the_owner(app):
    with app.app_context():
        assert [a.is_owner for a in Admin.query.order_by(Admin.id)] == [True]
        a, code = app_module.create_admin('second', PASSWORD)
        assert a.is_owner is False and a.status == 'active' and code
        pending, none = app_module.create_admin('third', PASSWORD, 'Third@Example.test', status='unverified')
        assert pending.pending_email == 'third@example.test' and pending.recovery_hash is None and none is None


def test_removing_an_unconfirmed_sign_up_mails_nobody(app, owned):
    other = app.test_client()
    signup(other, username='squatter', email='victim@example.test')  # never confirmed
    before = len(outbox(app))
    owned.post(f'/admin/admins/{row(app, "squatter")["id"]}/reject')
    assert row(app, 'squatter') is None and len(outbox(app)) == before
