"""Each administrator sees only their own clients, assessments, reports, exports and API data."""
import io
import re
import zipfile

import pytest
from sqlalchemy.exc import IntegrityError

import app as app_module
from app import Admin, Assessment, Client, Link, db
from conftest import PASSWORD, add_client, complete_session, db_count, make_user, new_link, sign_in, submit

KEY_RE = re.compile(r'value="(ors_[^"]+)"')


def make_key(c, label='laptop'):
    r = c.post('/admin/account/keys', data={'label': label, 'current': PASSWORD}, follow_redirects=True)
    return KEY_RE.search(r.get_data(as_text=True)).group(1)


def bearer(key):
    return {'Authorization': f'Bearer {key}'}


@pytest.fixture
def pair(app):
    """Administrator A (the seeded owner) with 'alice-only' and B with 'bea-only', one finished session each."""
    make_user(app, 'bob')
    a, b, anon = sign_in(app, 'admin'), sign_in(app, 'bob'), app.test_client()
    complete_session(a, anon, add_client(a, 'Alice', 'Only'))
    complete_session(b, anon, add_client(b, 'Bea', 'Only'), ors=[0.1, 0.1, 0.1, 0.1])
    return a, b, anon


def assessment_ids(app, slug):
    with app.app_context():
        return [x.id for x in Assessment.query.join(Client).filter(Client.slug == slug)]


# ---- client IDs are per administrator --------------------------------------------------------------------------------------

def test_two_administrators_can_each_have_a_jane_patel(app):
    make_user(app, 'bob')
    a, b = sign_in(app, 'admin'), sign_in(app, 'bob')
    assert add_client(a) == 'jane-patel' and add_client(b) == 'jane-patel'  # not jane-patel-2: nobody learns about the other
    assert add_client(a) == 'jane-patel-2' and add_client(b) == 'jane-patel-2'  # but one administrator's own are still unique
    with app.app_context():
        assert Client.query.count() == 4 and len({(c.admin_id, c.slug) for c in Client.query}) == 4


def test_the_same_client_id_is_a_different_person_for_each_admin(app):
    make_user(app, 'bob')
    a, b, anon = sign_in(app, 'admin'), sign_in(app, 'bob'), app.test_client()
    complete_session(a, anon, add_client(a, 'Jane', 'Patel'))
    add_client(b, 'Jane', 'Patel')
    assert 'Jane Patel' in b.get('/admin/client/jane-patel').get_data(as_text=True)
    assert b.get('/admin/client/jane-patel/export').status_code == 200
    page = b.get('/admin/client/jane-patel').get_data(as_text=True)
    assert 'No ORS recorded yet' in page and 'No SRS recorded yet' in page  # B's Jane has none of A's data


# ---- other people's pages are 404, never 403 and never data -------------------------------------------------------------------

@pytest.mark.parametrize('method, path', [
    ('get', '/admin/client/alice-only'), ('get', '/admin/client/alice-only/activity.json'),
    ('get', '/admin/client/alice-only/report'), ('get', '/admin/client/alice-only/export'),
    ('post', '/admin/client/alice-only/links'), ('post', '/admin/client/alice-only/links/revoke'),
    ('post', '/admin/client/alice-only/delete'),
])
def test_another_admins_client_routes_are_404(app, pair, method, path):
    a, b, _ = pair
    before = (db_count(app, Client), db_count(app, Link), db_count(app, Assessment))
    assert getattr(b, method)(path).status_code == 404
    assert (db_count(app, Client), db_count(app, Link), db_count(app, Assessment)) == before
    assert getattr(a, method)(path).status_code != 404 or method == 'post'  # the owner of the client still reaches it


def test_another_admins_assessment_is_404_to_view_and_to_delete(app, pair):
    a, b, _ = pair
    ids = assessment_ids(app, 'alice-only')
    assert len(ids) == 2
    for i in ids:
        assert b.get(f'/admin/assessment/{i}').status_code == 404
        assert b.post(f'/admin/assessment/{i}/delete').status_code == 404
        assert a.get(f'/admin/assessment/{i}').status_code == 200
    assert db_count(app, Assessment) == 4
    mine = assessment_ids(app, 'bea-only')[0]
    assert a.get(f'/admin/assessment/{mine}').status_code == 404  # the owner has no special reach either


def test_a_missing_client_and_someone_elses_look_the_same(pair):
    _, b, _ = pair
    other, nothing = b.get('/admin/client/alice-only'), b.get('/admin/client/nobody-at-all')
    assert other.status_code == nothing.status_code == 404 and other.get_data() == nothing.get_data()


def test_b_cannot_make_a_link_for_a_client_it_does_not_own(app, pair):
    a, b, anon = pair
    token = new_link(a, 'alice-only')
    assert b.post('/admin/client/alice-only/links', data={'kinds': 'ORS'}).status_code == 404
    assert anon.get(f'/client/{token}').status_code == 200  # A's live link is untouched


def test_activity_poll_is_scoped_and_still_needs_a_session(app, pair):
    a, b, anon = pair
    assert b.get('/admin/client/alice-only/activity.json').status_code == 404
    assert anon.get('/admin/client/alice-only/activity.json').status_code == 401
    assert a.get('/admin/client/alice-only/activity.json').status_code == 200


# ---- lists, counts, exports ---------------------------------------------------------------------------------------------------

def test_clients_list_search_and_counts_hold_only_my_clients(pair):
    a, b, _ = pair
    page = b.get('/admin').get_data(as_text=True)
    assert 'bea-only' in page and 'alice-only' not in page and 'Alice' not in page
    assert 'alice-only' not in b.get('/admin?q=alice').get_data(as_text=True)
    assert 'bea-only' not in a.get('/admin').get_data(as_text=True)


def test_stats_and_filters_do_not_count_other_admins(app, pair):
    a, b, _ = pair
    with app.app_context():
        owner = Admin.query.filter_by(username='admin').one().id
        db.session.add_all(Client(admin_id=owner, slug=f'extra-{i}', first_name='E', last_name=str(i), needs_attention=True) for i in range(3))
        db.session.commit()
    for slug in ('extra-0', 'extra-1', 'extra-2'):
        assert slug not in b.get('/admin?attention=1').get_data(as_text=True) and b.get(f'/admin/client/{slug}').status_code == 404
    assert 'extra-0' in a.get('/admin?attention=1').get_data(as_text=True)


def test_recent_assessments_lists_only_mine_with_and_without_a_type_filter(pair):
    a, b, _ = pair
    for query in ('', '?type=ORS', '?type=SRS'):
        page = b.get('/admin/recent' + query).get_data(as_text=True)
        assert 'bea-only' in page and 'alice-only' not in page
    assert 'bea-only' not in a.get('/admin/recent').get_data(as_text=True)


def test_export_all_holds_only_my_workbooks(pair):
    a, b, _ = pair
    names = lambda r: zipfile.ZipFile(io.BytesIO(r.data)).namelist()
    assert names(b.get('/admin/export-all')) == ['bea-only_PCOMS_report.xlsx']
    assert names(a.get('/admin/export-all')) == ['alice-only_PCOMS_report.xlsx']


def test_the_report_viewer_and_export_of_my_own_client_work(pair):
    _, b, _ = pair
    assert b.get('/admin/client/bea-only/report').status_code == 200 and b.get('/admin/client/bea-only/export').status_code == 200


def test_deleting_my_client_cannot_touch_the_other_admins_same_named_one(app):
    make_user(app, 'bob')
    a, b, anon = sign_in(app, 'admin'), sign_in(app, 'bob'), app.test_client()
    complete_session(a, anon, add_client(a))
    complete_session(b, anon, add_client(b))
    b.post('/admin/client/jane-patel/delete')
    with app.app_context():
        left = Client.query.all()
        assert [c.admin.username for c in left] == ['admin'] and Assessment.query.count() == 2


# ---- the JSON API ----------------------------------------------------------------------------------------------------------------

def test_api_keys_see_only_their_owners_data(app, pair):
    a, b, _ = pair
    ka, kb = make_key(a), make_key(b)
    c = app.test_client()
    mine = c.get('/api/v1/clients', headers=bearer(kb)).get_json()
    assert mine['total'] == 1 and [x['id'] for x in mine['clients']] == ['bea-only']
    assert c.get('/api/v1/clients/alice-only', headers=bearer(kb)).status_code == 404
    assert c.get('/api/v1/clients/bea-only', headers=bearer(kb)).status_code == 200
    recent = c.get('/api/v1/recent', headers=bearer(kb)).get_json()
    assert recent['total'] == 2 and {x['client'] for x in recent['assessments']} == {'bea-only'}
    assert {x['id'] for x in c.get('/api/v1/clients', headers=bearer(ka)).get_json()['clients']} == {'alice-only'}
    assert c.get('/api/v1/me', headers=bearer(kb)).get_json()['username'] == 'bob'


def test_api_me_reports_the_version(app, pair):
    _, b, _ = pair
    assert app.test_client().get('/api/v1/me', headers=bearer(make_key(b))).get_json()['version'] == app_module.VERSION


# ---- accounts that are not active ------------------------------------------------------------------------------------------------

def test_disabling_an_admin_stops_their_cookie_and_their_api_key(app, pair):
    a, b, _ = pair
    key = make_key(b)
    c = app.test_client()
    assert c.get('/api/v1/me', headers=bearer(key)).status_code == 200 and b.get('/admin').status_code == 200
    with app.app_context():
        Admin.query.filter_by(username='bob').one().status = 'disabled'
        db.session.commit()
    assert c.get('/api/v1/me', headers=bearer(key)).status_code == 401
    assert b.get('/admin').status_code == 302


def test_a_disabled_admins_clients_links_and_data_survive(app, pair):
    a, b, anon = pair
    token = new_link(b, 'bea-only')
    with app.app_context():
        Admin.query.filter_by(username='bob').one().status = 'disabled'
        db.session.commit()
    assert db_count(app, Client) == 2 and anon.get(f'/client/{token}').status_code == 200  # the client in the middle of a session is not cut off


def test_an_admin_with_clients_cannot_be_deleted_from_the_database(app, pair):
    with app.app_context():
        bob = Admin.query.filter_by(username='bob').one()
        db.session.delete(bob)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        assert Client.query.filter_by(slug='bea-only').count() == 1


def test_a_client_must_have_an_owner(app):
    with app.app_context():
        db.session.add(Client(slug='orphan', first_name='O', last_name='P'))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_client_facing_pages_never_name_the_administrator(app, pair):
    a, _, anon = pair
    token = new_link(a, 'alice-only')
    page = anon.get(f'/client/{token}').get_data(as_text=True)
    assert 'alice-only' not in page and 'bob' not in page and 'admin"' not in page
