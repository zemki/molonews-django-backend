# Installation

This guide covers a local development setup and a production deployment on a
Linux server (Debian/Ubuntu).

## Requirements

| Component | Version | Purpose |
|---|---|---|
| Python | 3.12+ | Application |
| PostgreSQL | 14+ | Database (the event search uses PostgreSQL regex lookups) |
| Redis | 6+ | Celery broker (optional, for background tasks) |
| Ollama | current | Local language models for the AI features (optional) |
| libmagic | any | File type detection for images (`apt install libmagic1`, macOS: `brew install libmagic`) |
| Locale `de_DE.UTF-8` | – | German weekday and month names in the admin (optional; Debian/Ubuntu: `locale-gen de_DE.UTF-8`) |
| nginx | any | Reverse proxy in production |

## Local development

```bash
git clone https://github.com/zemki/molonews-django-backend.git molo
cd molo
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` pins the exact version of every package, so each installation
gets the same versions. It is generated from `requirements.in`, see
[CONTRIBUTING.md](../CONTRIBUTING.md#dependencies).

Create a PostgreSQL database (for a quick look without PostgreSQL, skip this
and use [SQLite](#sqlite-for-quick-experiments)):

```bash
createuser molo --pwprompt
createdb molo --owner molo
```

Configure the instance:

```bash
cp .env.example .env
```

Set at least `DB_PASSWORD` and, for anything beyond local testing,
`DJANGO_SECRET_KEY`. `manage.py` uses `molo.settings.dev` by default, which
enables `DEBUG` and prints e-mails to the console.

Create the schema and an admin account. The migrations create two user groups:
**Editors** (full editorial rights) and **Contributors** (own articles and events);
adjust their permissions in the admin if needed.

```bash
python manage.py migrate
python manage.py createcachetable
python manage.py createsuperuser
python manage.py runserver
```

| URL | What |
|---|---|
| http://localhost:8000/ | Editorial interface (`newadmin`) |
| http://localhost:8000/oldadmin/ | Classic Django admin (Jet) |
| http://localhost:8000/api/v4/swagger/ | API documentation |

### Getting content into a fresh instance

A new installation is empty. A typical first setup in the admin:

1. Create **areas** (regions, usually by postal code and town).
2. Create **tags** (topics). Put article topics into category `1` and event
   topics into category `2`; the migrations create both categories, and the code
   relies on these IDs (article topics may also live in categories `3` and `4`).
   *Tag categories* (`TagCategories`) are a separate, optional grouping of event
   topics for the apps' category view.
3. Create **organizations** and their **sources** (e.g. an RSS feed). Mark sources
   as *active* and give them areas and default topics, otherwise imported entries
   do not show up in the apps (see [importing.md](importing.md#source-settings-that-decide-what-appears-in-the-apps)).
4. Run `python manage.py import_articles` to import the feeds.

See [importing.md](importing.md) for all source types.

### SQLite for quick experiments

For a quick look without PostgreSQL, set in `.env`:

```
DB_ENGINE=django.db.backends.sqlite3
```

The database is then the file `db.sqlite3` in the project directory (another
path: `SQLITE_PATH=/path/to/file.sqlite3`); `DB_NAME`, `DB_USER` etc. are
ignored. Most features work, but the natural-language event search requires
PostgreSQL.

## AI features (optional)

Install [Ollama](https://ollama.com/download) on the same machine or a machine
in your network and pull the model:

```bash
ollama pull gpt-oss:20b     # ~13 GB, runs on 16 GB RAM/VRAM
# or, on large hardware:
ollama pull gpt-oss:120b    # ~65 GB, needs ~80 GB RAM/VRAM
```

Point `LLM_BASE_URL` and `LLM_MODEL` in `.env` to it. Details in [ai.md](ai.md).

## Production deployment

The `deploy/` folder contains example files. Paths assume the project lives in
`/srv/molo` and runs as user `molo`.

```bash
sudo adduser --system --group --home /srv/molo molo
sudo -u molo git clone https://github.com/zemki/molonews-django-backend.git /srv/molo
cd /srv/molo
sudo -u molo python3.12 -m venv .venv
sudo -u molo .venv/bin/pip install -r requirements.txt
sudo -u molo cp .env.example .env && sudo -u molo chmod 600 .env
```

In `.env`, set at least `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`,
`DJANGO_CSRF_TRUSTED_ORIGINS`, `SERVER_URL`, the `DB_*` values and the mail
settings. Then:

```bash
export DJANGO_SETTINGS_MODULE=molo.settings.production
sudo -u molo -E .venv/bin/python manage.py migrate
sudo -u molo -E .venv/bin/python manage.py createcachetable
sudo -u molo -E .venv/bin/python manage.py collectstatic --noinput
sudo -u molo -E .venv/bin/python manage.py createsuperuser
```

### Application server

```bash
sudo cp deploy/systemd/molo.service /etc/systemd/system/
sudo cp deploy/systemd/molo-celery.service /etc/systemd/system/   # optional
sudo systemctl daemon-reload
sudo systemctl enable --now molo
```

### nginx

```bash
sudo cp deploy/nginx/molo.conf /etc/nginx/sites-available/molo
sudo ln -s /etc/nginx/sites-available/molo /etc/nginx/sites-enabled/
# adjust server_name and certificate paths, then:
sudo nginx -t && sudo systemctl reload nginx
```

### Scheduled jobs

Imports, cleanup and monitoring run as management commands. `scripts/manage.sh`
runs a command with the project's virtual environment; `deploy/crontab.example`
contains a complete schedule:

```bash
sudo -u molo crontab deploy/crontab.example   # after adjusting the paths
```

### Backups

`scripts/backup_database.sh` writes a compressed `pg_dump` to `BACKUP_DIR` and
removes dumps older than `BACKUP_RETENTION_DAYS` (default 30). Store the password
in `~/.pgpass` of the service user.

## Updating

```bash
cd /srv/molo
sudo -u molo git pull
sudo -u molo .venv/bin/pip install -r requirements.txt
sudo -u molo -E .venv/bin/python manage.py migrate
sudo -u molo -E .venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart molo
```
