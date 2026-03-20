# 📚 BookSearch

Self-hosted ebook search, download, and Kindle delivery system.

Searches [Anna's Archive](https://annas-archive.gl) via FlareSolverr (Cloudflare bypass), downloads via Stacks, auto-imports to Calibre, and optionally sends EPUB files to your Kindle via email.

## Architecture

```
User → BookSearch (:5000)
         │
         ├── Search ──→ FlareSolverr ──→ Anna's Archive
         │
         ├── Download ──→ Stacks ──→ /incoming/
         │                              │
         │                        Calibre-Import
         │                        (auto-import + .bin fix)
         │                              │
         │                              ▼
         │                        Calibre Library (/books/)
         │                         │           │
         │                         │           ▼
         │                         │     Calibre-Web (:8083)
         │                         │     (browse library)
         │                         ▼
         └── Kindle Queue ──→ Poll library ──→ SMTP email
              (per-user)                          │
                                                  ▼
                                             📱 Kindle
```

## Services

| Service | Port | Description |
|---------|------|-------------|
| **BookSearch** | 5000 | Search UI, download queue, Kindle sending, per-user settings |
| **Stacks** | 8585 | Anna's Archive downloader |
| **FlareSolverr** | — | Cloudflare bypass (internal only) |
| **Calibre** | 8182 | Calibre desktop (VNC) |
| **Calibre** | 8181 | Calibre desktop (HTTPS VNC) |
| **Calibre-Web** | 8084 | Web-based library browser |
| **Calibre-Import** | — | Auto-imports downloaded files to Calibre (internal) |

## Quick Start

### Option A: Docker Compose (CLI)

```bash
git clone https://github.com/muszkin/booksearch.git
cd booksearch
cp .env.example .env
# Edit .env — at minimum change SECRET_KEY
docker compose up -d
```

Open `http://localhost:5000` — login: `admin` / `admin`

### Option B: Portainer (GUI)

1. Open Portainer → **Stacks** → **Add stack**
2. Select **Repository**
3. Fill in:
   - **Repository URL:** `https://github.com/muszkin/booksearch`
   - **Repository reference:** `refs/heads/main`
   - **Compose path:** `docker-compose.yml`
4. Scroll down to **Environment variables** and add:

   | Name | Value | Required? |
   |------|-------|-----------|
   | `SECRET_KEY` | any random string (e.g. `my-super-secret-key-123`) | ✅ Yes |
   | `DEFAULT_USER` | `admin` | Optional (default: admin) |
   | `DEFAULT_PASS` | `admin` | Optional (default: admin) |
   | `PUID` | `1000` | Optional (match your host user) |
   | `PGID` | `1000` | Optional (match your host group) |
   | `TZ` | `Europe/Warsaw` | Optional |
   | `BOOKSEARCH_PORT` | `5000` | Optional |
   | `STACKS_PORT` | `8585` | Optional |
   | `CALIBRE_PORT` | `8182` | Optional |
   | `CALIBRE_WEB_PORT` | `8084` | Optional |
   | `CALIBRE_CONTENT_PORT` | `8181` | Optional |

5. Click **Deploy the stack**
6. Wait ~1 minute for all containers to start
7. Open `http://<your-server>:5000` → login with your credentials

> **Tip:** To auto-update when the repo changes, enable **GitOps updates** in Portainer stack settings and set a polling interval (e.g. 5 minutes).

### Option C: Portainer with custom volumes (advanced)

If you want to use existing directories (e.g. NAS storage), create a `docker-compose.override.yml` next to the main compose file:

```yaml
services:
  stacks:
    volumes:
      - /your/path/stacks-config:/config
      - /your/path/books/incoming:/opt/stacks/download

  calibre:
    volumes:
      - /your/path/calibre-config:/config
      - /your/path/books/library:/books
      - /your/path/books/incoming:/incoming

  calibre-web:
    volumes:
      - /your/path/calibre-web-config:/config
      - /your/path/books/library:/books:ro

  booksearch:
    volumes:
      - /your/path/books/library:/library:ro

  calibre-import:
    volumes:
      - /your/path/books/incoming:/incoming
      - /your/path/books/library:/books
```

> **Important:** BookSearch needs read access to the Calibre library (`/library:ro`) for Calibre badges and Kindle sending.

## Configuration

### Environment Variables (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `DEFAULT_USER` | `admin` | Initial admin username |
| `DEFAULT_PASS` | `admin` | Initial admin password |
| `SECRET_KEY` | `change-me...` | Flask session secret (change this!) |
| `PUID` | `1000` | User ID for Calibre containers |
| `PGID` | `1000` | Group ID for Calibre containers |
| `TZ` | `Etc/UTC` | Timezone |
| `BOOKSEARCH_PORT` | `5000` | BookSearch web UI port |
| `STACKS_PORT` | `8585` | Stacks web UI port |
| `CALIBRE_PORT` | `8182` | Calibre desktop VNC port |
| `CALIBRE_WEB_PORT` | `8084` | Calibre-Web port |
| `CALIBRE_CONTENT_PORT` | `8181` | Calibre content server port |
| `CALIBRE_LIBRARY_PATH` | `/library` | Path to Calibre library (for metadata.db) |

### Kindle Setup

1. Go to **Settings** (⚙️) in BookSearch UI
2. Enable Kindle sender
3. Fill in:
   - **Kindle email** — your `user@kindle.com` address
   - **SMTP host** — `smtp.gmail.com` (for Gmail)
   - **SMTP port** — `587`
   - **SMTP email** — your Gmail address
   - **SMTP password** — [Gmail App Password](https://myaccount.google.com/apppasswords)
4. Add your SMTP email to [Amazon Approved Senders](https://www.amazon.com/hz/mycd/myx#/home/settings/payment)

BookSearch has a built-in Kindle sender (per-user). When you click "📱 Kindle" on a search result:
1. The book is queued for Kindle delivery
2. A background worker polls every 30s until the book appears in Calibre
3. Once found, it sends the EPUB via SMTP email to your Kindle
4. Track status at `/kindle-queue` — pending, sent, and failed items with retry

### Calibre-Import

The `calibre-import` service watches the `/incoming` folder (shared with Stacks) and:
- Auto-imports EPUB, PDF, MOBI, AZW3, DOC, FB2, and other ebook formats
- Detects `.bin` files (Stacks sometimes saves with wrong extension) and renames based on MIME type
- Removes imported files from incoming

## Raspberry Pi (ARM64)

All Docker images are built for both `linux/amd64` and `linux/arm64`. Works on Raspberry Pi 4/5 out of the box.

**Note:** FlareSolverr runs a headless Chrome browser — on Pi 4 (4GB RAM) it may be slow. Pi 5 (8GB) is recommended.

## How It Works

1. **Search** — You type a book title in BookSearch. It sends the query through FlareSolverr (which solves Cloudflare challenges) to Anna's Archive.
2. **Download** — You click "Download → Kindle". BookSearch tells Stacks to download the file. Stacks uses FlareSolverr cookies to bypass protection.
3. **Import** — The downloaded file lands in `/incoming`. Calibre-Import detects it, fixes the extension if needed, and imports it into the Calibre library.
4. **Send to Kindle** — If you clicked "📱 Kindle", BookSearch queues the book. A background worker polls Calibre every 30s until the book appears, then sends the EPUB to your Kindle via email.

## Updating

### CLI
```bash
cd booksearch
docker compose pull
docker compose up -d
```

### Portainer
If GitOps is enabled, Portainer auto-pulls on the configured interval. Otherwise: Stack → **Pull and redeploy**.

## Troubleshooting

**Search takes too long or returns no results**
- FlareSolverr needs to solve a Cloudflare challenge — first search may take 15-30 seconds
- Subsequent searches use cached cookies and are faster
- Author-only searches may take longer (more results to paginate) — timeout is 60s with automatic retry
- Search now fetches up to 3 pages of results automatically (up to 50 results)
- Check logs: `docker compose logs flaresolverr`
- Check BookSearch logs: `docker compose logs booksearch`

**Books not appearing in Calibre**
- Check Calibre-Import logs: `docker compose logs calibre-import`
- Stacks sometimes saves files as `.bin` — Calibre-Import handles this automatically

**Kindle not receiving books**
- Verify SMTP settings in BookSearch → Settings (per-user)
- Check that your SMTP email is in Amazon's Approved Senders list
- Check Kindle queue at `/kindle-queue` for errors
- Check logs: `docker compose logs booksearch`

**Cannot log in**
- Default credentials: `admin` / `admin`
- If you forgot your password, delete the `booksearch-data` volume and restart

**Port conflicts**
- All ports are configurable via environment variables (see table above)
- If a port is in use, set a different one in `.env` or Portainer env vars

## Changelog

### v0.4.1 — Selection Panel + Calibre Badge Fix

- **Floating selection panel** — sliding panel on the right showing selected books (title + author) with remove/clear buttons
- **Fixed Calibre badges** — library volume mount was missing, badges now work correctly

### v0.4 — Kindle Sending Built-in

- **Kindle sending integrated into BookSearch** — no more separate kindle-sender container
- **Per-user Kindle settings** — each user configures their own Kindle email and SMTP
- **Kindle send queue** — visual queue at `/kindle-queue` showing pending, sent, and failed items
- **Reliable book matching** — uses Calibre metadata.db instead of filename matching
- **Retry logic** — failed sends retry up to 3 times

### v0.3.2 — Fix Calibre-only books sent to Kindle

- **Fixed no-kindle matching** — punctuation (commas, parentheses, etc.) caused mismatches between no-kindle list and Calibre filenames
- **Full path matching** — kindle-sender now checks Calibre's `Author/Title/` directory structure, not just filename
- **Consistent normalization** — both booksearch and kindle-sender use identical text normalization

### v0.3.1 — Search Fix: Author Search & Pagination

- **Fixed author search** — increased FlareSolverr timeout from 30s to 60s with retry logic (2 attempts)
- **Pagination support** — search now fetches up to 3 pages from Anna's Archive (up to 50 results)
- **Better error logging** — detailed FlareSolverr failure logging with attempt numbers
- **Result count** — UI shows total number of results found

### v0.3 — Calibre Library Integration

- **Calibre library check** — search results now show badges when a book is already in your Calibre library: "Already in Calibre" (exact match), "Title in library", or "Author in library"
- **Calibre settings** — configure Calibre library path in Settings page (reads metadata.db readonly)
- **No-kindle fix** — fixed diacritics matching (Polish characters ąćęłńóśźż) so no-kindle.txt properly filters books with stripped filenames
- **Diacritics normalization** — both kindle-sender and booksearch use NFKD unicode decomposition for robust text matching

### v0.2 — Bulk Downloads & Calibre-Only Mode

- **Calibre-only download** — each result now has two buttons: "📚 Calibre" (download without sending to Kindle) and "📱 Kindle" (download + send to Kindle)
- **Bulk selection** — checkbox on each result row, with a floating action bar at the bottom: "Selected: X | 📚 Calibre All | 📱 Kindle All"
- **Bulk download API** — new `POST /api/download/bulk` endpoint accepts a list of items for batch processing
- **Language flag emojis** — language selector now shows Unicode flag icons (🇵🇱 🇬🇧 🇩🇪 🇷🇺 🌍)
- **No-Kindle list** — books downloaded with "Calibre only" are saved to `/data/no-kindle.txt`; Kindle Sender skips matching files automatically
- **Style improvements** — distinct button colors (Calibre = purple, Kindle = green), dark-themed bulk action bar

### v0.1 — Initial Release

- Search Anna's Archive via FlareSolverr
- Download via Stacks
- Auto-import to Calibre
- Auto-send to Kindle (EPUB direct, other formats auto-converted)
- Session-based auth with password hashing
- Kindle SMTP configuration via UI
- Multi-platform Docker images (amd64 + arm64)

## License

MIT

---

# 📚 BookSearch (PL)

Samodzielnie hostowana wyszukiwarka ebooków z automatycznym dostarczaniem na Kindle.

Przeszukuje [Anna's Archive](https://annas-archive.gl) przez FlareSolverr (obejście Cloudflare), pobiera przez Stacks, automatycznie importuje do Calibre i opcjonalnie wysyła pliki EPUB na Kindle przez email.

## Szybki start

### Opcja A: Docker Compose (terminal)

```bash
git clone https://github.com/muszkin/booksearch.git
cd booksearch
cp .env.example .env
# Edytuj .env — zmień przynajmniej SECRET_KEY
docker compose up -d
```

Otwórz `http://localhost:5000` — login: `admin` / `admin`

### Opcja B: Portainer (GUI)

1. Otwórz Portainer → **Stacks** → **Add stack**
2. Wybierz **Repository**
3. Wypełnij:
   - **Repository URL:** `https://github.com/muszkin/booksearch`
   - **Repository reference:** `refs/heads/main`
   - **Compose path:** `docker-compose.yml`
4. Przewiń do **Environment variables** i dodaj:

   | Nazwa | Wartość | Wymagane? |
   |-------|---------|-----------|
   | `SECRET_KEY` | dowolny losowy tekst (np. `moj-tajny-klucz-123`) | ✅ Tak |
   | `DEFAULT_USER` | `admin` | Opcjonalne (domyślnie: admin) |
   | `DEFAULT_PASS` | `admin` | Opcjonalne (domyślnie: admin) |
   | `PUID` | `1000` | Opcjonalne |
   | `PGID` | `1000` | Opcjonalne |
   | `TZ` | `Europe/Warsaw` | Opcjonalne |

5. Kliknij **Deploy the stack**
6. Poczekaj ~1 minutę aż wszystkie kontenery się uruchomią
7. Otwórz `http://<twój-serwer>:5000` → zaloguj się

> **Wskazówka:** Aby stack się automatycznie aktualizował po zmianach w repo, włącz **GitOps updates** w ustawieniach stacku Portainer.

## Konfiguracja Kindle

1. Wejdź w **Ustawienia** (⚙️) w BookSearch
2. Włącz wysyłanie na Kindle
3. Uzupełnij:
   - **Email Kindle** — Twój adres `user@kindle.com`
   - **Host SMTP** — `smtp.gmail.com` (dla Gmail)
   - **Port SMTP** — `587`
   - **Email SMTP** — Twój adres Gmail
   - **Hasło SMTP** — [Hasło aplikacji Gmail](https://myaccount.google.com/apppasswords)
4. Dodaj swój email SMTP do [zatwierdzonych nadawców Amazon](https://www.amazon.com/hz/mycd/myx#/home/settings/payment)

## Jak to działa

1. **Szukasz** — wpisujesz tytuł w BookSearch. Zapytanie idzie przez FlareSolverr (obchodzi Cloudflare) do Anna's Archive.
2. **Pobierasz** — klikasz "Pobierz → Kindle". Stacks pobiera plik z Anna's Archive.
3. **Import** — pobrany plik ląduje w folderze incoming. Calibre-Import wykrywa go, poprawia rozszerzenie jeśli trzeba i importuje do biblioteki Calibre.
4. **Wysyłka** — Jeśli kliknąłeś "📱 Kindle", BookSearch dodaje książkę do kolejki. Worker sprawdza co 30s czy jest w Calibre, a potem wysyła EPUB emailem na Kindle.

## Raspberry Pi (ARM64)

Wszystkie obrazy Docker są budowane dla `amd64` i `arm64`. Działa na Raspberry Pi 4/5 od razu.

**Uwaga:** FlareSolverr uruchamia Chrome w tle — na Pi 4 (4GB RAM) może być wolny. Zalecany Pi 5 (8GB).

## Rozwiązywanie problemów

**Wyszukiwanie trwa długo lub nie zwraca wyników**
- FlareSolverr musi rozwiązać challenge Cloudflare — pierwsze wyszukiwanie może trwać 15-30 sekund
- Kolejne wyszukiwania używają cache'owanych cookies i są szybsze
- Wyszukiwanie po autorze może trwać dłużej (więcej wyników do paginacji) — timeout 60s z automatycznym retry
- Wyszukiwanie pobiera do 3 stron wyników automatycznie (maks. 50 wyników)
- Logi FlareSolverr: `docker compose logs flaresolverr`
- Logi BookSearch: `docker compose logs booksearch`

**Książki nie pojawiają się w Calibre**
- Sprawdź logi: `docker compose logs calibre-import`
- Stacks czasem zapisuje pliki jako `.bin` — Calibre-Import obsługuje to automatycznie

**Kindle nie otrzymuje książek**
- Sprawdź ustawienia SMTP w BookSearch → Ustawienia (per-user)
- Upewnij się, że Twój email SMTP jest na liście zatwierdzonych nadawców Amazon
- Sprawdź kolejkę Kindle na `/kindle-queue`
- Logi: `docker compose logs booksearch`

**Nie mogę się zalogować**
- Domyślne dane: `admin` / `admin`
- Jeśli zapomniałeś hasła, usuń wolumin `booksearch-data` i uruchom ponownie

## Licencja

MIT
