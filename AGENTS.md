# AGENTS.md

Context for AI coding agents working in this repository (also readable by people). `CLAUDE.md` carries the same facts for Claude Code; if you change one, change both.

## What this is

A private web portal that digitises the PCOMS outcome measures: the **ORS** (Outcome Rating Scale) and **SRS** (Session Rating Scale). A practitioner sends a client a one-time link; the client marks four 10 cm lines; the server turns each mark into a 0 to 10 score and stores it. Scores are **never shown to the client**. The practitioner sees everything inside the portal: client list, per-client history, trend charts, reference flags, per-assessment detail and an in-portal report viewer. Excel is an optional download generated on demand.

Origin: the project started as a ChatGPT-built prototype (log kept in `Automate_ORS_SRS_Excel_Scoring.md`) and was rebuilt in phases. The owner is a single practitioner; this is a one-practitioner tool, not a multi-tenant service (the ORS/SRS licence is for individual use).

Hard product constraints from the owner:
- Self-sufficient: every assessment must be viewable in the portal. No third-party workflows, integrations, trackers, CDNs or pingers at runtime.
- Free to host (target: Render free web service + Neon free Postgres). See `DEPLOY.md`.
- Client IDs are unique and built from first + last name (`jane-patel`, then `jane-patel-2`, ...). Never duplicate an ID.
- The client must not learn their score. Admin sign-in uses an ID and password.

## Status

| Phase | What | State |
|---|---|---|
| 0 Harden | no default password, CSRF, login rate limit, secure cookies, git ignore | done |
| 1 Database | SQL storage (SQLite dev, Postgres prod), no stored Excel | done |
| 2 In-portal view | clients list, client page, charts, report viewer | done |
| 3 Close | strict CSP, keyboard and touch use, security headers, one-time hashed links | done |
| 4 Deploy | steps written in `DEPLOY.md`, start command verified locally against Postgres; **not yet deployed** (needs the owner's GitHub, Neon and Render accounts) | ready |
| 3b Auth v2 | admins in the database, recovery-code reset, account page, per-admin API keys, read-only `/api/v1`, `python app.py cli` | done |
| 5 Operate | backup drill, typed delete confirmation, licence confirmation, consent text | open |

Progress log and the original gap analysis live in the plan file at `~/.claude/plans/the-app-is-running-immutable-valley.md` (outside the repo).

## Layout

| Path | Role |
|---|---|
| `render.yaml` | Render Blueprint (service, region, env vars). Secrets are `sync: false`, guarded by a test. |
| `app.py` | Flask app factory `create_app()`, models, all routes, Excel export, report viewer, security headers. CLI (`python app.py <cmd>`, list with a bad command): `setup`, `create-admin`, `reset-password`, `admins`, `api-key`, `cli` (reads the JSON API with an API key), `hash-password` (for a host), `rebuild-summaries`. `.env` is read only by this CLI entry point, never by `create_app()` or gunicorn. Auth section of `app.py`: `Admin`/`ApiKey` helpers, `admin_required`, `api_required`; routes `/admin/login|logout|forgot|account*`, `/api/v1/*`. |
| `scoring.py` | Pure logic, no Flask or DB: position to score, totals, reference flags and their stable codes, reliable-change rules. The single home of every clinical threshold. |
| `charts.py` | Server-rendered SVG charts (total trend, ORS domains). Tooltips come from `data-tip` attributes plus `static/admin.js`. |
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

- `Client`: `slug` (unique, indexed; this is the "Client ID" in the UI and in URLs), names, `age_band` (`adult` or `teen`), plus **summary columns kept in sync by `refresh_summary()`**: `session_count`, `last_session_on`, `ors_latest`, `srs_latest`, `flag_codes` (comma-delimited with outer commas, for example `,ors_low,srs_item,`), `needs_attention`. They let the clients list filter, sort and page in SQL.
- `Link`: one-time session link. Stores only the SHA-256 of the token. `kinds` is `ORS`, `SRS` or `ORS,SRS`. Expires after 7 days, closes when every requested form is submitted, can be revoked. Only one active link per client (a new one revokes the old).
- `Admin`: `username` (unique, lower-case), `password_hash`, `recovery_hash` (SHA-256 of a 96-bit code, nullable), `last_login_at`. `stamp` = first 16 hex of SHA-256(password_hash); the session cookie holds `aid` + `stamp`, so a password change ends every other session and SECRET_KEY alone cannot forge one. The first admin is seeded from `ORS_SRS_ADMIN_USERNAME` + `ORS_SRS_ADMIN_PASSWORD_HASH` **only while the table is empty** (`seed_admin`); afterwards the database wins and those variables are ignored.
- `ApiKey`: per-admin read-only key (`ors_` + 43 chars), only SHA-256 stored, shown once, revocable, max 10 active per admin. Accepted only as `Authorization: Bearer` on `/api/v1/*`, which ignores cookies (that is why those routes need no CSRF). A password reset (recovery code or CLI) revokes all of the admin's keys.
- `Assessment`: one ORS or SRS result. `scores` is JSON keyed by **canonical item names** (ORS: Individually, Interpersonally, Socially, Overall. SRS: Relationship, Goals and Topics, **Method/Approach**, Overall). The client page labels the third SRS line "Approach or Method"; the server maps by position, never by label. `UNIQUE(client_pk, session_no, type)`.
- Sessions: an ORS and an SRS taken the same day share one session number, even from separate links (`next_session_no`). `session_no` is assigned at the first submission.
- Status column on the clients list = state of each client's **newest** link: none, waiting, in_progress, completed, expired, revoked. `state_expr()` (SQL, used for filtering) and `link_state()` (Python, used for display) must always agree; a test enforces it.

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

1. No default or hardcoded secrets. The app refuses to start without `ORS_SRS_SECRET_KEY` (16+ chars), and, while no administrator exists, without a well-formed `ORS_SRS_ADMIN_PASSWORD_HASH` (shape-checked, because a shell that expands `$` corrupts hashes). Passwords, recovery codes and API keys are stored only as hashes; password 10+ chars.
2. The server computes scores. The browser sends mark positions (0 to 1) only. No response or page for a client contains a score or the client's ID.
3. CSRF on every admin POST (Flask-WTF). `/api/save` is exempt because the one-time token authenticates it. `WTF_CSRF_SSL_STRICT=False` is deliberate: with `Referrer-Policy: no-referrer` browsers send no Referer, and a strict check would reject every HTTPS POST (found and fixed during the deployment check; regression test exists).
4. Login limited to 5 POSTs per minute per address. `ORS_SRS_PROXY_HOPS` must be `1` behind Render so the real visitor address and `https` scheme are used.
5. Strict CSP: `script-src 'self'; style-src 'self'`. No inline `<script>`, `<style>`, `style=` attribute or `on*=` handler, in templates or in JS-generated markup. Setting `element.style.x` from JS is allowed. Tests scan every page for violations.
6. Link tokens are shown once, stored hashed, kept out of logs (no gunicorn access log), out of `Referer`, and pages are `no-store`.
7. Filter and sort inputs are whitelisted; search text is escaped for LIKE; names are length-capped on the server.
8. Admin session cookie is Secure, HttpOnly, SameSite=Lax, 8 hours. `ORS_SRS_ENV=dev` turns Secure off for local http only.
9. Client data never goes into logs, commits, test fixtures or chat output.
10. Account changes (password, recovery code, API key) re-ask for the current password; login, forgot and those forms are limited to 5 POSTs a minute; unknown ID and wrong password give the same message and cost the same hash check. Forgot-password uses a recovery code, never email (no third-party services).

## Commands

```bash
source .venv/bin/activate                         # Python 3.14 was used; .python-version says 3.14
python -m pip install -r requirements-dev.txt

python -m pytest                                  # whole suite (415 tests at time of writing, about 2 minutes)
python -m pytest tests/test_security.py -k csrf   # a slice
TEST_DATABASE_URL='postgresql+psycopg://user@host:port/scratch_db' python -m pytest   # same suite on Postgres (drops its tables!)

python app.py setup                               # asks for ID + password; creates/resets the admin in the DB, writes secret key + ORS_SRS_ENV=dev to .env (git-ignored, mode 600), prints a recovery code once
python app.py                                     # loads .env, then http://127.0.0.1:5000/admin/login
python app.py admins | create-admin ID | reset-password ID | api-key ID [label]   # manage administrators (works against any DATABASE_URL, no SECRET_KEY needed)
ORS_SRS_API_KEY=ors_... python app.py cli me|clients|client ID|recent           # read a running portal through its API
python app.py hash-password                       # prints just a hash for a host's first start (single-quote it in a shell; $ is special)
python app.py rebuild-summaries                   # recompute Client summary columns (after manual data fixes or restores)

gunicorn "app:create_app()" --workers 1 --threads 4 --bind 0.0.0.0:$PORT --timeout 60   # production start command
```

Configuration is environment variables (locally the same names may sit in `.env`; real variables win over the file); the full table is in `README.md`. Required: `ORS_SRS_SECRET_KEY`, `ORS_SRS_ADMIN_PASSWORD_HASH`. Useful: `DATABASE_URL` (default SQLite at `data/app.db`; `postgres://` is rewritten for psycopg 3), `ORS_SRS_TIMEZONE`, `ORS_SRS_PROXY_HOPS`, `ORS_SRS_ENV`.

## Testing

`TESTING.md` is the guide (commands, what each file covers, expected results, Postgres setup, a 14-step manual browser checklist). Test files: `test_scoring.py` (pure logic and charts), `test_security.py` (secrets, route gating, CSRF, rate limit, headers, tokens, `.env` loader), `test_auth.py` (admin accounts, sessions, recovery, API keys, JSON API, CLI), `test_flow.py` (client journey, one-time links, session numbering, races), `test_admin.py` (clients list, filters, summaries, client page, report viewer, deletes, Excel), `test_ui.py` (CSP compliance, dark mode contrast, motion, keyboard and chart markup). `conftest.py` has the fixtures (`app`, `admin`, `anon`) and helpers (`add_client`, `new_link`, `submit`, `complete_session`).

Rules for changes: run the full suite before saying work is done; add a test that fails without your change; when a test and the code disagree, work out which is wrong rather than loosening the test. The suite has been checked by deliberately breaking the code (mutation checks); keep it that way.

Browser checks (Playwright) were used for the slider, drawer, tooltips, dark mode, keyboard, real touch events, reduced motion and the live-refresh poll. Those are manual or scripted, not part of `pytest`.

## Deployment (summary; steps in `DEPLOY.md`)

Repo `github.com/TheDeal9/ORS_SRS_TEST` (public at the time of writing; the owner is advised to make it private). `render.yaml` is the Render Blueprint: free web service in **Singapore** (nearest region to India; Render and Neon have none), gunicorn command above, health check `/health`, `ORS_SRS_PROXY_HOPS=1` (trust only the last `X-Forwarded-For` entry), `ORS_SRS_SECRET_KEY` generated by Render, and `DATABASE_URL` / `ORS_SRS_ADMIN_USERNAME` / `ORS_SRS_ADMIN_PASSWORD_HASH` as `sync: false` (typed in the dashboard; a test forbids hardcoding them). Neon free Postgres in `aws-ap-southeast-1`, **direct** connection string, not `-pooler`. Free Neon has 6 h history and no scheduled backups, so a monthly `pg_dump` is part of operations. Firestore and Atlas were weighed and rejected (rewrite cost, paid backups); see `DEPLOY.md` section 9. The free web service sleeps after 15 minutes (about 1 minute to wake) and has no permanent disk, so nothing important may be written to local files. The start command was verified locally against Postgres 18 with Render-style proxy headers.

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

1. Deploy (owner action): follow `DEPLOY.md`, then run its first-run check.
2. Phase 5: practise a backup and restore; typed confirmation before deleting a client; confirm the ORS/SRS licence covers an online version; add consent and retention text.
3. Alembic before any schema change.
4. Real concurrent-request test (two clients saving at once); the retry path is only simulated.
5. Excel opened in desktop Excel (tests read it with openpyxl only).
6. Possible later: soft-delete (archive) clients, editable session numbers, a shared rate-limit store if more than one worker is ever used.
