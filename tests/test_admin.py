"""Phase 2: the in-portal view. Clients list (filters, status, flags, paging), recent assessments,
client page (summary, charts, tooltips), report viewer, deletes, live state, Excel export."""
import io
import re
import zipfile
from datetime import timedelta

import pytest
from openpyxl import load_workbook
from sqlalchemy import UniqueConstraint, event
from sqlalchemy.engine import Engine

import app as app_module
from app import Admin, Assessment, Client, Link, db, refresh_summary, utcnow
from conftest import (ORS_MARKS, SRS_MARKS, add_client, complete_session, db_count, new_link, submit)

LOW_ORS = [0.1, 0.1, 0.1, 0.1]          # total 4: below cutoff
LOW_SRS = [0.5, 0.9, 0.9, 0.9]          # Relationship 5: an item below 9, total 32 below 36


def age_all(app, days):
    with app.app_context():
        for a in Assessment.query:
            a.taken_on -= timedelta(days=days)
        db.session.commit()


def rows_of(html):
    """{slug: row html} for the clients table."""
    table = html.split('<dialog')[0]
    return {re.search(r'<div class="id">([^<]+)</div>', r).group(1): r for r in table.split('<tr class="reveal')[1:]}


def listed(html):
    return list(rows_of(html))


def get(admin, **query):
    return admin.get('/admin', query_string=query).get_data(as_text=True)


def make_states(app, admin, anon):
    """One client per Status value. Returns {state: slug}."""
    ids = {s: add_client(admin, n, 'Test') for s, n in [('none', 'Nora'), ('waiting', 'Walt'), ('in_progress', 'Ida'),
                                                         ('completed', 'Cora'), ('expired', 'Ed'), ('revoked', 'Rex')]}
    new_link(admin, ids['waiting'])
    submit(anon, new_link(admin, ids['in_progress']), 'ORS', ORS_MARKS)
    complete_session(admin, anon, ids['completed'])
    new_link(admin, ids['expired'])
    with app.app_context():
        Link.query.join(Client).filter(Client.slug == ids['expired']).one().expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    new_link(admin, ids['revoked'])
    admin.post(f'/admin/client/{ids["revoked"]}/links/revoke')
    return ids


# ---- clients page: layout, columns, concise flags ------------------------------------------------------

def test_empty_clients_page_guides_the_user(admin):
    html = get(admin)
    assert 'No clients yet' in html and 'Add client' in html and '<table' not in html


def test_header_has_clients_and_recent_assessments_navigation(admin):
    html = get(admin)
    assert 'href="/admin"' in html and 'href="/admin/recent"' in html and 'Recent assessments' in html
    assert 'No assessments' not in html     # recent assessments no longer sit on this page


def test_add_client_form_lives_in_a_drawer_and_opens_itself_after_an_error(admin):
    html = get(admin)
    drawer = re.search(r'<dialog class="drawer" id="addClient"(.*?)</dialog>', html, re.S).group(1)
    assert 'name="first_name"' in drawer and 'name="age_band"' in drawer and 'data-autoopen' not in html
    assert 'data-open="addClient"' in html
    r = admin.post('/admin/clients', data={'first_name': '', 'last_name': 'x', 'age_band': 'adult'})
    assert r.headers['Location'].endswith('/admin?new=1')
    assert 'data-autoopen' in admin.get('/admin?new=1').get_data(as_text=True)


def test_clients_table_has_status_sessions_and_last_session_columns_and_no_link_column(app, admin, anon):
    cid = add_client(admin, 'Cora', 'Test')
    complete_session(admin, anon, cid)
    html = get(admin)
    heads = re.findall(r'<th>([^<]*)</th>', html)
    assert heads == ['Client', 'Status', 'Sessions', 'Last session', 'Latest ORS', 'Latest SRS', 'Flags', '']
    cells = re.findall(r'<td[^>]*>(.*?)</td>', rows_of(html)[cid], re.S)
    assert 'Completed' in cells[1] and cells[2] == '1'
    with app.app_context():
        assert Client.query.one().last_session_on.isoformat() in cells[3]
    assert '25.00' in cells[4] and '38.40' in cells[5]


def test_reference_flags_are_short_chips_with_the_detail_in_a_tooltip(admin, anon):
    cid = add_client(admin, 'Low', 'Scorer')
    complete_session(admin, anon, cid, LOW_ORS, LOW_SRS)
    row = rows_of(get(admin))[cid]
    for chip in ('ORS &lt; 25', 'SRS &lt; 36', 'SRS item &lt; 9'):
        assert f'>{chip}</span>' in row
    assert 'data-tip="Latest ORS total is below the clinical cutoff of 25"' in row
    assert 'ORS 4 is below the cutoff of 25' not in row           # the long client-page sentence is not on the list


def test_chip_text_follows_the_age_band_and_good_flags_are_green(app, admin, anon):
    teen = add_client(admin, 'Teen', 'Ager', 'teen')
    complete_session(admin, anon, teen, [0.3] * 4, SRS_MARKS)            # ORS 12 < 28
    assert '>ORS &lt; 28</span>' in rows_of(get(admin))[teen]
    adult = add_client(admin, 'Gains', 'Made')
    complete_session(admin, anon, adult, [0.4] * 4, SRS_MARKS)            # ORS 16
    age_all(app, 7)
    complete_session(admin, anon, adult, [0.8] * 4, SRS_MARKS)            # ORS 32: +16 and across 25
    row = rows_of(get(admin))[adult]
    assert 'chip good' in row and '>ORS ↑ ✓</span>' in row and 'ORS &lt;' not in row


def test_needs_attention_badge_only_on_flagged_clients(admin, anon):
    ok, low = add_client(admin, 'Fine', 'Client'), add_client(admin, 'Low', 'Client')
    complete_session(admin, anon, ok)
    complete_session(admin, anon, low, LOW_ORS, LOW_SRS)
    rows = rows_of(get(admin))
    assert 'chip attn' in rows[low] and 'Needs attention' in rows[low] and 'Needs attention' not in rows[ok]


def test_stat_cards_count_clients_attention_and_awaiting(app, admin, anon):
    ids = make_states(app, admin, anon)
    complete_session(admin, anon, ids['none'], LOW_ORS, LOW_SRS)          # now flagged, and completed
    cards = {m[1]: int(m[0]) for m in re.findall(r'<b>(\d+)</b><span>([^<]+)</span>', get(admin))}
    assert cards == {'Clients': 6, 'Need attention': 1, 'Awaiting client': 1}


def test_client_names_are_html_escaped(admin):
    cid = add_client(admin, '<script>alert(1)</script>', 'Smith')
    for path in ('/admin', f'/admin/client/{cid}', f'/admin/client/{cid}/report'):
        assert '<script>alert(1)</script>' not in admin.get(path).get_data(as_text=True)


# ---- filters ----------------------------------------------------------------------------------------------

def test_search_matches_name_and_id_case_insensitively(admin):
    add_client(admin, 'Jane', 'Patel'); add_client(admin, 'Sam', 'Rivera')
    for q, hit, miss in [('jane', 'jane-patel', 'sam-rivera'), ('RIVERA', 'sam-rivera', 'jane-patel'),
                         ('sam-riv', 'sam-rivera', 'jane-patel'), ('jane patel', 'jane-patel', 'sam-rivera')]:
        got = listed(get(admin, q=q))
        assert hit in got and miss not in got, q
    assert 'No clients match' in get(admin, q='zzz')


@pytest.mark.parametrize('q', ['%', '_', '\\', "'; DROP TABLE client; --", '%_%'])
def test_search_treats_wildcards_and_sql_as_plain_text(app, admin, q):
    add_client(admin, 'Jane', 'Patel')
    assert 'No clients match' in get(admin, q=q) and db_count(app, Client) == 1


def test_attention_filter_both_directions(admin, anon):
    ok, low = add_client(admin, 'Fine', 'Client'), add_client(admin, 'Low', 'Client')
    complete_session(admin, anon, ok)
    complete_session(admin, anon, low, LOW_ORS, LOW_SRS)
    assert listed(get(admin, attention='1')) == [low]
    assert listed(get(admin, attention='0')) == [ok]
    assert set(listed(get(admin))) == {ok, low}


def test_status_filter_matches_each_status_exactly_and_agrees_with_the_status_column(app, admin, anon):
    ids = make_states(app, admin, anon)
    for state, cid in ids.items():
        html = get(admin, status=state)
        assert listed(html) == [cid], state
        assert f'<span class="chip {state}">{app_module.STATUS_LABELS[state]}</span>' in rows_of(html)[cid]
    everything = rows_of(get(admin))
    for state, cid in ids.items():        # the unfiltered list shows the same label the filter promised
        assert f'chip {state}' in everything[cid]


def test_status_uses_the_newest_link_only(admin, anon):
    cid = add_client(admin)
    complete_session(admin, anon, cid)                      # old link: completed
    new_link(admin, cid)                                    # newest link: waiting
    assert listed(get(admin, status='waiting')) == [cid] and listed(get(admin, status='completed')) == []


def test_age_band_and_flag_filters(app, admin, anon):
    teen = add_client(admin, 'Teen', 'Ager', 'teen'); adult = add_client(admin, 'Adult', 'Person')
    complete_session(admin, anon, teen, LOW_ORS, SRS_MARKS)
    complete_session(admin, anon, adult, [0.9] * 4, LOW_SRS)
    assert listed(get(admin, band='teen')) == [teen] and listed(get(admin, band='adult')) == [adult]
    assert listed(get(admin, flag='ors_low')) == [teen]
    assert listed(get(admin, flag='srs_item')) == [adult] and listed(get(admin, flag='srs_low')) == [adult]
    assert listed(get(admin, flag='ors_csc')) == []


def test_flag_filter_matches_whole_codes_only(app, admin, anon):
    cid = add_client(admin)
    complete_session(admin, anon, cid, [0.4] * 4, SRS_MARKS)
    age_all(app, 7)
    complete_session(admin, anon, cid, [0.8] * 4, SRS_MARKS)     # ors_csc, never ors_up
    assert listed(get(admin, flag='ors_csc')) == [cid] and listed(get(admin, flag='ors_up')) == []


def test_filters_combine_with_and(admin, anon):
    a, b = add_client(admin, 'Ann', 'Low'), add_client(admin, 'Bob', 'Low')
    complete_session(admin, anon, a, LOW_ORS, SRS_MARKS); complete_session(admin, anon, b, LOW_ORS, SRS_MARKS)
    assert set(listed(get(admin, attention='1', flag='ors_low'))) == {a, b}
    assert listed(get(admin, attention='1', flag='ors_low', q='bob')) == [b]
    assert listed(get(admin, attention='1', status='waiting')) == []


@pytest.mark.parametrize('query', [{'attention': 'maybe'}, {'status': 'nope'}, {'band': 'senior'}, {'flag': "x,'--"}, {'sort': 'DROP'},
                                   {'page': 'abc'}, {'page': '-4'}, {'page': '9999'}])
def test_unknown_filter_values_are_ignored_not_trusted(admin, query):
    cid = add_client(admin)
    r = admin.get('/admin', query_string=query)
    assert r.status_code == 200
    if 'page' not in query:
        assert listed(r.get_data(as_text=True)) == [cid]          # treated as no filter at all


def test_sort_orders(app, admin, anon):
    zed = add_client(admin, 'Zed', 'Zulu'); amy = add_client(admin, 'Amy', 'Able'); mid = add_client(admin, 'Mid', 'Dle')
    complete_session(admin, anon, zed, [0.9] * 4, SRS_MARKS)         # ORS 36, healthy
    complete_session(admin, anon, amy, LOW_ORS, SRS_MARKS)           # ORS 4, flagged
    complete_session(admin, anon, mid, [0.5] * 4, SRS_MARKS)         # ORS 20, flagged
    assert listed(get(admin, sort='new')) == [mid, amy, zed]
    assert listed(get(admin, sort='name')) == [amy, mid, zed]
    assert listed(get(admin, sort='ors')) == [amy, mid, zed]
    attention = listed(get(admin, sort='attention'))
    assert set(attention[:2]) == {amy, mid} and attention[2] == zed


def test_sort_last_session_puts_never_seen_clients_last(app, admin, anon):
    never, seen = add_client(admin, 'Never', 'Seen'), add_client(admin, 'Has', 'Session')
    complete_session(admin, anon, seen)
    assert listed(get(admin, sort='recent')) == [seen, never]
    assert listed(get(admin, sort='ors')) == [seen, never]


def test_filter_panel_shows_selected_values_a_count_and_removable_chips(admin):
    add_client(admin)
    html = get(admin, attention='1', status='waiting', q='jane', sort='name')
    assert 'class="count">3</span>' in html                         # attention, status, sort; the search box shows q itself
    assert 'value="jane"' in html and '<option value="1" selected>' in html and '<option value="waiting" selected>' in html
    assert '<option value="name" selected>' in html
    for label in ('Search: jane', 'Needs attention', 'Status: Awaiting client', 'Sort: Name A to Z'):
        assert label in html
    removal = re.search(r'<a href="([^"]+)" aria-label="Remove filter Needs attention"', html).group(1).replace('&amp;', '&')
    assert 'attention' not in removal and 'status=waiting' in removal and 'q=jane' in removal
    assert 'Clear all' in html


def test_unfiltered_page_has_no_count_and_no_chips(admin):
    add_client(admin)
    html = get(admin)
    assert 'class="count"' not in html and 'Showing:' not in html


def test_empty_filter_result_offers_to_clear(admin):
    add_client(admin)
    html = get(admin, status='completed')
    assert 'No clients match these filters' in html and 'Clear all' in html


def test_clients_are_paginated_in_sql_and_paging_keeps_the_filters(app, admin):
    with app.app_context():
        owner = Admin.query.one().id
        db.session.add_all(Client(admin_id=owner, slug=f'bulk-{i:02d}', first_name='Bulk', last_name=f'{i:02d}') for i in range(45))
        db.session.commit()
    p1, p2, p3 = (get(admin, page=n, q='bulk', sort='name') for n in (1, 2, 3))
    assert [len(listed(p)) for p in (p1, p2, p3)] == [20, 20, 5]
    assert listed(p1)[0] == 'bulk-00' and listed(p3)[-1] == 'bulk-44'
    assert 'Page 2 of 3' in p2 and '45 clients' in p2
    nxt = re.search(r'<a href="([^"]+)">Next →</a>', p1).group(1).replace('&amp;', '&')
    assert 'page=2' in nxt and 'q=bulk' in nxt and 'sort=name' in nxt
    assert 'Previous' not in p1 and 'Next' not in p3


def test_client_list_query_count_does_not_grow_with_the_number_of_clients(app, admin):
    def queries_for_dashboard():
        seen = []
        spy = lambda conn, cursor, statement, *a: seen.append(statement)
        event.listen(Engine, 'before_cursor_execute', spy)
        try:
            admin.get('/admin')
        finally:
            event.remove(Engine, 'before_cursor_execute', spy)
        return len(seen)
    with app.app_context():
        owner = Admin.query.one().id
        db.session.add_all(Client(admin_id=owner, slug=f'c-{i}', first_name='C', last_name=str(i), session_count=3, ors_latest=20.0) for i in range(5))
        db.session.commit()
    few = queries_for_dashboard()
    with app.app_context():
        owner = Admin.query.one().id
        db.session.add_all(Client(admin_id=owner, slug=f'd-{i}', first_name='D', last_name=str(i), session_count=3, ors_latest=20.0) for i in range(15))
        db.session.commit()
    assert queries_for_dashboard() == few and few <= 8


# ---- database: slug, summary columns, indexes ----------------------------------------------------------------

def test_slug_is_the_unique_indexed_lookup_key_and_summary_columns_are_indexed():
    uniques = {tuple(c.name for c in u.columns) for u in Client.__table__.constraints if isinstance(u, UniqueConstraint)}
    assert ('admin_id', 'slug') in uniques and not hasattr(Client, 'client_id')  # unique per administrator, not globally
    indexed = {c.name for ix in Client.__table__.indexes for c in ix.columns}
    assert {'needs_attention', 'last_session_on', 'created_at'} <= indexed
    assessment_indexed = {c.name for ix in Assessment.__table__.indexes for c in ix.columns}
    assert {'client_pk', 'link_pk', 'created_at'} <= assessment_indexed


def test_urls_use_the_slug(admin):
    cid = add_client(admin, 'Jane', 'Patel')
    assert admin.get(f'/admin/client/{cid}').status_code == 200 and cid == 'jane-patel'


def summary(app, slug):
    with app.app_context():
        c = Client.query.filter_by(slug=slug).one()
        return (c.session_count, c.last_session_on, c.ors_latest, c.srs_latest, c.flag_codes, c.needs_attention)


def test_new_client_summary_is_empty(app, admin):
    assert summary(app, add_client(admin)) == (0, None, None, None, '', False)


def test_summary_updates_on_save_and_on_delete(app, admin, anon):
    cid = add_client(admin)
    complete_session(admin, anon, cid, LOW_ORS, LOW_SRS)
    count, last, ors, srs, codes, attn = summary(app, cid)
    assert (count, ors, srs, attn) == (1, 4.0, 32.0, True) and last is not None
    assert codes == ',ors_low,srs_low,srs_item,'
    with app.app_context():
        srs_id = Assessment.query.filter_by(type='SRS').one().id
    admin.post(f'/admin/assessment/{srs_id}/delete')
    assert summary(app, cid)[2:] == (4.0, None, ',ors_low,', True)


def test_summary_goes_healthy_again_after_a_better_session(app, admin, anon):
    cid = add_client(admin)
    complete_session(admin, anon, cid, LOW_ORS, LOW_SRS)
    with app.app_context():
        for a in Assessment.query:
            db.session.delete(a)
        db.session.commit()
    complete_session(admin, anon, cid)
    with app.app_context():
        refresh_summary(Client.query.one()); db.session.commit()
    assert summary(app, cid)[2:] == (25.0, 38.4, '', False)


def test_summary_counts_sessions_not_assessments_and_tracks_the_latest_session(app, admin, anon):
    cid = add_client(admin)
    submit(anon, new_link(admin, cid, 'ORS'), 'ORS', [0.1] * 4)
    age_all(app, 14)
    complete_session(admin, anon, cid, [0.9] * 4, SRS_MARKS)
    count, last, ors, srs, codes, attn = summary(app, cid)
    assert count == 2 and ors == 36.0 and srs == 38.4 and ',ors_csc,' in codes and attn is False


def test_deleting_the_last_assessment_resets_the_summary(app, admin, anon):
    cid = add_client(admin)
    submit(anon, new_link(admin, cid, 'ORS'), 'ORS', LOW_ORS)
    with app.app_context():
        aid = Assessment.query.one().id
    admin.post(f'/admin/assessment/{aid}/delete')
    assert summary(app, cid) == (0, None, None, None, '', False)


def test_rebuild_repairs_drifted_summaries(app, admin, anon):
    cid = add_client(admin)
    complete_session(admin, anon, cid, LOW_ORS, LOW_SRS)
    good = summary(app, cid)
    with app.app_context():
        c = Client.query.one()
        c.session_count, c.ors_latest, c.flag_codes, c.needs_attention, c.last_session_on = 99, 1.0, ',bogus,', False, None
        db.session.commit()
        assert summary(app, cid) != good
        for client in Client.query:
            refresh_summary(client)
        db.session.commit()
    assert summary(app, cid) == good


def test_summary_matches_a_fresh_calculation_after_a_mixed_history(app, admin, anon):
    cid = add_client(admin)
    for i, (ors, srs) in enumerate([([0.3] * 4, SRS_MARKS), ([0.5] * 4, LOW_SRS), ([0.7] * 4, SRS_MARKS)]):
        if i:
            age_all(app, 7)       # backdate earlier sessions through the database (the summary only follows real saves)
            with app.app_context():
                refresh_summary(Client.query.one()); db.session.commit()
        complete_session(admin, anon, cid, ors, srs)
    stored = summary(app, cid)
    with app.app_context():
        refresh_summary(Client.query.one()); db.session.commit()
    assert summary(app, cid) == stored


# ---- recent assessments page ------------------------------------------------------------------------------------

def test_recent_page_lists_newest_first_with_scores_and_view_links(app, admin, anon):
    cid = add_client(admin, 'Recent', 'Person')
    complete_session(admin, anon, cid)
    html = admin.get('/admin/recent').get_data(as_text=True)
    assert 'Recent Person' in html and '25.00' in html and '38.40' in html
    assert html.index('<td>SRS</td>') < html.index('<td>ORS</td>')    # SRS was saved last, so it is first
    assert html.count('>View<') == 2


def test_recent_page_empty_state_and_type_filter(admin, anon):
    assert 'No assessments received yet' in admin.get('/admin/recent').get_data(as_text=True)
    complete_session(admin, anon, add_client(admin))
    only = admin.get('/admin/recent?type=ORS').get_data(as_text=True)
    assert only.count('>View<') == 1 and '<td>ORS</td>' in only and '<td>SRS</td>' not in only
    assert admin.get('/admin/recent?type=EVIL').get_data(as_text=True).count('>View<') == 2


def test_recent_page_paginates_and_forgives_bad_pages(app, admin, anon):
    cid = add_client(admin)
    for _ in range(11):                                                # 22 assessments, 20 per page
        complete_session(admin, anon, cid)
        age_all(app, 1)
    p1 = admin.get('/admin/recent').get_data(as_text=True)
    p2 = admin.get('/admin/recent?page=2').get_data(as_text=True)
    assert p1.count('>View<') == 20 and p2.count('>View<') == 2 and 'Page 2 of 2' in p2 and 'Older' in p1 and 'Newer' in p2
    for bad in ('abc', '0', '-3', '999'):
        assert admin.get('/admin/recent', query_string={'page': bad}).status_code == 200


# ---- client page ------------------------------------------------------------------------------------------------------

def test_unknown_client_is_404(admin):
    for path in ('/admin/client/ghost', '/admin/client/ghost/export', '/admin/client/ghost/report'):
        assert admin.get(path).status_code == 404
    assert admin.post('/admin/client/ghost/delete').status_code == 404


def test_new_client_page_shows_empty_states_and_the_ors_cutoff(admin):
    html = admin.get(f'/admin/client/{add_client(admin)}').get_data(as_text=True)
    assert 'No ORS recorded yet' in html and 'No SRS recorded yet' in html and 'No assessments yet' in html
    assert 'No session link has been created yet' in html and 'ORS cutoff 25' in html and 'Nothing to flag' in html
    assert 'Clinical summary' not in html and 'ORS domains' not in html


def test_teen_client_uses_the_28_cutoff(admin):
    assert 'ORS cutoff 28' in admin.get(f'/admin/client/{add_client(admin, "Teen", "Ager", "teen")}').get_data(as_text=True)


def test_client_page_offers_view_report_download_and_print(admin):
    cid = add_client(admin)
    html = admin.get(f'/admin/client/{cid}').get_data(as_text=True)
    assert f'href="/admin/client/{cid}/report"' in html and f'href="/admin/client/{cid}/export"' in html
    assert 'View report' in html and 'Download .xlsx' in html and 'data-print' in html


def history(app, admin, anon, marks):
    cid = add_client(admin)
    for ors, srs in marks:
        complete_session(admin, anon, cid, ors, srs)
        age_all(app, 7)
    return cid


def test_clinical_summary_cards_state_intake_latest_change_and_status(app, admin, anon):
    cid = history(app, admin, anon, [([0.4] * 4, SRS_MARKS), ([0.8] * 4, LOW_SRS)])      # ORS 16 -> 32
    html = admin.get(f'/admin/client/{cid}').get_data(as_text=True)
    assert 'Clinical summary' in html and 'Intake 16 → latest 32' in html and '+16' in html
    assert 'Reliable and clinically significant improvement.' in html and 'At or above the cutoff of 25' in html
    assert 'Average 35.2 over 2 sessions' in html and '1 below 36' in html and 'Relationship below 9' in html


def test_no_reliable_change_is_reported_plainly(app, admin, anon):
    cid = history(app, admin, anon, [([0.5] * 4, SRS_MARKS), ([0.6] * 4, SRS_MARKS)])    # ORS 20 -> 24
    html = admin.get(f'/admin/client/{cid}').get_data(as_text=True)
    assert 'No reliable change yet.' in html and 'Below the cutoff of 25' in html


def test_single_session_is_labelled_intake_only(app, admin, anon):
    cid = history(app, admin, anon, [([0.5] * 4, SRS_MARKS)])
    assert 'Intake only.' in admin.get(f'/admin/client/{cid}').get_data(as_text=True)


def test_client_page_flags_use_the_full_sentences(app, admin, anon):
    cid = history(app, admin, anon, [(LOW_ORS, LOW_SRS)])
    html = admin.get(f'/admin/client/{cid}').get_data(as_text=True)
    assert 'ORS 4 is below the cutoff of 25' in html and 'SRS item below 9: Relationship' in html and 'Needs attention' in html


def test_charts_have_zone_band_status_shapes_and_tooltips(app, admin, anon):
    cid = history(app, admin, anon, [([0.4] * 4, SRS_MARKS), ([0.5] * 4, LOW_SRS), ([0.8] * 4, SRS_MARKS)])   # ORS 16, 20, 32
    html = admin.get(f'/admin/client/{cid}').get_data(as_text=True)
    assert html.count('<svg class="chart') == 3 and html.count('class="band"') == 1 and html.count('class="zone"') == 2
    tips = [t.replace('&#10;', '\n') for t in re.findall(r'data-tip="([^"]*)"', html)]
    ors = [t for t in tips if t.startswith('Session') and 'ORS total' in t]
    assert len(ors) == 3
    assert 'Intake (first ORS)' in ors[0]
    assert '+4 since intake · no reliable change' in ors[1] and '+4 since previous session' in ors[1]
    assert '+16 since intake · reliable and clinically significant improvement' in ors[2]
    assert 'Below the cutoff of 25' in ors[1] and 'At or above the cutoff of 25' in ors[2]
    srs = [t for t in tips if 'SRS total' in t]
    assert len(srs) == 3 and any('Below 9: Relationship' in t for t in srs)
    assert any('SRS total 32 / 40 · below 36' in t for t in srs)
    assert 'class="pt good"' in html and 'class="halo"' in html            # the clinically significant point has a ring


def test_domain_chart_and_change_table(app, admin, anon):
    cid = history(app, admin, anon, [([0.2, 0.5, 0.4, 0.3], SRS_MARKS), ([0.6, 0.5, 0.2, 0.9], SRS_MARKS)])
    html = admin.get(f'/admin/client/{cid}').get_data(as_text=True)
    assert 'ORS domains by session' in html and html.count('class="col"') == 2
    row = lambda name: re.search(rf'<td>{name}</td><td class="num">([^<]+)</td><td class="num">([^<]+)</td><td class="num[^"]*">([^<]+)</td>', html).groups()
    assert row('Individually') == ('2', '6', '+4') and row('Socially') == ('4', '2', '-2') and row('Interpersonally')[2] == '+0'


def test_domain_table_waits_for_two_ors_results(app, admin, anon):
    cid = history(app, admin, anon, [([0.5] * 4, SRS_MARKS)])
    assert 'appear once there are two ORS results' in admin.get(f'/admin/client/{cid}').get_data(as_text=True)


def test_how_to_read_note_cites_sources_and_makes_no_prediction(admin):
    html = admin.get(f'/admin/client/{add_client(admin)}').get_data(as_text=True)
    assert 'How to read these charts' in html and 'tpcjournal.nbcc.org' in html and 'corc.uk.net' in html
    assert 'no "on track" verdict' in html.replace('&#34;', '"')
    assert re.search(r'<a href="https://[^"]+" rel="noreferrer noopener"', html)


def test_link_status_text_follows_the_client_journey(admin, anon):
    cid = add_client(admin)
    page = lambda: admin.get(f'/admin/client/{cid}').get_data(as_text=True)
    assert 'No session link has been created yet' in page()
    token = new_link(admin, cid)
    assert 'Waiting for the client to complete ORS and SRS' in page()
    submit(anon, token, 'ORS', ORS_MARKS)
    assert 'ORS received. Waiting for SRS' in page()
    submit(anon, token, 'SRS', SRS_MARKS)
    assert 'Session 1 complete' in page()
    new_link(admin, cid)
    admin.post(f'/admin/client/{cid}/links/revoke')
    assert 'last link was revoked' in page()


def test_revoke_button_only_appears_while_a_link_is_active(admin):
    cid = add_client(admin)
    assert 'Revoke active link' not in admin.get(f'/admin/client/{cid}').get_data(as_text=True)
    new_link(admin, cid)
    assert 'Revoke active link' in admin.get(f'/admin/client/{cid}').get_data(as_text=True)


# ---- live refresh state -----------------------------------------------------------------------------------------------

def test_activity_state_changes_on_every_visible_event(admin, anon):
    cid = add_client(admin)
    state = lambda: admin.get(f'/admin/client/{cid}/activity.json').get_json()['state']
    seen = [state()]
    token = new_link(admin, cid)
    seen.append(state())
    submit(anon, token, 'ORS', ORS_MARKS)
    seen.append(state())
    submit(anon, token, 'SRS', SRS_MARKS)
    seen.append(state())
    assert len(set(seen)) == 4 and state() == state()
    assert f'data-state="{seen[-1]}"' in admin.get(f'/admin/client/{cid}').get_data(as_text=True)    # page and poll agree


def test_activity_state_changes_when_a_link_expires(app, admin):
    cid = add_client(admin)
    new_link(admin, cid)
    before = admin.get(f'/admin/client/{cid}/activity.json').get_json()['state']
    with app.app_context():
        Link.query.one().expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    assert admin.get(f'/admin/client/{cid}/activity.json').get_json()['state'] != before


# ---- assessment detail --------------------------------------------------------------------------------------------------

def first_id(app, kind):
    with app.app_context():
        return Assessment.query.filter_by(type=kind).first().id


def test_ors_detail_shows_each_item_in_order_with_its_mark(app, admin, anon):
    complete_session(admin, anon, add_client(admin))
    html = admin.get(f'/admin/assessment/{first_id(app, "ORS")}').get_data(as_text=True)
    order = [html.index(x) for x in ('Individually', 'Interpersonally', 'Socially', 'Overall')]
    assert order == sorted(order)
    for score in ('2.00 / 10', '5.00 / 10', '8.00 / 10', '10.00 / 10'):
        assert score in html
    assert 'Total 25.00' in html and html.count('class="stop"') == 4 and 'x1="10" x2="66.0"' in html


def test_srs_detail_flags_items_below_nine(app, admin, anon):
    complete_session(admin, anon, add_client(admin), ORS_MARKS, LOW_SRS)
    html = admin.get(f'/admin/assessment/{first_id(app, "SRS")}').get_data(as_text=True)
    assert 'Method/Approach' in html and '5.00 / 10 (below 9)' in html and html.count('(below 9)') == 1


def test_unknown_assessment_is_404(admin):
    assert admin.get('/admin/assessment/99999').status_code == 404
    assert admin.post('/admin/assessment/99999/delete').status_code == 404


# ---- deletes ------------------------------------------------------------------------------------------------------------

def test_delete_one_assessment_leaves_the_rest(app, admin, anon):
    cid = add_client(admin)
    complete_session(admin, anon, cid)
    r = admin.post(f'/admin/assessment/{first_id(app, "ORS")}/delete', follow_redirects=True)
    assert b'Assessment deleted' in r.data and db_count(app, Assessment) == 1
    assert admin.get(f'/admin/client/{cid}').get_data(as_text=True).count('href="/admin/assessment/') == 1


def test_delete_client_removes_every_trace_in_one_go(app, admin, anon):
    keep, gone = add_client(admin, 'Keep', 'Me'), add_client(admin, 'Drop', 'Me')
    complete_session(admin, anon, keep)
    token = complete_session(admin, anon, gone)
    admin.post(f'/admin/client/{gone}/delete')
    with app.app_context():
        assert [c.slug for c in Client.query] == [keep]
        assert {a.client.slug for a in Assessment.query} == {keep} and Assessment.query.count() == 2
        assert Link.query.count() == 1
    assert admin.get(f'/admin/client/{gone}').status_code == 404 and anon.get(f'/client/{token}').status_code == 404


def test_deleted_client_name_can_be_reused_from_scratch(admin):
    add_client(admin, 'Jane', 'Patel')
    admin.post('/admin/client/jane-patel/delete')
    assert add_client(admin, 'Jane', 'Patel') == 'jane-patel'


# ---- report viewer (pyexcel) ----------------------------------------------------------------------------------------------

def sheets(html):
    out = {}
    for name, body in re.findall(r'data-panel="(\w+)"[^>]*>(.*?)</table>', html, re.S):
        out[name] = [re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.S) for row in re.findall(r'<tr>(.*?)</tr>', body, re.S)]
    return out


def test_report_page_shows_all_three_sheets_with_scores_and_a_download_button(app, admin, anon):
    cid = add_client(admin, 'Jane', 'Patel')
    complete_session(admin, anon, cid)
    html = admin.get(f'/admin/client/{cid}/report').get_data(as_text=True)
    assert html.count('role="tab"') == 3 and 'data-tab="ORS"' in html and 'data-tab="Total"' in html
    assert f'href="/admin/client/{cid}/export"' in html and 'Download .xlsx' in html
    s = sheets(html)
    assert s['ORS'][0] == ['Date', 'Individually', 'Interpersonally', 'Socially', 'Overall']
    assert s['ORS'][1][1:] == ['2.00', '5.00', '8.00', '10.00']
    assert s['SRS'][0][3] == 'Method/Approach' and s['SRS'][1][1:] == ['9.00', '9.50', '9.90', '10.00']
    assert s['Total'][0] == ['Date', 'ORS Total', 'SRS Total'] and s['Total'][1][1:] == ['25.00', '38.40']
    assert s['Total'][1][0] == s['ORS'][1][0]
    assert 'data-panel="SRS" class="sheet-panel hidden"' in html and 'data-panel="ORS" class="sheet-panel "' in html   # ORS tab opens first


def test_report_is_read_through_pyexcel_and_matches_the_download(app, admin, anon):
    cid = add_client(admin)
    complete_session(admin, anon, cid, [0.3] * 4, LOW_SRS)
    age_all(app, 7)
    complete_session(admin, anon, cid)
    with app.app_context():
        tables = app_module.report_tables(Client.query.one())
    wb = load_workbook(io.BytesIO(admin.get(f'/admin/client/{cid}/export').data))
    for name in ('ORS', 'SRS'):
        want = [[c.value for c in row[:5]] for row in wb[name].iter_rows(min_row=2, max_row=3)]
        assert [[v if v != '' else None for v in r] for r in tables[name][1]] == want
    assert tables['Total'][1][0][1:] == [12.0, 32.0] and tables['Total'][1][1][1:] == [25.0, 38.4]


def test_report_handles_a_session_without_an_srs_and_an_empty_client(admin, anon):
    cid = add_client(admin)
    assert 'No sessions yet' in admin.get(f'/admin/client/{cid}/report').get_data(as_text=True)
    submit(anon, new_link(admin, cid, 'ORS'), 'ORS', ORS_MARKS)
    s = sheets(admin.get(f'/admin/client/{cid}/report').get_data(as_text=True))
    assert s['SRS'][1][1:] == ['—'] * 4 and s['Total'][1][1:] == ['25.00', '—']


def test_report_has_only_this_clients_data_and_every_sessions_row(app, admin, anon):
    a, b = add_client(admin, 'Ann', 'A'), add_client(admin, 'Bob', 'B')
    complete_session(admin, anon, a, [1] * 4, SRS_MARKS)
    complete_session(admin, anon, b, [0] * 4, SRS_MARKS)
    s = sheets(admin.get(f'/admin/client/{a}/report').get_data(as_text=True))
    assert s['ORS'][1][1:] == ['10.00'] * 4 and len(s['ORS']) == 2
    for _ in range(3):
        age_all(app, 7)
        complete_session(admin, anon, a)
    assert len(sheets(admin.get(f'/admin/client/{a}/report').get_data(as_text=True))['ORS']) == 5


def test_report_is_not_a_download(admin):
    r = admin.get(f'/admin/client/{add_client(admin)}/report')
    assert r.mimetype == 'text/html' and 'attachment' not in r.headers.get('Content-Disposition', '')


# ---- Excel export (generated from the database, never stored) -----------------------------------------------------------------

def workbook(response):
    assert response.status_code == 200
    assert response.mimetype == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    return load_workbook(io.BytesIO(response.data))


def db_date(app):
    with app.app_context():
        return Assessment.query.first().taken_on.isoformat()


def test_export_matches_the_template_layout(app, admin, anon):
    cid = add_client(admin, 'Jane', 'Patel')
    complete_session(admin, anon, cid)
    r = admin.get(f'/admin/client/{cid}/export')
    assert 'attachment' in r.headers['Content-Disposition'] and 'jane-patel_PCOMS_report.xlsx' in r.headers['Content-Disposition']
    wb = workbook(r)
    assert wb.sheetnames == ['ORS', 'SRS', 'Total']
    today = db_date(app)
    assert [c.value for c in wb['ORS'][2][:5]] == [today, 2.0, 5.0, 8.0, 10.0]
    assert [c.value for c in wb['SRS'][2][:5]] == [today, 9.0, 9.5, 9.9, 10.0]
    assert wb['Total']['B2'].value == '=IF(COUNT(ORS!B2:E2)=0,"",SUM(ORS!B2:E2))'
    assert wb['Total']['C2'].value == '=IF(COUNT(SRS!B2:E2)=0,"",SUM(SRS!B2:E2))'
    assert wb['ORS']['A3'].value is None and wb['Total']['A3'].value is None
    assert all(len(ws._charts) == 1 for ws in wb) and wb.properties.subject == 'Client ID: jane-patel'


def test_export_orders_sessions_and_handles_ors_only_sessions(app, admin, anon):
    cid = add_client(admin)
    submit(anon, new_link(admin, cid, 'ORS'), 'ORS', [.1] * 4)
    age_all(app, 14)
    complete_session(admin, anon, cid)
    ws = workbook(admin.get(f'/admin/client/{cid}/export'))
    assert [ws['ORS'][r][1].value for r in (2, 3)] == [1.0, 2.0]
    assert ws['SRS']['B2'].value is None and ws['SRS']['B3'].value == 9.0


def test_export_of_an_empty_client_has_no_data_rows(admin):
    wb = workbook(admin.get(f'/admin/client/{add_client(admin)}/export'))
    assert wb['ORS']['A2'].value is None and wb['Total']['B2'].value is None and wb['ORS']['A1'].value == 'Date'


def test_each_clients_workbook_contains_only_their_own_data(admin, anon):
    a, b = add_client(admin, 'Ann', 'A'), add_client(admin, 'Bob', 'B')
    complete_session(admin, anon, a, [1] * 4, SRS_MARKS)
    complete_session(admin, anon, b, [0] * 4, SRS_MARKS)
    assert workbook(admin.get(f'/admin/client/{a}/export'))['ORS']['B2'].value == 10.0
    assert workbook(admin.get(f'/admin/client/{b}/export'))['ORS']['B2'].value == 0.0


def test_export_all_zip_has_one_valid_workbook_per_client(admin, anon):
    ids = [add_client(admin, 'Ann', 'A'), add_client(admin, 'Bob', 'B')]
    complete_session(admin, anon, ids[0])
    r = admin.get('/admin/export-all')
    assert r.mimetype == 'application/zip'
    z = zipfile.ZipFile(io.BytesIO(r.data))
    assert sorted(z.namelist()) == ['ann-a_PCOMS_report.xlsx', 'bob-b_PCOMS_report.xlsx']
    assert load_workbook(io.BytesIO(z.read('ann-a_PCOMS_report.xlsx')))['ORS']['E2'].value == 10.0


def test_export_all_with_no_clients_is_an_empty_zip(admin):
    assert zipfile.ZipFile(io.BytesIO(admin.get('/admin/export-all').data)).namelist() == []


def test_export_and_report_write_nothing_to_disk(admin, anon, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cid = add_client(admin)
    complete_session(admin, anon, cid)
    admin.get(f'/admin/client/{cid}/export'); admin.get(f'/admin/client/{cid}/report')
    assert list(tmp_path.glob('*.xlsx')) == [] and not (tmp_path / 'data').exists()
