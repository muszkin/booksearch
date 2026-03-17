# TASK — Build muszkin/booksearch GitHub repo

## Context
BookSearch is a self-hosted ebook search & download app that:
1. Searches Anna's Archive via FlareSolverr (Cloudflare bypass)
2. Downloads via Stacks (Anna's Archive downloader)
3. Auto-imports to Calibre library
4. Auto-sends EPUB to Kindle via email (with auto-conversion from PDF/MOBI/AZW3)

Current code exists in this directory:
- `app.py` — Flask web app (search + auth + settings)
- `Dockerfile` — BookSearch container
- `kindle-sender.py` — Kindle email sender with auto-conversion

## What to build

### 1. GitHub repo: `muszkin/booksearch` (public)
Create it with `gh repo create muszkin/booksearch --public --source=. --push`

### 2. Repository structure
```
booksearch/
├── docker-compose.yml          # Full stack: booksearch, stacks, flaresolverr, calibre, calibre-web, kindle-sender
├── booksearch/
│   ├── app.py                  # Main Flask app (MODIFY — see below)
│   ├── Dockerfile
│   └── requirements.txt
├── kindle-sender/
│   ├── kindle-sender.py        # MODIFY — see below
│   ├── Dockerfile
│   └── requirements.txt
├── .github/
│   └── workflows/
│       └── build.yml           # GitHub Actions: build & push Docker images on merge to main
├── .env.example                # Example env vars (NO secrets!)
├── README.md                   # English + Polish docs
├── LICENSE                     # MIT
└── TASK.md                     # Delete this file before committing
```

### 3. docker-compose.yml
Full stack that a non-technical person can deploy by just pointing Portainer to the repo.
Services:
- **booksearch** — `ghcr.io/muszkin/booksearch:latest` (port 5000)
- **stacks** — `zelest/stacks:latest` (port 7788 internal)
- **flaresolverr** — `ghcr.io/flaresolverr/flaresolverr:latest`
- **calibre** — `lscr.io/linuxserver/calibre:latest`
- **calibre-web** — `lscr.io/linuxserver/calibre-web:latest`
- **kindle-sender** — `ghcr.io/muszkin/booksearch-kindle-sender:latest`

All on shared network. Volumes for persistent data.
Environment variables from `.env` file (with sane defaults).

### 4. Modify app.py — add Kindle settings to Settings page
In the `/settings` page, add fields for:
- **Kindle email** (e.g. user@kindle.com)
- **SMTP host** (default: smtp.gmail.com)
- **SMTP port** (default: 587)
- **SMTP email** (sender address)
- **SMTP password** (app password)
- **Enable/disable Kindle sender**

Save these to `/data/kindle-settings.json`.
The kindle-sender container should mount the same volume to read these settings.

Default login: **admin / admin** (not muszkin — this is for public distribution)

### 5. Modify kindle-sender.py
- Read config from `/data/kindle-settings.json` (watch for changes)
- If no config exists or sender disabled, just watch and wait
- Keep auto-conversion (PDF/MOBI/AZW3 → EPUB via ebook-convert)
- ebook-convert runs in the calibre container: `docker exec calibre ebook-convert ...`
  BUT for the kindle-sender container, we need a different approach since it can't call docker exec.
  Instead: mount the same volumes and install calibre's ebook-convert in the kindle-sender image, OR
  just watch for EPUB files only and let Calibre handle conversion separately.
  SIMPLEST: kindle-sender watches for ALL supported formats, converts using calibre's ebook-convert 
  binary (install `calibre` package in the kindle-sender Dockerfile), sends EPUB only.

### 6. GitHub Actions (.github/workflows/build.yml)
- Trigger: push to main (or PR merge)
- Build two images:
  - `ghcr.io/muszkin/booksearch:latest` (from booksearch/)
  - `ghcr.io/muszkin/booksearch-kindle-sender:latest` (from kindle-sender/)
- Push to GitHub Container Registry (ghcr.io)
- Tag: always `latest` only (minimize space)
- Use `GITHUB_TOKEN` for auth (automatic in Actions)

### 7. README.md
Write in **English first, then Polish translation below**.

Sections:
- What is BookSearch?
- Architecture diagram (ASCII)
- Quick Start (docker compose up)
- Configuration (.env)
- Kindle Setup (SMTP + approved sender)
- Screenshots (skip for now, add placeholders)
- For Raspberry Pi (ARM64 note — add arm64 to build matrix)
- Troubleshooting
- License

### 8. .env.example
```env
# BookSearch Configuration
DEFAULT_USER=admin
DEFAULT_PASS=admin
SECRET_KEY=change-me-to-random-string

# Stacks (Anna's Archive downloader)  
# No config needed — works out of the box

# Calibre / Calibre-Web
PUID=1000
PGID=1000
TZ=Etc/UTC

# Kindle (configure in BookSearch Settings UI)
# KINDLE_EMAIL=user@kindle.com
# SMTP_HOST=smtp.gmail.com
# SMTP_PORT=587
# SMTP_FROM=sender@gmail.com
# SMTP_PASSWORD=app-password
```

### 9. IMPORTANT constraints
- **NO secrets** in repo (no API keys, no passwords, no tokens)
- **Public repo** — anyone can see
- Docker images must support **amd64 AND arm64** (Raspberry Pi 5)
- Default credentials: **admin / admin**
- Everything configurable via environment variables or Settings UI
- Delete TASK.md before final commit

### 10. After creating repo
- Push all code to main
- Verify GitHub Actions build succeeds
- Clean up TASK.md
