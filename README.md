# ORS/SRS private assessment portal

A practitioner adds a client, sends a **one-time session link**, and the client marks the ORS and SRS lines. Scores are computed on the server and are only ever visible to the signed-in administrator, inside the portal: client list with filters, per-client history, trend charts with tooltips, reference flags, per-assessment detail and an in-portal report viewer. Excel is an optional download.

Status: phases 0 to 3 are done (hardening, database, in-portal view, strict security policy). Deployment steps are in [DEPLOY.md](DEPLOY.md). Tests are explained in [TESTING.md](TESTING.md). Project context for contributors and AI agents: [AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md).

## Run locally

Python 3.10+ (developed on 3.14). Linux/macOS shown; Windows notes below.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt

python app.py setup                # asks for an administrator ID and password, saves them to .env (see below)
python app.py                      # http://127.0.0.1:5000/admin/login
```

The app refuses to start without `ORS_SRS_SECRET_KEY` (16+ characters) and at least one administrator. There is no default password.

### How sign-in works

- **Where logins live:** in the database, table `admin` (locally `data/app.db`, on Render your Neon database). It stores the ID, a **hash** of the password and a hash of the recovery code. The password itself is never stored, and a hash cannot be turned back into it. API keys are in table `api_key`, also as hashes only.
- `python app.py setup` asks for an ID (default `admin`; use exactly the ID you choose when signing in) and a password (10+ characters, typed twice). It creates (or resets) that administrator in the database, writes **`.env`** with a session secret and `ORS_SRS_ENV=dev` (so login works over plain `http://localhost`), and prints a **recovery code** once.
- At sign-in the server hashes what you typed and compares it with the stored hash. Five attempts per minute are allowed per address. The ID is not case-sensitive.
- The session cookie carries the admin's id plus a stamp derived from the password hash. Changing the password signs every other browser out, and knowing `ORS_SRS_SECRET_KEY` alone is not enough to forge a session.
- `.env` is git-ignored, created readable by you only, and read only when you start with `python app.py` (not by tests or gunicorn). A variable already set in your shell or on a host wins over the file.
- On a host such as Render there is no `.env`: run `python app.py hash-password`, put the hash in `ORS_SRS_ADMIN_PASSWORD_HASH` (and the ID in `ORS_SRS_ADMIN_USERNAME`). They are used **once**, on the first start, to create the first administrator. After that the database is the source of truth and those two variables are ignored; change the password in the portal.
- The first prototype's `ORS_SRS_ADMIN_PASSWORD` is no longer read by anything.

### Forgot your password

1. **Recovery code (no email involved, nothing leaves the portal):** sign in once and open **Account** → *Create a recovery code*, and keep it safely. Later use **Forgot your password?** on the sign-in page: ID + code + new password. It signs everyone out, revokes your API keys and gives you a fresh code.
2. **No code? From the computer that runs the app:** `python app.py reset-password <id>` (same effects). For the hosted app run it on your own computer against the production database: `DATABASE_URL='postgresql://...' python app.py reset-password <id>`.
3. Change a known password under **Account** → *Change password*.

### Command line and API keys

Each administrator can have up to 10 API keys (Account page, or `python app.py api-key <id> [label]`). A key is shown once, stored hashed, can be revoked, and only **reads** the portal through `/api/v1/me`, `/clients`, `/clients/<id>`, `/recent` (send `Authorization: Bearer <key>`; browser cookies never work there). The built-in client:

```bash
export ORS_SRS_API_KEY=ors_...                       # or put it in .env
export ORS_SRS_URL=http://127.0.0.1:5000             # default; use your https address for the hosted app
python app.py cli me | clients [attention=1 status=waiting q=jane page=2] | client <id> | recent [type=ORS] [--json]
```

`ORS_SRS_SECRET_KEY` is not a login credential: it only signs cookies. Never use it as an API key.

Other commands: `python app.py admins` (list administrators, no secrets), `create-admin <id>`, `reset-password <id>`, `api-key <id>`, `rebuild-summaries`, `hash-password`.

**Windows:** `run_windows.bat` installs, runs `python app.py setup` the first time (when `.env` is missing), then starts the app (untested on Windows).

## Settings (environment variables, or the same names in `.env` when running locally)

| Variable | Required | Meaning |
|---|---|---|
| `ORS_SRS_SECRET_KEY` | yes | Signs session cookies. 16+ random characters. |
| `ORS_SRS_ADMIN_PASSWORD_HASH` | first start only | Used once to create the first administrator when the `admin` table is empty. Locally `python app.py setup` does this for you; on a host paste the output of `python app.py hash-password`. |
| `ORS_SRS_ADMIN_USERNAME` | first start only | ID of that first administrator. Default `admin`. |
| `ORS_SRS_API_KEY`, `ORS_SRS_URL` | for `python app.py cli` | Your API key and the portal address. |
| `DATABASE_URL` | no | Default: SQLite at `data/app.db`. `postgres://` and `postgresql://` URLs are accepted. |
| `ORS_SRS_TIMEZONE` | no | Default `UTC`. Set your own (for example `Asia/Kolkata`) so assessment dates and times follow your calendar day. |
| `ORS_SRS_ENV` | no | `dev` turns off the Secure cookie flag so http://localhost works. |
| `ORS_SRS_PROXY_HOPS` | no | Number of reverse proxies in front of the app (default 0). Set `1` on Render so links use `https` and the login limit sees real visitor addresses. |
| `PCOMS_TEMPLATE` | no | Path to the Excel template. Default `PCOMS_template.xlsx`. |

Other commands: `python app.py rebuild-summaries` recomputes the client list's summary columns (use it after a restore or a manual data fix).

## Using it

1. **Clients** page: **+ Add client** opens a side panel (first and last name, age band). The ID is `first-last`; a repeated name becomes `first-last-2`, `-3`, and so on.
2. The list shows each client's **Status** (No link, Awaiting client, In progress, Completed, Expired, Revoked), **Sessions**, **Last session**, latest ORS and SRS, a red **Needs attention** badge, and short flag chips (hover one for its definition). Use **Filters** for attention, status, age band, clinical flag and sort order; active filters show as chips you can remove. The URL always reflects the filters, so a filtered view can be bookmarked. The header's **Recent assessments** shows every result, newest first.
3. Open a client, choose ORS, SRS or both, press **Create session link**. The link is displayed **once** (only its hash is stored). Copy it and send it to the client.
4. The client opens the link, marks each line (drag with a mouse, pen or finger; or use the arrow keys, Home and End), and finishes. Nothing numeric is shown to them. The link then closes. It also closes after 7 days or when you revoke it.
5. The client page in your portal updates by itself within about 5 seconds: status, history, clinical summary, trend charts, domain chart, flags. Hover or tab to any chart point for its detail. **View report** shows the three PCOMS sheets inside the portal; **Download .xlsx** saves the workbook; **Print report** gives a clean printout. A **Dark mode** toggle is in the header (it follows your system setting until you choose).

An ORS and an SRS taken on the same day, even from two separate links, share one session number.

## Reference flags

Aids for clinical judgement, defined in `scoring.py`: ORS below 25 (13 to 17 years: 28); reliable ORS change of 5 or more points since the first ORS (and "clinically significant" when it also crosses the cutoff); SRS total below 36; any single SRS line below 9. No outcome prediction is shown. Confirm these against your own training and licence before relying on them. The client page has a "How to read these charts and flags" note with sources.

## Data and privacy

- All records live in the database (`data/app.db` locally). `data/` is git-ignored. Back it up; it holds client names and scores.
- Link tokens are stored only as SHA-256 hashes. The app sends a strict Content-Security-Policy and loads no third-party code or fonts.
- ORS and SRS are copyrighted measures licensed for individual use. Check that your licence covers an online version and keep the copyright notices intact.
- Not a security audit and not clinical validation. Verify scoring against hand-checked examples before real use.

## Layout

`app.py` routes, models, export, command line. `render.yaml` Render Blueprint (Singapore, free plan, secrets asked for, never stored). `scoring.py` pure scoring and flags. `charts.py` SVG charts. `templates/` pages. `static/` styles and scripts (`vendor/motion.js` is the vendored animation library). `tests/` the suite.
