"""Real simultaneous requests (threads released together by a barrier), against a file-backed database.

Run on SQLite by default; with TEST_DATABASE_URL set the same file exercises Postgres, where the row lock in
lock_client() and the UNIQUE constraints are what keep the data right. No request may end in a 500, no duplicate row
may appear, and the client summary columns must equal what refresh_summary() recomputes from the assessments.
"""
import re
import threading

import pytest

import app as app_module
from app import Admin, Assessment, Client, Link, db, refresh_summary, utcnow
from conftest import ORS_MARKS, SRS_MARKS, PASSWORD, add_client, make_user, new_link, sign_in, submit

LOW_ORS = [0.1, 0.1, 0.1, 0.1]


def race(n, work):
    """Run work(i) in n threads that all start at once. Returns the results; any exception fails the test."""
    barrier, results = threading.Barrier(n), [None] * n

    def run(i):
        try:
            barrier.wait(15)
            results[i] = work(i)
        except Exception as exc:  # noqa: BLE001 - reported below with its thread number
            results[i] = exc
    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads), 'a request hung'
    for i, r in enumerate(results):
        assert not isinstance(r, Exception), f'thread {i}: {r!r}'
    return results


def codes(responses):
    return sorted(r.status_code for r in responses)


def drift(app):
    """Client rows whose stored summary differs from a fresh recomputation. Must be empty."""
    fields = ('session_count', 'last_session_on', 'ors_latest', 'srs_latest', 'flag_codes', 'needs_attention')
    bad = []
    with app.app_context():
        for c in Client.query:
            stored = {f: getattr(c, f) for f in fields}
            refresh_summary(c)
            if stored != {f: getattr(c, f) for f in fields}:
                bad.append((c.slug, stored, {f: getattr(c, f) for f in fields}))
            db.session.rollback()
    return bad


def assert_no_duplicates(app):
    with app.app_context():
        pairs = [(a.client_pk, a.session_no, a.type) for a in Assessment.query]
        per_link = [(a.link_pk, a.type) for a in Assessment.query if a.link_pk]
        assert len(pairs) == len(set(pairs)) and len(per_link) == len(set(per_link))


@pytest.fixture
def owner(app):
    return sign_in(app, 'admin')


# ---- simultaneous submissions ----------------------------------------------------------------------------------------------------

def test_eight_identical_submissions_on_one_link_store_exactly_one(app, owner):
    token = new_link(owner, add_client(owner))
    responses = race(8, lambda i: submit(app.test_client(), token, 'ORS', ORS_MARKS))
    assert codes(responses).count(200) == 1 and set(codes(responses)) <= {200, 403, 409}
    with app.app_context():
        assert Assessment.query.count() == 1
    assert_no_duplicates(app)
    assert drift(app) == []


def test_ors_and_srs_sent_together_from_one_link_share_one_session_number(app, owner):
    token = new_link(owner, add_client(owner))
    responses = race(8, lambda i: submit(app.test_client(), token, 'ORS' if i % 2 else 'SRS', ORS_MARKS if i % 2 else SRS_MARKS))
    assert codes(responses).count(200) == 2 and set(codes(responses)) <= {200, 403, 409}
    with app.app_context():
        rows = Assessment.query.order_by(Assessment.type).all()
        assert [a.type for a in rows] == ['ORS', 'SRS'] and rows[0].session_no == rows[1].session_no == 1
        link = Link.query.one()
        assert link.completed_at is not None and link.session_no == 1
    assert_no_duplicates(app) and drift(app) == []


def test_a_second_session_started_by_two_simultaneous_forms_numbers_both_alike(app, owner):
    slug = add_client(owner)
    first = new_link(owner, slug)
    anon = app.test_client()
    assert submit(anon, first, 'ORS', ORS_MARKS).status_code == 200 and submit(anon, first, 'SRS', SRS_MARKS).status_code == 200
    again = new_link(owner, slug)
    race(2, lambda i: submit(app.test_client(), again, ['ORS', 'SRS'][i], [ORS_MARKS, SRS_MARKS][i]))
    with app.app_context():
        numbers = {(a.type, a.session_no) for a in Assessment.query}
        assert numbers == {('ORS', 1), ('SRS', 1), ('ORS', 2), ('SRS', 2)}
    assert drift(app) == []


def test_many_clients_saving_their_own_links_at_once_all_succeed(app, owner):
    slugs = [add_client(owner, f'Client{i}', 'Race') for i in range(10)]
    tokens = [new_link(owner, s) for s in slugs]
    responses = race(20, lambda i: submit(app.test_client(), tokens[i // 2], ['ORS', 'SRS'][i % 2], [ORS_MARKS, SRS_MARKS][i % 2]))
    assert codes(responses) == [200] * 20
    with app.app_context():
        assert Assessment.query.count() == 20
        for c in Client.query:
            assert c.session_count == 1 and c.ors_latest == 25.0 and c.srs_latest == 38.4
        assert Link.query.filter(Link.completed_at.is_(None)).count() == 0
    assert_no_duplicates(app) and drift(app) == []


def test_clients_of_two_administrators_saving_at_once_stay_apart(app):
    make_user(app, 'bob')
    a, b = sign_in(app, 'admin'), sign_in(app, 'bob')
    pairs = [(a, add_client(a, 'Same', 'Name')) for _ in range(3)] + [(b, add_client(b, 'Same', 'Name')) for _ in range(3)]
    tokens = [new_link(c, s) for c, s in pairs]
    race(6, lambda i: submit(app.test_client(), tokens[i], 'ORS', ORS_MARKS if i < 3 else LOW_ORS))
    with app.app_context():
        for c in Client.query:
            assert c.ors_latest == (25.0 if c.admin.username == 'admin' else 4.0) and c.session_count == 1
        assert {len([c for c in Client.query if c.admin.username == n]) for n in ('admin', 'bob')} == {3}
    assert drift(app) == []


def test_the_race_holds_up_when_repeated(app, owner):
    for round_ in range(10):
        token = new_link(owner, add_client(owner, f'Round{round_}', 'Again'))
        responses = race(6, lambda i: submit(app.test_client(), token, ['ORS', 'SRS'][i % 2], [ORS_MARKS, SRS_MARKS][i % 2]))
        assert codes(responses).count(200) == 2 and set(codes(responses)) <= {200, 403, 409}, (round_, codes(responses))
    with app.app_context():
        assert Assessment.query.count() == 20
    assert_no_duplicates(app) and drift(app) == []


# ---- other simultaneous writes ---------------------------------------------------------------------------------------------------------

def test_eight_people_adding_the_same_name_get_eight_distinct_ids(app, owner):
    responses = race(8, lambda i: _add(app, 'Jane', 'Patel'))
    assert codes(responses) == [302] * 8
    slugs = sorted(r.headers['Location'].rsplit('/', 1)[-1] for r in responses)
    assert slugs == sorted(['jane-patel'] + [f'jane-patel-{n}' for n in range(2, 9)])
    with app.app_context():
        assert Client.query.count() == 8


def _add(app, first, last):
    c = app.test_client()
    c.post('/admin/login', data={'username': 'admin', 'password': PASSWORD})
    return c.post('/admin/clients', data={'first_name': first, 'last_name': last, 'age_band': 'adult'})


def test_simultaneous_new_link_clicks_leave_exactly_one_live_link(app, owner):
    slug = add_client(owner)

    def click(i):
        c = app.test_client()
        c.post('/admin/login', data={'username': 'admin', 'password': PASSWORD})
        return c.post(f'/admin/client/{slug}/links', data={'kinds': 'ORS,SRS'})
    assert codes(race(8, click)) == [302] * 8
    with app.app_context():
        links = Link.query.all()
        assert len(links) == 8 and sum(1 for l in links if l.status() == 'active') == 1
        assert len({l.token_hash for l in links}) == 8


def test_simultaneous_wrong_codes_cannot_exceed_the_five_tries(app):
    make_user(app, 'doc', email='doc@example.test')
    c = app.test_client()
    c.post('/admin/login', data={'username': 'doc', 'password': PASSWORD})
    code = re.search(r'code: (\d{6})', app.config['MAIL_OUTBOX'][-1]['subject']).group(1)
    wrong = '000000' if code != '000000' else '111111'
    with c.session_transaction() as s:
        pre = dict(s['pre'])

    def guess(i):
        d = app.test_client()
        with d.session_transaction() as s:
            s['pre'] = pre
        return d.post('/admin/2fa', data={'code': wrong})
    race(12, guess)
    with app.app_context():
        a = Admin.query.filter_by(username='doc').one()
        assert a.otp_tries == 5 and a.locked_until  # counted one by one: five tries, then dead; ten failures then lock
    d = app.test_client()
    with d.session_transaction() as s:
        s['pre'] = pre
    assert d.post('/admin/2fa', data={'code': code}).status_code == 200  # even the right code no longer works


# ---- the schema itself is the last line of defence --------------------------------------------------------------------------------------

def test_the_database_refuses_duplicate_rows_even_without_the_app_checks(app, owner):
    from sqlalchemy.exc import IntegrityError
    slug = add_client(owner)
    token = new_link(owner, slug)
    submit(app.test_client(), token, 'ORS', ORS_MARKS)
    with app.app_context():
        first = Assessment.query.one()
        for numbers, extra in ((dict(session_no=first.session_no, link_pk=None), 'same client, session and type'),
                               (dict(session_no=first.session_no + 5, link_pk=first.link_pk), 'same link and type')):
            db.session.add(Assessment(client_pk=first.client_pk, type='ORS', taken_on=utcnow().date(), scores=first.scores, **numbers))
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()
        dup_client = Client(admin_id=first.client.admin_id, slug=first.client.slug, first_name='X', last_name='Y')
        db.session.add(dup_client)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        db.session.add(Link(client_pk=first.client_pk, token_hash=Link.query.one().token_hash, kinds='ORS', expires_at=utcnow()))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        assert Assessment.query.count() == 1


def test_hand_entered_rows_without_a_link_do_not_clash(app, owner):
    slug = add_client(owner)
    with app.app_context():
        client = Client.query.filter_by(slug=slug).one()
        for no in (1, 2):
            db.session.add(Assessment(client_pk=client.id, link_pk=None, session_no=no, type='ORS', taken_on=utcnow().date(), scores={}))
        db.session.commit()
        assert Assessment.query.count() == 2  # NULL link_pk is not a duplicate of NULL link_pk


def test_a_summary_that_drifted_is_detected_by_the_audit(app, owner):
    slug = add_client(owner)
    submit(app.test_client(), new_link(owner, slug), 'ORS', ORS_MARKS)
    assert drift(app) == []
    with app.app_context():
        Client.query.one().ors_latest = 3.0
        db.session.commit()
    assert [d[0] for d in drift(app)] == [slug]


def test_one_authenticator_code_sent_twice_at_once_signs_in_once(app):
    from test_2fa import enrol, full_login, set_admin
    make_user(app, 'doc', email='doc@example.test')
    secret = enrol(app, full_login(app))
    set_admin(app, totp_last_step=0)
    browsers = []
    for _ in range(6):
        c, _r = __import__('conftest').login_as(app, 'doc')
        browsers.append(c)
    code = __import__('conftest').totp_now(secret)
    responses = race(6, lambda i: browsers[i].post('/admin/2fa', data={'code': code}))
    assert codes(responses).count(302) == 1 and codes(responses).count(200) == 5  # the other five are replays
    assert sum(1 for c in browsers if c.get('/admin').status_code == 200) == 1


def test_an_admin_removed_by_four_clicks_at_once_is_removed_once_without_an_error(app):
    make_user(app, 'dr-new')
    with app.app_context():
        target = Admin.query.filter_by(username='dr-new').one().id
    responses = race(4, lambda i: _remove(app, target))
    assert set(codes(responses)) <= {302, 404} and codes(responses).count(302) >= 1
    with app.app_context():
        assert db.session.get(Admin, target) is None


def _remove(app, target):
    c = app.test_client()
    c.post('/admin/login', data={'username': 'admin', 'password': PASSWORD})
    return c.post(f'/admin/admins/{target}/remove')
