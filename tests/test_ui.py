"""Phase 3 and UI: the page must satisfy the strict Content-Security-Policy, dark mode must be complete and
readable, animations must be optional, and the client page must work with a keyboard."""
import re
from pathlib import Path

import pytest

from conftest import ORS_MARKS, PASSWORD, SRS_MARKS, add_client, complete_session, new_link, submit

STATIC = Path(__file__).resolve().parent.parent / 'static'


def css():
    return (STATIC / 'app.css').read_text()


def js(name):
    return (STATIC / name).read_text()


@pytest.fixture
def pages(app, admin, anon):
    """Every kind of page the app serves, with enough data that charts and tables render."""
    cid = add_client(admin, 'Jane', 'Patel')
    complete_session(admin, anon, cid, [0.4] * 4, SRS_MARKS)
    complete_session(admin, anon, cid, [0.8] * 4, [0.5, 0.9, 0.9, 0.9])
    token = new_link(admin, cid)
    from app import Assessment
    with app.app_context():
        aid = Assessment.query.first().id
    html = lambda c, path: c.get(path).get_data(as_text=True)
    secret_page = admin.post('/admin/account/recovery', data={'current': PASSWORD}, follow_redirects=True).get_data(as_text=True)
    code = re.search(r'value="([0-9a-f]{4}(?:-[0-9a-f]{4}){5})"', secret_page).group(1)
    key_page = admin.post('/admin/account/keys', data={'label': 'laptop', 'current': PASSWORD}, follow_redirects=True).get_data(as_text=True)
    from app import Admin, db
    with app.app_context():
        Admin.query.filter_by(username='admin').one().email = 'owner@example.test'  # totp setup needs a confirmed email
        db.session.commit()
    half = app.test_client()  # password right, code not yet entered
    half.post('/admin/signup', data={'username': 'dr-new', 'email': 'new@example.test', 'password': PASSWORD, 'confirm': PASSWORD})
    verifying = html(half, '/admin/verify')
    from conftest import make_user
    make_user(app, 'doc', email='doc@example.test')
    second = app.test_client()
    second.post('/admin/login', data={'username': 'doc', 'password': PASSWORD})
    out = {
        'signup': html(anon, '/admin/signup'), 'verify': verifying, 'twofactor': html(second, '/admin/2fa'),
        'totp': html(admin, '/admin/account/totp'), 'admins': html(admin, '/admin/admins'), 'caseload': html(admin, '/admin/caseload'),
        'landing': html(anon, '/'), 'login': html(anon, '/admin/login'), 'closed': html(anon, '/client/nope'),
        'assessment': html(anon, f'/client/{token}'), 'clients': html(admin, '/admin'), 'recent': html(admin, '/admin/recent'),
        'client': html(admin, f'/admin/client/{cid}'), 'detail': html(admin, f'/admin/assessment/{aid}'),
        'report': html(admin, f'/admin/client/{cid}/report'),
        'forgot': html(anon, '/admin/forgot'), 'account': html(admin, '/admin/account'),
        'account_secret': secret_page, 'account_key': key_page,
    }
    # last, because resetting the password ends the admin's session
    out['recovery_done'] = anon.post('/admin/forgot', data={'username': 'admin', 'code': code, 'password': 'another long password',
                                                            'confirm': 'another long password'}).get_data(as_text=True)
    return out


# ---- strict CSP: nothing inline, nothing third-party ---------------------------------------------------

PAGES = ['signup', 'verify', 'twofactor', 'totp', 'admins', 'caseload', 'landing', 'login', 'closed', 'assessment', 'clients', 'recent', 'client', 'detail', 'report',
         'forgot', 'account', 'account_secret', 'account_key', 'recovery_done']


@pytest.mark.parametrize('name', PAGES)
def test_no_inline_style_script_or_handler_anywhere(pages, name):
    html = pages[name]
    assert '<style' not in html, 'inline <style> is blocked by the CSP'
    assert not re.search(r'\sstyle=', html), 'inline style attribute is blocked by the CSP'
    assert not re.search(r'<[^>]+\son[a-z]+=', html), 'inline event handler is blocked by the CSP'
    assert not re.search(r'<script(?![^>]*\ssrc=)', html), 'inline <script> is blocked by the CSP'
    assert 'javascript:' not in html


@pytest.mark.parametrize('name', PAGES)
def test_pages_load_only_local_files(pages, name):
    html = pages[name]
    for tag in re.findall(r'<(?:script|link|img|iframe|form|source)\b[^>]*>', html):
        for url in re.findall(r'(?:src|href|action)="([^"]*)"', tag):
            assert not url.startswith(('http:', 'https:', '//')), tag
    for url in re.findall(r'url\(([^)]+)\)', html):
        assert 'http' not in url


def test_external_links_do_not_leak_the_referrer_or_window(pages):
    for name in PAGES:
        for tag in re.findall(r'<a\b[^>]*href="https?://[^"]*"[^>]*>', pages[name]):
            assert 'noreferrer' in tag and 'noopener' in tag, tag


@pytest.mark.parametrize('name', ['assessment.js', 'admin.js', 'theme.js'])
def test_scripts_emit_no_inline_styles_handlers_or_eval(name):
    src = js(name)
    assert not re.search(r'style\s*=\s*\\?["\']', src.replace('.style', '')), 'JS-generated markup must not use style attributes'
    assert not re.search(r'\bon[a-z]+\s*=\s*\\?["\']', src.replace('.onclick', ''))
    assert 'eval(' not in src and 'new Function' not in src and 'document.write' not in src


def test_scripts_are_loaded_deferred_and_motion_comes_first(pages):
    for name in ('clients', 'recent', 'client', 'report', 'login', 'signup', 'verify', 'twofactor', 'totp', 'admins', 'caseload'):
        tags = re.findall(r'<script src="([^"]+)"([^>]*)>', pages[name])
        srcs = [t[0] for t in tags]
        assert srcs[0].endswith('theme.js') and not tags[0][1].strip()           # theme first, blocking, no flash of the wrong theme
        assert srcs[-2].endswith('vendor/motion.js') and srcs[-1].endswith('admin.js') and all('defer' in t[1] for t in tags[1:])
    assert re.findall(r'<script src="([^"]+)"', pages['assessment']) == ['/static/theme.js', '/static/assessment.js']


def test_static_files_are_served_with_the_right_types_and_nothing_else_is(anon):
    for path, kind in [('/static/app.css', 'text/css'), ('/static/admin.js', 'javascript'), ('/static/assessment.js', 'javascript'),
                       ('/static/theme.js', 'javascript'), ('/static/vendor/motion.js', 'javascript')]:
        r = anon.get(path)
        assert r.status_code == 200 and kind in r.mimetype, path
    for path in ('/app.py', '/scoring.py', '/data/app.db', '/PCOMS_template.xlsx', '/.env', '/tests/conftest.py',
                 '/static/../app.py', '/static/..%2fapp.py', '/static/%2e%2e/app.py', '/requirements.txt', '/.git/config'):
        assert anon.get(path).status_code == 404, path


# ---- dark mode -------------------------------------------------------------------------------------------

def blocks():
    text = css()
    light = re.search(r':root \{(.*?)\n\}', text, re.S).group(1)
    media = re.search(r'@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme="light"\]\) \{(.*?)\n  \}', text, re.S).group(1)
    toggle = re.search(r':root\[data-theme="dark"\] \{(.*?)\n\}', text, re.S).group(1)
    var = lambda block: dict(re.findall(r'(--[\w-]+):\s*([^;]+);', block))
    return var(light), var(media), var(toggle)


def test_dark_theme_is_defined_for_the_os_setting_and_for_the_toggle_identically():
    light, media, toggle = blocks()
    assert media == toggle and len(toggle) > 20
    assert set(media) <= set(light), 'every dark token must also exist in light'


def test_dark_theme_redefines_every_surface_and_text_token():
    light, _, dark = blocks()
    for token in ('--bg', '--surface', '--surface-2', '--text', '--text-2', '--muted', '--line', '--primary', '--on-primary',
                  '--soft', '--soft-text', '--danger-bg', '--danger', '--ok-bg', '--ok', '--s1', '--s2', '--s3', '--s4', '--grid', '--zone', '--band'):
        assert token in dark and dark[token] != light[token], token


def luminance(hex_):
    if len(hex_) == 4:                                    # #abc -> #aabbcc
        hex_ = '#' + ''.join(ch * 2 for ch in hex_[1:])
    r, g, b = (int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_text_is_readable_in_both_themes(theme):
    light, _, dark = blocks()
    t = {**light, **dark} if theme == 'dark' else light
    for fg, bg, minimum in [('--text', '--surface', 7), ('--text-2', '--surface', 4.5), ('--muted', '--surface', 4.5), ('--muted', '--bg', 4.5), ('--muted', '--soft', 4.5), ('--muted', '--surface-2', 4.5),
                            ('--on-primary', '--primary', 7), ('--soft-text', '--soft', 7), ('--text', '--bg', 7),
                            ('--danger', '--surface', 4.5), ('--ok', '--surface', 4.5), ('--info', '--surface', 4.5)]:
        assert contrast(t[fg], t[bg]) >= minimum, f'{theme}: {fg} on {bg} = {contrast(t[fg], t[bg]):.2f}'


def test_chart_series_are_visible_against_the_chart_surface_in_both_themes():
    light, _, dark = blocks()
    for theme, t in (('light', light), ('dark', {**light, **dark})):
        for s in ('--s1', '--s2', '--s3', '--s4', '--good', '--bad'):
            assert contrast(t[s], t['--surface']) >= 2.0, f'{theme} {s}'     # graphical objects: distinguishable, labelled by shape and text too


def test_theme_toggle_exists_on_admin_and_login_pages_but_not_the_client_form(pages):
    for name in ('clients', 'recent', 'client', 'report', 'login', 'signup', 'verify', 'twofactor', 'totp', 'admins', 'caseload'):
        assert 'data-theme-toggle' in pages[name], name
    assert 'data-theme-toggle' not in pages['assessment']


def test_theme_script_restores_the_saved_choice_and_marks_the_page_script_enabled():
    src = js('theme.js')
    assert "localStorage.getItem('theme')" in src and "classList.add('js')" in src and "dataset.theme = saved" in src
    assert "try" in src                                                    # storage can throw in private windows
    assert "localStorage.setItem('theme'" in js('admin.js')


def test_client_form_follows_the_system_theme_through_the_same_tokens():
    text = css()
    assert re.search(r'body\.cp \.panel|body\.cp \.item', text) and 'background: #fff' not in text.split('/* client-facing')[1].split('@media')[0]


# ---- motion is optional -----------------------------------------------------------------------------------

def test_motion_is_vendored_pinned_and_licensed():
    lib = STATIC / 'vendor' / 'motion.js'
    assert lib.stat().st_size > 50_000 and 'stagger' in lib.read_text() and 'Motion' in lib.read_text()
    note = (STATIC / 'vendor' / 'README.txt').read_text()
    assert 'MIT' in note and (STATIC / 'vendor' / 'motion.version').read_text().strip() in note


def test_animation_respects_reduced_motion_and_never_leaves_content_hidden():
    script, style = js('admin.js'), css()
    assert 'prefers-reduced-motion' in script and 'prefers-reduced-motion' in style
    assert '.js .reveal { opacity: 0; }' in style
    assert 'show(cards.concat(rows))' in script and script.count('show(cards.concat(rows))') >= 2     # both the skip path and the error path
    assert 'catch (err)' in script


def test_animated_elements_are_marked_up_for_the_script(pages):
    assert pages['clients'].count('reveal') >= 5 and 'class="stat reveal"' in pages['clients'] and 'class="heading reveal"' in pages['clients']
    assert 'class="panel reveal' in pages['client'] and 'tr class="reveal' in pages['clients']


# ---- drawer, header, chart markup ---------------------------------------------------------------------------

def test_add_client_is_a_native_dialog_with_a_close_button(pages):
    html = pages['clients']
    assert '<dialog class="drawer" id="addClient"' in html and 'data-close' in html and 'aria-labelledby="addTitle"' in html
    assert 'showModal' in js('admin.js') and "addEventListener('cancel'" in js('admin.js')


def test_header_marks_the_current_section(pages):
    assert re.search(r'class="nav-link on" href="/admin">Clients', pages['clients'])
    assert re.search(r'class="nav-link on" href="/admin/recent">Recent assessments', pages['recent'])
    assert not re.search(r'class="nav-link on" href="/admin">Clients', pages['recent'])


def test_chart_points_can_be_reached_by_keyboard_and_are_described(pages):
    html = pages['client']
    points = re.findall(r'<circle class="hit"[^>]*>', html)
    assert len(points) >= 4
    for p in points:
        assert 'tabindex="0"' in p and 'data-tip=' in p and 'aria-label=' in p
    assert "addEventListener('focusin'" in js('admin.js') and "addEventListener('keydown'" in js('admin.js')


def test_tooltip_code_writes_text_never_html():
    src = js('admin.js')
    tip = src.split('tooltips:')[1].split('motion (')[0]
    assert 'textContent' in tip and 'innerHTML' not in tip


def test_legends_explain_every_marker_and_shaded_area(pages):
    html = pages['client']
    for phrase in ('Reliable improvement', 'Reliable decline', 'No reliable change', 'Below cutoff', 'Within ±5 of intake', 'A line below 9', 'Below 36'):
        assert phrase in html, phrase
    for glyph in ('▲', '▼', '●', '■', '◆'):
        assert glyph in html


# ---- client page: keyboard, touch and no numbers ---------------------------------------------------------------

def test_client_scale_is_keyboard_operable_and_exposes_no_numeric_value():
    src = js('assessment.js')
    for key in ('ArrowRight', 'ArrowLeft', 'Home', 'End'):
        assert key in src
    assert 'tabindex="0"' in src and 'aria-roledescription' in src and 'aria-live="polite"' in src
    assert 'aria-valuenow' not in src and 'aria-valuetext' not in src and 'role="slider"' not in src


def test_client_script_sends_positions_not_scores_and_uses_pointer_events_for_touch():
    src = js('assessment.js')
    assert 'positions:' in src and 'scores' not in src
    for ev in ('pointerdown', 'pointermove', 'pointerup', 'pointercancel'):
        assert ev in src
    assert 'touch-action: none' in css() and 'setPointerCapture' in src


def test_client_page_carries_only_the_token_and_pending_forms(pages):
    html = pages['assessment']
    assert re.search(r'<body class="cp" data-token="[\w-]{20,}" data-pending="SRS,ORS|ORS,SRS">', html)
    assert 'Jane Patel' in html and 'jane-patel' not in html
