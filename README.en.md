<div align="center">

<img src=".github/assets/cover.png" alt="KukeChat Server" width="880">

# KukeChat Server

**A real-time chat backend built for creator communities**

The server behind KukeChat: a FastAPI application that carries direct messages, group chats, community posts and community self-moderation over REST and WebSocket.

<p align="center">
<img src=".github/assets/icons/lock.svg" width="16" height="16" align="absmiddle">&nbsp;Private chats stay private&emsp;&emsp;
<img src=".github/assets/icons/radio.svg" width="16" height="16" align="absmiddle">&nbsp;REST + WebSocket&emsp;&emsp;
<img src=".github/assets/icons/scale.svg" width="16" height="16" align="absmiddle">&nbsp;Community self-moderation&emsp;&emsp;
<img src=".github/assets/icons/server.svg" width="16" height="16" align="absmiddle">&nbsp;Self-hostable
</p>

[![License](https://img.shields.io/badge/License-MIT-111111?style=flat-square&logo=opensourceinitiative&logoColor=white)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-111111?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-111111?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15-111111?style=flat-square&logo=postgresql&logoColor=white)](https://www.postgresql.org)
[![Redis](https://img.shields.io/badge/Redis-7-111111?style=flat-square&logo=redis&logoColor=white)](https://redis.io)
[![Docker](https://img.shields.io/badge/Docker-Compose-111111?style=flat-square&logo=docker&logoColor=white)](docker-compose.yml)
[![CI](https://img.shields.io/github/actions/workflow/status/kukemc/KukeChat-Server/ci.yml?branch=main&style=flat-square&label=CI&color=111111&logo=githubactions&logoColor=white)](https://github.com/kukemc/KukeChat-Server/actions/workflows/ci.yml)

[简体中文](README.md) · **English**

[About](#about) · [Core commitments](#core-commitments) · [Features](#features) · [Quick start](#quick-start) · [Configuration](#configuration) · [Integration points](#integration-points) · [Deployment](#deployment) · [Related repositories](#related-repositories)

</div>

---

## About

KukeChat is a chat app for the Scratch / CCW creator community. This repository is its one and only backend: more than four hundred REST routes and four WebSocket channels, covering messaging, groups, friends, posts, team-up, bots, reports and the community jury, the admin console's API, and app releases.

Together with [KukeChat-Client](https://github.com/kukemc/KukeChat-Client) and [KukeChat-Android](https://github.com/kukemc/KukeChat-Android), you can run a complete chat service for your own community. The repository ships with no production addresses, secrets or operator identifiers. To use the official service instead, grab a client from the [KukeChat download page](https://chat-api.kuke.ink/download).

## Core commitments

A chat service inevitably holds people's private conversations, so this section comes first.

> [!IMPORTANT]
> **Admins cannot browse private chats.** The only way to read a message is that someone reported it; private content can be reviewed only through the evidence snapshot frozen at the moment the report was filed.

### What admins can see

| Capability | Admins |
| --- | :---: |
| View a **reported** message and the messages around it | Yes |
| View the evidence snapshot frozen when the report was filed | Yes |
| Search messages across the whole site | No |
| Page through everything a given user has said | No |
| Browse the messages of an arbitrary conversation | No |

The boundary is enforced at the endpoint layer: every path that reads messages must first pass through `services/reports.py::ensure_message_is_under_report`, and without a matching report the request is rejected with 403. Reporting an entire conversation or a user does not unlock the other messages in that conversation either — the evidence for those reports is frozen into `target_snapshot` at submission time, and admins only ever see the snapshot.

The boundary is locked in by tests; see [`tests/test_admin_message_scope.py`](tests/test_admin_message_scope.py).

### What jurors can see

When the community jury is enabled, some reports are handed to community members who have passed the jury exam. They see even less than admins:

| Content | Jurors |
| --- | :---: |
| A reported **group message** and a few messages around it (anonymized: no usernames, avatars or group names) | Yes |
| A reported **public post** | Yes |
| A reported message from a **private chat** | No |
| Who filed the report, and who was reported | No |
| Cases from their own groups, involving their own friends, or that they reported themselves | Recused automatically |

Reports about private chats, users and groups are handled by admins only. Jurors also see nothing but the frozen snapshot, and anonymization happens on the server (`services/jury.py::juror_case_view`).

### Also worth knowing

- **Sensitive reads leave a trail**: viewing the context of a reported message, opening a case snapshot and similar actions call `log_sensitive_read()`, which writes to the append-only `admin_audit_logs` table — who, when, what, and how wide a range.
- **Automated moderation**: when enabled, messages, posts and images are sent to the external moderation service you configure. Content judged as violating or uncertain leaves a moderation record for human review; records judged as clean are purged after 7 days by default (`CONTENT_MODERATION_LOG_RETENTION_DAYS`).
- **Device identifiers against ban evasion**: clients report a server-issued device token, a hashed machine ID (desktop, Android) and a browser fingerprint (web). The server stores only an HMAC of each value, and the admin console shows only the first 8 characters — enough to tell whether several accounts come from the same device. See [`docs/device-bans.md`](docs/device-bans.md).
- **Game mode is the one public exception**: a group owner can connect a public group to a CCW project, after which the group's recent messages are visible to everyone playing that project, including visitors who are not signed in. Only the owner can turn it on, and only for public groups.

If you deploy this service, you are the one in control of this data. At a minimum, state clearly in your privacy notice what access admins have and when it is used, review the audit log regularly, and do not bring back the removed message-browsing endpoints for the sake of easier debugging.

## Features

| &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Area&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; | Capabilities |
| --- | --- |
| <img src=".github/assets/icons/message-circle.svg" width="16" height="16" align="absmiddle">&nbsp;Messaging | Direct messages, group chats, temporary conversations; text, images, voice, stickers; replies, @mentions, reactions, pokes; recall, forwarding and bundled forwarding, bookmarks, featured messages; polls; typing indicators; voice-to-text (requires a speech recognition service); chat history search limited to your own conversations |
| <img src=".github/assets/icons/users.svg" width="16" height="16" align="absmiddle">&nbsp;Groups | Roles and permissions, mutes, slow mode, join approval, invite links; member titles, group levels, check-in leaderboards; group announcements; group emojis; group upgrades (member limit, group files, permanent featured messages, group to-dos) |
| <img src=".github/assets/icons/heart.svg" width="16" height="16" align="absmiddle">&nbsp;Social | Friends and friend requests, friend intimacy and achievements, friend sparks, profile likes, account levels, birthdays |
| <img src=".github/assets/icons/newspaper.svg" width="16" height="16" align="absmiddle">&nbsp;Community | Posts (topics, comments, likes, pinning), team-up hub, collaborative tasks, site announcements with reactions, notifications |
| <img src=".github/assets/icons/coins.svg" width="16" height="16" align="absmiddle">&nbsp;Coins | Shop cosmetics, gifts, red packets (fixed, random-split, chain, voice passphrase, targeted); balances come from an external coin ledger — see [Integration points](#integration-points) |
| <img src=".github/assets/icons/bot.svg" width="16" height="16" align="absmiddle">&nbsp;Bots | Bot directory with reviews, installing bots into groups, Bot API (key auth) with real-time events over `/bot/ws`, interactive button components in messages |
| <img src=".github/assets/icons/shield-check.svg" width="16" height="16" align="absmiddle">&nbsp;Governance | Reports grouped into cases, a violation ledger with escalating penalties, appeals; community jury (exam, voting, senior panels and re-review, precedents, assembly hall); external content moderation; device bans and registration limits; audit log |
| <img src=".github/assets/icons/plug.svg" width="16" height="16" align="absmiddle">&nbsp;Platform | CCW account binding and profile sync, game mode (chat embedded in a project), app releases and update checks, download page, a public data endpoint for project showcase pages, local or S3-compatible storage, SMTP email, health checks |

Bot integration docs live in the client repository: [`docs/bot-api.md`](https://github.com/kukemc/KukeChat-Client/blob/main/docs/bot-api.md).

## Tech stack

<p align="center">
<img src="https://skillicons.dev/icons?i=py,fastapi,postgres,sqlite,redis,docker,nginx&theme=dark" height="40" alt="Python, FastAPI, PostgreSQL, SQLite, Redis, Docker, Nginx">
</p>

| Layer | Choice |
| --- | --- |
| Framework | FastAPI + Pydantic v2 |
| ORM | SQLAlchemy 2, fully async |
| Database | PostgreSQL (asyncpg) recommended for production; SQLite (aiosqlite) by default for development |
| Real-time | Native WebSocket; Redis Pub/Sub for fan-out across processes |
| Auth | JWT (python-jose) + session cookies; password hashing with passlib / bcrypt |
| Storage | Local disk or S3-compatible object storage (boto3), with optional presigned uploads to a private bucket |
| Images | Pillow, with automatic compression and thumbnails |
| Runtime | Gunicorn + Uvicorn workers |

Tables are created from the models at startup, and columns missing from older databases are added automatically, so there is no separate migration step.

## Quick start

### Docker

```bash
git clone https://github.com/kukemc/KukeChat-Server.git
cd KukeChat-Server
cp .env.example .env

# Generate a signing key and put it in JWT_SECRET_KEY in .env
python -c "import secrets; print(secrets.token_urlsafe(64))"

docker compose up -d
```

The service listens on `http://127.0.0.1:8000`, with API docs at `/docs` and a health check at `/health`.

`docker-compose.yml` includes the API and Redis, but no database. If you use SQLite, keep the database on the mounted volume by setting `DATABASE_URL=sqlite+aiosqlite:///./data/kukechat.db` in `.env`. The container runs as UID 10001, so `data/` and `uploads/` on the host must be writable by that user.

### Local development

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # set at least JWT_SECRET_KEY
uvicorn app.main:app --reload
```

Run the tests:

```bash
pip install pytest
python -m pytest tests/ -q
```

> [!NOTE]
> Requests under `/api/v1` require a client proof header (see `app/core/request_guard.py`), so calling them directly with curl will be rejected. The bot endpoints under `/api/v1/bot-api` use key authentication and are exempt.

## Configuration

All configuration is injected through `.env`. See [`.env.example`](.env.example) for the full list, and [`app/core/config.py`](app/core/config.py) for every field and its default.

| Variable | Description |
| --- | --- |
| `JWT_SECRET_KEY` | **Required.** The service **refuses to start** if it is empty, still a placeholder, or shorter than 32 characters |
| `DATABASE_URL` | Defaults to `sqlite+aiosqlite:///./kukechat.db`; `postgresql+asyncpg://…` recommended for production |
| `CORS_ORIGINS` / `CORS_ORIGIN_REGEX` | Must include your frontend's origin; the defaults cover local development only |
| `COOKIE_SECURE` | Defaults to `true`, so session cookies travel over HTTPS only; keep it on in production |
| `REDIS_URL` | Optional for a single process; **required with multiple workers or instances**, otherwise WebSocket events cannot reach other processes |
| `WEB_CONCURRENCY` | Worker count for `deploy_start.py`; when unset, 2 if Redis is reachable, otherwise 1 |
| `STORAGE_BACKEND` | `local` or `s3`; with `s3`, fill in `S3_*`, and use `S3_PRIVATE_BUCKET` for group files |
| `CONTENT_MODERATION_ENABLED` | Off by default. No moderation service is bundled; when enabled, `CONTENT_MODERATION_URL` is required |
| `VOICE_ASR_URL` | Speech recognition service URL; when empty, voice-to-text and voice passphrase red packets are unavailable |
| `CCW_BIND_*` | CCW account binding; the feature is unavailable when empty, and it also needs a CCW gateway — see [Integration points](#integration-points) |
| `SMTP_*` | Outgoing email, off by default |
| `INVITE_BASE_URL` | Landing page for invite links, usually your deployed client |
| `JURY_*` | Community jury thresholds; all have defaults and rarely need changing |

**About `JWT_SECRET_KEY`**: the service rejects empty values, known placeholders and keys that are too short at startup. This is deliberate — a predictable signing key means anyone can forge a token for any user, admins included. Use a random value, and do not reuse it across environments.

## Integration points

Two features depend on an external platform, and **how data is fetched from that platform is not part of this repository**. The repository only defines the interfaces and placeholder implementations in `app/integrations/`; the business logic — the binding flow, profile sync, the shop, project link cards — is all in the open-source code.

| Interface | File | What it provides | Without an implementation |
| --- | --- | --- | --- |
| CCW gateway `CcwGateway` | `app/integrations/ccw_gateway.py` | `latest_comments(subject_oid, subject_type)`, the latest page of comments in a comment section; `student_payloads(student_oid)`, a student's profile and homepage stats; `creation_detail(oid, access_key)`, the details of a project. Failures always raise `CcwUnavailable` | CCW account binding shows as unavailable (starting a binding returns 503, and the comment-polling background task does not start); profile sync and project link cards are silently skipped; password reset via comment verification code is unavailable |
| Coin ledger `CoinLedger` | `app/integrations/coin_ledger.py` | `credited_to(account_id)`, an account's total credited coins; `recent_credits()` / `all_credits()`, credit lists; `topup_url()`, the top-up page; `configured()` / `source()`. Anything that cannot be verified raises `CoinsUnavailable` | The shop can be browsed, level-unlocked items can be claimed and equipped; the balance is always `stale`, and coin purchases return 503 (see [`docs/shop.md`](docs/shop.md)) |

**Providing your own implementation**: place a Python package at `app/_private/` (already in `.gitignore`, so it never gets committed) that exports two zero-argument factory functions:

```python
# app/_private/__init__.py
from app.integrations import CcwGateway, CoinLedger

def create_ccw_gateway() -> CcwGateway: ...
def create_coin_ledger() -> CoinLedger: ...
```

At startup, `app/integrations/loader.py` uses this package if it finds one and falls back to the placeholders otherwise. If the package exists but is broken (an import error, a missing factory, or a return value that does not implement the interface), the service **refuses to start** instead of quietly falling back. To provide only one of the two, have the other factory return `NullCcwGateway()` / `NullCoinLedger()`. Implementations are responsible for not blocking the event loop (run synchronous requests in a thread pool); each interface's contract is documented in its file's docstrings.

In tests, swap in fakes with `integrations.use_ccw_gateway(...)` / `use_coin_ledger(...)`; see [`tests/integration_fakes.py`](tests/integration_fakes.py) for examples. The full test suite passes without `app/_private`.

## Project structure

```text
app/
  api/v1/          REST endpoints, split by domain; aggregated in router.py
  api/public_showcase.py  public data read by project showcase pages
  core/            config, database, auth dependencies, request checks, device signals
  integrations/    external platform interfaces and placeholders (see "Integration points")
  models/          SQLAlchemy models
  schemas/         Pydantic request / response models
  services/        business logic
  websocket/       connection management and the four real-time channels
  templates/       download page template
  static/          download page assets
docs/              API contracts and operations guides
scripts/           operations scripts (SQLite-to-PostgreSQL migration, S3 direct-upload check, etc.)
tests/             tests
deploy_start.py    production launcher (Gunicorn + Uvicorn workers)
```

Real-time channels: `/ws` (users), `/admin/ws` (admin refresh signals), `/bot/ws` (bot events), `/game/ws` (game mode).

## Documentation

| Document | Contents |
| --- | --- |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`moderation-v2.md`](docs/moderation-v2.md) | API contract for moderation, penalties, reports and the community jury |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`jury-v2.md`](docs/jury-v2.md) | Jury system v2: exams, panels, re-review, precedents, assembly hall |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`jury-demo.md`](docs/jury-demo.md) | A local end-to-end demo instance of the jury system |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`device-bans.md`](docs/device-bans.md) | Device signals, linked accounts and device bans |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`chat-features.md`](docs/chat-features.md) | Polls, typing indicators, group emojis, pokes, announcement reactions, profile likes and more |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`group-plus.md`](docs/group-plus.md) | Group upgrades, group files, permanent featured messages, group to-dos, chat history search |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`group-announcements.md`](docs/group-announcements.md) | Group announcements v2 |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`group-levels.md`](docs/group-levels.md) | Group levels and level titles |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`account-level.md`](docs/account-level.md) | Account levels |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`intimacy.md`](docs/intimacy.md) | Friend intimacy and friend achievements |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`sparks.md`](docs/sparks.md) | Friend sparks |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`shop.md`](docs/shop.md) | Shop |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`gifts.md`](docs/gifts.md) | Gifts |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`red-packets.md`](docs/red-packets.md) | Red packets |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`releases.md`](docs/releases.md) | Release service: app versions, installers, changelogs, auto-update |
| <img src=".github/assets/icons/file-text.svg" width="16" height="16" align="absmiddle">&nbsp;[`postgres-cutover.md`](docs/postgres-cutover.md) | Runbook for switching from SQLite to PostgreSQL |

The documents are written in Chinese.

## Deployment

`deploy_start.py` installs `requirements.txt` and Gunicorn if anything is missing, then starts the app with Gunicorn, listening on `127.0.0.1:8000` by default (override with `HOST` / `PORT`):

```bash
python deploy_start.py
```

Put Nginx in front of it as an HTTPS reverse proxy. The proxy must preserve these headers, or real client IP detection and WebSocket will both break:

```nginx
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;

proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
```

- **PostgreSQL**: point `DATABASE_URL` at `postgresql+asyncpg://…` and you are done; tables are created at startup, and the server tries to enable `pg_trgm` (the index behind chat history search). To migrate an existing SQLite database, use `scripts/migrate_sqlite_to_postgres.py` and follow [`docs/postgres-cutover.md`](docs/postgres-cutover.md).
- **Multiple processes**: with more than one worker, or more than one instance, `REDIS_URL` is required.
- **Runtime data you must not overwrite**: the database, the `uploads/` directory, and `.env`.

## Related repositories

| Repository | Description |
| --- | --- |
| <img src=".github/assets/icons/monitor.svg" width="16" height="16" align="absmiddle">&nbsp;[KukeChat-Client](https://github.com/kukemc/KukeChat-Client) | Community extension and desktop app (Windows; macOS in beta) |
| <img src=".github/assets/icons/smartphone.svg" width="16" height="16" align="absmiddle">&nbsp;[KukeChat-Android](https://github.com/kukemc/KukeChat-Android) | Native Android client, Kotlin + Jetpack Compose |
| <img src=".github/assets/icons/server.svg" width="16" height="16" align="absmiddle">&nbsp;**KukeChat-Server** | This repository: the backend API and real-time service |

## Contributing

Issues and pull requests are welcome. Please make sure `python -m pytest tests/ -q` passes before submitting.

Two hard rules:

1. **Never hard-code server addresses, secrets or operator identifiers.** Everything goes through `app/core/config.py` and `.env.example`.
2. **Never widen admins' access to messages.** Any new path that reads messages must go through `ensure_message_is_under_report()` and call `log_sensitive_read()` to leave a trail.

## License

[MIT](LICENSE)

---

<div align="center">

<sub>MIT © 2026 kukemc</sub>

</div>
