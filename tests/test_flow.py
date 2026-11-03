"""The client journey: IDs, one-time links, server-side scoring, hidden scores, session numbering."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import IntegrityError

import app as app_module
from app import Assessment, Client, Link, db, utcnow
from conftest import (ORS_MARKS, SRS_MARKS, add_client, complete_session, db_count, make_app, new_link, submit)


def pending_of(html):
    """The forms the page will offer, read from the body's own data attribute."""
    import re
    return [k for k in re.search(r'data-pending="([^"]*)"', html).group(1).split(',') if k]


def assessments(app):
    with app.app_context():
        return [(a.session_no, a.type, a.scores, a.taken_on) for a in Assessment.query.order_by(Assessment.id)]


# ---- unique client IDs ---------------------------------------------------------------------------

def test_client_id_is_first_dash_last_lowercase(admin):
    assert add_client(admin, 'Jane', 'Patel') == 'jane-patel'


def test_same_name_gets_numeric_suffixes_and_never_duplicates(app, admin):
    ids = [add_client(admin, 'Jane', 'Patel') for _ in range(4)]
    assert ids == ['jane-patel', 'jane-patel-2', 'jane-patel-3', 'jane-patel-4']
    assert db_count(app, Client) == 4


def test_names_that_slug_identically_still_get_distinct_ids(admin):
    assert len({add_client(admin, "Jane", "O'Neil"), add_client(admin, 'jane', 'o neil'), add_client(admin, 'JANE', 'O-Neil')}) == 3


def test_accents_and_extra_spaces_are_normalised(app, admin):
    assert add_client(admin, '  José ', ' Núñez  ') == 'jose-nunez'
    with app.app_context():
        c = Client.query.one()
        assert (c.first_name, c.last_name) == ('José', 'Núñez')  # display name keeps accents


def test_non_latin_names_fall_back_to_a_placeholder_but_stay_unique(admin):
    a, b = add_client(admin, 'ऋषि', 'जोशी'), add_client(admin, 'ऋषि', 'जोशी')
    assert a != b and a.startswith('client-client')


def test_age_band_is_stored(app, admin):
    add_client(admin, 'Teen', 'Ager', 'teen')
    with app.app_context():
        assert Client.query.one().age_band == 'teen'


# ---- link page -----------------------------------------------------------------------------------------

def test_valid_link_opens_assessment_with_client_name_and_pending_forms(admin, anon):
    cid = add_client(admin, 'Jane', 'Patel')
    html = anon.get(f'/client/{new_link(admin, cid)}').get_data(as_text=True)
    assert 'Jane Patel' in html and pending_of(html) == ['ORS', 'SRS']


@pytest.mark.parametrize('token', ['nope', 'x' * 500, '%00', 'a b', '%E2%98%83', '..'])
def test_unknown_tokens_get_a_friendly_404_never_a_500(anon, token):
    r = anon.get(f'/client/{token}')
    assert r.status_code == 404 and b'no longer available' in r.data


def test_path_traversal_style_tokens_do_not_route_at_all(anon):
    assert anon.get('/client/..%2f..%2fetc%2fpasswd').status_code == 404


def test_client_page_never_contains_a_score_or_the_clients_id(admin, anon):
    cid = add_client(admin, 'Jane', 'Patel')
    token = new_link(admin, cid)
    submit(anon, token, 'ORS', ORS_MARKS)
    html = anon.get(f'/client/{token}').get_data(as_text=True)   # SRS-only remainder page
    assert cid not in html and 'Total' not in html and 'total' not in html


# ---- saving: server-side scoring, hidden scores -------------------------------------------------------

def test_save_scores_are_computed_on_the_server_from_positions(app, admin, anon):
    token = new_link(admin, add_client(admin), 'ORS')
    r = submit(anon, token, 'ORS', ORS_MARKS)
    assert r.status_code == 200
    (no, kind, scores, _), = assessments(app)
    assert (no, kind) == (1, 'ORS')
    assert scores == {'Individually': 2.0, 'Interpersonally': 5.0, 'Socially': 8.0, 'Overall': 10.0}


def test_save_response_contains_no_scores(admin, anon):
    token = new_link(admin, add_client(admin))
    r = submit(anon, token, 'ORS', [0.123, 0.456, 0.789, 0.5])
    assert r.get_json() == {'ok': True, 'done': False}
    for number in ('1.23', '4.56', '7.89'):
        assert number not in r.get_data(as_text=True)


def test_client_supplied_scores_or_date_are_ignored(app, admin, anon):
    token = new_link(admin, add_client(admin), 'ORS')
    r = anon.post('/api/save', json={'token': token, 'type': 'ORS', 'positions': [.5] * 4, 'date': '2001-01-01',
                                     'scores': {'Individually': 10, 'Interpersonally': 10, 'Socially': 10, 'Overall': 10}})
    assert r.status_code == 200
    (_, _, scores, taken_on), = assessments(app)
    assert set(scores.values()) == {5.0} and taken_on.year != 2001


def test_assessment_date_is_server_today_in_the_configured_timezone(tmp_path):
    app = make_app(tmp_path, TIMEZONE='Pacific/Kiritimati')  # UTC+14: its "today" often differs from UTC's
    admin, anon = app.test_client(), app.test_client()
    admin.post('/admin/login', data={'username': 'admin', 'password': 'correct horse battery staple'})
    token = new_link(admin, add_client(admin), 'ORS')
    submit(anon, token, 'ORS', [.5] * 4)
    assert assessments(app)[0][3] == datetime.now(ZoneInfo('Pacific/Kiritimati')).date()


@pytest.mark.parametrize('body', [
    {'positions': [.5] * 4},                       # no type
    {'type': 'XYZ', 'positions': [.5] * 4},        # unknown type
    {'type': 'ORS'},                               # no marks
    {'type': 'ORS', 'positions': [.5] * 3},        # too few
    {'type': 'ORS', 'positions': [.5] * 5},        # too many
    {'type': 'ORS', 'positions': ['a', 'b', 'c', 'd']},
    {'type': 'ORS', 'positions': [.5, .5, .5, 1.01]},
    {'type': 'ORS', 'positions': [.5, .5, .5, -0.01]},
    {'type': 'ORS', 'positions': [.5, .5, .5, None]},
    {'type': 'ORS', 'positions': [.5, .5, .5, True]},
    {'type': 'ORS', 'positions': [.5, .5, .5, float('nan')]},
    {'type': 'ORS', 'positions': 'abcd'},
    {'type': 'ORS', 'positions': {'Individually': .5}},
])
def test_malformed_submissions_are_rejected_and_save_nothing(app, admin, anon, body):
    token = new_link(admin, add_client(admin), 'ORS')
    assert anon.post('/api/save', json={'token': token, **body}).status_code == 400
    assert db_count(app, Assessment) == 0
    assert anon.get(f'/client/{token}').status_code == 200  # a bad attempt must not burn the link


@pytest.mark.parametrize('kwargs', [
    dict(data='not json', content_type='application/json'),
    dict(data='[1,2,3]', content_type='application/json'),
    dict(data='null', content_type='application/json'),
    dict(data='token=x', content_type='application/x-www-form-urlencoded'),
    dict(data=b'', content_type='application/json'),
])
def test_non_object_bodies_are_400_not_500(anon, kwargs):
    assert anon.post('/api/save', **kwargs).status_code == 400


def test_save_with_bad_or_missing_token_is_403(anon):
    for token in ('nope', '', None, 123, ['x'], {'a': 1}):
        assert anon.post('/api/save', json={'token': token, 'type': 'ORS', 'positions': [.5] * 4}).status_code == 403
    assert anon.post('/api/save', json={'type': 'ORS', 'positions': [.5] * 4}).status_code == 403


# ---- one-time link lifecycle ---------------------------------------------------------------------------------------

def test_link_closes_itself_after_all_requested_forms_are_submitted(app, admin, anon):
    token = new_link(admin, add_client(admin))
    assert submit(anon, token, 'ORS', ORS_MARKS).get_json()['done'] is False
    assert anon.get(f'/client/{token}').status_code == 200            # SRS still pending
    assert submit(anon, token, 'SRS', SRS_MARKS).get_json()['done'] is True
    assert anon.get(f'/client/{token}').status_code == 404            # now spent
    assert submit(anon, token, 'ORS', ORS_MARKS).status_code == 403   # and cannot be replayed
    assert db_count(app, Assessment) == 2


def test_pending_list_shrinks_as_forms_are_submitted(admin, anon):
    token = new_link(admin, add_client(admin))
    assert pending_of(anon.get(f'/client/{token}').get_data(as_text=True)) == ['ORS', 'SRS']
    submit(anon, token, 'ORS', ORS_MARKS)
    assert pending_of(anon.get(f'/client/{token}').get_data(as_text=True)) == ['SRS']


def test_resubmitting_the_same_form_is_409_and_keeps_the_first_answer(app, admin, anon):
    token = new_link(admin, add_client(admin))
    submit(anon, token, 'ORS', [.5] * 4)
    r = submit(anon, token, 'ORS', [.9] * 4)
    assert r.status_code == 409
    assert [s for _, _, s, _ in assessments(app)] == [{'Individually': 5.0, 'Interpersonally': 5.0, 'Socially': 5.0, 'Overall': 5.0}]


def test_single_form_links_close_after_that_form_and_refuse_the_other(admin, anon):
    cid = add_client(admin)
    token = new_link(admin, cid, 'ORS')
    assert submit(anon, token, 'SRS', SRS_MARKS).status_code == 400
    assert submit(anon, token, 'ORS', ORS_MARKS).get_json()['done'] is True
    assert anon.get(f'/client/{token}').status_code == 404


def test_expired_link_is_dead_and_saves_nothing(app, admin, anon):
    token = new_link(admin, add_client(admin))
    with app.app_context():
        Link.query.one().expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    assert anon.get(f'/client/{token}').status_code == 404
    assert submit(anon, token, 'ORS', ORS_MARKS).status_code == 403
    assert db_count(app, Assessment) == 0


def test_link_lives_seven_days(app, admin):
    new_link(admin, add_client(admin))
    with app.app_context():
        link = Link.query.one()
        assert timedelta(days=6, hours=23) < link.expires_at - link.created_at <= timedelta(days=7, seconds=5)


def test_revoking_kills_the_link(app, admin, anon):
    cid = add_client(admin)
    token = new_link(admin, cid)
    admin.post(f'/admin/client/{cid}/links/revoke')
    assert anon.get(f'/client/{token}').status_code == 404
    assert submit(anon, token, 'ORS', ORS_MARKS).status_code == 403


def test_a_new_link_replaces_the_old_unused_one(admin, anon):
    cid = add_client(admin)
    old, new = new_link(admin, cid), new_link(admin, cid)
    assert anon.get(f'/client/{old}').status_code == 404 and anon.get(f'/client/{new}').status_code == 200


def test_link_of_one_client_cannot_write_to_another(app, admin, anon):
    a, b = add_client(admin, 'Ann', 'A'), add_client(admin, 'Bob', 'B')
    ta = new_link(admin, a)
    new_link(admin, b)
    submit(anon, ta, 'ORS', ORS_MARKS)
    with app.app_context():
        assert {x.client.slug for x in Assessment.query} == {a}


def test_unknown_link_kinds_are_rejected(admin):
    cid = add_client(admin)
    assert admin.post(f'/admin/client/{cid}/links', data={'kinds': 'ORS,SRS,XYZ'}).status_code == 400


# ---- session numbering -------------------------------------------------------------------------------------------------

def test_sessions_count_up_per_client_independently(app, admin, anon):
    a, b = add_client(admin, 'Ann', 'A'), add_client(admin, 'Bob', 'B')
    # same day, both forms done -> one session; next link on the same day would pair, so age the rows
    complete_session(admin, anon, a)
    complete_session(admin, anon, b)
    with app.app_context():
        for row in Assessment.query:
            row.taken_on = row.taken_on - timedelta(days=7)
        db.session.commit()
    complete_session(admin, anon, a)
    with app.app_context():
        by = {(x.client.slug, x.session_no) for x in Assessment.query}
    assert by == {(a, 1), (a, 2), (b, 1)}


def test_ors_and_srs_on_separate_links_the_same_day_share_a_session(app, admin, anon):
    cid = add_client(admin)
    submit(anon, new_link(admin, cid, 'ORS'), 'ORS', ORS_MARKS)
    submit(anon, new_link(admin, cid, 'SRS'), 'SRS', SRS_MARKS)
    assert [(n, k) for n, k, _, _ in assessments(app)] == [(1, 'ORS'), (1, 'SRS')]


def test_a_second_ors_the_same_day_starts_a_new_session(app, admin, anon):
    cid = add_client(admin)
    submit(anon, new_link(admin, cid, 'ORS'), 'ORS', ORS_MARKS)
    submit(anon, new_link(admin, cid, 'ORS'), 'ORS', ORS_MARKS)
    assert [n for n, _, _, _ in assessments(app)] == [1, 2]


def test_srs_on_a_later_day_does_not_pair_with_an_old_ors(app, admin, anon):
    cid = add_client(admin)
    submit(anon, new_link(admin, cid, 'ORS'), 'ORS', ORS_MARKS)
    with app.app_context():
        Assessment.query.one().taken_on -= timedelta(days=1)
        db.session.commit()
    submit(anon, new_link(admin, cid, 'SRS'), 'SRS', SRS_MARKS)
    assert [(n, k) for n, k, _, _ in assessments(app)] == [(1, 'ORS'), (2, 'SRS')]


def test_deleting_an_assessment_lets_a_new_link_redo_it_in_the_same_session(app, admin, anon):
    cid = add_client(admin)
    complete_session(admin, anon, cid)
    with app.app_context():
        ors_id = Assessment.query.filter_by(type='ORS').one().id
    admin.post(f'/admin/assessment/{ors_id}/delete')
    submit(anon, new_link(admin, cid, 'ORS'), 'ORS', [.1] * 4)
    assert sorted((n, k) for n, k, _, _ in assessments(app)) == [(1, 'ORS'), (1, 'SRS')]


# ---- integrity backstops ------------------------------------------------------------------------------------------------

def test_database_refuses_a_duplicate_session_and_type(app, admin):
    cid = add_client(admin)
    with app.app_context():
        pk = Client.query.filter_by(slug=cid).one().id
        row = dict(client_pk=pk, session_no=1, type='ORS', taken_on=utcnow().date(), scores={})
        db.session.add(Assessment(**row))
        db.session.commit()
        db.session.add(Assessment(**row))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_a_lost_session_number_race_is_retried_with_the_next_number(app, admin, anon, monkeypatch):
    """Another request took session 1 between our read and our write: the UNIQUE constraint fires, we retry."""
    cid = add_client(admin)
    submit(anon, new_link(admin, cid, 'ORS'), 'ORS', ORS_MARKS)           # session 1 ORS exists
    real, calls = app_module.next_session_no, []

    def stale_then_real(*args):
        calls.append(1)
        return 1 if len(calls) == 1 else real(*args)

    monkeypatch.setattr(app_module, 'next_session_no', stale_then_real)
    token = new_link(admin, cid, 'ORS')
    r = submit(anon, token, 'ORS', ORS_MARKS)
    assert r.status_code == 200 and len(calls) == 2
    assert sorted(n for n, _, _, _ in assessments(app)) == [1, 2]


def test_gives_up_cleanly_if_the_race_never_resolves(app, admin, anon, monkeypatch):
    cid = add_client(admin)
    submit(anon, new_link(admin, cid, 'ORS'), 'ORS', ORS_MARKS)
    monkeypatch.setattr(app_module, 'next_session_no', lambda *a: 1)
    r = submit(anon, new_link(admin, cid, 'ORS'), 'ORS', ORS_MARKS)
    assert r.status_code == 409 and db_count(app, Assessment) == 1
