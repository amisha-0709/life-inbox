# Deployment guide (private beta + Google OAuth)

Class 8 demo (`tmiiitd/life-inbox`) supports **Google OAuth** for anyone
(no domain restriction) plus local Dev Login. Owner isolation is enforced
server-side. Real Google client id/secret and a public HTTPS origin are
required for the classroom demo URL.

The server validates configuration before startup completes. Invalid deployments
exit with a `ConfigError` instead of serving traffic. A successful health response
reports configuration; it does not prove database connectivity or beta readiness.

## Environments

| Env | DB | Auth | Notes |
| --- | --- | --- | --- |
| `local` (default) | SQLite | Dev Login optional; anonymous maps to Local Owner | Google OAuth also works if credentials are set |
| `beta` | PostgreSQL required | Dev Login **forced off**; Google OAuth; anonymous `/api/*` → 401 | Signed HttpOnly session cookies (`secure`) |
| `production` | PostgreSQL required | Same as beta | Same fail-closed rules |

## Ownership and isolation

Every request is bound to exactly one owner by the HTTP middleware in
`app/main.py`:

- A valid signed session for an existing user binds that user.
- `LIFE_INBOX_ENV=local` with no valid session binds the Local Owner
  (`local@life-inbox.local`). Signing out in local mode returns you to it.
- Any other environment with no valid session returns 401 for every `/api/*`
  route except `/api/health` and `/api/auth/*`. Nothing falls back to Local
  Owner.

`require_owner_id()` in `app/database.py` raises when no owner is bound.
`inbox_items`, `tasks`, `topics` and `analysis_runs` carry `owner_user_id`.
Derived rows are owned through their parents: `item_insights` via `item_id`,
`item_relations` only when both items share an owner, `task_source_items` and
`calendar_action_receipts` via their task. Foreign ids behave as not found
(404). `tests/test_isolation.py` covers both directions for every feature.

## Required environment variables

```bash
LIFE_INBOX_ENV=local|beta|production
DATABASE_URL=sqlite:////tmp/life_inbox.db   # local only; beta requires PostgreSQL
# With the installed psycopg 3 driver: postgresql+psycopg://USER:PASS@HOST/DB
LIFE_INBOX_UPLOAD_DIR=/data/uploads
LIFE_INBOX_SESSION_SECRET=<32+ private random chars>   # required in beta/production
LIFE_INBOX_AUTH_MODE=local|google
LIFE_INBOX_ALLOW_DEV_LOGIN=1|0                 # ignored / forced off outside local
LIFE_INBOX_PUBLIC_BASE_URL=https://YOUR-HOST   # public HTTPS origin
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=https://YOUR-HOST/api/auth/google/callback   # or omit to derive
LIFE_INBOX_TIMEZONE=UTC
LIFE_INBOX_MAX_UPLOAD_MB=5
LIFE_INBOX_CALENDAR_PROVIDER=mock|google
```

The built-in local secret is rejected in beta/production even if explicitly
exported. `SESSION_SECRET` is a supported alias; `LIFE_INBOX_SESSION_SECRET` takes
precedence. Generate a private value with
`python -c 'import secrets; print(secrets.token_urlsafe(48))'` and save it only in
your environment/secret store.
Do not reuse the sample secret below for a shared deployment.

Unknown `LIFE_INBOX_ENV` values, missing upload paths, unsupported auth modes, and
invalid upload limits also fail startup. With `LIFE_INBOX_AUTH_MODE=google` in
beta/production, the client ID, client secret and redirect URI (explicit or
derived) must all be configured. Local mode can run without Google credentials;
Google start then returns 503.

Optional: `OPENAI_API_KEY` enables live organization, grounded Ask answers and voice
transcription. Automated tests remove inherited credentials and mock providers.

## Google OAuth redirect

Authorized redirect URI in Google Cloud Console must match exactly:

`{LIFE_INBOX_PUBLIC_BASE_URL}/api/auth/google/callback`

Example: `https://life-inbox-demo.onrender.com/api/auth/google/callback`

Routes: `GET /api/auth/google/start` → Google → `GET /api/auth/google/callback` → signed session cookie → Settings.

## Local run

```bash
export DATABASE_URL="sqlite:////tmp/life_inbox_beta.db"
export LIFE_INBOX_ENV=local
export LIFE_INBOX_UPLOAD_DIR="/tmp/life_inbox_uploads"
export LIFE_INBOX_SESSION_SECRET="local-dev-secret-at-least-32-chars!!"
unset OPENAI_API_KEY
python3 -m pip install -r requirements.txt
python3 -m alembic upgrade head
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Dev login:

```bash
curl -c cookies.txt -X POST http://127.0.0.1:8000/api/auth/dev-login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","display_name":"You"}'
```

## Container / Postgres sketch

`docker-compose.yml` boots Postgres + the app image in `beta` mode with Google
sign-in. It is for private evaluation only. Compose refuses to start until you
export these in the same shell:

- `LIFE_INBOX_SESSION_SECRET` — private, at least 32 characters
- `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`
- `LIFE_INBOX_PUBLIC_BASE_URL` (optional; defaults to `http://localhost:8000`,
  so register `http://localhost:8000/api/auth/google/callback` in Google)

Before any shared beta:

1. Use a fresh private `LIFE_INBOX_SESSION_SECRET`
2. Keep `LIFE_INBOX_ALLOW_DEV_LOGIN=0`
3. Use an HTTPS `LIFE_INBOX_PUBLIC_BASE_URL` and register that exact callback URI
4. Mount durable upload storage and database volumes
5. Take a backup before migrations

`psycopg[binary]` is already in `requirements.txt`. Use a SQLAlchemy driver URL
compatible with psycopg 3 (`postgresql+psycopg://...`). A real PostgreSQL connection
and the hosting provider's supplied URL still need an environment-specific smoke
test; local SQLite tests do not establish that the hosted database is reachable.

## Migrations / backups / rollback

- Alembic is the only schema owner: `alembic upgrade head`
- Backup SQLite file or Postgres dump **before** upgrading
- Rollback of data-bearing revisions is disabled by design; restore from backup
- Uploads live under `LIFE_INBOX_UPLOAD_DIR` (local filesystem backend)

## Security / readiness checklist

- [ ] Session secret is unique and long
- [ ] Dev login disabled outside local
- [ ] Google sign-in completes with a test account; missing local credentials return 503
- [ ] Calendar writes still disabled
- [ ] User A cannot read, change, or infer anything from User B's data (`tests/test_isolation.py`)
- [ ] Anonymous `/api/items` returns 401 in beta
- [ ] Health endpoint reports env + warnings
- [ ] No secrets committed; uploads/DBs gitignored
- [ ] Owner approval before public DNS / paid hosting / live OAuth

## Verification before a shared beta

Run `python -m pytest -q --tb=short`. Startup/authentication regressions live in
`tests/test_startup_config.py`; ownership checks remain in `tests/test_isolation.py`.
Tests use disposable databases and ignore exported application credentials.

On an isolated instance, start beta mode without a session secret: Uvicorn must
exit before reporting startup complete and identify `LIFE_INBOX_SESSION_SECRET`.
Do not perform this check by changing a running shared service. Restore valid
settings, start normally, confirm anonymous `/api/items` returns 401, sign in with
Google, and verify two accounts see separate items. Keep calendar writes disabled.

## Remaining limitations

- Automated Google sign-in tests mock Google; live consent must be checked manually
- Object storage (S3) wiring
- Public internet deployment
- Purchases or paid managed services

The ranked follow-up work is in `docs/product-roadmap.md`. Deadline behavior and
the required 0014 migration are documented in `docs/deadlines.md`. Date-only
deadlines are supported; exact-time scheduling, fully incremental AI processing
and hosted readiness still require further work.
