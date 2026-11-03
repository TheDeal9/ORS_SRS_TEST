"""Calculations done in the database (window functions, GROUP BY) and the caseload page built on them.

The SQL side must agree with the Python rules in scoring.py, so these tests build many clients directly and compare."""
import itertools
import re
import statistics
from datetime import date, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

import app as app_module
import scoring
from app import Admin, Assessment, Client, caseload_by_session, caseload_stats, client_series, db, refresh_summary
from conftest import ORS_MARKS, SRS_MARKS, PASSWORD, add_client, complete_session, make_user, sign_in


def scores_for(kind, total):
    """Four item scores (0 to 10, 2 places) that add up to `total`."""
    each = round(total / 4, 2)
    last = round(total - 3 * each, 2)
    return dict(zip(scoring.ITEMS[kind], [each, each, each, last]))


def seed(app, admin_username, band, ors=(), srs=(), name=None):
    """Insert a client with ORS totals and SRS totals at sessions 1, 2, ... (SRS sessions numbered alike)."""
    with app.app_context():
        owner = Admin.query.filter_by(username=admin_username).one().id
        n = Client.query.count()
        c = Client(admin_id=owner, slug=name or f'c{n:04d}', first_name='C', last_name=str(n), age_band=band)
        db.session.add(c)
        db.session.flush()
        for i, t in enumerate(ors, start=1):
            db.session.add(Assessment(client_pk=c.id, session_no=i, type='ORS', taken_on=date(2026, 1, 1) + timedelta(days=7 * i), scores=scores_for('ORS', t)))
        for i, t in enumerate(srs, start=1):
            db.session.add(Assessment(client_pk=c.id, session_no=i, type='SRS', taken_on=date(2026, 1, 1) + timedelta(days=7 * i), scores=scores_for('SRS', t)))
        db.session.flush()
        refresh_summary(c)
        db.session.commit()
        return c.slug


def stats_for(app, username='admin'):
    with app.app_context():
        return caseload_stats(Admin.query.filter_by(username=username).one().id)


# ---- SQL agrees with scoring.py -----------------------------------------------------------------------------------------------

def test_sql_totals_equal_the_python_total(app):
    cases = [(0, 0), (40, 40), (12.34, 7.89), (24.99, 25.01), (33.33, 1.01)]
    for ors, srs in cases:
        seed(app, 'admin', 'adult', ors=[ors], srs=[srs])
    with app.app_context():
        for c in Client.query:
            for r in client_series(c.id):
                a = db.session.get(Assessment, r['id'])
                assert r['total'] == a.total == scoring.total(a.scores)


def test_sql_outcome_classes_equal_scoring_ors_status_for_every_pair(app):
    """Intake x latest on a grid that includes both sides of every threshold, for both age bands."""
    totals = sorted({0, 3, 6, 9, 12, 15, 18, 20, 22, 24, 24.99, 25, 25.01, 27, 27.99, 28, 28.01, 30, 33, 36, 40})
    expected = {'csc': 0, 'up': 0, 'flat': 0, 'down': 0}
    above = 0
    for band in ('adult', 'teen'):
        for first, latest in itertools.product(totals, totals):
            seed(app, 'admin', band, ors=[first, latest])
            status = scoring.ors_status(band, first, latest)
            expected[{'csc': 'csc', 'up': 'up', 'down': 'down', None: 'flat'}[status]] += 1
            above += latest >= scoring.ORS_CUTOFF[band]
    s = stats_for(app)
    assert {k: s[k] for k in expected} == expected
    assert s['above'] == above and s['judged'] == s['with_ors'] == 2 * len(totals) ** 2
    assert s['csc'] + s['up'] + s['flat'] + s['down'] == s['judged']


def test_reliable_change_boundary_is_exactly_the_scoring_constant(app):
    rc = scoring.ORS_RELIABLE_CHANGE
    for first, latest in ((10, 10 + rc), (10, 10 + rc - 0.01), (30, 30 - rc), (30, 30 - rc + 0.01)):
        seed(app, 'admin', 'adult', ors=[first, latest])
    s = stats_for(app)
    assert (s['up'], s['flat'], s['down'], s['csc']) == (1, 2, 1, 0)


def test_the_cutoff_crossing_rule_for_each_age_band(app):
    seed(app, 'admin', 'adult', ors=[20, 25])    # +5 and now at 25: csc
    seed(app, 'admin', 'teen', ors=[20, 25])     # +5 but 25 < 28: reliable only
    seed(app, 'admin', 'teen', ors=[24, 29])     # +5 and across 28: csc
    seed(app, 'admin', 'adult', ors=[26, 35])    # started above: reliable only
    s = stats_for(app)
    assert (s['csc'], s['up']) == (2, 2)


def test_clients_with_one_ors_count_for_the_cutoff_but_not_for_change(app):
    seed(app, 'admin', 'adult', ors=[30])
    seed(app, 'admin', 'adult', ors=[10])
    seed(app, 'admin', 'adult', srs=[35])      # no ORS at all: not in the ORS caseload
    s = stats_for(app)
    assert (s['with_ors'], s['above'], s['judged']) == (2, 1, 0) and s['mean_change'] is None and s['effect_size'] is None


def test_only_first_and_latest_ors_matter_not_the_ones_between(app):
    seed(app, 'admin', 'adult', ors=[10, 40, 0, 20])
    s = stats_for(app)
    assert (s['judged'], s['up'], s['sum_change'], s['sum_intake']) == (1, 1, 10.0, 10.0)


def test_empty_caseload_is_all_zero_and_the_page_says_so(app):
    s = stats_for(app)
    assert s['with_ors'] == s['judged'] == s['above'] == 0 and s['effect_size'] is None
    page = sign_in(app, 'admin').get('/admin/caseload').get_data(as_text=True)
    assert 'No ORS results yet' in page and '<svg' not in page


# ---- effect size and averages: hand calculations -----------------------------------------------------------------------------

def test_effect_size_formula():
    assert scoring.effect_size(9, 10, 100, 1200) is None  # too few clients
    assert scoring.effect_size(10, 50, 200, 4100) == 1.5  # mean change 5; intake variance (4100 - 4000) / 9, sd 3.33
    assert scoring.effect_size(10, 50, 200, 4000) is None  # everyone started on 20: no spread, no effect size
    assert scoring.EFFECT_SIZE_MIN_CLIENTS == 10


def test_caseload_effect_size_matches_a_statistics_module_calculation(app):
    intakes = [12, 18, 20, 22, 15, 24, 10, 19, 21, 17, 14]
    gains = [8, 3, 6, 10, -2, 5, 7, 4, 9, 1, 6]
    for first, gain in zip(intakes, gains):
        seed(app, 'admin', 'adult', ors=[first, first + gain])
    s = stats_for(app)
    expected = round(statistics.mean(gains) / statistics.stdev(intakes), 2)
    assert s['judged'] == 11 and s['effect_size'] == expected and s['mean_change'] == round(statistics.mean(gains), 1)


def test_no_effect_size_with_nine_clients_but_one_more_brings_it_in(app):
    for i in range(9):
        seed(app, 'admin', 'adult', ors=[10 + i, 15 + i + i])
    assert stats_for(app)['effect_size'] is None
    seed(app, 'admin', 'adult', ors=[5, 30])
    assert stats_for(app)['effect_size'] is not None


def test_averages_by_session_number_with_client_counts(app):
    seed(app, 'admin', 'adult', ors=[10, 20, 30], srs=[30, 34])
    seed(app, 'admin', 'adult', ors=[20, 30], srs=[36])
    seed(app, 'admin', 'adult', ors=[15])
    with app.app_context():
        rows = caseload_by_session(Admin.query.filter_by(username='admin').one().id)
    assert rows == [{'no': 1, 'ORS': (15.0, 3), 'SRS': (33.0, 2)},
                    {'no': 2, 'ORS': (25.0, 2), 'SRS': (34.0, 1)},
                    {'no': 3, 'ORS': (30.0, 1), 'SRS': None}]


# ---- only this administrator's clients ----------------------------------------------------------------------------------------------

def test_another_administrators_clients_never_count(app):
    make_user(app, 'bob')
    seed(app, 'admin', 'adult', ors=[10, 30])
    for _ in range(5):
        seed(app, 'bob', 'adult', ors=[5, 40], srs=[20, 20])
    mine, theirs = stats_for(app), stats_for(app, 'bob')
    assert (mine['with_ors'], mine['sum_change']) == (1, 20.0) and (theirs['with_ors'], theirs['sum_change']) == (5, 175.0)
    with app.app_context():
        assert caseload_by_session(Admin.query.filter_by(username='admin').one().id) == [
            {'no': 1, 'ORS': (10.0, 1), 'SRS': None}, {'no': 2, 'ORS': (30.0, 1), 'SRS': None}]
    page = sign_in(app, 'admin').get('/admin/caseload').get_data(as_text=True)
    assert re.search(r'Clients with an ORS</h3><div class="big">1</div>', page)


# ---- one client's series ------------------------------------------------------------------------------------------------------------

def test_client_series_changes_equal_a_python_calculation(app):
    slug = seed(app, 'admin', 'adult', ors=[14, 19.5, 18, 27], srs=[33, 30.25, 36])
    with app.app_context():
        c = Client.query.filter_by(slug=slug).one()
        rows = client_series(c.id)
        for kind in ('ORS', 'SRS'):
            totals = [(r['no'], r['total']) for r in rows if r['type'] == kind]
            mine = [r for r in rows if r['type'] == kind]
            assert mine[0]['since_intake'] is None and mine[0]['since_prev'] is None
            for i in range(1, len(mine)):
                assert mine[i]['since_intake'] == round(totals[i][1] - totals[0][1], 2)
                assert mine[i]['since_prev'] == round(totals[i][1] - totals[i - 1][1], 2)
        assert [(r['no'], r['type']) for r in rows] == sorted((r['no'], r['type']) for r in rows)


def test_client_series_is_empty_for_a_client_without_results(app):
    with app.app_context():
        owner = Admin.query.one().id
        db.session.add(Client(admin_id=owner, slug='none', first_name='N', last_name='O'))
        db.session.commit()
        assert client_series(Client.query.one().id) == []


# ---- the page and its charts ----------------------------------------------------------------------------------------------------------

def test_page_shows_the_numbers_a_hand_calculation_gives(app):
    seed(app, 'admin', 'adult', ors=[20, 30], srs=[34, 38])       # +10, csc
    seed(app, 'admin', 'adult', ors=[30, 32], srs=[37])           # flat
    seed(app, 'admin', 'adult', ors=[26, 20])                     # +(-6): decline
    seed(app, 'admin', 'teen', ors=[22])                          # one ORS, below 28
    page = sign_in(app, 'admin').get('/admin/caseload').get_data(as_text=True)
    big = re.findall(r'<h3>([^<]+)</h3><div class="big">(.*?)</div>', page)
    values = {k: re.sub(r'<[^>]+>', '', v) for k, v in big}
    assert values['Clients with an ORS'] == '4'
    assert values['At or above the cutoff now'] == '50%'           # 30>=25? latest: 30, 32, 20, 22 -> two of four
    assert values['Reliable improvement'] == '33%' and values['Reliable decline'] == '33%'
    assert values['Average ORS change'] == '+2' and values['Effect size'] == '–'
    assert page.count('<svg') == 2 and 'Hover or focus' in page and 'Shown from 10 clients' in page


def test_charts_are_keyboard_and_screen_reader_friendly_and_inline_style_free(app):
    seed(app, 'admin', 'adult', ors=[20, 30, 31], srs=[34, 38])
    seed(app, 'admin', 'adult', ors=[20, 21], srs=[40])
    page = sign_in(app, 'admin').get('/admin/caseload').get_data(as_text=True)
    assert 'style=' not in page and '<style' not in page and '<script>' not in page
    for svg in re.findall(r'<svg.*?</svg>', page, re.S):
        assert 'role="group"' in svg and 'aria-label="' in svg
        for el in re.findall(r'<(?:rect|circle) [^>]*data-tip="[^>]*>', svg):
            assert 'tabindex="0"' in el and 'aria-label="' in el
    assert 'ORS average 25' in page.replace('&#34;', '"') or 'ORS average' in page
    assert 'from 2 clients' in page and 'from 1 client)' in page


def test_nav_has_a_caseload_link_that_highlights_on_its_page(app):
    c = sign_in(app, 'admin')
    page = c.get('/admin/caseload').get_data(as_text=True)
    assert re.search(r'<a class="nav-link on" href="/admin/caseload">Caseload</a>', page)
    assert not re.search(r'<a class="nav-link on" href="/admin">Clients</a>', page)
    assert 'href="/admin/caseload"' in c.get('/admin').get_data(as_text=True)


def test_caseload_needs_a_sign_in(anon):
    assert anon.get('/admin/caseload').status_code == 302


def test_caseload_query_count_does_not_grow_with_the_caseload(app):
    c = sign_in(app, 'admin')

    def queries():
        seen = []
        spy = lambda conn, cursor, statement, *a: seen.append(statement)
        event.listen(Engine, 'before_cursor_execute', spy)
        try:
            assert c.get('/admin/caseload').status_code == 200
        finally:
            event.remove(Engine, 'before_cursor_execute', spy)
        return len(seen)
    for _ in range(3):
        seed(app, 'admin', 'adult', ors=[10, 20], srs=[30])
    few = queries()
    for _ in range(30):
        seed(app, 'admin', 'adult', ors=[10, 20, 25], srs=[30, 31])
    assert queries() == few and few <= 8


def test_client_page_tooltips_pair_the_two_scales_and_show_srs_change_without_flagging_it(app):
    c = sign_in(app, 'admin')
    anon = app.test_client()
    slug = add_client(c)
    complete_session(c, anon, slug, ors=[0.5, 0.5, 0.5, 0.5], srs=[0.9, 0.9, 0.9, 0.9])   # ORS 20, SRS 36
    complete_session(c, anon, slug, ors=[0.6, 0.6, 0.6, 0.6], srs=[0.8, 0.8, 0.8, 0.8])   # ORS 24, SRS 32
    page = c.get(f'/admin/client/{slug}').get_data(as_text=True).replace('&#34;', '"')
    assert 'SRS that session: 36' in page and 'SRS that session: 32 (below 36)' in page
    assert 'ORS that session: 24' in page and '-4 since previous SRS' in page and '+4 since previous session' in page
    flags = re.search(r'<h2>Reference flags</h2>.*?</section>', page, re.S).group(0)
    assert 'fell' not in flags.lower() and 'srs_drop' not in page  # shown in the tooltip only; no new flag
