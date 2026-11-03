# Testing

The automated suite needs no browser, no secrets and no network. Every test builds its own throwaway app and database.

## Commands

Run from the project folder with the virtual environment active (`source .venv/bin/activate`, or `.venv\Scripts\Activate.ps1` on Windows). First time only: `python -m pip install -r requirements-dev.txt`.

| Goal | Command | Expected result |
|---|---|---|
| Everything, on SQLite | `python -m pytest` | `415 passed` in about 2 minutes |
| One file | `python -m pytest tests/test_security.py` | all passed (counts below) |
| One test, verbose | `python -m pytest -v -k rate_limited` | the named test passes |
| Stop at first failure | `python -m pytest -x` | stops and prints the failing assertion |
| Everything, on Postgres | see "Postgres run" | `415 passed` |

If a run fails, the failure text names the assertion. Common causes: forgot `pip install -r requirements-dev.txt` (import error), or ran from another folder (run from the project root so `pytest.ini` is found).

## What each file covers

| File (tests) | Covers |
|---|---|
| `tests/test_scoring.py` (71) | Mark position to score: endpoints, 2-decimal rounding, rejects NaN, infinity, strings, booleans, out-of-range. Item names match the Excel template (`Method/Approach`). Reliable change at exactly 5 and just under, including float noise. Clinically significant change needs a reliable rise **and** crossing the cutoff (adult 25, teen 28). ORS and SRS flag boundaries (below warns, equal does not). Flag codes, chip text and tooltips are complete and short. Domain differences. SVG charts: empty and single-point cases, clinical zone, reliable-change band clipped to the axis, marker shape changes with status (not colour alone), rings, per-point tooltip and aria text, escaping, label thinning for many sessions, the four-line domain chart. |
| `tests/test_security.py` (68) | App refuses to start without a secret key or a real password hash; the old hardcoded password is gone and rejected. Every admin route (GET and POST, including the recent page and the report) redirects when signed out; the live-refresh endpoint returns 401. Login: wrong user or password, logout is POST-only, limited to 5 per minute (6th gets 429). CSRF on login and every admin POST; the client save endpoint is exempt; **CSRF still works over HTTPS when browsers send no Referer** (regression for a bug that would have broken every HTTPS login). Cookie flags and 8-hour lifetime. Content-Security-Policy contains no `unsafe-*` and no hosts; frame, permissions and referrer headers; HSTS only over HTTPS; `no-store` on sensitive pages. Tokens stored hashed and shown once. Input caps. An old database schema stops the app at start with a clear message. **Local `.env`:** `setup` stores a hash and never the password, file is owner-only, rerun keeps the secret and other lines, real environment variables beat the file, the app starts from the file alone, `create_app()` never reads it, `.env` is git-ignored. The direct Neon string (`DATABASE_URL_UNPOOLED`) wins over the pooled one when both are set. **`render.yaml`: secrets are never hardcoded (database string, admin ID, hash) and the secret key is generated, one trusted proxy hop, no dev mode, no access log, free plan in Singapore, Python version matches `.python-version`.** **`requirements.txt` lists gunicorn, pyexcel and its xlsx plugin** (a host installs only that file). |
| `tests/test_flow.py` (56) | Client IDs: lower-case slug, duplicates become `-2`, `-3`, accents and spaces normalised, non-Latin names stay unique. Bad link tokens give a friendly 404. Scores computed on the server from positions; responses and the client page never contain a score or the client's ID; client-supplied scores and dates ignored; dates follow the configured timezone. 13 malformed payloads and 5 non-JSON bodies rejected without burning the link. One-time link lifecycle: closes after all requested forms, replay refused, double submit gives 409, single-form links, 7-day expiry, revoke, a new link replaces the old, links cannot cross clients. Session numbering and pairing. Database `UNIQUE` backstop; a lost session-number race is retried; gives up cleanly if it never resolves. |
| `tests/test_admin.py` (82) | **Clients page:** empty state; header navigation; add-client side panel (reopens itself after an error); columns (Status, Sessions, Last session; no Link column); concise flag chips with tooltip detail; age-band chip text; Needs attention badge; stat cards; HTML escaping. **Filters:** search by name or ID (case-insensitive, wildcard and SQL characters inert); attention yes/no; **every Status value matches the filter and the displayed label**; newest-link rule; age band; whole-code flag matching; combined filters; unknown values ignored; all sort orders; filter count, selected values and removable chips; paging in SQL keeps the filters; query count does not grow with client count. **Database:** `slug` unique and indexed, summary columns indexed, summaries update on save and delete, rebuild repairs drift, summary equals a fresh calculation. **Recent page:** order, type filter, paging, bad page values. **Client page:** summary cards, charts with correct tooltips (intake, since previous, reliable change, below/above cutoff), domain table, how-to-read note with sources, link status text, live-refresh state. **Report viewer:** three sheets, correct numbers, Download button, matches the real export, handles missing SRS and empty clients, this client's data only, not a download. **Deletes** and **Excel export** (layout, ordering, isolation, ZIP, nothing written to disk). |
| `tests/test_auth.py` (85) | **Where credentials live:** first start seeds the admin from the environment once, later changes ignored; database holds only hashes (password, recovery code, API key); a corrupted `$`-less hash refuses to start with a hint, and is ignored once an admin exists. **Sessions:** ID ignores case and spaces; unknown ID and wrong password look identical and both cost a hash check; cookie holds ids and a stamp; **a cookie signed with the right secret but no/wrong stamp is rejected**; deleting the admin ends the session. **Password change:** needs the current password, policy errors change nothing, this browser stays in and others are signed out, keys and recovery code survive. **Recovery:** code shown once, any reasonable typing works, a reset signs everyone out, revokes keys, rotates the code, a rejected new password does not spend the code, wrong ID or code gives one generic message, rate limit 5 a minute, CSRF. **API keys:** shown once, label required, cap of 10, only the owner can revoke, each admin sees only their own, last-used written at most once a minute. **JSON API:** every path 401 without a valid header, browser cookies never open it (a key exists in that test), key in the query string refused, read-only, data matches the portal, no-store, rate limit. **Command line:** `setup` (settings file, database admin, printed code works, old `.env` hash removed and no second admin seeded, rerun keeps the secret and revokes keys, weak or mistyped passwords write nothing), `create-admin`, `reset-password`, `api-key`, `admins` (no secrets in output), unknown command prints usage, and the `cli` client against a real local server (all four views, `--json`, 404, bad key, missing key, unreachable server, SECRET_KEY refused as a key). |
| `tests/test_ui.py` (53) | **Strict CSP:** every page type (landing, login, closed, client form, clients, recent, client, detail, report) has no inline `<style>`, `style=`, `on*=` handler or inline `<script>`, and loads only local files; JS emits none either; external links carry `noreferrer noopener`; static files served with the right types; source files, database and `.git` are not reachable. **Dark mode:** the OS-setting block and the toggle block are identical; every surface and text token differs from light; text contrast meets WCAG in both themes (7:1 body, 4.5:1 muted) and chart colours stay visible. **Motion:** vendored, pinned, licensed; reduced motion honoured; content never left hidden. **UI markup:** native dialog drawer, active nav item, keyboard-reachable described chart points, legends explain every marker, tooltips write text not HTML. **Client form:** keyboard operable (arrows, Home, End), no numeric aria value, sends positions not scores, pointer events for touch. |

Total: 415 tests.

## Mutation checks (does the suite notice broken code?)

During development the code was deliberately broken and the suite had to fail each time. Round 1 (phases 0 to 2): link never completes; responses leak scores; unique constraint removed; login limit raised; same-day pairing removed; admin gate removed; CSRF disabled; tokens stored in plain text; link expiry ignored; wrong timezone. Round 2 (this release), 18 breakages: status filter SQL; summary not refreshed on save; summary not refreshed on delete; CSP allows inline script; needs-attention never set; filters trust raw values; report Total drops a column; chip removal keeps the filter; flag filter matches substrings; clinically significant without crossing the cutoff; reliable change set to 6; SRS item floor set to 8; inline style added to a page; inline handler added to a page; dark-theme blocks drift apart; faint muted text; tooltip uses innerHTML; marker shape no longer follows status. Also the strict-Referer CSRF bug. `.env` round (5 breakages, all caught): file overrides real variables; secret rotates on rerun; keys appended twice; file world-readable; `create_app()` loads `.env`. Blueprint round (5 breakages, all caught): database string hardcoded; wrong region; two proxy hops; access log added; dev mode added. Accounts round (12 breakages, all caught after one test was strengthened): session stamp unchecked; revoked keys still accepted; reset keeps old keys; reset keeps old recovery code; unknown ID skips the hash check; password change signs the current browser out; any admin can revoke any key; corrupt environment hash accepted; forgot route not rate limited; API falls back to the browser session (needed a test with a key present); account changes skip the password check; recovery code matching case-sensitive. Result: all caught. One variant (flag filter using a 5-character prefix) survived because it behaves identically for today's flag codes; a stronger variant (4-character prefix) was caught. Any new feature should get the same treatment: break it on purpose and confirm a test fails.

## Postgres run (same tests against the production database engine)

Postgres enforces foreign keys and unique constraints exactly as the live host will. Requires a local PostgreSQL install.

```bash
initdb -D /tmp/ors_pg --auth=trust -U ors
SOCK=$(mktemp -d /tmp/pgs.XXXX)        # keep the socket path short
pg_ctl -D /tmp/ors_pg -o "-p 54329 -k $SOCK -c listen_addresses=127.0.0.1" -l /tmp/ors_pg.log -w start
createdb -h 127.0.0.1 -p 54329 -U ors ors_test
TEST_DATABASE_URL='postgresql+psycopg://ors@127.0.0.1:54329/ors_test' python -m pytest
pg_ctl -D /tmp/ors_pg stop
```

Expected: `415 passed`. The suite drops and recreates its tables, so point it only at a scratch database.

## Live Neon check (run once, on a throwaway branch)

Run from a laptop in India, each database round trip to Neon Singapore is about 180 ms, so the full suite would take hours there; on Render, next to the database, it is not an issue. Instead, on a copy-on-write branch of `production` that was deleted afterwards: six tests that exercise the database hardest (unique and foreign-key constraints, the session-number race retry, the API, the password reset, admin deletion) passed against real Neon Postgres 18; the production start command (`gunicorn "app:create_app()"`) served a complete flow (first-start admin from the environment, login, client, https one-time link, both client submissions, link reuse refused, API key, `python app.py cli`) with no secrets in its log; the rows, hashes and constraint violations were then read back with SQL. The object storage bucket was checked with the S3 API (put, head, get, list, presigned GET, anonymous GET refused, delete). Commands are in `DEPLOY.md` section 2b.

## Deployment-shape check (before the first deploy and after server changes)

Run the exact production start command against a scratch Postgres with Render-style proxy headers:

```bash
DATABASE_URL='postgresql://ors@127.0.0.1:54329/ors_deploy' ORS_SRS_SECRET_KEY=<16+ chars> \
ORS_SRS_ADMIN_PASSWORD_HASH='<scrypt hash>' ORS_SRS_PROXY_HOPS=1 \
gunicorn "app:create_app()" --workers 1 --threads 4 --bind 127.0.0.1:5002
```

Then send requests with `X-Forwarded-Proto: https`, `X-Forwarded-Host` and `X-Forwarded-For` headers. Expected: tables and indexes created on Postgres; login works with a CSRF token and no Referer; the session cookie is `Secure; HttpOnly; SameSite=Lax`; HSTS and the CSP are present; the one-time link starts with `https://` and the forwarded host; six bad logins from one forwarded address give 200 x5 then 429 while another address is unaffected; the gunicorn log contains no `/client/` token paths. This was run and passed.

## Manual browser check (about 8 minutes)

Do this once after install, and again after any change to templates, CSS or scripts. Start the app as in the README, then:

| Step | Do | Expected |
|---|---|---|
| 1 | Open `/admin/login`, press **Dark mode**, reload | Page stays dark; button now says Light mode |
| 2 | Sign in; press **+ Add client** | Side panel slides in from the right with a dimmed background; Esc or the close button closes it |
| 3 | Add "Demo Client", then a second client (13 to 17) | Each opens its client page; IDs `demo-client`, etc. |
| 4 | On Demo Client choose ORS and SRS, press Create session link | Yellow box shows the link once; status "Waiting for the client to complete ORS and SRS" |
| 5 | Reload the page | The link box is gone and cannot be shown again |
| 6 | Open the link in a private window | ORS form, tabs visible, no numbers anywhere |
| 7 | Press Finish without marking | Yellow warning; it disappears after you mark a line |
| 8 | Drag each line (mouse, or finger on a phone); drag one again | Green mark follows and can be moved before finishing. Tab to a line and use arrows, Shift+arrows, Home, End |
| 9 | Finish, then complete SRS | "Thank you" screen. Opening the link again says it is no longer available |
| 10 | Watch the admin client page without reloading | Within about 5 s: "Session 1 complete", cards, charts, flags, session row |
| 11 | Hover (or Tab to) a chart point | Tooltip shows session, date, total, change since intake and since previous, and cutoff position |
| 12 | Press **View report**, switch tabs, press **Download .xlsx** | Sheets show in the portal; the file downloads only when you press the button |
| 13 | Open **Clients**, press **Filters**, choose Needs attention | Panel stays on screen (also on a phone); URL is short; a chip shows the filter and removes it |
| 14 | Toggle Dark mode on the client page and the report | Everything stays readable; charts keep their markers and legends |
| 15 | Turn on "reduce motion" in your system settings, reload | Pages appear instantly without sliding or fading |
| 16 | Sign out, then open `/admin` | Redirected to sign in |
| 17 | On the sign-in page press **Forgot your password?** and submit a wrong code | One generic message: "That ID and recovery code do not match." |
| 18 | Signed in, **Account** → create a recovery code, then reload | Code shown once in a box with Copy; gone after reload |
| 19 | Sign out, use **Forgot your password?** with that code and a new password | New code shown once; old password and old code no longer work; new password signs in |
| 20 | **Account** → create an API key, run `ORS_SRS_API_KEY=... python app.py cli clients` | The client table prints; revoking the key makes the same command fail with 401 |

Last run: steps 1 to 16 passed earlier, scripted with Playwright against a live server (mouse, keyboard and real touch events; dark system theme on the client form; reduced-motion emulation; no Content-Security-Policy violations and no console errors). Steps 17 to 19 were scripted the same way on a scratch copy (account pages in dark mode and at 390 px wide, no console or policy errors); step 20 was run against a live server and, over HTTPS proxy headers, against gunicorn on Postgres.

## Not covered yet

- Real concurrent requests from two clients at once (the retry path is tested by simulating the lost race, not with threads).
- Excel opened in actual desktop Excel (tests read the file with openpyxl; formulas are checked as text, not calculated).
- Safari and Firefox (browser runs used Chromium).
- A live deployment on Render and Neon (instructions are in `DEPLOY.md`; the start command was verified locally).
