# Changelog

## 0.1.1 fix: authenticator app without email (2026-11-04)

The first Render deploy of 0.1.1 stopped at start with `ORS_SRS_MAIL_FROM must be a sender address` because the Blueprint switched mail on (`ORS_SRS_MAIL=brevo`) while the sender was empty. Mail is now off unless the owner turns it on, so two-step sign-in no longer depends on it.

**Added**
- The authenticator app needs no email or mail server. It is the main second step.
- **Forced set-up** (`ORS_SRS_REQUIRE_2FA`, default on): an administrator with no second step is sent to `/admin/2fa/setup` after the password; nothing else opens until the app is on.
- **Backup codes and recovery kit:** ten one-time codes (keyed hashes only, in `admin.backup_hashes`) usable in place of the app code; shown once with a **Download recovery kit** button and advice to save the file on the administrator's own computer; Account shows the number left and makes a new set (password required, old set cancelled). A password recovery code is added to the kit when the administrator had none.

**Fixed**
- An administrator whose authenticator secret can no longer be read (`SECRET_KEY` changed) and who has no email is now refused instead of signing in with the password alone.
- `render.yaml` no longer sets `ORS_SRS_MAIL`.

**Changed**
- `reset-2fa` also deletes the backup codes. Turning the app off deletes them too.
- Schema: one new nullable column, `admin.backup_hashes`. A database made before this fix needs `ALTER TABLE admin ADD COLUMN backup_hashes TEXT;` (nothing else).

## 0.1.1 (2026-11-03)

Administrator sign-up, per-administrator data, a second sign-in step, concurrent-write protection and a Caseload page.

**Added**
- **Administrator sign-up** next to the sign-in page: ID, email, password; a six-digit emailed code proves the address; the owner (the first administrator) approves or rejects on a new **Admins** page; disabling an administrator signs them out at once and keeps their data.
- **Each administrator sees only their own clients.** Clients, assessments, reports, exports, recent results, counts, the Caseload page and the JSON API are scoped to the signed-in administrator; another administrator's pages are "not found". Client IDs are unique per administrator.
- **Second step at sign-in**: a one-time emailed code (10 minutes, single use, five tries per code, lock after ten wrong codes), or an optional **authenticator app** (TOTP, sealed secret, replay-proof, QR code). The browser is not signed in until the code is accepted. Email can be added or changed under Account.
- **Mail** (`mailer.py`): Brevo HTTPS API (works on Render's free plan, which blocks SMTP) or SMTP; console and in-memory backends for development and tests.
- **Caseload page**: share at or above the cutoff, reliable improvement / clinically significant improvement / decline since intake, average change, standardised effect size (from 10 clients), and average ORS and SRS by session number with the number of clients behind each point. All figures are calculated in the database.
- Client-page tooltips pair the two scales at each session and show the change since the previous SRS (display only, no new flag).
- `python app.py reset-2fa <id>`; `admins` lists status, owner, second step and client count; `create-admin` and `setup` accept an optional email.

**Changed**
- Concurrency: saving an assessment, creating a link, and checking a code now lock the row they read-then-write (Postgres row lock; a per-row lock on SQLite). New `UNIQUE (link, type)` on assessments. Fixed a lost update where an ORS and an SRS sent together from one link could leave the link open and the client summary stale, and one where simultaneous wrong codes were counted once.
- Assessment and recent-assessment lookups check ownership (the old lookup by id would have exposed other administrators' records).
- The old-schema startup message no longer suggests deleting `data/app.db`.

**Upgrading from 0.1.0**: the schema changed and there is no migration. Move the old database aside and start fresh (README, "Data and privacy").

**New dependencies**: `pyotp`, `qrcode`, `cryptography`.

**Verified**: 625 tests (415 before) pass on SQLite and on Postgres 18.6; 31 deliberate breakages are all caught; a scripted browser pass covered sign-up, approval, both second steps, isolation, the Caseload page, dark mode and a 390 px screen. Not verified: a live send through Brevo or SMTP (local stand-ins only) and a deployed Render/Neon instance.

## 0.1.0

Single-practitioner portal: hashed one-time session links, server-side scoring, in-portal client list, history, charts, reference flags, report viewer, on-demand Excel, database-backed administrators, recovery-code reset, read-only API keys, strict CSP, Render and Neon deployment files.
