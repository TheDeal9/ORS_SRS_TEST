# CLAUDE.md

Project context for Claude Code. `AGENTS.md` carries the same facts for other agents; if you change one, change both.

Working notes for Claude Code:
- Read `scoring.py` before touching anything clinical; every threshold lives there.
- This repo may hold real client data under `data/`. Do not open, print, summarise or commit it.
- Use the test suite as the definition of done, and verify UI changes in a real browser (Playwright is available) because `pytest` cannot see layout, focus or animation.
- The owner prefers short, plain answers, a plan and a progress log for larger work, and honest reporting of what was and was not verified.

## What this is

A private web portal that digitises the PCOMS outcome measures: the **ORS** (Outcome Rating Scale) and **SRS** (Session Rating Scale). A practitioner sends a client a one-time link; the client marks four 10 cm lines; the server turns each mark into a 0 to 10 score and stores it. Scores are **never shown to the client**. The practitioner sees everything inside the portal: client list, per-client history, trend charts, reference flags, per-assessment detail and an in-portal report viewer. Excel is an optional download generated on demand.

Origin: the project started as a ChatGPT-built prototype (log kept in `Automate_ORS_SRS_Excel_Scoring.md`) and was rebuilt in phases. The owner runs one portal; from v0.1.1 other practitioners may register as administrators (Google-confirmed email on the owner's allow-list, no approval step) and each sees only their own clients. It is still not a general multi-tenant service: the ORS/SRS licence is for individual use, so the owner gates who registers, and confirming the licence covers this stays an open item.

Hard product constraints from the owner:
- Self-sufficient: every assessment must be viewable in the portal. No third-party workflows, integrations, trackers, CDNs or pingers at runtime. The one deliberate exception (v0.1.1): the portal sends plain-text one-time codes and account notices through the mail provider the owner configures (Brevo HTTPS API on Render free, which blocks SMTP; or SMTP). No client data is ever sent. The second deliberate exception: administrator sign-up uses Google (the server swaps a one-time code at Google's token endpoint, the browser visits `accounts.google.com`); the portal learns only the Google account id (`sub`) and a verified email address, and Google learns nothing about clients.
- Free to host (target: Render free web service + Neon free Postgres). See `DEPLOY.md`.
- Client IDs are unique and built from first + last name (`jane-patel`, then `jane-patel-2`, ...). Never duplicate an ID.
- The client must not learn their score. Admin sign-in uses an ID and password, then an authenticator-app code (every admin with no second step is forced to set it up at sign-in; ten single-use backup codes in a downloadable recovery kit are the way back in if the phone is lost), or a one-time emailed code if the admin has a confirmed email. Email is optional and off by default.
- An administrator sees only their own clients, assessments, reports, exports and API data. Another admin's rows are 404, never 403. The owner manages administrators (disable, enable, remove one who has no clients) but never sees their clients.

## Status

| Phase | What | State |
|---|---|---|
| 0 Harden | no default password, CSRF, login rate limit, secure cookies, git ignore | done |
| 1 Database | SQL storage (SQLite dev, Postgres prod), no stored Excel | done |
| 2 In-portal view | clients list, client page, charts, report viewer | done |
| 3 Close | strict CSP, keyboard and touch use, security headers, one-time hashed links | done |
| 4 Deploy | steps written in `DEPLOY.md`, start command verified locally against Postgres; **not yet deployed** (needs the owner's GitHub, Neon and Render accounts) | ready |
| 3b Auth v2 | admins in the database, recovery-code reset, account page, per-admin API keys, read-only `/api/v1`, `python app.py cli` | done |
| 6b 2FA without email (v0.1.1 fix) | authenticator app needs no mail, forced set-up (`ORS_SRS_REQUIRE_2FA`), ten backup codes + downloadable recovery kit, fail-closed when the app secret is unreadable; Render boot failure (mail on with empty sender) fixed by leaving `ORS_SRS_MAIL` out of `render.yaml` | done, not yet redeployed |
| 6 Accounts (v0.1.1) | per-admin ownership of all data, emailed-code and TOTP second step, row-locked concurrent writes, caseload page with database-side calculations | done |
| 6c Google sign-up | sign-up open at boot; Google proves the email, the owner's allow-list gates who may register (no approval step), then ID + password and the forced authenticator set-up with ten backup codes and a recovery code; replaces the Brevo email-code sign-up and the approval queue | built and tested; not yet deployed (needs the three `ORS_SRS_GOOGLE_*` variables) |
| 5 Operate | backup drill, typed delete confirmation, licence confirmation, consent text | open |

Progress log and the original gap analysis live in the plan file at `~/.claude/plans/the-app-is-running-immutable-valley.md` (outside the repo).

## Layout

| Path | Role |
|---|---|
| `render.yaml` | Render Blueprint (service, region, env vars). Secrets are `sync: false`, guarded by a test. |
| `app.py` | Flask app factory `create_app()`, models, all routes, Excel export, report viewer, security headers. CLI (`python app.py <cmd>`, list with a bad command): `setup`, `create-admin`, `reset-password`, `admins`, `api-key`, `cli` (reads the JSON API with an API key), `hash-password` (for a host), `rebuild-summaries`. `.env` is read only by this CLI entry point, never by `create_app()` or gunicorn. Auth section of `app.py`: `Admin`/`ApiKey` helpers, `admin_required`, `api_required`; routes `/admin/login|logout|forgot|signup|signup/finish|google/start|google/callback|account*`, `/api/v1/*`. |
| `mailer.py` | Pure stdlib mail sender: `smtp`, `brevo` (HTTPS API), `console` (dev only), `memory` (tests). `MailError` messages never contain a body, key or address. |
| `twofactor.py` | Pure TOTP helpers: Fernet-sealed secret (key derived from `SECRET_KEY`), provisioning URI, QR as SVG data URI, `verify` with one step of drift and replay refusal. |
| `scoring.py` | Pure logic, no Flask or DB: position to score, totals, reference flags and their stable codes, reliable-change rules. The single home of every clinical threshold. |
| `charts.py` | Server-rendered SVG charts (total trend, ORS domains, caseload outcome bar, averages by session number). Tooltips come from `data-tip` attributes plus `static/admin.js`. |
| `templates/` | Jinja pages. `base.html` is the admin shell. Client-facing pages: `assessment.html`, `closed.html`. Auth/landing: `login.html`, `landing.html`. |
| `static/app.css` | All styles, light and dark tokens. **No inline CSS anywhere** (CSP). |
| `static/admin.js` | Admin behaviour (theme toggle, drawer, tabs, tooltips, live refresh, animation), driven by `data-` attributes. |
| `static/assessment.js` | Client page: pointer, touch and keyboard marking; sends positions only. |
| `static/theme.js` | Runs in `<head>`: restores saved theme, adds the `js` class. |
| `static/vendor/motion.js` | Vendored Motion for JavaScript (MIT), pinned; see `static/vendor/README.txt`. |
| `PCOMS_template.xlsx` | The Excel template (ORS, SRS, Total sheets, with charts). Keep at repo root. |
| `tests/` | pytest suite; `TESTING.md` explains it. |
| `data/` | Local SQLite DB and old prototype files. **Git-ignored, holds client data. Never commit or print it.** |

## Data model (SQLAlchemy, `app.py`)

- `Client`: `admin_id` (owner, FK, not null), `slug` (the "Client ID" in the UI and URLs; **unique per administrator**: `UNIQUE(admin_id, slug)`), names, `age_band` (`adult` or `teen`), plus **summary columns kept in sync by `refresh_summary()`**: `session_count`, `last_session_on`, `ors_latest`, `srs_latest`, `flag_codes` (comma-delimited with outer commas, for example `,ors_low,srs_item,`), `needs_attention`. They let the clients list filter, sort and page in SQL.
- `Link`: one-time session link. Stores only the SHA-256 of the token. `kinds` is `ORS`, `SRS` or `ORS,SRS`. Expires after 7 days, closes when every requested form is submitted, can be revoked. Only one active link per client (a new one revokes the old).
- `Admin`: `username` (unique, lower-case), `password_hash`, `recovery_hash` (SHA-256 of a 96-bit code, nullable), `last_login_at`. `stamp` = first 16 hex of SHA-256(password_hash); the session cookie holds `aid` + `stamp`, so a password change ends every other session and SECRET_KEY alone cannot forge one. The first admin is seeded from `ORS_SRS_ADMIN_USERNAME` + `ORS_SRS_ADMIN_PASSWORD_HASH` **only while the table is empty** (`seed_admin`); afterwards the database wins and those variables are ignored. v0.1.1 adds: `status` (`active` ↔ `disabled`; only `active` holds a session or API key; `unverified`/`pending` rows left from the retired mail sign-up cannot sign in), `is_owner` (the first administrator created; manages the rest), `email` (unique, set only once proven) and `pending_email`, one live email code (`otp_hash` = HMAC of SECRET_KEY, id and purpose; `otp_purpose` `verify` (a new address under Account)|`login`, `otp_expires_at`, `otp_tries`), `mfa_fails` + `locked_until`, and `totp_secret` (Fernet-sealed) + `totp_enabled_at` + `totp_last_step`, and `backup_hashes` (comma-joined HMACs of the ten one-time backup codes via `otp_digest(admin, 'backup', code)`; spent codes are removed; cleared when the app is turned off or by `reset-2fa`). `second_step_method()` returns `totp`, `email`, `''` (none: forced to `/admin/2fa/setup` while `REQUIRE_2FA`) or `blocked` (app on but secret unreadable and no email: refused, never password-only). `Admin.clients` has **no** delete cascade.
- `AdminIdentity`: the outside account that vouched for an administrator's email at sign-up (`provider` `google`, `subject` = Google's `sub`, `email`). `UNIQUE(provider, subject)`: one Google account, one administrator. `ON DELETE CASCADE` from `Admin`. Written in the same commit as the `Admin` row (`create_admin(identity=...)`). The address lives here and **never in `Admin.email`**: setting that column switches the second step to emailed codes, which would stop sign-in whenever mail is off.
- `ApiKey`: per-admin read-only key (`ors_` + 43 chars), only SHA-256 stored, shown once, revocable, max 10 active per admin. Accepted only as `Authorization: Bearer` on `/api/v1/*`, which ignores cookies (that is why those routes need no CSRF). A password reset (recovery code or CLI) revokes all of the admin's keys.
- `Assessment`: one ORS or SRS result. `scores` is JSON keyed by **canonical item names** (ORS: Individually, Interpersonally, Socially, Overall. SRS: Relationship, Goals and Topics, **Method/Approach**, Overall). The client page labels the third SRS line "Approach or Method"; the server maps by position, never by label. `UNIQUE(client_pk, session_no, type)`. `UNIQUE(link_pk, type)` as well: a link holds at most one ORS and one SRS.
- Sessions: an ORS and an SRS taken the same day share one session number, even from separate links (`next_session_no`). `session_no` is assigned at the first submission.
- Status column on the clients list = state of each client's **newest** link: none, waiting, in_progress, completed, expired, revoked. `state_expr()` (SQL, used for filtering) and `link_state()` (Python, used for display) must always agree; a test enforces it.
- Writes that read-then-write a client or an admin take `lock_row()` first (`lock_client`, `lock_admin`): Postgres `SELECT .. FOR UPDATE`; SQLite, which has no row locks, uses an in-process lock per row. That is what keeps session numbers, summary columns, the one-live-link rule, try counters and TOTP replay refusal right under simultaneous requests; the UNIQUE constraints are the last line of defence.
- Calculations done in SQL (`total_sql`, `client_series`, `caseload_stats`, `caseload_by_session`): totals, change since intake and previous (window functions), caseload outcome counts and effect-size sums. Thresholds still come only from `scoring.py`; tests assert the SQL classes equal `scoring.ors_status`.

## Clinical rules (all in `scoring.py`; owner chose these)

- ORS and SRS totals are 0 to 40 (four lines of 0 to 10).
- ORS cutoff: 25 adult, 28 for ages 13 to 17. Below the cutoff is the clinical range.
- Reliable change: 5 points, measured **against the first ORS (intake)**, not the previous session. Some publishers use 6; the owner picked 5.
- Reliable and clinically significant improvement: reliable improvement AND crossing from below to at/above the cutoff. Flag code `ors_csc` replaces `ors_up`.
- SRS: total below 36, or any single line below 9, prompts "invite the client to comment". Wording is deliberately neutral; it is not a diagnosis.
- No "on track" or outcome prediction anywhere (guidance is contested and the known algorithms are proprietary). Do not add one without the owner asking.
- Flags: `ors_low`, `ors_down`, `ors_up`, `ors_csc`, `srs_low`, `srs_item`. Codes are persisted in `Client.flag_codes`; renaming a code needs `python app.py rebuild-summaries`. `needs_attention` = any flag with level `warn`.
- Dashboard shows short chips (`ORS < 25`) with the definition in a tooltip; the client page shows full sentences.

## Security invariants (do not weaken; tests cover each)

1. No default or hardcoded secrets. The app refuses to start without `ORS_SRS_SECRET_KEY` (16+ chars), and, while no administrator exists, without a well-formed `ORS_SRS_ADMIN_PASSWORD_HASH` (shape-checked, because a shell that expands `$` corrupts hashes). Passwords, recovery codes, API keys and emailed codes are stored only as hashes (codes as an HMAC); authenticator secrets only sealed; password 10+ chars.
2. The server computes scores. The browser sends mark positions (0 to 1) only. No response or page for a client contains a score or the client's ID.
3. CSRF on every admin POST (Flask-WTF). `/api/save` is exempt because the one-time token authenticates it. `WTF_CSRF_SSL_STRICT=False` is deliberate: with `Referrer-Policy: no-referrer` browsers send no Referer, and a strict check would reject every HTTPS POST (found and fixed during the deployment check; regression test exists).
4. Login, sign-up finish (also 20 a day), verify, second-step and resend limited to 5 POSTs per minute per address; Google start and callback 10 a minute; code tries are also counted in the database (5 per code, lock after 10 wrong codes). `ORS_SRS_PROXY_HOPS` must be `1` behind Render so the real visitor address and `https` scheme are used.
5. Strict CSP: `script-src 'self'; style-src 'self'`. No inline `<script>`, `<style>`, `style=` attribute or `on*=` handler, in templates or in JS-generated markup. Setting `element.style.x` from JS is allowed. Tests scan every page for violations.
6. Link tokens are shown once, stored hashed, kept out of logs (no gunicorn access log), out of `Referer`, and pages are `no-store`.
7. Filter and sort inputs are whitelisted; search text is escaped for LIKE; names are length-capped on the server.
8. Admin session cookie is Secure, HttpOnly, SameSite=Lax, 8 hours. `ORS_SRS_ENV=dev` turns Secure off for local http only.
9. Client data never goes into logs, commits, test fixtures or chat output.
10. Account changes (password, recovery code, API key) re-ask for the current password; login, forgot and those forms are limited to 5 POSTs a minute; unknown ID and wrong password give the same message and cost the same hash check. Forgot-password uses a recovery code, never email. Mail carries one-time codes and plain notices only (no links, no client data); a code never reaches a log, flash or response. A password-only browser holds `pre` (purpose `login` or `enroll`), not `aid`, so nothing opens until the second step passes (or the forced app set-up finishes), and an admin with an email is refused (not waved through) when mail is down. Backup codes bypass only the app code, never the password, and share the 5-tries and 10-failures limits. The recovery kit (codes + advice to save it on the admin's own computer) rides in one `kit` flash and is shown once; `data:` download link, no inline script.
11. Google sign-up (`/admin/signup`, `/admin/google/start|callback`, `/admin/signup/finish`). Open at boot; with `ORS_SRS_SIGNUP=off` it is closed (404). `google_ready()` needs client id, secret and a non-empty allow-list; half set up never stops boot (a warning names the missing variables) and the page says it is not set up. The callback checks, in order: no `error`, `state` equals the one in the session (used up on first use, 10 minutes), a code, the token reply, then `iss`, `aud`, `exp`, `nonce` and `email_verified is true`, then an existing `AdminIdentity` (sent to sign in), then the allow-list. The id token is not signature-checked because it arrives straight from Google's token endpoint over TLS (OpenID Connect Core 3.1.3.7). Failures give one neutral message and log the cause only, never a code, token, secret or address. The session carries `gstate` (state + nonce) and `gsignup` (sub + email, 10 minutes); `set_pre` clears both. Finish creates an `active`, non-owner admin with **no recovery code yet** (`create_admin(with_code=False)`), then `set_pre(..., 'enroll', 'totp')` and `/admin/2fa/setup` regardless of `REQUIRE_2FA`: nothing opens until a valid authenticator code, whose one-time kit holds ten backup codes and the recovery code. The allow-list gates sign-up only; remove someone with Disable or Remove on the Admins page. Removing an address from the list does not remove an admin.

## Commands

```bash
source .venv/bin/activate                         # Python 3.14 was used; .python-version says 3.14
python -m pip install -r requirements-dev.txt

python -m pytest                                  # whole suite (690 tests at time of writing, about 5 minutes)
python -m pytest tests/test_security.py -k csrf   # a slice
TEST_DATABASE_URL='postgresql+psycopg://user@host:port/scratch_db' python -m pytest   # same suite on Postgres (drops its tables!)

python app.py setup                               # asks for ID + password; creates/resets the admin in the DB, writes secret key + ORS_SRS_ENV=dev to .env (git-ignored, mode 600), prints a recovery code once
python app.py                                     # loads .env, then http://127.0.0.1:5000/admin/login
python app.py admins | create-admin ID | reset-password ID | reset-2fa ID | api-key ID [label]   # manage administrators (works against any DATABASE_URL, no SECRET_KEY needed)
ORS_SRS_API_KEY=ors_... python app.py cli me|clients|client ID|recent           # read a running portal through its API
python app.py hash-password                       # prints just a hash for a host's first start (single-quote it in a shell; $ is special)
python app.py rebuild-summaries                   # recompute Client summary columns (after manual data fixes or restores)

gunicorn "app:create_app()" --workers 1 --threads 4 --bind 0.0.0.0:$PORT --timeout 60   # production start command
```

Configuration is environment variables (locally the same names may sit in `.env`; real variables win over the file); the full table is in `README.md`. Required: `ORS_SRS_SECRET_KEY`, `ORS_SRS_ADMIN_PASSWORD_HASH` (only while no administrator exists). Sign-up: `ORS_SRS_GOOGLE_CLIENT_ID`, `ORS_SRS_GOOGLE_CLIENT_SECRET`, `ORS_SRS_GOOGLE_ALLOWED_EMAILS` (all three or the page says it is not set up; `ORS_SRS_SIGNUP=off` closes it). Useful: `DATABASE_URL` (default SQLite at `data/app.db`; `postgres://` is rewritten for psycopg 3), `ORS_SRS_TIMEZONE`, `ORS_SRS_PROXY_HOPS`, `ORS_SRS_ENV`, `ORS_SRS_REQUIRE_2FA` (default on: set `off` in a local `.env` to skip the forced authenticator set-up in development).

## Testing

`TESTING.md` is the guide (commands, what each file covers, expected results, Postgres setup, a 14-step manual browser checklist). Test files: `test_scoring.py` (pure logic and charts), `test_security.py` (secrets, route gating, CSRF, rate limit, headers, tokens, `.env` loader), `test_auth.py` (admin accounts, sessions, recovery, API keys, JSON API, CLI), `test_flow.py` (client journey, one-time links, session numbering, races), `test_admin.py` (clients list, filters, summaries, client page, report viewer, deletes, Excel), `test_ui.py` (CSP compliance, dark mode contrast, motion, keyboard and chart markup). v0.1.1 adds `test_signup.py` (one-time email codes under Account, the owner's Admins page: disable, enable, remove), `test_google_signup.py` (Google sign-up against a stub on this machine, forced two-step set-up, races), `test_2fa.py` (emailed codes, authenticator app, lockout), `test_backup_codes.py` (app without email, ten backup codes, downloadable recovery kit, forced set-up, fail-closed; `conftest.make_app` sets `REQUIRE_2FA=False` so older tests may sign in with the password alone, and `enable_totp` is the helper), `test_ownership.py` (cross-admin isolation matrix), `test_caseload.py` (SQL calculations equal the Python rules; caseload page), `test_concurrency.py` (real threads; run it on Postgres too) and `test_mailer.py` (SMTP and Brevo against local stubs). Compare whole pages only after removing the `csrf_token` value: Flask-WTF signs it with the current second. `conftest.py` has the fixtures (`app`, `admin`, `anon`) and helpers (`add_client`, `new_link`, `submit`, `complete_session`).

Rules for changes: run the full suite before saying work is done; add a test that fails without your change; when a test and the code disagree, work out which is wrong rather than loosening the test. The suite has been checked by deliberately breaking the code (mutation checks); keep it that way.

Browser checks (Playwright) were used for the slider, drawer, tooltips, dark mode, keyboard, real touch events, reduced motion and the live-refresh poll. Those are manual or scripted, not part of `pytest`.

## Deployment (summary; steps in `DEPLOY.md`)

Repo `github.com/TheDeal9/ORS_SRS_TEST` (public at the time of writing; the owner is advised to make it private). `render.yaml` is the Render Blueprint: free web service in **Singapore** (nearest region to India; Render and Neon have none), gunicorn command above, health check `/health`, `ORS_SRS_PROXY_HOPS=1` (trust only the last `X-Forwarded-For` entry), `ORS_SRS_SECRET_KEY` generated by Render, and `DATABASE_URL` / `ORS_SRS_ADMIN_USERNAME` / `ORS_SRS_ADMIN_PASSWORD_HASH` as `sync: false` (typed in the dashboard; a test forbids hardcoding them). Neon free Postgres in `aws-ap-southeast-1`, **direct** connection string, not `-pooler`. Free Neon has 6 h history and no scheduled backups, so a monthly `pg_dump` is part of operations. Firestore and Atlas were weighed and rejected (rewrite cost, paid backups); see `DEPLOY.md` section 9. The free web service sleeps after 15 minutes (about 1 minute to wake) and has no permanent disk, so nothing important may be written to local files. The start command was verified locally against Postgres 18 with Render-style proxy headers.

## Where secrets and variables live

Names only. Never print values from `.env`, `.env.local` or the Render dashboard; list key names when asked what is set.

| File | In git | Git-ignored | Holds (names only) | Class |
|---|---|---|---|---|
| `.env` | no | yes | `ORS_SRS_ENV`, `ORS_SRS_SECRET_KEY`; for testing Google locally add the **dev** OAuth client's `ORS_SRS_GOOGLE_*` | LOCAL dev config; read only by `python app.py`, never by `create_app()` or gunicorn |
| `.env.local` | no | yes | `DATABASE_URL`, `DATABASE_URL_UNPOOLED`, `NEON_BRANCH`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3`, `AWS_REGION` | LOCAL Neon credentials (they reach the production database); the app never reads this file; it may point to an older database than Render now uses, so check before using it |
| `.neon` | no | yes | `branch`, `orgId`, `projectId` | LOCAL tooling ids |
| `render.yaml` | yes | n/a | key names only; every secret is `sync: false` and a test forbids a value | DEPLOYMENT template, no values |
| Render dashboard, Environment tab | n/a | n/a | `ORS_SRS_SECRET_KEY` (generated), `DATABASE_URL`, `ORS_SRS_ADMIN_USERNAME`, `ORS_SRS_ADMIN_PASSWORD_HASH`, `ORS_SRS_GOOGLE_CLIENT_ID`, `ORS_SRS_GOOGLE_CLIENT_SECRET`, `ORS_SRS_GOOGLE_ALLOWED_EMAILS`; optional: `ORS_SRS_MAIL`, `ORS_SRS_MAIL_FROM`, `ORS_SRS_BREVO_API_KEY` | DEPLOYMENT secrets |
| `.mcp.json`, `neon.ts`, `package.json` | yes | no | no secrets | shared tooling config |
| `tests/*` | yes | no | throwaway values set in code | test only |

A `client_secret_*.json` downloaded from Google Cloud is a secret too: keep it outside the repo (it is git-ignored) and paste its values into the Render dashboard. The production Google client secret never goes into a local file; local testing uses a separate dev client whose redirect is `http://127.0.0.1:5000/admin/google/callback`.

## Neon tooling in the repo

- `neon.ts` (services: Postgres, private bucket `assets`; Neon Auth off), `.neon` (git-ignored project link), `package.json` + lockfile (only `neon.ts` dependencies; `node_modules/` git-ignored; there is no build script and the app has no Node code), `.claude/skills/neon*` + `skills-lock.json` (Neon agent skills), `.mcp.json` (Neon MCP: read-only, pinned to project `divine-cloud-09547430`, OAuth so no key is stored).
- Neon credentials live in **`.env.local`** (git-ignored, mode 600): `DATABASE_URL` (pooled), `DATABASE_URL_UNPOOLED` (direct), `NEON_BRANCH`, and the S3 keys `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3`, `AWS_REGION`. Never print them. The app prefers `DATABASE_URL_UNPOOLED` over `DATABASE_URL` and does not read `.env.local` itself (`load_env` reads `.env` only).
- Object storage is provisioned but deliberately **not used by the app** (no feature stores files). Do not wire it in without the owner asking.
- Gotcha: `neon init`, `neon link` and `neon checkout` write Neon variables into an existing `.env`, which would make plain `python app.py` talk to the Neon database. Back `.env` up first, or use `--no-env-pull` and `neon env pull --file .env.local`.
- Gotcha: tests drop tables. Never point `TEST_DATABASE_URL` at `production`; use a throwaway branch (`neon branches create/delete`). About 180 ms per query from India to Singapore makes the full suite impractical there; run a subset or run it on Render's side.
- The Neon MCP can read client rows even when read-only. The rule in this file still holds: do not open, print or summarise client data.

## Conventions

- Keep it small and plain: standard library and existing dependencies first; no new dependency for what a few lines do; no speculative abstractions. Deliberate shortcuts carry a `ponytail:` comment stating the ceiling and the upgrade path (grep for them).
- Comments explain why, not what. Match the surrounding style. Python is formatted by hand, about 120 columns.
- Everything the UI needs is in `static/` and `templates/`; no CDN, no web fonts, no analytics.
- Chart design follows the dataviz rules: meaning is never colour alone (marker shapes, rings and text), every mark has a hover and keyboard tooltip, legends explain every encoding, light and dark tokens are separate and contrast-tested.
- Animations use the vendored Motion library, always optional: `prefers-reduced-motion` skips them and content is never left hidden if the library fails.
- Database changes: `db.create_all()` only creates missing tables, and `create_app()` raises a clear error if an existing table lacks a model column. Before the first column change, adopt Alembic and back up. (An older local `data/app.db` therefore needs deleting; a stale one from the pre-`slug` version was moved to `data/app.db.old-schema-backup`.)
- Do not commit `data/`, `.venv/`, `.env`, database dumps or real client names.

## Gotchas already paid for

- SQLite does not enforce foreign keys unless asked; `app.py` turns the pragma on. Postgres enforces them; run the suite on both.
- `Client` summary columns drift if you write `Assessment` rows without calling `refresh_summary()` before commit. Use it, or run `rebuild-summaries`.
- pyexcel reads the generated workbook for the report viewer, but a file written by code has no cached formula results, so the Total sheet is recomputed in Python (`report_tables`). The downloadable file keeps the real Excel formulas.
- Flask's `tojson` filter puts spaces after commas; the client page therefore passes the pending forms as a plain comma-separated `data-pending` attribute.
- `pkill -f "port=5001"` kills the shell that contains that text. Kill servers by PID.
- Python 3.14 is what was tested; Render's default is close but check its build log.
- The old prototype's `data/*.json` and `data/clients/*.xlsx` are inert leftovers (the owner chose to start clean) and may be deleted.

## Open items and likely next work

1. Deploy (owner action): follow `DEPLOY.md`, then run its first-run check. Before redeploying 0.1.1: delete `ORS_SRS_MAIL` from Render's Environment tab if it is still there (the Blueprint no longer sets it), and recreate the Neon tables if they came from 0.1.0 or from before `admin.backup_hashes` existed (`DROP TABLE IF EXISTS assessment, link, api_key, client, admin CASCADE;` on the right branch; the app rebuilds them and seeds the owner).
2. Phase 5: practise a backup and restore; typed confirmation before deleting a client; confirm the ORS/SRS licence covers an online version; add consent and retention text.
3. Alembic before the next schema change that adds a column to an existing table (0.1.1 changed the schema with no migration because nothing was deployed; a 0.1.0 database must be recreated). Google sign-up added only a new table, which `db.create_all()` creates by itself.
4. (Done in 0.1.1: real concurrent-request tests on SQLite and Postgres.) Next: a shared rate-limit store if more than one worker is ever used, and a background mail queue if sending ever slows a request.
5. Excel opened in desktop Excel (tests read it with openpyxl only).
6. Possible later: soft-delete (archive) clients, editable session numbers, a shared rate-limit store if more than one worker is ever used.
