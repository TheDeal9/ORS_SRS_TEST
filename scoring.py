"""Pure scoring and clinical-reference logic. No Flask, no database.

Reference values (ORS cutoff 25 adult / 28 ages 13-17, reliable change 5,
SRS total cutoff 36, SRS item floor 9) come from the published PCOMS
literature (see the "How to read" note in the portal). The practitioner must
confirm them; they live here only.
"""
import math

ORS_ITEMS = ('Individually', 'Interpersonally', 'Socially', 'Overall')
SRS_ITEMS = ('Relationship', 'Goals and Topics', 'Method/Approach', 'Overall')
ITEMS = {'ORS': ORS_ITEMS, 'SRS': SRS_ITEMS}

ORS_CUTOFF = {'adult': 25, 'teen': 28}
ORS_RELIABLE_CHANGE = 5
SRS_CUTOFF = 36
SRS_ITEM_FLOOR = 9

# Flag codes are stored on the client row, so keep them short and stable.
# code -> (level, dashboard chip, tooltip, filter label). '{c}' is the ORS cutoff for the client's age band.
FLAG_INFO = {
    'ors_low': ('warn', 'ORS < {c}', 'Latest ORS total is below the clinical cutoff of {c}', 'ORS below cutoff'),
    'ors_down': ('warn', 'ORS ↓', 'Reliable deterioration: ORS is {rc} or more points below intake', 'ORS reliable decline'),
    'ors_up': ('good', 'ORS ↑', 'Reliable improvement: ORS is {rc} or more points above intake', 'ORS reliable improvement'),
    'ors_csc': ('good', 'ORS ↑ ✓', 'Reliable and clinically significant improvement: up {rc} or more points and across the cutoff of {c}', 'ORS clinically significant improvement'),
    'srs_low': ('warn', 'SRS < 36', 'Latest SRS total is below 36: invite the client to comment on the session', 'SRS below 36'),
    'srs_item': ('warn', 'SRS item < 9', 'A single SRS line is below 9: invite the client to comment', 'SRS item below 9'),
}


def flag_chip(code, age_band='adult'):
    level, short, tip, _label = FLAG_INFO[code]
    fmt = {'c': ORS_CUTOFF[age_band], 'rc': ORS_RELIABLE_CHANGE}
    return {'code': code, 'level': level, 'short': short.format(**fmt), 'tip': tip.format(**fmt)}


def position_to_score(p):
    """Mark position as a fraction of the line (0 = left end, 1 = right end) -> 0-10, 2 dp."""
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1:
        raise ValueError('position must be a number between 0 and 1')
    return round(p * 10, 2)


def scores_from_positions(kind, positions):
    """Four positions in item order -> {item: score}. Item names are canonical (workbook) names."""
    items = ITEMS[kind]
    if not isinstance(positions, list) or len(positions) != len(items):
        raise ValueError(f'{kind} needs exactly {len(items)} marks')
    return {item: position_to_score(p) for item, p in zip(items, positions)}


def total(scores):
    return round(sum(scores.values()), 2)


def ors_change(first_total, latest_total):
    """Reliable change since intake: 'improved', 'deteriorated' or None."""
    delta = round(latest_total - first_total, 2)
    if delta >= ORS_RELIABLE_CHANGE:
        return 'improved'
    if delta <= -ORS_RELIABLE_CHANGE:
        return 'deteriorated'
    return None


def ors_status(age_band, first_total, value):
    """Status of one ORS total against intake: 'csc' (reliable AND crossed the cutoff upward),
    'up' (reliable improvement), 'down' (reliable deterioration) or None."""
    change = ors_change(first_total, value)
    if change == 'improved':
        return 'csc' if first_total < ORS_CUTOFF[age_band] <= value else 'up'
    return 'down' if change == 'deteriorated' else None


def srs_low_items(scores):
    return [k for k in SRS_ITEMS if scores[k] < SRS_ITEM_FLOOR]


def domain_changes(kind, first_scores, latest_scores):
    """[(item, intake, latest, change)] in item order."""
    return [(i, first_scores[i], latest_scores[i], round(latest_scores[i] - first_scores[i], 2)) for i in ITEMS[kind]]


def client_flags(age_band, ors_totals, srs_scores):
    """Reference flags for one client.

    ors_totals: ORS totals in session order. srs_scores: item scores of the latest SRS or None.
    Returns [{'code','level','short','tip','text'}]; `text` is the detailed sentence for the client page.
    """
    flags = []

    def add(code, text):
        flags.append({**flag_chip(code, age_band), 'text': text})

    cutoff = ORS_CUTOFF[age_band]
    if ors_totals:
        if ors_totals[-1] < cutoff:
            add('ors_low', f'ORS {ors_totals[-1]:g} is below the cutoff of {cutoff}')
        if len(ors_totals) > 1:
            status = ors_status(age_band, ors_totals[0], ors_totals[-1])
            delta = round(ors_totals[-1] - ors_totals[0], 2)
            if status == 'csc':
                add('ors_csc', f'Reliable and clinically significant improvement since intake (+{delta:g}, now above {cutoff})')
            elif status == 'up':
                add('ors_up', f'Reliable improvement since intake (+{delta:g})')
            elif status == 'down':
                add('ors_down', f'Reliable deterioration since intake ({delta:g})')
    if srs_scores:
        srs_total = total(srs_scores)
        if srs_total < SRS_CUTOFF:
            add('srs_low', f'SRS {srs_total:g} is below {SRS_CUTOFF}: invite the client to comment')
        low = srs_low_items(srs_scores)
        if low:
            add('srs_item', f'SRS item below {SRS_ITEM_FLOOR}: ' + ', '.join(low))
    return flags
