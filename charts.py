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
