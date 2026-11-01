# Configuration

All instance-specific values are read from environment variables. For
convenience, a file `.env` in the project root is loaded automatically at
startup; variables already set in the environment take precedence. The template
[`.env.example`](../.env.example) lists every option.

Settings modules:

| Module | Used by | Purpose |
|---|---|---|
| `molo.settings.dev` | `manage.py` (default) | Local development: `DEBUG`, console e-mail, permissive CORS |
| `molo.settings.production` | `wsgi.py`, `celery.py`, `scripts/manage.sh` | Production: requires secret key and hosts, secure cookies, security headers |

Switch with `DJANGO_SETTINGS_MODULE`.

## Core

| Variable | Default | Description |
|---|---|---|
| `DJANGO_SECRET_KEY` | – | Required in production. Generate with `python -c "import secrets; print(secrets.token_urlsafe(50))"` |
| `DJANGO_DEBUG` | `false` | Debug mode (always on in `dev`) |
| `DJANGO_ALLOWED_HOSTS` | – | Comma-separated host names |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | – | Comma-separated origins incl. scheme |
| `DJANGO_COOKIE_DOMAIN` | – | e.g. `.example.org` to share session cookies across subdomains |
| `DJANGO_TIME_ZONE` | `Europe/Berlin` | Time zone |
| `SERVER_URL` | `http://localhost:8000/` | Public URL of this backend, used in links and e-mails |
| `SECURE_HSTS_SECONDS` | `0` | Production: HSTS lifetime; set (e.g. `31536000`) once HTTPS works everywhere |
| `SECURE_SSL_REDIRECT` | `false` | Production: let Django redirect HTTP to HTTPS (not needed if nginx already does) |

## Database

| Variable | Default |
|---|---|
| `DB_ENGINE` | `django.db.backends.postgresql` (`django.db.backends.sqlite3` for experiments) |
| `DB_NAME` | `molo` |
| `DB_USER` | `molo` |
| `DB_PASSWORD` | – |
| `DB_HOST` | `localhost` |
| `DB_PORT` | `5432` |
| `SQLITE_PATH` | `db.sqlite3` in the project (only with the SQLite engine; `DB_NAME` etc. are then ignored) |

## Directories

All default to subfolders of the project and are excluded from Git.

| Variable | Default | Content |
|---|---|---|
| `STATIC_ROOT` | `www/static` | Collected static files |
| `MEDIA_ROOT` | `www/media` | Uploaded images |
| `LOG_DIR` | `logs` | Log files, monitoring results |
| `IMPORT_DATA_DIR` | `import_data` | Files for scheduled imports |
| `IMPORT_SCRIPTS_DIR` | `import_scripts` | AI-generated scraper scripts |
| `BACKUP_DIR` | `backups` | Event backups of maintenance commands, database dumps |

## API and authentication

| Variable | Description |
|---|---|
| `CORS_ALLOWED_ORIGINS` | Comma-separated origins allowed to call the API from a browser |
| `CORS_ALLOW_ALL_ORIGINS` | `true` allows every origin (not recommended in production) |
| `JWT_SECRET_KEY` | Signing key for API tokens; defaults to `DJANGO_SECRET_KEY` |
| `JWT_ACCESS_DAYS`, `JWT_REFRESH_DAYS` | Token lifetimes (180 / 240 days). Every password change revokes all tokens issued before it |
| `ACTIVATION_URL` | Link target of activation mails (default `<SERVER_URL>/api/v4/users/activate/`) |
| `PASSWORD_RESET_TIMEOUT_HOURS` | Validity of password reset links (default 24) |
| `THROTTLE_RATE_AUTH` | Rate limit per client IP for login, registration and password changes (default `20/min`) |
| `THROTTLE_RATE_MAIL` | Rate limit for endpoints that send e-mail: password reset, activation mail, contact forms (default `10/hour`) |
| `THROTTLE_RATE_AI` | Rate limit for AI search and voice input (default `30/min`) |
| `TRUSTED_PROXY_COUNT` | Number of reverse proxies in front of Django (default `1` for nginx). Determines which `X-Forwarded-For` entry counts as client IP; `0` uses the direct peer address |
| `UPLOAD_AUTH_TOKEN` | Bearer token for external file and image uploads |
| `AI_TAGGING_API_KEY` | Key for external tagging workers (`Authorization: ApiKey <key>`) |

Rotate all keys when you set up a new instance; never reuse values from examples.

The application rate limits are counted in the Django cache. The default
in-memory cache counts per gunicorn worker; configure a shared cache (for example
Redis) in `molo/settings/local.py` if the limits must apply across workers. The
limits complement the nginx limits of [deploy/nginx/molo.conf](../deploy/nginx/molo.conf).

## E-mail

| Variable | Description |
|---|---|
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS` | SMTP server |
| `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | SMTP login |
| `DEFAULT_FROM_EMAIL` | Sender address |
| `PASSWORD_RESET_URL` | Deep link of the app for password resets (`molo://password-reset/`) |
| `ERROR_NOTIFICATION_EMAIL` | Receives monitoring alerts |
| `EMAIL_BACKEND` | Django mail backend. All mails (activation, password reset, reports, monitoring, contact form) go through it. In `molo.settings.dev` it defaults to the console backend, so mails appear in the `runserver` output |
| `FEEDBACK_MAIL_TARGET` | Receives feedback, contact requests and reported content |

## Public information

| Variable | Description |
|---|---|
| `WEBSITE_URL` | Website of the project, used by the QR redirect |
| `CONTACT_EMAIL` | Shown in welcome texts for new areas |
| `APP_STORE_URL`, `PLAY_STORE_URL` | Store links for the QR redirect (`/api/v4/redirect/`) |
| `GEOCODER_USER_AGENT` | User agent for OpenStreetMap Nominatim; include contact details as required by its usage policy |

## Content defaults

| Variable | Description |
|---|---|
| `DEFAULT_AREA_IDS` | Comma-separated area IDs used for devices that have not chosen an area yet (feed, hot article, AI search). Empty = no area restriction |
| `IMPORT_DAILY_SOURCE_IDS` | Comma-separated IDs of RSS sources fetched only once a day (for sources that publish rarely or block frequent requests) |
| `IMPORT_FALLBACK_ARTICLE_TAG_ID`, `IMPORT_FALLBACK_EVENT_TAG_ID` | Topic assigned to imported articles / events that end up without any topic. Empty = none |
| `DUPLICATE_PROTECTED_SOURCE_IDS` | Comma-separated source IDs whose articles `cleanup_article_duplicates` never deletes (e.g. your own editorial source) |
| `EVENT_PREVIEW_SOURCE_IDS` | Comma-separated source IDs whose submitted, not yet published events without topics are listed by `/api/v4/events/preview/` (for embedding on the organization's website). Empty = the preview returns nothing. Drafts are never listed |
| `AREA_WELCOME_SOURCE_ID`, `AREA_WELCOME_TAG_ID` | Source and tag of the welcome article that is created when the app adds a new area. Without a source no welcome article is created; the tag is optional |
| `ALLOW_PRIVATE_IMPORT_URLS` | `true` allows source links that resolve to internal addresses (localhost, LAN, cloud metadata). For local development only; keep `false` in production to prevent SSRF |

## Monitoring

`python manage.py monitoring` checks the API, the import pipeline, the tagging
queue and (if enabled) the language model server. Results are written to
`LOG_DIR/monitoring.json` and shown on the status page `/api/v4/monitoring/status/` (superusers only); failures are mailed to
`ERROR_NOTIFICATION_EMAIL`.

| Variable | Description |
|---|---|
| `MONITORING_USER_ID` | ID of a user allowed to create events; enables the create/delete API check |

## Push notifications (optional)

| Variable | Description |
|---|---|
| `ENABLE_PUSH` | `true` enables push messages |
| `FIREBASE_SERVICE_ACCOUNT_KEY` | Path to the Firebase service account JSON, stored outside the repository |
| `PUSH_TEST_ACCESS_CODE` | Access code for `POST /api/v4/admin/test-push/`; empty disables the endpoint |

## Background tasks

| Variable | Default |
|---|---|
| `CELERY_BROKER_URL` | `redis://127.0.0.1:6379/0` |

## AI

See [ai.md](ai.md) for `LLM_*`, `USE_RAG_EVENT_SEARCH`, `RAG_*`,
`SPEECH_TO_TEXT_ENABLED` and `WHISPER_*`.

## Local overrides

For settings that are not exposed as environment variables, create
`molo/settings/local.py` (ignored by Git) and import it at the end of your
settings module, or create your own settings module that imports from
`molo.settings.production`.
