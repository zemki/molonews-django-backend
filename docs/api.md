# REST API

The backend serves a REST API for the molo apps and other clients. The interactive
documentation (OpenAPI) is generated from the code:

| URL | Format |
|---|---|
| `/api/v4/swagger/` | Swagger UI |
| `/api/v4/redoc/` | ReDoc |
| `/api/v4/swagger.json` | OpenAPI schema |

## Versions

| Prefix | Status | Notes |
|---|---|---|
| `/api/v4/` | current | All new clients should use this version |
| `/api/v3/` | legacy | Kept for older app versions |
| `/api/v1/`, `/api/v2/` | legacy | Kept for older app versions |

## Authentication

| Client | Mechanism |
|---|---|
| Apps (anonymous) | Header `X-Device-ID: <random id per installation>`; creates a pseudonymous profile on first use |
| Editors / contributors | JWT: `POST /api/v4/token/` with user name or e-mail and password, then `Authorization: Bearer <access>`; refresh via `/api/v4/token/refresh/` |
| External uploads | `Authorization: Bearer <UPLOAD_AUTH_TOKEN>` |
| Tagging workers | `Authorization: ApiKey <AI_TAGGING_API_KEY>` |

### Tokens, passwords and publishing rights

- A password change (API, editorial interface, Django admin) revokes all access
  and refresh tokens issued before it. `POST /api/v4/users/change-password/`
  keeps the calling access token valid and returns a new token pair
  (`access`, `refresh`). Clients should store this pair: the previous refresh
  token of the device is revoked as well.
- Password reset links expire after `PASSWORD_RESET_TIMEOUT_HOURS` and can be
  used once.
- `POST /api/v4/articles/` and `POST /api/v4/events/` create content under the
  source whose `related_user` is the calling account (self-registered accounts
  get one automatically); other accounts receive `400`, accounts without the
  editor or contributor role `403` (also for changing and deleting). Articles need
  `article_tags` (list of tag IDs) and an `area` list unless the source has
  default areas; events need `occurrences` and `event_tags`.
- Accounts without `can_publish` cannot publish: `published: true` is ignored,
  and changes to their content are submitted for review (`up_for_review`).
- Login, registration, password and mail endpoints as well as the AI search
  answer `429 Too Many Requests` when the rate limit is exceeded.

## Main endpoint groups (v4)

| Path | Purpose |
|---|---|
| `articles/`, `events/`, `events/summary/` | Content lists and details |
| `events/preview/` | Submitted, not yet published events without topics from sources listed in `EVENT_PREVIEW_SOURCE_IDS` (website embedding) |
| `users/…` | Device profile: areas, topics, organizations, bookmarks, archive, location, push settings; account management for contributors |
| `area/`, `area/closest/`, `area/by_distance/` | Regions |
| `organizations/`, `sources/`, `categories/`, `tags/…` | Taxonomy and publishers |
| `articles/picture-upload/`, `events/picture-upload/` | Image upload for contributors |
| `contact/feedback/`, `contact/participate/`, `general/flag-article/`, `general/flag-event/` | Feedback and reporting |
| `import/upload-file/`, `import/import-file/`, `import/status/` | Upload of event files for import (see [importing.md](importing.md)) |
| `event/ai-search/`, `api/event/ai-search/`, `api/event/transcribe/` | Natural-language event search and voice input (see [ai.md](ai.md)) |
| `ai-tagging/articles/…`, `ai-tagging/events/…` | Queue for external tagging workers |
| `public/articles/<id>/` | Public HTML view of locally created articles |
| `monitoring/status/` | Status page of the last monitoring run (superusers only) |
| `redirect/` | QR code redirect to the app stores |

## Tagging worker interface

External workers can take over tagging (e.g. a separate machine with a GPU). The
flow for events (articles analogously):

1. `GET /api/v4/ai-tagging/events/get-untagged/` – returns the next untagged event
   and the list of available tags.
2. The worker determines suitable tag IDs.
3. `POST /api/v4/ai-tagging/events/submit-tags/` with `{"event_id": …, "tag_ids": [...]}`.

Batch variants (`get-untagged-batch`, `submit-tags-batch`) and all parameters are documented in Swagger. Tags marked as
"assigned by rule" are never offered to workers and are kept when tags are replaced.
