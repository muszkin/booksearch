# 📚 BookSearch

Self-hosted ebook search, download, and Kindle delivery system.

Searches [Anna's Archive](https://annas-archive.gl) via FlareSolverr (Cloudflare bypass), downloads via Stacks, auto-imports to Calibre, and optionally sends EPUB files to your Kindle via email.

## Architecture

```
User → BookSearch (:5000)
         │
         ├── Search ──→ FlareSolverr ──→ Anna's Archive
         │
         └── Download ──→ Stacks ──→ /incoming/
                                        │
                                  Calibre-Import
                                  (auto-import + .bin fix)
                                        │
                                        ▼
                                  Calibre Library (/books/)
                                   │           │
                                   │           ▼
                                   │     Calibre-Web (:8083)
                                   │     (browse library)
                                   ▼
                              Kindle-Sender
                              (auto-convert + email)
                                   │
                                   ▼
                              📱 Kindle
```

## Services

| Service | Port | Description |
|---------|------|-------------|
| **BookSearch** | 5000 | Search UI, download queue, Kindle settings |
| **Stacks** | 8585 | Anna's Archive downloader |
| **FlareSolverr** | — | Cloudflare bypass (internal only) |
| **Calibre** | 8182 | Calibre desktop (VNC) |
| **Calibre** | 8181 | Calibre desktop (HTTPS VNC) |
| **Calibre-Web** | 8084 | Web-based library browser |
| **Calibre-Import** | — | Auto-imports downloaded files to Calibre (internal) |
| **Kindle Sender** | — | Auto-sends EPUB to Kindle via email (internal) |

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

  calibre-import:
    volumes:
      - /your/path/books/incoming:/incoming
      - /your/path/books/library:/books

  kindle-sender:
    volumes:
      - booksearch-data:/data
      - /your/path/books/library:/library:ro
```

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

The Kindle Sender watches the Calibre library and automatically:
- Sends EPUB files directly
- Converts PDF/MOBI/AZW3/FB2 to EPUB first, then sends

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
4. **Send to Kindle** — Kindle Sender watches the Calibre library. When a new book appears, it converts to EPUB (if needed) and emails it to your Kindle address.

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
- Check logs: `docker compose logs flaresolverr`

**Books not appearing in Calibre**
- Check Calibre-Import logs: `docker compose logs calibre-import`
- Stacks sometimes saves files as `.bin` — Calibre-Import handles this automatically

**Kindle not receiving books**
- Verify SMTP settings in BookSearch → Settings
- Check that your SMTP email is in Amazon's Approved Senders list
- Check logs: `docker compose logs kindle-sender`

**Cannot log in**
- Default credentials: `admin` / `admin`
- If you forgot your password, delete the `booksearch-data` volume and restart

**Port conflicts**
- All ports are configurable via environment variables (see table above)
- If a port is in use, set a different one in `.env` or Portainer env vars

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
4. **Wysyłka** — Kindle Sender obserwuje bibliotekę. Nowa książka → konwersja na EPUB (jeśli potrzebna) → email na Kindle.

## Raspberry Pi (ARM64)

Wszystkie obrazy Docker są budowane dla `amd64` i `arm64`. Działa na Raspberry Pi 4/5 od razu.

**Uwaga:** FlareSolverr uruchamia Chrome w tle — na Pi 4 (4GB RAM) może być wolny. Zalecany Pi 5 (8GB).

## Rozwiązywanie problemów

**Wyszukiwanie trwa długo lub nie zwraca wyników**
- FlareSolverr musi rozwiązać challenge Cloudflare — pierwsze wyszukiwanie może trwać 15-30 sekund
- Kolejne wyszukiwania używają cache'owanych cookies i są szybsze
- Logi: `docker compose logs flaresolverr`

**Książki nie pojawiają się w Calibre**
- Sprawdź logi: `docker compose logs calibre-import`
- Stacks czasem zapisuje pliki jako `.bin` — Calibre-Import obsługuje to automatycznie

**Kindle nie otrzymuje książek**
- Sprawdź ustawienia SMTP w BookSearch → Ustawienia
- Upewnij się, że Twój email SMTP jest na liście zatwierdzonych nadawców Amazon
- Logi: `docker compose logs kindle-sender`

**Nie mogę się zalogować**
- Domyślne dane: `admin` / `admin`
- Jeśli zapomniałeś hasła, usuń wolumin `booksearch-data` i uruchom ponownie

## Licencja

MIT
