"""Pure-logic tests: position-to-score maths, reference flags, SVG chart. No Flask, no database."""
import math

import pytest

import scoring
from charts import domains_svg, trend_svg


# ---- position_to_score -----------------------------------------------------------------

@pytest.mark.parametrize('p, score', [(0, 0.0), (1, 10.0), (0.5, 5.0), (0.8, 8.0), (0.735, 7.35), (0.12345, 1.23), (0.999, 9.99)])
def test_position_maps_to_score_with_two_decimals(p, score):
    assert scoring.position_to_score(p) == score


@pytest.mark.parametrize('bad', [-0.001, 1.001, 2, -1, math.nan, math.inf, -math.inf, '0.5', None, True, False, [0.5], {}])
def test_position_rejects_out_of_range_and_non_numbers(bad):
    with pytest.raises(ValueError):
        scoring.position_to_score(bad)


def test_endpoints_are_inclusive():
    assert scoring.position_to_score(0) == 0 and scoring.position_to_score(1) == 10


# ---- scores_from_positions + total -------------------------------------------------------

def test_ors_scores_use_workbook_item_names_in_order():
    s = scoring.scores_from_positions('ORS', [0.2, 0.5, 0.8, 1.0])
    assert list(s) == ['Individually', 'Interpersonally', 'Socially', 'Overall']
    assert list(s.values()) == [2.0, 5.0, 8.0, 10.0]


def test_srs_third_item_is_method_approach_not_the_client_label():
    s = scoring.scores_from_positions('SRS', [0.1, 0.2, 0.3, 0.4])
    assert list(s) == ['Relationship', 'Goals and Topics', 'Method/Approach', 'Overall']
    assert 'Approach or Method' not in s


@pytest.mark.parametrize('bad', [None, 'abc', [], [0.1] * 3, [0.1] * 5, (0.1, 0.2, 0.3, 0.4), [0.1, 0.2, 0.3, 'x'], [0.1, 0.2, 0.3, 1.5]])
def test_wrong_shape_or_bad_mark_is_rejected(bad):
    with pytest.raises(ValueError):
        scoring.scores_from_positions('ORS', bad)


def test_total_is_rounded_sum():
    assert scoring.total({'a': 0.1, 'b': 0.2, 'c': 0.3, 'd': 0.4}) == 1.0
    assert scoring.total(scoring.scores_from_positions('ORS', [1, 1, 1, 1])) == 40.0


# ---- reliable change ------------------------------------------------------------------------

@pytest.mark.parametrize('first, latest, expected', [
    (20, 25, 'improved'),        # exactly +5 counts
    (20, 24.99, None),           # just under
    (25, 20, 'deteriorated'),    # exactly -5 counts
    (25, 20.01, None),
    (20.1, 25.1, 'improved'),    # float noise must not hide a real 5-point change
    (10, 10, None),
])
def test_ors_reliable_change_boundaries(first, latest, expected):
    assert scoring.ors_change(first, latest) == expected


# ---- client_flags -----------------------------------------------------------------------------

def texts(flags):
    return [f['text'] for f in flags]


def test_no_data_no_flags():
    assert scoring.client_flags('adult', [], None) == []


def test_ors_below_cutoff_warns_but_at_cutoff_does_not():
    assert any('below the cutoff of 25' in t for t in texts(scoring.client_flags('adult', [24.99], None)))
    assert scoring.client_flags('adult', [25], None) == []


def test_teen_cutoff_is_28():
    assert any('cutoff of 28' in t for t in texts(scoring.client_flags('teen', [27.5], None)))
    assert scoring.client_flags('adult', [27.5], None) == []


def test_single_ors_never_reports_change():
    assert not any('since intake' in t for t in texts(scoring.client_flags('adult', [10], None)))


def test_improvement_is_good_and_deterioration_is_warn_both_against_intake():
    up = scoring.client_flags('adult', [20, 22, 26], None)
    assert [f for f in up if 'improvement' in f['text']][0]['level'] == 'good'
    down = scoring.client_flags('adult', [35, 33, 29], None)
    assert [f for f in down if 'deterioration' in f['text']][0]['level'] == 'warn'
    # +3 since the previous session but only +4 since intake: not reliable
    assert not any('since intake' in t for t in texts(scoring.client_flags('adult', [20, 21, 24], None)))


def test_srs_total_below_36_warns_but_36_does_not():
    low = {'Relationship': 9, 'Goals and Topics': 9, 'Method/Approach': 9, 'Overall': 8.99}
    assert any('below 36' in t for t in texts(scoring.client_flags('adult', [], low)))
    ok = {'Relationship': 9, 'Goals and Topics': 9, 'Method/Approach': 9, 'Overall': 9}
    assert scoring.client_flags('adult', [], ok) == []


def test_srs_single_item_below_9_warns_even_when_total_is_fine():
    s = {'Relationship': 8.99, 'Goals and Topics': 10, 'Method/Approach': 10, 'Overall': 10}
    flags = scoring.client_flags('adult', [], s)
    assert len(flags) == 1 and 'Relationship' in flags[0]['text']


# ---- reference-flag metadata (codes are stored on the client row, so they must stay stable) -----------

def test_every_flag_code_has_level_chip_tip_and_filter_label():
    for code, info in scoring.FLAG_INFO.items():
        level, chip, tip, label = info
        assert level in ('warn', 'good') and chip and tip and label, code
        assert len(scoring.flag_chip(code)['short']) <= 14, 'dashboard chips must stay short'
        assert ',' not in code  # codes are stored comma-delimited


def test_chip_text_uses_the_clients_age_band_cutoff():
    assert scoring.flag_chip('ors_low', 'adult')['short'] == 'ORS < 25'
    assert scoring.flag_chip('ors_low', 'teen')['short'] == 'ORS < 28'
    assert '28' in scoring.flag_chip('ors_csc', 'teen')['tip']


def test_flags_carry_code_chip_and_long_text():
    flags = scoring.client_flags('adult', [10], {'Relationship': 5, 'Goals and Topics': 9, 'Method/Approach': 9, 'Overall': 9})
    assert [f['code'] for f in flags] == ['ors_low', 'srs_low', 'srs_item']
    assert flags[0]['short'] == 'ORS < 25' and 'below the cutoff of 25' in flags[0]['text']
    assert all(f['level'] == 'warn' for f in flags)


@pytest.mark.parametrize('band, first, value, expected', [
    ('adult', 20, 25, 'csc'),      # +5 and lands exactly on the cutoff
    ('adult', 20, 24, None),       # +4 only
    ('adult', 30, 36, 'up'),       # reliable but was already above the cutoff: not "crossed"
    ('adult', 24, 29.5, 'csc'),
    ('teen', 20, 26, 'up'),        # teen cutoff is 28, so +6 to 26 has not crossed it
    ('teen', 20, 28, 'csc'),
    ('adult', 25, 20, 'down'),
    ('adult', 10, 10, None),
])
def test_ors_status_separates_reliable_from_clinically_significant(band, first, value, expected):
    assert scoring.ors_status(band, first, value) == expected


def test_clinically_significant_replaces_plain_improvement_flag():
    codes = [f['code'] for f in scoring.client_flags('adult', [18, 22, 27], None)]
    assert 'ors_csc' in codes and 'ors_up' not in codes


def test_srs_low_items_lists_only_lines_below_nine_in_order():
    s = {'Relationship': 8.99, 'Goals and Topics': 9, 'Method/Approach': 3, 'Overall': 10}
    assert scoring.srs_low_items(s) == ['Relationship', 'Method/Approach']


def test_domain_changes_are_per_item_differences_in_item_order():
    first = {'Individually': 2, 'Interpersonally': 5, 'Socially': 4, 'Overall': 3}
    latest = {'Individually': 5.5, 'Interpersonally': 4, 'Socially': 4, 'Overall': 9}
    assert scoring.domain_changes('ORS', first, latest) == [
        ('Individually', 2, 5.5, 3.5), ('Interpersonally', 5, 4, -1), ('Socially', 4, 4, 0), ('Overall', 3, 9, 6)]


# ---- charts -----------------------------------------------------------------------------------

import re


def pt(no, value, status=None, ring=False, tip='tip'):
    return {'no': no, 'value': value, 'status': status, 'ring': ring, 'tip': tip}


def test_chart_empty_is_blank():
    assert str(trend_svg([], 25, 'ORS')) == '' and str(domains_svg([], scoring.ORS_ITEMS, 'x')) == ''


def test_chart_single_point_has_marker_and_target_but_no_line():
    svg = str(trend_svg([pt(1, 30.0)], 25, 'ORS'))
    assert 'class="mk"' in svg and 'class="hit"' in svg and '<polyline' not in svg


def test_chart_has_clinical_zone_cutoff_and_optional_reliable_change_band():
    two = [pt(1, 20.0), pt(2, 30.0)]
    svg = str(trend_svg(two, 25, 'ORS'))
    assert '<polyline' in svg and 'class="zone"' in svg and 'cutoff 25' in svg and 'class="band"' not in svg
    assert 'class="band"' in str(trend_svg(two, 25, 'ORS', band=(15, 25)))


def test_reliable_change_band_is_clipped_to_the_axis():
    svg = str(trend_svg([pt(1, 3.0)], 25, 'ORS', band=(-2, 8)))
    y_of_zero = float(re.search(r'class="gridline"[^>]*y1="([\d.]+)"', svg).group(1))
    band = re.search(r'class="band" x="[\d.]+" y="([\d.]+)" width="[\d.]+" height="([\d.]+)"', svg)
    assert float(band.group(1)) + float(band.group(2)) <= y_of_zero + 0.2   # never drawn below the zero line


def test_status_changes_marker_shape_not_just_colour():
    shapes = {}
    for status in (None, 'up', 'csc', 'down'):
        svg = str(trend_svg([pt(1, 10.0, status)], 25, 'ORS'))
        shapes[status] = ('circle' if '<circle class="mk"' in svg else 'path')
    assert shapes[None] == 'circle' and shapes['up'] == shapes['csc'] == shapes['down'] == 'path'
    up = str(trend_svg([pt(1, 10.0, 'up')], 25, 'ORS')); down = str(trend_svg([pt(1, 10.0, 'down')], 25, 'ORS'))
    assert up != down and 'pt good' in up and 'pt bad' in down


def test_clinically_significant_and_low_item_points_get_a_ring():
    assert 'class="halo"' in str(trend_svg([pt(1, 10.0, 'csc')], 25, 'ORS'))
    assert 'class="halo"' in str(trend_svg([pt(1, 10.0, ring=True)], 25, 'SRS'))
    assert 'class="halo"' not in str(trend_svg([pt(1, 10.0, 'up')], 25, 'ORS'))


def test_every_point_has_a_hover_and_screen_reader_description_and_is_keyboard_reachable():
    svg = str(trend_svg([pt(1, 10.0, tip='Session 1\nORS total 10 / 40')], 25, 'ORS'))
    assert 'data-tip="Session 1\nORS total 10 / 40"' in svg and 'aria-label="Session 1. ORS total 10 / 40"' in svg and 'tabindex="0"' in svg


def test_chart_escapes_untrusted_text():
    svg = str(trend_svg([pt(1, 20.0, tip='<script>alert(1)</script>')], 25, '"><img src=x>'))
    assert '<script>' not in svg and '<img' not in svg


def test_chart_points_stay_inside_the_plot():
    svg = str(trend_svg([pt(1, 0.0), pt(2, 40.0)], 25, 'x'))
    for cy in re.findall(r'cy="([\d.]+)"', svg):
        assert 0 < float(cy) < 240


def test_many_sessions_thin_their_axis_labels():
    svg = str(trend_svg([pt(i, 20.0) for i in range(1, 41)], 25, 'x'))
    assert len(re.findall(r'class="axis" x="[\d.]+" y="220"', svg)) <= 14


def rows(n):
    return [{'no': i, 'date': f'2026-11-{i:02d}', 'scores': dict(zip(scoring.ORS_ITEMS, (1.5 * i, 2.0, 3.0, 4.0)))} for i in range(1, n + 1)]


def test_domains_chart_has_four_series_four_shapes_and_one_hover_column_per_session():
    svg = str(domains_svg(rows(3), scoring.ORS_ITEMS, 'ORS domains'))
    assert svg.count('<polyline') == 4 and svg.count('class="col"') == 3
    assert all(f'series s{k}' in svg for k in (1, 2, 3, 4))
    assert '<circle class="mk"' in svg and '<rect class="mk"' in svg and svg.count('<path class="mk"') == 6   # diamond + triangle x 3
    assert 'Individually: 1.5' in svg and 'Overall: 4' in svg and 'Session 1' in svg


def test_domains_chart_with_one_session_has_no_lines():
    assert '<polyline' not in str(domains_svg(rows(1), scoring.ORS_ITEMS, 'x'))
