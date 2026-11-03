"""Server-rendered SVG charts. No JS needed to draw them; static/admin.js adds tooltips and animation.

Every point carries its detail in `data-tip` (and aria-label), so the same text serves mouse hover,
keyboard focus and screen readers. Meaning is never colour alone: status also changes the marker shape.
"""
import math

from markupsafe import Markup, escape

W, H, LEFT, RIGHT, TOP, BOTTOM = 520, 240, 38, 58, 16, 40  # RIGHT leaves room for the cutoff label beside the plot
PAD = 14  # keeps the first and last points off the axis edges


class _Plot:
    def __init__(self, n, ymax):
        self.n, self.ymax, self.w = n, ymax, W - LEFT - RIGHT
        self.inner = self.w - 2 * PAD

    def x(self, i):
        return LEFT + (self.w / 2 if self.n == 1 else PAD + i * self.inner / (self.n - 1))

    def y(self, v):
        return TOP + (H - TOP - BOTTOM) * (1 - v / self.ymax)


def _axes(p, ticks, nos):
    out = []
    for g in ticks:
        out.append(f'<line class="gridline" x1="{LEFT}" x2="{W - RIGHT}" y1="{p.y(g):.1f}" y2="{p.y(g):.1f}"/>'
                   f'<text class="axis" x="{LEFT - 6}" y="{p.y(g) + 4:.1f}" text-anchor="end">{g:g}</text>')
    step = max(1, math.ceil(p.n / 12))  # thin the labels when there are many sessions
    for i, no in enumerate(nos):
        if i % step == 0:
            out.append(f'<text class="axis" x="{p.x(i):.1f}" y="{H - 20}" text-anchor="middle">{no}</text>')
    out.append(f'<text class="axis" x="{LEFT + p.w / 2:.1f}" y="{H - 5}" text-anchor="middle">Session</text>')
    return ''.join(out)


def _marker(shape, x, y):
    if shape == 'up':
        return f'<path class="mk" d="M{x:.1f} {y - 7:.1f}L{x + 6.5:.1f} {y + 5:.1f}H{x - 6.5:.1f}Z"/>'
    if shape == 'down':
        return f'<path class="mk" d="M{x:.1f} {y + 7:.1f}L{x + 6.5:.1f} {y - 5:.1f}H{x - 6.5:.1f}Z"/>'
    if shape == 'square':
        return f'<rect class="mk" x="{x - 5:.1f}" y="{y - 5:.1f}" width="10" height="10" rx="1.5"/>'
    if shape == 'diamond':
        return f'<path class="mk" d="M{x:.1f} {y - 7:.1f}L{x + 7:.1f} {y:.1f}L{x:.1f} {y + 7:.1f}L{x - 7:.1f} {y:.1f}Z"/>'
    return f'<circle class="mk" cx="{x:.1f}" cy="{y:.1f}" r="5"/>'


def _hit(x, y, tip):
    """A 28px-wide invisible target so a small marker is easy to hover, tap or tab to."""
    label = escape(tip.replace('\n', '. '))
    return f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="14" tabindex="0" data-tip="{escape(tip)}" aria-label="{label}"/>'


def trend_svg(points, cutoff, title, band=None, ymax=40):
    """Total-score trend.

    points: [{'no', 'value', 'status', 'ring', 'tip'}] in session order. status is None, 'up', 'csc' or 'down'.
    cutoff: the clinical cutoff (shaded zone below it). band: (low, high) around intake, the no-reliable-change zone.
    """
    if not points:
        return Markup('')
    p = _Plot(len(points), ymax)
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="group" aria-label="{escape(title)}">',
           f'<rect class="zone" x="{LEFT}" y="{p.y(cutoff):.1f}" width="{p.w}" height="{p.y(0) - p.y(cutoff):.1f}"/>']
    if band:
        lo, hi = max(band[0], 0), min(band[1], ymax)
        out.append(f'<rect class="band" x="{LEFT}" y="{p.y(hi):.1f}" width="{p.w}" height="{p.y(lo) - p.y(hi):.1f}"/>')
    out.append(_axes(p, range(0, ymax + 1, 10), [pt['no'] for pt in points]))
    out.append(f'<line class="cutoff" x1="{LEFT}" x2="{W - RIGHT}" y1="{p.y(cutoff):.1f}" y2="{p.y(cutoff):.1f}"/>'
               f'<text class="axis cut" x="{W - RIGHT + 6}" y="{p.y(cutoff) + 4:.1f}">cutoff {cutoff}</text>')
    if p.n > 1:
        line = ' '.join(f'{p.x(i):.1f},{p.y(pt["value"]):.1f}' for i, pt in enumerate(points))
        out.append(f'<polyline class="series" points="{line}"/>')
    for i, pt in enumerate(points):
        x, y, status = p.x(i), p.y(pt['value']), pt.get('status')
        shape = 'up' if status in ('up', 'csc') else 'down' if status == 'down' else 'circle'
        tone = 'good' if status in ('up', 'csc') else 'bad' if status == 'down' else 'flat'
        halo = '<circle class="halo" cx="%.1f" cy="%.1f" r="11"/>' % (x, y) if status == 'csc' or pt.get('ring') else ''
        out.append(f'<g class="pt {tone}{" warnring" if pt.get("ring") else ""}">{halo}{_marker(shape, x, y)}{_hit(x, y, pt["tip"])}</g>')
    out.append('</svg>')
    return Markup(''.join(out))


DOMAIN_SHAPES = ('circle', 'square', 'diamond', 'up')


def domains_svg(rows, items, title):
    """One line per ORS domain (0-10). rows: [{'no', 'date', 'scores'}]. Hover a column to read all four."""
    if not rows:
        return Markup('')
    p = _Plot(len(rows), 10)
    out = [f'<svg class="chart wide" viewBox="0 0 {W} {H}" role="group" aria-label="{escape(title)}">',
           _axes(p, (0, 5, 10), [r['no'] for r in rows])]
    for k, item in enumerate(items):
        if p.n > 1:
            line = ' '.join(f'{p.x(i):.1f},{p.y(r["scores"][item]):.1f}' for i, r in enumerate(rows))
            out.append(f'<polyline class="series s{k + 1}" points="{line}"/>')
        for i, r in enumerate(rows):
            out.append(f'<g class="pt s{k + 1}">{_marker(DOMAIN_SHAPES[k], p.x(i), p.y(r["scores"][item]))}</g>')
    colw = p.inner / (p.n - 1) if p.n > 1 else p.w
    for i, r in enumerate(rows):
        tip = f'Session {r["no"]} · {r["date"]}\n' + '\n'.join(f'{it}: {r["scores"][it]:g}' for it in items)
        out.append(f'<rect class="col" x="{p.x(i) - colw / 2:.1f}" y="{TOP}" width="{colw:.1f}" height="{H - TOP - BOTTOM}" '
                   f'tabindex="0" data-tip="{escape(tip)}" aria-label="{escape(tip.replace(chr(10), ". "))}"/>')
    out.append('</svg>')
    return Markup(''.join(out))


OUTCOME_ORDER = ('csc', 'up', 'flat', 'down')


def outcome_bar_svg(parts, title):
    """One horizontal bar split by outcome. parts: [(class, label, count)] in OUTCOME_ORDER. The exact numbers and the
    glyph that names each part sit in the table beside it, so colour is never the only carrier of meaning."""
    total = sum(n for _c, _l, n in parts)
    if not total:
        return Markup('')
    out = [f'<svg class="chart bar" viewBox="0 0 {W} 56" role="group" aria-label="{escape(title)}">']
    x, width = 4.0, W - 8.0
    for cls, label, n in parts:
        if not n:
            continue
        w = width * n / total
        tip = f'{label}\n{n} of {total} client{"s" if total != 1 else ""} ({100 * n / total:.0f}%)'
        out.append(f'<rect class="seg {cls}" x="{x:.1f}" y="14" width="{w:.1f}" height="28" tabindex="0" '
                   f'data-tip="{escape(tip)}" aria-label="{escape(tip.replace(chr(10), ". "))}"/>')
        x += w
    out.append('</svg>')
    return Markup(''.join(out))


def session_means_svg(rows, title, srs_cutoff, ymax=40):
    """Average ORS and SRS total at each session number, with the number of clients behind each point as bars.
    rows: [{'no', 'ORS': (mean, n)|None, 'SRS': (mean, n)|None}]. Only session numbers that exist are drawn."""
    if not rows:
        return Markup('')
    p = _Plot(len(rows), ymax)
    top_n = max(n for r in rows for k in ('ORS', 'SRS') if r[k] for n in [r[k][1]])
    base = p.y(0)
    out = [f'<svg class="chart wide" viewBox="0 0 {W} {H}" role="group" aria-label="{escape(title)}">',
           _axes(p, range(0, ymax + 1, 10), [r['no'] for r in rows])]
    colw = p.inner / (p.n - 1) if p.n > 1 else p.w
    for i, r in enumerate(rows):  # sample size, drawn first so the lines sit on top
        n = max((r[k][1] for k in ('ORS', 'SRS') if r[k]), default=0)
        h = 38 * n / top_n
        out.append(f'<rect class="nbar" x="{p.x(i) - min(colw, 40) * 0.3:.1f}" y="{base - h:.1f}" width="{min(colw, 40) * 0.6:.1f}" height="{h:.1f}"/>')
    out.append(f'<line class="cutoff" x1="{LEFT}" x2="{W - RIGHT}" y1="{p.y(srs_cutoff):.1f}" y2="{p.y(srs_cutoff):.1f}"/>'
               f'<text class="axis cut" x="{W - RIGHT + 6}" y="{p.y(srs_cutoff) + 4:.1f}">SRS {srs_cutoff}</text>')
    for kind, cls, shape in (('ORS', 's1', 'circle'), ('SRS', 's2', 'square')):
        pts = [(i, r[kind][0]) for i, r in enumerate(rows) if r[kind]]
        if len(pts) > 1:
            out.append(f'<polyline class="series {cls}" points="' + ' '.join(f'{p.x(i):.1f},{p.y(v):.1f}' for i, v in pts) + '"/>')
        for i, v in pts:
            out.append(f'<g class="pt {cls}">{_marker(shape, p.x(i), p.y(v))}</g>')
    for i, r in enumerate(rows):
        tip = f'Session {r["no"]}\n' + '\n'.join(
            f'{k} average {r[k][0]:g} (from {r[k][1]} client{"s" if r[k][1] != 1 else ""})' if r[k] else f'No {k} at this session' for k in ('ORS', 'SRS'))
        out.append(f'<rect class="col" x="{p.x(i) - colw / 2:.1f}" y="{TOP}" width="{colw:.1f}" height="{H - TOP - BOTTOM}" '
                   f'tabindex="0" data-tip="{escape(tip)}" aria-label="{escape(tip.replace(chr(10), ". "))}"/>')
    out.append('</svg>')
    return Markup(''.join(out))
