# Deploying the portal for free (Render + Neon)

Result: an HTTPS web address you can send to clients, with all data in a free Postgres database. Total cost: $0.
Time: about 30 minutes the first time. You need free accounts on GitHub, Neon and Render. No card is required for Neon; check Render's sign-up page for its current requirement.

How the pieces fit: Render runs the code (it has no permanent disk on the free plan, so nothing important may live there). Neon keeps the data. The code is pulled from a private GitHub repository.

## 0. Check before you start

```bash
source .venv/bin/activate
python -m pytest            # expect: 316 passed
git status --short          # data/, .venv/ and .env must NOT be listed
```

`data/` holds your local database and is git-ignored. Never commit it.

## 1. Put the code on GitHub (private repository)

Create an empty **private** repository on GitHub (no README), then:

```bash
git add -A
git commit -m "ORS/SRS portal"
git branch -M main
git remote add origin git@github.com:<your-user>/<your-repo>.git
git push -u origin main
```

## 2. Create the database on Neon

1. neon.com, sign up, create a project. Pick the region closest to you and your clients.
2. Open the project's **Connect** panel. Turn **Connection pooling off** and copy the **direct** connection string (host does not contain `-pooler`). It looks like `postgresql://user:password@ep-xxxx.region.aws.neon.tech/dbname?sslmode=require`.
   Why direct: Neon's pooled endpoint runs PgBouncer in transaction mode, which can reject the prepared statements the Postgres driver uses. One small web worker does not need a pooler.
3. Keep that string private. It is your `DATABASE_URL`. The app accepts `postgresql://` and `postgres://` as given.

The tables are created automatically the first time the app starts. Nothing to import.

## 3. Make your two secrets (on your own computer)

```bash
python app.py hash-password
# type your administrator password twice (8+ characters); it prints one line starting with scrypt:  <- copy it all

python -c "import secrets; print(secrets.token_hex(32))"
# prints 64 random characters  <- this is ORS_SRS_SECRET_KEY
```

These go into Render's settings in step 4. Do not upload your local `.env`: it is git-ignored on purpose and only used when you run `python app.py` on your own computer.

## 4. Create the web service on Render

New, Web Service, connect your GitHub repository, then:

| Setting | Value |
|---|---|
| Language / Runtime | Python 3 |
| Branch | `main` |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn "app:create_app()" --workers 1 --threads 4 --bind 0.0.0.0:$PORT --timeout 60` |
| Instance Type | Free |
| Health Check Path | `/health` |

Python version: the repository's `.python-version` file asks for 3.14 (the portal is tested on 3.14.8). If Render's build log says it cannot provide it, set the environment variable `PYTHON_VERSION` to a version Render lists, then run the tests on that version locally before trusting it.

Environment variables (Environment tab). Paste values exactly; Render does not treat `$` specially, so the hash is safe here.

| Name | Value |
|---|---|
| `DATABASE_URL` | the Neon direct string from step 2 |
| `ORS_SRS_SECRET_KEY` | the 64 characters from step 3 |
| `ORS_SRS_ADMIN_PASSWORD_HASH` | the whole `scrypt:...` line from step 3 |
| `ORS_SRS_ADMIN_USERNAME` | optional, default `admin` |
| `ORS_SRS_TIMEZONE` | your timezone, for example `Asia/Kolkata`, so dates match your calendar day |
| `ORS_SRS_PROXY_HOPS` | `1` (required: without it the one-time links come out as `http://` and the login limit sees Render's address instead of the visitor's) |

Do **not** set `ORS_SRS_ENV`. Leaving it unset keeps the Secure cookie flag on, which is what you want on HTTPS.

The start command deliberately has no `--access-logfile`, so the secret link addresses never appear in Render's logs. Do not add one. Keep `--workers 1`: the login rate limiter keeps its counters in memory.

Press **Create Web Service**. The first build takes a few minutes.

## 5. First-run check (do this from your phone on mobile data)

| Step | Expected |
|---|---|
| Open `https://<your-service>.onrender.com/health` | `{"status":"ok"}` |
| Open `/admin/login`, sign in | Dashboard, "No clients yet" |
| Add a client, create a session link, copy it | Link starts with `https://` |
| Open the link in a private tab and complete it | "Thank you"; the admin page updates within about 5 seconds |
| Open the same link again | "This link is no longer available" |
| View report, then Download .xlsx | Report shows in the portal; the workbook downloads |
| `curl -sI https://<your-service>.onrender.com/` | `Content-Security-Policy`, `Strict-Transport-Security`, `X-Frame-Options: DENY` present |
| Delete the practice client | Gone from the list |

## 6. Things that behave differently on the free plan

- **Sleep:** the service sleeps after 15 minutes without visitors and takes about a minute to wake. Before a session, open your own `/admin` page two minutes early so the client's link opens at once. Do not add an outside "keep awake" pinger; it breaks the no-third-party rule. A paid instance removes the delay.
- **Free database limits:** Neon's free plan has a small storage cap and short history. Each assessment is under 1 KB, so space is not a concern. History is, so keep your own backups (next section).
- **Hours:** Render's free plan grants a monthly allowance of instance hours; one always-warm service fits inside it.

## 7. Backups (do not skip)

Monthly, and before any upgrade, from a computer with the PostgreSQL client tools:

```bash
pg_dump "<DATABASE_URL>" -Fc -f ors_backup_$(date +%F).dump      # full copy of every table
# restore into an empty database:
pg_restore --no-owner -d "<NEW_DATABASE_URL>" ors_backup_YYYY-MM-DD.dump
DATABASE_URL="<NEW_DATABASE_URL>" python app.py rebuild-summaries   # recompute the client list's summary columns
```

Also use **Download all reports (.zip)** on the Clients page for a spreadsheet copy. Store backups encrypted; they contain client names and scores.

## 8. Updating the app

```bash
python -m pytest && git add -A && git commit -m "describe the change" && git push
```

Render rebuilds on every push to `main`. Changing the database schema (adding a column) is the one case that needs care: the app only creates missing tables and never alters existing ones. If you deploy code that expects a new column, the app refuses to start and the Render log says which columns are missing (instead of failing page by page). Add a migration tool (Alembic) before the first schema change, and back up first.

To change the administrator password: make a new hash (step 3), replace `ORS_SRS_ADMIN_PASSWORD_HASH` in Render, let it redeploy. Changing `ORS_SRS_SECRET_KEY` signs everyone out.

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Build fails on `psycopg` | Python version not supported by the wheel. Set `PYTHON_VERSION` to a version Render offers and retry. |
| Service crashes at start with `RuntimeError: ORS_SRS_SECRET_KEY...` or `...PASSWORD_HASH...` | The variable is missing, too short, or the hash was pasted wrongly (it must start with `scrypt:` and be on one line). |
| Login says incorrect, password is right | The hash was corrupted when pasted (a shell expanded the `$`), or the username differs. Re-create the hash and paste it in Render's dashboard, not through a shell. |
| Links start with `http://` | `ORS_SRS_PROXY_HOPS` is not `1`. |
| `OperationalError ... SSL` or `prepared statement already exists` | Use the **direct** Neon string (no `-pooler`) with `?sslmode=require`. |
| `429 Too Many Requests` on login | Five wrong attempts in a minute. Wait a minute. |
| First page after a quiet spell takes a minute | The free service was asleep (section 6). |

## 10. Before real clients

- Confirm your ORS/SRS licence covers an online version, and keep the copyright notices.
- Add a short consent/privacy note that fits your practice and country, and decide how long records are kept.
- Store as little as you need: first and last name only, no contact details.
- Run the first-run check (section 5) again after any change to the code.
