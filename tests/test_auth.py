"""Administrator accounts: database-backed logins, sessions, forgotten-password recovery, per-admin API keys,
the read-only JSON API and the command-line tools. Passwords, recovery codes and keys are only ever stored as hashes."""
import builtins
import getpass
import hashlib
import os
import re
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.serving import make_server

import app as app_module
from app import Admin, ApiKey, create_app, db, utcnow
from conftest import PASSWORD, PASSWORD_HASH, add_client, complete_session, make_app

NEW_PASSWORD = 'a brand new password'
CODE_RE = re.compile(r'value="([0-9a-f]{4}(?:-[0-9a-f]{4}){5})"')
KEY_RE = re.compile(r'value="(ors_[A-Za-z0-9_-]+)"')


def login(app, username='admin', password=PASSWORD):
    c = app.test_client()
    r = c.post('/admin/login', data={'username': username, 'password': password})
    assert r.status_code == 302, r.data
    return c


def make_recovery_code(c):
    r = c.post('/admin/account/recovery', data={'current': PASSWORD}, follow_redirects=True)
    return CODE_RE.search(r.get_data(as_text=True)).group(1)


def make_key(c, label='laptop'):
    r = c.post('/admin/account/keys', data={'label': label, 'current': PASSWORD}, follow_redirects=True)
    return KEY_RE.search(r.get_data(as_text=True)).group(1)


def bearer(key):
    return {'Authorization': f'Bearer {key}'}


def forgot(c, code, password=NEW_PASSWORD, username='admin', confirm=None):
    return c.post('/admin/forgot', data={'username': username, 'code': code, 'password': password,
                                         'confirm': password if confirm is None else confirm})


# ---- where the credentials live ------------------------------------------------------------------------

def test_first_start_creates_the_admin_from_the_environment_and_later_changes_are_ignored(tmp_path):
    uri = f'sqlite:///{tmp_path / "x.db"}'
    cfg = dict(SECRET_KEY='s' * 32, SQLALCHEMY_DATABASE_URI=uri, ADMIN_USERNAME='Dr-Demo', ADMIN_PASSWORD_HASH=generate_password_hash('first password 1'))
    app = create_app(cfg)
    with app.app_context():
        assert [a.username for a in Admin.query] == ['dr-demo']  # stored lower-case
    other = create_app({**cfg, 'ADMIN_USERNAME': 'someone-else', 'ADMIN_PASSWORD_HASH': generate_password_hash('second password 2')})
    with other.app_context():  # the table already has an admin: the variables no longer matter
        assert [a.username for a in Admin.query] == ['dr-demo']
        assert check_password_hash(Admin.query.one().password_hash, 'first password 1')


def test_the_database_holds_hashes_only(app):
    with app.app_context():
        admin = Admin.query.one()
        assert admin.password_hash != PASSWORD and PASSWORD not in admin.password_hash
    c = login(app)
    code = make_recovery_code(c)
    key = make_key(c)
    with app.app_context():
        admin = Admin.query.one()
        assert admin.recovery_hash == hashlib.sha256(code.replace('-', '').encode()).hexdigest() and code not in admin.recovery_hash
        stored = ApiKey.query.one()
        assert stored.key_hash == hashlib.sha256(key.encode()).hexdigest() and key not in (stored.key_hash, stored.prefix)
        assert key.startswith(stored.prefix)


@pytest.mark.parametrize('bad', ['scrypt:32768:8:1', 'scrypt:32768:8:1$salt', 'scrypt:32768:8:1$salt$', 'plain', 'pbkdf2:sha256:1$$abc', 'scrypt:32768:8:1$sa lt$ab'])
def test_a_corrupted_hash_is_refused_at_start_with_a_hint_about_the_dollar_signs(bad):
    with pytest.raises(RuntimeError, match=r'PASSWORD_HASH.*\$'):
        create_app({'SECRET_KEY': 's' * 32, 'SQLALCHEMY_DATABASE_URI': 'sqlite://', 'ADMIN_PASSWORD_HASH': bad})


def test_a_corrupted_hash_is_ignored_once_an_admin_exists(app):
    create_app({'SECRET_KEY': 's' * 32, 'SQLALCHEMY_DATABASE_URI': app.config['SQLALCHEMY_DATABASE_URI'], 'ADMIN_PASSWORD_HASH': 'scrypt:1:2:3'})


def test_real_hashes_pass_the_shape_check():
    for h in (generate_password_hash('x'), PASSWORD_HASH, generate_password_hash('x', method='pbkdf2:sha256')):
        assert app_module.HASH_RE.match(h), h


def test_an_unusable_admin_id_in_the_environment_is_refused():
    with pytest.raises(RuntimeError, match='ORS_SRS_ADMIN_USERNAME'):
        create_app({'SECRET_KEY': 's' * 32, 'SQLALCHEMY_DATABASE_URI': 'sqlite://', 'ADMIN_USERNAME': 'has space', 'ADMIN_PASSWORD_HASH': PASSWORD_HASH})


# ---- sign-in and sessions ------------------------------------------------------------------------------

def test_the_id_ignores_case_and_surrounding_spaces(app):
    login(app, '  ADMIN ')


def test_unknown_id_and_wrong_password_look_identical_and_both_cost_a_hash_check(app, monkeypatch):
    used = []
    real = app_module.check_password_hash
    monkeypatch.setattr(app_module, 'check_password_hash', lambda h, p: used.append(h) or real(h, p))
    c = app.test_client()
    a = c.post('/admin/login', data={'username': 'admin', 'password': 'wrong'})
    b = c.post('/admin/login', data={'username': 'nobody', 'password': 'wrong'})
    assert a.status_code == b.status_code == 200 and a.get_data() == b.get_data()
    assert len(used) == 2 and used[1] == app_module.DUMMY_HASH  # unknown ID still ran a full comparison


def test_sign_in_records_the_time_and_the_cookie_holds_ids_not_a_flag(app):
    c = login(app)
    with c.session_transaction() as s:
        assert set(s) - {'_flashes'} == {'aid', 'stamp', '_permanent'} and 'admin' not in s
    with app.app_context():
        assert Admin.query.one().last_login_at is not None


def forged_cookie(app, **data):
    return SecureCookieSessionInterface().get_signing_serializer(app).dumps(data)


def test_the_secret_key_alone_cannot_forge_a_session(app):
    """A cookie signed with the right secret still needs the admin's current stamp, which lives only in the database."""
    with app.app_context():
        admin = Admin.query.one()
        aid, stamp = admin.id, admin.stamp
    for data in ({'admin': True}, {'aid': aid}, {'aid': aid, 'stamp': 'guess'}, {'aid': 999, 'stamp': stamp}):
        c = app.test_client()
        c.set_cookie('session', forged_cookie(app, **data))
        assert c.get('/admin').status_code == 302, data
    c = app.test_client()
    c.set_cookie('session', forged_cookie(app, aid=aid, stamp=stamp))
    assert c.get('/admin').status_code == 200  # the control: the genuine stamp does open it


def test_deleting_an_admin_ends_their_session(app):
    c = login(app)
    with app.app_context():
        db.session.delete(Admin.query.one())
        db.session.commit()
    assert c.get('/admin').status_code == 302


def test_sign_in_page_explains_which_id_to_use_and_links_to_recovery(anon):
    html = anon.get('/admin/login').get_data(as_text=True)
    assert 'The ID you chose at setup' in html and '/admin/forgot' in html


# ---- changing the password while signed in -------------------------------------------------------------

def change(c, current=PASSWORD, new=NEW_PASSWORD, confirm=None):
    return c.post('/admin/account/password', data={'current': current, 'password': new, 'confirm': new if confirm is None else confirm}, follow_redirects=True)


@pytest.mark.parametrize('kwargs, message', [
    (dict(current='wrong'), 'current password was not correct'),
    (dict(new='short'), 'at least 10'),
    (dict(confirm='different one!'), 'differ'),
    (dict(new='x' * 129, confirm='x' * 129), '128 characters'),
])
def test_bad_password_changes_are_refused_and_change_nothing(app, kwargs, message):
    c = login(app)
    assert message in change(c, **kwargs).get_data(as_text=True)
    login(app)  # the old password still works


def test_changing_the_password_keeps_this_browser_in_and_signs_the_others_out(app):
    mine, other = login(app), login(app)
    assert 'Password changed' in change(mine).get_data(as_text=True)
    assert mine.get('/admin').status_code == 200
    assert other.get('/admin').status_code == 302 and other.get('/admin/account').status_code == 302
    assert app.test_client().post('/admin/login', data={'username': 'admin', 'password': PASSWORD}).status_code == 200
    login(app, password=NEW_PASSWORD)


def test_changing_the_password_leaves_keys_and_recovery_code_alone(app):
    c = login(app)
    code, key = make_recovery_code(c), make_key(c)
    change(c)
    assert app.test_client().get('/api/v1/me', headers=bearer(key)).status_code == 200
    assert forgot(app.test_client(), code).status_code == 200


def test_account_changes_need_the_current_password(app):
    c = login(app)
    for path, data in [('/admin/account/recovery', {}), ('/admin/account/keys', {'label': 'x'}), ('/admin/account/recovery', {'current': 'nope'})]:
        c.post(path, data=data, follow_redirects=True)
    with app.app_context():
        assert Admin.query.one().recovery_hash is None and ApiKey.query.count() == 0


def test_account_password_forms_are_rate_limited(tmp_path):
    app = make_app(tmp_path, RATELIMIT_ENABLED=True)
    app_module.limiter.reset()
    c = login(app)
    codes = [c.post('/admin/account/password', data={'current': 'bad'}).status_code for _ in range(6)]
    assert codes == [302] * 5 + [429]
    app_module.limiter.reset()


# ---- recovery code and the forgot-password route -------------------------------------------------------

def test_no_recovery_code_at_first_and_the_account_says_so(app):
    c = app.test_client()
    r = c.post('/admin/login', data={'username': 'admin', 'password': PASSWORD}, follow_redirects=True)
    assert 'no recovery code yet' in r.get_data(as_text=True)
    assert 'No recovery code yet' in c.get('/admin/account').get_data(as_text=True)
    assert 'do not match' in forgot(app.test_client(), '0000-0000-0000-0000-0000-0000').get_data(as_text=True)


def test_recovery_code_is_shown_once_and_a_new_one_replaces_the_old(app):
    c = login(app)
    first = make_recovery_code(c)
    assert first not in c.get('/admin/account').get_data(as_text=True)  # gone on reload
    second = make_recovery_code(c)
    assert first != second
    assert 'do not match' in forgot(app.test_client(), first).get_data(as_text=True)
    assert 'Password changed' in forgot(app.test_client(), second).get_data(as_text=True)


@pytest.mark.parametrize('shape', [lambda c: c, lambda c: c.upper(), lambda c: c.replace('-', ''), lambda c: ' ' + c.replace('-', ' ') + ' '])
def test_the_code_may_be_typed_any_reasonable_way(app, shape):
    code = make_recovery_code(login(app))
    assert 'Password changed' in forgot(app.test_client(), shape(code)).get_data(as_text=True)


def test_forgot_password_resets_everything_that_could_be_exposed(app):
    mine = login(app)
    old_code, key = make_recovery_code(mine), make_key(mine)
    r = forgot(app.test_client(), old_code)
    page = r.get_data(as_text=True)
    new_code = CODE_RE.search(page).group(1)
    assert r.status_code == 200 and new_code != old_code and 'Password changed' in page
    assert r.headers['Cache-Control'] == 'no-store'
    login(app, password=NEW_PASSWORD)
    assert app.test_client().post('/admin/login', data={'username': 'admin', 'password': PASSWORD}).status_code == 200
    assert mine.get('/admin').status_code == 302  # every session ended
    assert app.test_client().get('/api/v1/me', headers=bearer(key)).status_code == 401  # every key revoked
    assert 'do not match' in forgot(app.test_client(), old_code).get_data(as_text=True)  # the used code is dead
    assert 'Password changed' in forgot(app.test_client(), new_code, password='yet another password').get_data(as_text=True)


@pytest.mark.parametrize('username, code', [('admin', 'wrong'), ('nobody', 'RIGHT'), ('', 'RIGHT'), ('admin', '')])
def test_wrong_id_or_code_gets_one_generic_message_and_changes_nothing(app, username, code):
    right = make_recovery_code(login(app))
    r = forgot(app.test_client(), right if code == 'RIGHT' else code, username=username)
    assert 'That ID and recovery code do not match.' in r.get_data(as_text=True)
    login(app)  # unchanged


def test_a_rejected_new_password_does_not_use_up_the_code(app):
    code = make_recovery_code(login(app))
    c = app.test_client()
    assert 'at least 10' in forgot(c, code, password='short').get_data(as_text=True)
    assert 'differ' in forgot(c, code, confirm='not the same').get_data(as_text=True)
    assert 'Password changed' in forgot(c, code).get_data(as_text=True)


def test_forgot_is_rate_limited_so_codes_cannot_be_guessed(tmp_path):
    app = make_app(tmp_path, RATELIMIT_ENABLED=True)
    app_module.limiter.reset()
    c = app.test_client()
    codes = [forgot(c, 'guess').status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]
    app_module.limiter.reset()


def test_forgot_requires_a_csrf_token(tmp_path):
    c = make_app(tmp_path, WTF_CSRF_ENABLED=True).test_client()
    assert forgot(c, 'x').status_code == 400


# ---- API keys ------------------------------------------------------------------------------------------

def test_creating_a_key_shows_it_once_and_lists_it_without_the_secret(app):
    c = login(app)
    key = make_key(c, 'my laptop')
    page = c.get('/admin/account').get_data(as_text=True)
    assert key not in page and 'my laptop' in page and key[:12] in page and 'Active' in page


def test_key_needs_a_label_and_the_number_of_active_keys_is_capped(app):
    c = login(app)
    c.post('/admin/account/keys', data={'label': '  ', 'current': PASSWORD})
    for i in range(12):
        c.post('/admin/account/keys', data={'label': f'k{i}', 'current': PASSWORD})
    with app.app_context():
        assert ApiKey.query.count() == 10


def test_revoking_a_key_stops_it_and_only_the_owner_can_revoke(app):
    c = login(app)
    key = make_key(c)
    with app.app_context():
        other = Admin(username='second', password_hash=PASSWORD_HASH)
        db.session.add(other)
        db.session.commit()
        key_id = ApiKey.query.one().id
    intruder = login(app, 'second')
    assert intruder.post(f'/admin/account/keys/{key_id}/revoke').status_code == 404
    assert app.test_client().get('/api/v1/me', headers=bearer(key)).status_code == 200
    assert c.post(f'/admin/account/keys/{key_id}/revoke', follow_redirects=True).status_code == 200
    assert app.test_client().get('/api/v1/me', headers=bearer(key)).status_code == 401
    assert 'Revoked' in c.get('/admin/account').get_data(as_text=True)


def test_each_admin_has_their_own_keys(app):
    c = login(app)
    with app.app_context():
        db.session.add(Admin(username='second', password_hash=PASSWORD_HASH))
        db.session.commit()
    second = login(app, 'second')
    k1 = make_key(c)
    k2 = second.post('/admin/account/keys', data={'label': 'x', 'current': PASSWORD}, follow_redirects=True)
    k2 = KEY_RE.search(k2.get_data(as_text=True)).group(1)
    anon = app.test_client()
    assert anon.get('/api/v1/me', headers=bearer(k1)).get_json()['username'] == 'admin'
    assert anon.get('/api/v1/me', headers=bearer(k2)).get_json()['username'] == 'second'
    with app.app_context():
        assert [k.label for k in Admin.query.filter_by(username='admin').one().api_keys] == ['laptop']


# ---- the JSON API --------------------------------------------------------------------------------------

API_PATHS = ['/api/v1/me', '/api/v1/clients', '/api/v1/clients/some-id', '/api/v1/recent']


@pytest.mark.parametrize('path', API_PATHS)
@pytest.mark.parametrize('headers', [{}, {'Authorization': 'Bearer'}, {'Authorization': 'Bearer nope'}, {'Authorization': 'Basic abc'}, {'Authorization': 'ors_x'}])
def test_api_refuses_missing_or_wrong_keys(anon, path, headers):
    r = anon.get(path, headers=headers)
    assert r.status_code == 401 and r.headers['WWW-Authenticate'] == 'Bearer' and 'error' in r.get_json()


@pytest.mark.parametrize('path', API_PATHS)
def test_a_signed_in_browser_session_does_not_open_the_api(app, admin, path):
    """These endpoints skip CSRF because they never trust cookies. A key exists here, so a fallback to it would show."""
    make_key(admin)
    assert admin.get(path).status_code == 401


def test_the_key_is_accepted_only_in_the_header(app):
    key = make_key(login(app))
    anon = app.test_client()
    assert anon.get(f'/api/v1/me?api_key={key}&key={key}&token={key}').status_code == 401


def test_api_is_read_only(app):
    key = make_key(login(app))
    for method in ('post', 'put', 'delete', 'patch'):
        assert getattr(app.test_client(), method)('/api/v1/clients', headers=bearer(key)).status_code in (404, 405)


def test_api_returns_the_same_data_the_portal_shows(app):
    admin = login(app)
    anon = app.test_client()
    jane = add_client(admin, 'Jane', 'Patel')
    add_client(admin, 'Sam', 'Rivera', 'teen')
    complete_session(admin, anon, jane, [0.1] * 4, [0.5] * 4)
    key = make_key(admin)
    api = lambda path: anon.get(path, headers=bearer(key))

    me = api('/api/v1/me').get_json()
    assert me['username'] == 'admin' and me['key']['label'] == 'laptop' and key.startswith(me['key']['prefix'])

    listing = api('/api/v1/clients').get_json()
    assert listing['total'] == 2 and {c['id'] for c in listing['clients']} == {jane, 'sam-rivera'}
    row = next(c for c in listing['clients'] if c['id'] == jane)
    assert row['sessions'] == 1 and row['needs_attention'] is True and 'ors_low' in row['flags'] and row['status'] == 'completed'
    assert row['ors_latest'] == 4 and row['srs_latest'] == 20
    assert [c['id'] for c in api('/api/v1/clients?attention=1').get_json()['clients']] == [jane]
    assert [c['id'] for c in api('/api/v1/clients?q=rive').get_json()['clients']] == ['sam-rivera']
    assert api('/api/v1/clients?status=none').get_json()['total'] == 1
    assert api('/api/v1/clients?q=%25').get_json()['total'] == 0  # LIKE wildcards stay inert

    detail = api(f'/api/v1/clients/{jane}').get_json()
    assert detail['name'] == 'Jane Patel' and detail['sessions'][0]['ors']['total'] == 4
    assert detail['sessions'][0]['srs']['scores']['Method/Approach'] == 5 and detail['flags'][0]['code'] == 'ors_low'
    assert api('/api/v1/clients/does-not-exist').status_code == 404

    recent = api('/api/v1/recent').get_json()
    assert recent['total'] == 2 and {a['type'] for a in recent['assessments']} == {'ORS', 'SRS'} and recent['assessments'][0]['client'] == jane
    assert api('/api/v1/recent?type=SRS').get_json()['total'] == 1


def test_api_responses_are_never_cached(app):
    key = make_key(login(app))
    r = app.test_client().get('/api/v1/me', headers=bearer(key))
    assert r.headers['Cache-Control'] == 'no-store'


def test_use_of_a_key_is_recorded_at_most_once_a_minute(app):
    c = login(app)
    key = make_key(c)
    anon = app.test_client()
    with app.app_context():
        assert ApiKey.query.one().last_used_at is None
    anon.get('/api/v1/me', headers=bearer(key))
    with app.app_context():
        first = ApiKey.query.one().last_used_at
        assert first is not None
    anon.get('/api/v1/me', headers=bearer(key))
    with app.app_context():
        assert ApiKey.query.one().last_used_at == first
        ApiKey.query.one().last_used_at = utcnow() - timedelta(minutes=5)
        db.session.commit()
    anon.get('/api/v1/me', headers=bearer(key))
    with app.app_context():
        assert ApiKey.query.one().last_used_at > first - timedelta(minutes=1)


def test_the_api_is_rate_limited(tmp_path):
    app = make_app(tmp_path, RATELIMIT_ENABLED=True)
    app_module.limiter.reset()
    key = make_key(login(app))
    anon = app.test_client()
    codes = {anon.get('/api/v1/me', headers=bearer(key)).status_code for _ in range(125)}
    assert codes == {200, 429}
    app_module.limiter.reset()


# ---- command line: setup, admin management and the cli client ------------------------------------------

@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    """A throwaway environment, .env and database for the command-line tools. The real .env is never touched."""
    monkeypatch.setattr(os, 'environ', {'DATABASE_URL': f'sqlite:///{tmp_path / "cli.db"}'})
    monkeypatch.setattr(app_module, 'ENV_FILE', tmp_path / '.env')
    return tmp_path


def type_into(monkeypatch, username, *passwords):
    monkeypatch.setattr(builtins, 'input', lambda prompt='': username)
    answers = iter(passwords)
    monkeypatch.setattr(getpass, 'getpass', lambda prompt='': next(answers))


def cli_app():
    return create_app({'SECRET_KEY': 's' * 32, 'RATELIMIT_ENABLED': False, 'WTF_CSRF_ENABLED': False})


def admins_in(cli_env):
    with app_module.admin_app():
        return {a.username: a.password_hash for a in Admin.query}, Admin.query.count()


def test_setup_creates_the_login_in_the_database_and_the_settings_in_env(cli_env, monkeypatch, capsys):
    type_into(monkeypatch, 'Dr-Demo', NEW_PASSWORD, NEW_PASSWORD)
    app_module.command_setup()
    out = capsys.readouterr().out
    env = app_module.read_env()
    assert len(env['ORS_SRS_SECRET_KEY']) == 64 and env['ORS_SRS_ENV'] == 'dev'
    assert not any(k.startswith('ORS_SRS_ADMIN') for k in env) and NEW_PASSWORD not in (cli_env / '.env').read_text()
    assert 'Sign in with exactly that ID' in out and '"dr-demo"' in out
    os.environ.update(env)
    app = create_app({'RATELIMIT_ENABLED': False, 'WTF_CSRF_ENABLED': False})
    assert app.config['SESSION_COOKIE_SECURE'] is False
    login(app, 'dr-demo', NEW_PASSWORD)
    code = re.search(r'[0-9a-f]{4}(?:-[0-9a-f]{4}){5}', out).group(0)
    assert 'Password changed' in forgot(app.test_client(), code, username='dr-demo').get_data(as_text=True)  # the printed code works


def test_setup_replaces_an_old_env_hash_and_does_not_leave_a_second_admin(cli_env, monkeypatch):
    (cli_env / '.env').write_text("# mine\nORS_SRS_ADMIN_USERNAME='bhargav'\nORS_SRS_ADMIN_PASSWORD_HASH='%s'\nKEEP='me'\n" % generate_password_hash('old password 1'))
    app_module.load_env()  # what python app.py does before any command
    type_into(monkeypatch, 'bhargav', NEW_PASSWORD, NEW_PASSWORD)
    app_module.command_setup()
    admins, count = admins_in(cli_env)
    assert count == 1 and check_password_hash(admins['bhargav'], NEW_PASSWORD)
    text = (cli_env / '.env').read_text()
    assert 'ADMIN' not in text and "KEEP='me'" in text and text.startswith('# mine')


def test_rerunning_setup_resets_the_password_revokes_keys_and_keeps_the_secret(cli_env, monkeypatch):
    type_into(monkeypatch, 'dr-demo', NEW_PASSWORD, NEW_PASSWORD)
    app_module.command_setup()
    secret = app_module.read_env()['ORS_SRS_SECRET_KEY']
    os.environ.update(app_module.read_env())
    app = create_app({'RATELIMIT_ENABLED': False, 'WTF_CSRF_ENABLED': False})
    c = login(app, 'dr-demo', NEW_PASSWORD)
    key = KEY_RE.search(c.post('/admin/account/keys', data={'label': 'k', 'current': NEW_PASSWORD}, follow_redirects=True).get_data(as_text=True)).group(1)
    type_into(monkeypatch, 'dr-demo', 'second password 22', 'second password 22')
    app_module.command_setup()
    assert app_module.read_env()['ORS_SRS_SECRET_KEY'] == secret
    assert c.get('/admin').status_code == 302 and app.test_client().get('/api/v1/me', headers=bearer(key)).status_code == 401
    assert admins_in(cli_env)[1] == 1
    login(create_app({'RATELIMIT_ENABLED': False, 'WTF_CSRF_ENABLED': False}), 'dr-demo', 'second password 22')


@pytest.mark.parametrize('passwords, message', [(('short', 'short'), 'at least 10'), ((NEW_PASSWORD, 'typo typo typo'), 'differ')])
def test_setup_refuses_weak_or_mistyped_passwords_and_writes_nothing(cli_env, monkeypatch, passwords, message):
    type_into(monkeypatch, 'dr-demo', *passwords)
    with pytest.raises(SystemExit, match=message):
        app_module.command_setup()
    assert not (cli_env / '.env').exists() and admins_in(cli_env)[1] == 0


def test_admin_commands(cli_env, monkeypatch, capsys):
    type_into(monkeypatch, 'ignored', NEW_PASSWORD, NEW_PASSWORD)
    app_module.command_create_admin(['Second'])
    out = capsys.readouterr().out
    assert 'Created administrator "second"' in out and re.search(r'[0-9a-f]{4}(-[0-9a-f]{4}){5}', out)
    type_into(monkeypatch, 'ignored', NEW_PASSWORD, NEW_PASSWORD)
    with pytest.raises(ValueError, match='already exists'):
        app_module.command_create_admin(['second'])
    with pytest.raises(SystemExit, match='Usage'):
        app_module.command_create_admin([])

    app_module.command_api_key(['second', 'build', 'server'])
    key = re.search(r'ors_[A-Za-z0-9_-]+', capsys.readouterr().out).group(0)
    app = cli_app()
    me = app.test_client().get('/api/v1/me', headers=bearer(key)).get_json()
    assert me['username'] == 'second' and me['key']['label'] == 'build server'
    with pytest.raises(SystemExit, match='No administrator'):
        app_module.command_api_key(['ghost'])

    app_module.command_admins()
    listing = capsys.readouterr().out
    assert 'second' in listing and 'active API keys 1' in listing and 'recovery code set' in listing
    assert 'scrypt' not in listing and NEW_PASSWORD not in listing and key not in listing

    type_into(monkeypatch, 'ignored', 'third password 33', 'third password 33')
    app_module.command_reset_password(['SECOND'])
    out = capsys.readouterr().out
    assert 'revoked' in out and re.search(r'[0-9a-f]{4}(-[0-9a-f]{4}){5}', out)
    assert cli_app().test_client().get('/api/v1/me', headers=bearer(key)).status_code == 401
    login(cli_app(), 'second', 'third password 33')
    with pytest.raises(SystemExit, match='No administrator'):
        app_module.command_reset_password(['ghost'])


def test_admins_command_says_so_when_there_are_none(cli_env, capsys):
    app_module.command_admins()
    assert 'No administrators yet' in capsys.readouterr().out


def test_unknown_commands_print_the_usage_and_do_nothing(cli_env):
    import subprocess
    import sys
    r = subprocess.run([sys.executable, str(Path(app_module.__file__)), 'bogus'], capture_output=True, text=True, timeout=60,
                       env={**os.environ, 'ORS_SRS_SECRET_KEY': 'x' * 32})
    assert r.returncode != 0 and 'Commands (python app.py' in r.stderr


@pytest.fixture
def live(tmp_path, monkeypatch):
    """The app on a real local port, so the cli client's HTTP code runs for real."""
    app = make_app(tmp_path, RATELIMIT_ENABLED=False)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    admin = login(app)
    jane = add_client(admin, 'Jane', 'Patel')
    complete_session(admin, app.test_client(), jane, [0.1] * 4, [0.5] * 4)
    key = make_key(admin)
    monkeypatch.setenv('ORS_SRS_URL', f'http://127.0.0.1:{server.server_port}')
    monkeypatch.setenv('ORS_SRS_API_KEY', key)
    yield jane
    server.shutdown()


def test_cli_reads_the_running_portal_with_an_api_key(live, capsys):
    app_module.command_cli(['me'])
    assert 'as "admin" with key "laptop"' in capsys.readouterr().out
    app_module.command_cli([])  # no argument means me
    assert 'as "admin"' in capsys.readouterr().out
    app_module.command_cli(['clients'])
    out = capsys.readouterr().out
    assert '1 clients, page 1 of 1' in out and live in out and 'ors_low' in out and 'completed' in out and 'yes' in out
    app_module.command_cli(['clients', 'attention=0'])
    assert '0 clients' in capsys.readouterr().out
    app_module.command_cli(['client', live])
    out = capsys.readouterr().out
    assert 'Jane Patel' in out and 'NEEDS ATTENTION' in out and '[warn]' in out and 'SESSION' in out
    app_module.command_cli(['recent', 'type=ORS'])
    assert '1 assessments' in capsys.readouterr().out
    app_module.command_cli(['me', '--json'])
    assert '"username": "admin"' in capsys.readouterr().out


def test_cli_explains_failures(live, monkeypatch):
    with pytest.raises(SystemExit, match='404'):
        app_module.command_cli(['client', 'nobody'])
    with pytest.raises(SystemExit, match='Usage'):
        app_module.command_cli(['bogus'])
    monkeypatch.setenv('ORS_SRS_API_KEY', 'ors_wrong')
    with pytest.raises(SystemExit, match='401: Missing or invalid API key'):
        app_module.command_cli(['me'])
    monkeypatch.delenv('ORS_SRS_API_KEY')
    with pytest.raises(SystemExit, match='Set ORS_SRS_API_KEY'):
        app_module.command_cli(['me'])
    monkeypatch.setenv('ORS_SRS_API_KEY', 'ors_x')
    monkeypatch.setenv('ORS_SRS_URL', 'http://127.0.0.1:9')
    with pytest.raises(SystemExit, match='Cannot reach'):
        app_module.command_cli(['me'])


def test_cli_refuses_urls_that_are_not_http(monkeypatch):
    monkeypatch.setenv('ORS_SRS_API_KEY', 'ors_x')
    for url in ('file:///etc/passwd', 'ftp://example.test', 'example.test'):
        monkeypatch.setenv('ORS_SRS_URL', url)
        with pytest.raises(SystemExit, match='must start with http'):
            app_module.command_cli(['me'])


def test_the_secret_key_is_not_a_credential_for_the_cli_or_the_api(live, monkeypatch):
    """Putting SECRET_KEY where the API key goes must fail: it only signs cookies."""
    monkeypatch.setenv('ORS_SRS_API_KEY', 's' * 32)
    with pytest.raises(SystemExit, match='401'):
        app_module.command_cli(['me'])
