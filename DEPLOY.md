# Deploying the portal for free (Render + Neon, Singapore)

Result: an HTTPS web address you can send to clients, with all data in a free managed Postgres database. Cost: $0.
Time: about 30 minutes the first time. You need free accounts on GitHub, Neon and Render. Neon needs no card; check Render's sign-up page for its current requirement.

How the pieces fit: Render runs the code (no permanent disk on the free plan, so nothing important lives there). Neon keeps the data. `render.yaml` in the repository tells Render everything except three secrets, which you type once.

## 0. Check before you start

```bash
source .venv/bin/activate
python -m pytest            # expect: 625 passed
git status --short          # data/, .venv/ and .env must NOT be listed
```

`data/` holds your local database and is git-ignored. Never commit it.

## 1. The code on GitHub

The code is at `github.com/TheDeal9/ORS_SRS_TEST` (branch `main`). Update it with `git push`.

**Visibility.** Make the repository **private** (Settings, Danger Zone). The code holds no secrets, but there is no reason to publish a clinical tool. Render can read a public repository by its URL. For a private one, the GitHub account that owns the repository must authorise Render's GitHub app (Render's connect screen walks you through it).

## 2. Create the database on Neon (Singapore)

1. neon.com, sign up, create a project (yours already exists: **ORS SRS Portal**, id `divine-cloud-09547430`, branch `production`). **Region: Asia Pacific (Singapore), `aws-ap-southeast-1`.** Neon has no India region; Singapore is the nearest one it offers, and it matches the Render region so the app and database sit on the same network.
2. Open the project's **Connect** panel. Turn **Connection pooling off** and copy the **direct** connection string (the host does not contain `-pooler`). It looks like `postgresql://user:password@ep-xxxx.ap-southeast-1.aws.neon.tech/dbname?sslmode=require`.
   Why direct: Neon's pooled endpoint runs PgBouncer in transaction mode, which can reject the prepared statements the Postgres driver uses. One small web worker does not need a pooler.
3. Keep that string private. It is your `DATABASE_URL`. With the Neon CLI: `neon connection-string production` prints the direct string by default (`--pooled` for the other one), and `neon env pull --file .env.local` writes both as `DATABASE_URL` (pooled) and `DATABASE_URL_UNPOOLED` (direct). On Render use the **direct** value as `DATABASE_URL`.

The tables are created automatically the first time the app starts. Nothing to import.

## 2b. Neon tooling in this repository (already set up)

`neon.ts` declares the Neon services (no Neon Auth, because the portal has its own sign-in, and one private bucket `assets`); `neon deploy` applies it and `neon config plan` shows drift. `.neon` (git-ignored) links this folder to the project. `package.json` exists only for `neon.ts`'s two dependencies; the app itself is Python and has no build step (`npm run build` does not exist and is not needed). The Neon agent skills are in `.claude/skills/` (listed in `skills-lock.json`), and `.mcp.json` registers the Neon MCP server **read-only, pinned to this project, with OAuth** (no key is stored; Claude Code asks you to sign in on first use).

Neon's CLI writes connection strings into `.env` when it exists (`neon init`, `neon link`, `neon checkout`). Keep `.env` for local settings only, otherwise plain `python app.py` would use the Neon database. Use `neon env pull --file .env.local` (git-ignored) or `--no-env-pull`.

Never run the test suite with `TEST_DATABASE_URL` pointing at `production`: it drops every table. Use a throwaway branch: `neon branches create --name check --parent production`, `neon connection-string check`, and `neon branches delete check` afterwards. From a distant laptop the full suite is slow (each query crosses the network), so run a subset there.

## 3. Make your first administrator's hash (on your own computer)

```bash
python app.py hash-password
# type the password you want (10+ characters) twice; it prints one line starting with scrypt:  <- copy it all
```

Choose the administrator ID you want (for example `bhargav`). You do not need to make a secret key: Render generates one.

## 4. Create the service on Render (Blueprint)

1. dashboard.render.com, **New**, **Blueprint**, connect the repository, branch `main`.
2. Render reads `render.yaml` and shows one service, `ors-srs-portal`: Python, **Free**, **Singapore**. Everything below is already filled in. It asks for five values (the last two only if you want sign-up and emailed codes; see section 4c):

   | Render asks for | Enter |
   |---|---|
   | `DATABASE_URL` | the Neon direct string from step 2 |
   | `ORS_SRS_ADMIN_USERNAME` | your administrator ID, for example `bhargav` |
   | `ORS_SRS_ADMIN_PASSWORD_HASH` | the whole `scrypt:...` line from step 3, pasted in the dashboard (it does not expand `$`) |
   | `ORS_SRS_MAIL_FROM` | the sender address you verified in Brevo, for example `ORS SRS Portal <you@example.com>` |
   | `ORS_SRS_BREVO_API_KEY` | the Brevo API key from section 4c |

3. Click **Apply**. The first build takes a few minutes. Your address is `https://ors-srs-portal.onrender.com` (or similar; shown on the service page).

Already set for you by `render.yaml` (check them on the Environment tab):

| Name | Value | Why |
|---|---|---|
| `ORS_SRS_SECRET_KEY` | generated by Render | signs session cookies; never in the repository |
| `ORS_SRS_PROXY_HOPS` | `1` | trust exactly one proxy (Render's); without it links come out as `http://` and the login limit sees the wrong address |
| `ORS_SRS_TIMEZONE` | `Asia/Kolkata` | dates follow your calendar day |
| `PYTHON_VERSION` | `3.14.8` | the version the tests ran on |
| `ORS_SRS_MAIL` | `brevo` | one-time codes are sent over HTTPS, because Render's free plan blocks the SMTP ports (25, 465, 587) |

Do **not** set `ORS_SRS_ENV`: leaving it unset keeps the Secure cookie flag on. The start command deliberately has no `--access-logfile`, so the secret client links never appear in logs. Keep `--workers 1`: the login rate limiter keeps its counters in memory.

**Manual alternative** (if you prefer no Blueprint): New, Web Service, same repository; Runtime Python 3; Region Singapore; Build `pip install -r requirements.txt`; Start `gunicorn "app:create_app()" --workers 1 --threads 4 --bind 0.0.0.0:$PORT --timeout 60`; Instance Free; Health check `/health`; then add the variables from the two tables above, generating a 64-character `ORS_SRS_SECRET_KEY` yourself with `python -c "import secrets; print(secrets.token_hex(32))"`.

## 4b. Right after the first sign-in

1. Open the address, **Sign in** with your ID and password.
2. **Account** → *Change password* to a new one only you know. (The first password's hash sits in Render's settings; this makes it stale.)
3. **Account** → *Create a recovery code*; keep it somewhere other than this computer (password manager or paper).
4. In Render, Environment tab: **delete `ORS_SRS_ADMIN_PASSWORD_HASH`** (and optionally the username). It was used once to create your account and is no longer needed; keeping it only leaves an old hash lying around.
5. Optional: *Create API key*, then on your computer `export ORS_SRS_URL=https://<your-service>.onrender.com ORS_SRS_API_KEY=ors_...` and `python app.py cli clients`.

## 4c. Email: one-time codes and sign-up (Brevo, free)

The portal emails six-digit codes (sign-up confirmation and sign-in) and a few notices. It sends no links and no client data. Render's free web service cannot use SMTP, so the Blueprint uses Brevo's HTTPS API.

1. Create a free account at brevo.com. Under **Senders, Domains & Dedicated IPs** add and **verify a sender address** you can receive mail at (a domain you own gives the best delivery; a single verified address works to start).
2. **SMTP & API → API keys → Generate a new API key.** Copy it once.
3. In Render's Environment tab set `ORS_SRS_MAIL_FROM` (that verified address, optionally as `Name <address>`) and `ORS_SRS_BREVO_API_KEY`. Leave `ORS_SRS_MAIL=brevo`.
4. Redeploy, sign in, then **Account → Two-step sign-in**: add your email, type the code you receive. From then on every sign-in asks for a code.
5. Sign-up is now open on the sign-in page. Every sign-up must confirm an email and then be approved by you (the owner) under **Admins**. To close it, set `ORS_SRS_SIGNUP=off`.

Free-plan limits worth knowing: Brevo's free plan allows about 300 mails a day (each sign-in uses one), and a code that lands in spam can be requested again after a minute. If mail is switched off or fails, an administrator who already has an email **cannot sign in** (the portal never falls back to the password alone); the owner fixes the mail settings or, from their own computer, runs `DATABASE_URL='<Neon direct string>' python app.py reset-2fa <id>`.

## 5. First-run check (do this from your phone on mobile data)

| Step | Expected |
|---|---|
| Open `https://<your-service>.onrender.com/health` | `{"status":"ok"}` |
| Open `/admin/login`, sign in | Dashboard, "No clients yet" |
| Add a client, create a session link, copy it | Link starts with `https://` (if `http://`, `ORS_SRS_PROXY_HOPS` is wrong) |
| Open the link in a private tab and complete it | "Thank you"; the admin page updates within about 5 seconds |
| Open the same link again | "This link is no longer available" |
| View report, then Download .xlsx | Report shows in the portal; the workbook downloads |
| `curl -sI https://<your-service>.onrender.com/` | `Content-Security-Policy`, `Strict-Transport-Security`, `X-Frame-Options: DENY` present |
| Visit the `http://` address | Redirects to `https://` |
| Delete the practice client | Gone from the list |

## 6. Speed for users in India

Neither Render nor Neon has an India region. Singapore is the nearest region both offer, so the app and the database are both there. Keep them together: if they were on different continents, every database query on every page would cross an ocean, which costs far more than the one hop from India to Singapore. All styles, scripts and fonts are served by the app itself (no third-party downloads), and pages are small. Measure with your browser's Network tab after the first-run check. Regions can be changed later, but moving the database means a dump and restore (section 9).

## 7. Security checklist

Built in and tested: TLS ends at Render and plain HTTP is redirected to HTTPS; the app adds HSTS, a strict Content-Security-Policy, `X-Frame-Options: DENY`, `no-referrer`, `nosniff`; cookies are Secure, HttpOnly, SameSite=Lax, 8 hours; CSRF on every admin form; passwords, recovery codes, API keys and client links are stored only as hashes; scores are computed on the server and never sent to a client; five tries a minute on sign-in, recovery and account forms; no default or shared secrets; secrets live only in Render's dashboard (the Blueprint carries none, and a test enforces that); dependencies are pinned and `pip-audit` reported no known vulnerabilities on the day of release; no request logging of link paths.

Your part:
- Turn on two-factor sign-in for your **GitHub, Render and Neon** accounts. They are the keys to everything.
- Make the repository private (section 1).
- Do steps 2 to 4 of section 4b (new password, recovery code, delete the bootstrap hash variable).
- Never paste the Neon string, the hash, an API key or a client link into chat, email or tickets.
- Store backups encrypted (section 9); they contain client names and scores.

Known limits on the free plans (accepted, not hidden):
- Neon and Render free plans have no IP allow-list, so the database accepts connections from the internet, protected by TLS and its long random password. A paid Neon plan adds an allow-list.
- Render appends to the `X-Forwarded-For` header and visitors can send their own, so the app trusts only the **last** entry (`ORS_SRS_PROXY_HOPS=1`). If that entry turns out to be Render's edge rather than the visitor, the five-tries limit is shared among visitors behind that edge: it still blocks guessing, it just may block you too for a minute.
- The emailed second step is only as strong as the mailbox that receives it (turn on two-step sign-in for that mailbox too). The authenticator app, which does not depend on email, is the stronger option; its emailed fallback is as strong as the mailbox. Administrators without a confirmed email still sign in with the password alone, so confirm yours.
- The sign-in code limits (five tries per code, a lock after ten wrong codes) are counted in the database; the per-address request limits are in memory and assume one worker.
- The portal is a clinical aid, not a certified medical system, and the data lives with US-based providers in Singapore. Check what applies to your practice (India's DPDP Act, your professional body). This is not legal advice.

## 8. Things that behave differently on the free plan

- **Sleep:** Render's free service sleeps after 15 minutes without visitors and takes about a minute to wake (browsers see a loading page). Neon's compute also suspends after 5 idle minutes and normally wakes within a second or two. Before a session open your own `/admin` page two minutes early so the client's link opens at once. Do not add an outside "keep awake" pinger; it breaks the no-third-party rule. A paid Render instance removes the delay.
- **Hours:** 750 free instance hours per month per Render workspace. One always-warm service uses at most 744, so keep this the only free web service there.
- **Neon inactivity:** Neon's docs say free projects that are inactive for 90 days or more may be deleted. Using the portal, or a monthly backup, counts as activity. Do not leave the project untouched for months.
- **No shell on Render free:** run admin commands (`reset-password`, `admins`) from your own computer with `DATABASE_URL='<Neon direct string>'` in front.

## 9. Database choice and backups

Neon is already a hosted, managed Postgres cluster; nothing about your data lives on your laptop. What the free plan lacks is history: 6 hours of change history, one manual snapshot, no scheduled backups. So keep your own copies.

Monthly, and before any upgrade, from a computer with the PostgreSQL client tools:

```bash
pg_dump "<DATABASE_URL>" -Fc -f ors_backup_$(date +%F).dump      # full copy of every table
# restore into an empty database:
pg_restore --no-owner -d "<NEW_DATABASE_URL>" ors_backup_YYYY-MM-DD.dump
DATABASE_URL="<NEW_DATABASE_URL>" python app.py rebuild-summaries   # recompute the client list's summary columns
```

Also use **Download all reports (.zip)** on the Clients page for a spreadsheet copy, and take Neon's one free manual snapshot before risky changes. Practise the restore once into a scratch database before you rely on it.

Why not Firebase (Firestore) or MongoDB Atlas instead:
- **Firestore:** scheduled backups and point-in-time recovery need the paid Blaze plan (a billing account). The free quota (1 GiB, 20,000 writes a day) is fine, but there are no SQL unique constraints or joins, and the portal's session numbering, filtering, sorting and paging all depend on them.
- **Atlas free (M0):** 0.5 GB, 100 operations a second; its backup add-on is a paid, optional feature (about $0.10 a day, capped near $3 a month, card required); it pauses after 30 days of inactivity; whether Mumbai is offered for free clusters I could not confirm.
- Both would mean rewriting the data layer and re-proving the 625 tests, with a real risk to clinical records, for no free backup advantage. Neither moves the app itself into India, because Render has no India region.
- If you later want managed backups, the cheapest route is Neon's paid plan (scheduled backups, 7-day history), a one-line change to `DATABASE_URL`.

## 10. Updating the app

```bash
python -m pytest && git add -A && git commit -m "describe the change" && git push
```

Render rebuilds on every push to `main`. **Upgrading from 0.1.0 to 0.1.1 changes the schema** (administrator ownership, sign-up and second-step fields) and there is no migration: a v0.1.0 database must be recreated, so the app refuses to start on one. If you have not entered real clients yet, create a fresh Neon branch or empty the database and let the app build the new tables. If you have, export first (**Download all reports**), do not push 0.1.1 until you have a plan, and ask for a migration script. Changing the database schema (adding a column) is the one case that needs care: the app only creates missing tables and never alters existing ones. If you deploy code that expects a new column, the app refuses to start and the Render log says which columns are missing. Add a migration tool (Alembic) before the first schema change, and back up first.

Change your password under **Account**. Changing `ORS_SRS_ADMIN_PASSWORD_HASH` in Render later does nothing, because it is read only while the `admin` table is empty. Forgotten password: **Forgot your password?** with the recovery code, or from your own computer `DATABASE_URL='<Neon direct string>' python app.py reset-password <id>`. Changing `ORS_SRS_SECRET_KEY` signs everyone out.

## 11. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Build fails on `psycopg` | Python version not supported by the wheel. Keep `PYTHON_VERSION` at `3.14.8` or another version Render lists, and rerun the tests on it. |
| Service crashes at start with `RuntimeError: ORS_SRS_SECRET_KEY...` | The key is missing or under 16 characters. Re-add it on the Environment tab. |
| Crash: "No administrator exists yet ... PASSWORD_HASH" | First start without a valid hash. Paste the whole `scrypt:...` line in the dashboard (one line, no quotes), or see `python app.py hash-password`. |
| Cannot sign in after changing `ORS_SRS_ADMIN_PASSWORD_HASH` | By design that variable is read only on the very first start. Use **Forgot your password?** or `reset-password` from your computer. |
| "Incorrect administrator ID or password" | The ID is the one you chose (not necessarily `admin`); capitals do not matter. Five wrong tries in a minute are blocked for a minute. `python app.py admins` (with `DATABASE_URL` set) lists the IDs. |
| Links start with `http://` | `ORS_SRS_PROXY_HOPS` is not `1`. |
| `OperationalError ... SSL` or `prepared statement already exists` | Use the **direct** Neon string (no `-pooler`) with `?sslmode=require`. |
| `429 Too Many Requests` on sign-in | Five wrong attempts in a minute. Wait a minute. |
| Sign-in says "Email codes are not available" or "could not email the code" | `ORS_SRS_MAIL` is unset or the provider rejected the send (Render log: `mail not sent (brevo: status 401)` or similar, never the code). Check `ORS_SRS_BREVO_API_KEY`, that `ORS_SRS_MAIL_FROM` is a verified sender, and Brevo's daily limit. Locked out entirely: `python app.py reset-2fa <id>` from your computer. |
| Startup error "ORS_SRS_MAIL..." | A mail setting is incomplete (see the message). `smtp` needs host and sender; `brevo` needs key and sender; `console` is for `ORS_SRS_ENV=dev` only. |
| First page after a quiet spell takes a minute | The free service was asleep (section 8). |

## 12. Before real clients

- Confirm your ORS/SRS licence covers an online version, and keep the copyright notices.
- Add a short consent/privacy note that fits your practice and country, and decide how long records are kept.
- Store as little as you need: first and last name only, no contact details.
- Run the first-run check (section 5) again after any change to the code.
