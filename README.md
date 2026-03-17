# BookSearch

Self-hosted ebook search, download, and Kindle delivery system.

Searches [Anna's Archive](https://annas-archive.gl) via FlareSolverr (Cloudflare bypass), downloads via Stacks, auto-imports to Calibre, and optionally sends EPUB files to your Kindle via email.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌────────────┐
│  BookSearch  │────>│ FlareSolverr │────>│   Anna's   │
│  (Flask UI)  │     │  (CF bypass) │     │  Archive   │
│  :5000       │     └──────────────┘     └────────────┘
│              │
│              │     ┌──────────────┐     ┌────────────┐
│              │────>│    Stacks    │────>│  Download   │
│              │     │  (downloader)│     │   files     │
└──────┬───────┘     └──────────────┘     └─────┬──────┘
       │                                        │
       │  kindle-settings.json                  v
       │                                 ┌────────────┐
       v                                 │  Calibre   │
┌──────────────┐    reads library        │  Library   │
│Kindle Sender │<────────────────────────│  :8080     │
│(auto-convert)│                         └────────────┘
│              │                         ┌────────────┐
│              │                         │Calibre-Web │
└──────┬───────┘                         │  :8083     │
       │                                 └────────────┘
       v
  Kindle (email)
```

## Quick Start

1. Clone the repository:
```bash
git clone https://github.com/muszkin/booksearch.git
cd booksearch
```

2. Copy and edit the environment file:
```bash
cp .env.example .env
# Edit .env — at minimum change SECRET_KEY
```

3. Start the stack:
```bash
docker compose up -d
```

4. Open BookSearch at `http://localhost:5000`
   - Default login: `admin` / `admin`

5. Configure Kindle delivery in Settings (optional)

## Services

| Service | Port | Description |
|---------|------|-------------|
| BookSearch | 5000 | Search UI + settings |
| Calibre | 8080 | Calibre desktop (VNC) |
| Calibre | 8181 | Calibre desktop (HTTPS VNC) |
| Calibre-Web | 8083 | Web-based library browser |
| FlareSolverr | — | Cloudflare bypass (internal) |
| Stacks | — | Anna's Archive downloader (internal) |
| Kindle Sender | — | Auto-sends books to Kindle (internal) |

## Configuration

### Environment Variables (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `DEFAULT_USER` | `admin` | Initial admin username |
| `DEFAULT_PASS` | `admin` | Initial admin password |
| `SECRET_KEY` | random | Flask session secret |
| `PUID` | `1000` | User ID for Calibre containers |
| `PGID` | `1000` | Group ID for Calibre containers |
| `TZ` | `Etc/UTC` | Timezone |

### Kindle Setup

1. Go to **Settings** in BookSearch UI
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
- Converts PDF/MOBI/AZW3/FB2 to EPUB, then sends

## Raspberry Pi (ARM64)

Docker images are built for both `amd64` and `arm64`. Works on Raspberry Pi 4/5 out of the box.

## Screenshots

*Coming soon*

## Troubleshooting

**Search takes too long or returns no results**
- FlareSolverr needs to solve a Cloudflare challenge — first search may take 15-30 seconds
- Check FlareSolverr logs: `docker compose logs flaresolverr`

**Books not appearing in Calibre**
- Stacks downloads to the shared volume — Calibre should auto-detect
- Check Stacks logs: `docker compose logs stacks`

**Kindle not receiving books**
- Verify SMTP settings in BookSearch Settings
- Check that your SMTP email is in Amazon's Approved Senders list
- Check Kindle Sender logs: `docker compose logs kindle-sender`

**Cannot log in**
- Default credentials: `admin` / `admin`
- If you changed the password and forgot it, delete the `booksearch-data` volume and restart

## License

MIT

---

# BookSearch (PL)

Samodzielnie hostowana wyszukiwarka ebookow z automatycznym dostarczaniem na Kindle.

Przeszukuje [Anna's Archive](https://annas-archive.gl) przez FlareSolverr (obejscie Cloudflare), pobiera przez Stacks, automatycznie importuje do Calibre i opcjonalnie wysyla pliki EPUB na Kindle przez email.

## Szybki start

1. Sklonuj repozytorium:
```bash
git clone https://github.com/muszkin/booksearch.git
cd booksearch
```

2. Skopiuj i edytuj plik konfiguracyjny:
```bash
cp .env.example .env
# Zmien przynajmniej SECRET_KEY
```

3. Uruchom stos:
```bash
docker compose up -d
```

4. Otworz BookSearch: `http://localhost:5000`
   - Domyslny login: `admin` / `admin`

5. Skonfiguruj wysylanie na Kindle w Ustawieniach (opcjonalnie)

## Konfiguracja Kindle

1. Wejdz w **Ustawienia** w interfejsie BookSearch
2. Wlacz wysylanie na Kindle
3. Uzupelnij:
   - **Email Kindle** — Twoj adres `user@kindle.com`
   - **Host SMTP** — `smtp.gmail.com` (dla Gmail)
   - **Port SMTP** — `587`
   - **Email SMTP** — Twoj adres Gmail
   - **Haslo SMTP** — [Haslo aplikacji Gmail](https://myaccount.google.com/apppasswords)
4. Dodaj swoj email SMTP do [zatwierdzonych nadawcow Amazon](https://www.amazon.com/hz/mycd/myx#/home/settings/payment)

Kindle Sender obserwuje biblioteke Calibre i automatycznie:
- Wysyla pliki EPUB bezposrednio
- Konwertuje PDF/MOBI/AZW3/FB2 na EPUB, a nastepnie wysyla

## Raspberry Pi (ARM64)

Obrazy Docker sa budowane dla `amd64` i `arm64`. Dziala na Raspberry Pi 4/5 od razu.

## Rozwiazywanie problemow

**Wyszukiwanie trwa dlugo lub nie zwraca wynikow**
- FlareSolverr musi rozwiazac wyzwanie Cloudflare — pierwsze wyszukiwanie moze trwac 15-30 sekund
- Sprawdz logi: `docker compose logs flaresolverr`

**Ksiazki nie pojawiaja sie w Calibre**
- Stacks pobiera do wspoldzielonego wolumenu — Calibre powinno automatycznie wykryc
- Sprawdz logi: `docker compose logs stacks`

**Kindle nie otrzymuje ksiazek**
- Sprawdz ustawienia SMTP w Ustawieniach BookSearch
- Upewnij sie, ze Twoj email SMTP jest na liscie zatwierdzonych nadawcow Amazon
- Sprawdz logi: `docker compose logs kindle-sender`

**Nie moge sie zalogowac**
- Domyslne dane: `admin` / `admin`
- Jesli zmieniles haslo i je zapomnialesz, usun wolumin `booksearch-data` i uruchom ponownie

## Licencja

MIT
